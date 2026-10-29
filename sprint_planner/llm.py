"""One chat call with tools to an OpenAI-compatible provider (OpenRouter, Google Gemini, Groq, Ollama…):
fallback models, retries on rate limits, a pace limit (requests per minute), cost tracking, disk cache.

Returns a plain dict {"content", "tool_calls": [{"id","name","arguments"}], "usage": {...}, "cost", "model"}
so the rest of the code doesn't depend on the SDK's objects.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time

from . import config

_client = None
_pace_lock = threading.Lock()
_last_call = 0.0


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI

        if config.KEY_ENV and not config.API_KEY:
            raise RuntimeError(f"{config.KEY_ENV} is missing: add it to .env (provider: {config.PROVIDER}), "
                               "or set PLANNER_FAKE_LLM=1 to run offline.")
        _client = OpenAI(base_url=config.BASE_URL, api_key=config.API_KEY or "none", timeout=180, max_retries=0,
                         default_headers={"X-Title": "Sprint Planner Agent"})
    return _client


def _pace() -> None:
    """Stay under the provider's requests-per-minute limit."""
    global _last_call
    if config.MIN_INTERVAL <= 0:
        return
    with _pace_lock:
        wait = config.MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()


CACHE_VERSION = 2   # bump when the saved format changes (v2 keeps tool-call extras such as Gemini thought signatures)


def _cache_file(payload: dict):
    key = hashlib.sha256(json.dumps({"v": CACHE_VERSION, "provider": config.PROVIDER, **payload}, sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()[:32]
    return config.CACHE_DIR / f"{key}.json"


def _is_busy(msg: str) -> bool:
    low = msg.lower()
    return any(s in low for s in ("429", "rate", "quota", "502", "503", "overloaded", "unavailable", "resource_exhausted"))


class Busy(RuntimeError):
    """The model is rate-limited right now; another model may still answer."""


def _call(model: str, payload: dict):
    """One attempt on one model. Raises Busy when it is rate-limited (the caller switches model)."""
    extra = None
    if config.PROVIDER == "openrouter":   # OpenRouter does the fallback itself and reports cost
        extra = {"usage": {"include": True},
                 "models": [model] + [m for m in config.FALLBACK_MODELS if m != model]}
    more = {"reasoning_effort": config.REASONING_EFFORT} if config.REASONING_EFFORT else {}
    sig_fixed = False
    for _ in range(3):
        _pace()
        t0 = time.time()
        try:
            resp = _get_client().chat.completions.create(
                **{**payload, "model": model}, max_tokens=2500, extra_body=extra, **more)
            print(f"  · {model} answered in {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
            return resp
        except Exception as e:  # noqa: BLE001
            msg = str(getattr(e, "body", None) or e).lower()
            if more and "reason" in msg:   # this model doesn't take reasoning_effort: retry without it
                more = {}
                continue
            if "signature" in msg and not sig_fixed:
                # signatures made by another model (after a switch) can be rejected: use Gemini's documented
                # placeholder that skips the check, then retry once
                payload = {**payload, "messages": _neutral_signatures(payload["messages"])}
                sig_fixed = True
                continue
            if _is_busy(str(e)):
                raise Busy(str(e)) from e
            raise
    raise RuntimeError(f"{model}: no answer")


def _neutral_signatures(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m.get("tool_calls"):
            m = {**m, "tool_calls": [{**tc, "extra_content": {"google": {"thought_signature": "skip_thought_signature_validator"}}}
                                     if tc.get("extra_content") else tc for tc in m["tool_calls"]]}
        out.append(m)
    return out


_busy_until: dict[str, float] = {}   # model -> time it may be tried again


def chat(messages: list[dict], tools: list[dict], *, use_cache: bool | None = None) -> dict:
    use_cache = config.USE_CACHE if use_cache is None else use_cache
    payload = {"model": config.MODEL, "messages": messages, "tools": tools, "temperature": 0.2}
    path = _cache_file(payload)
    if use_cache and path.exists():
        out = json.loads(path.read_text(encoding="utf-8"))
        return {**out, "cost": 0.0, "cached": True}

    models = [config.MODEL] if config.PROVIDER == "openrouter" else \
        [config.MODEL] + [m for m in config.FALLBACK_MODELS if m != config.MODEL]
    errors: list[str] = []
    resp = None
    # Each model has its own rate limit: when one is busy, switch to the next one at once instead of waiting.
    # Only when every model is busy, wait a little and go round again.
    for rnd in range(12 if len(models) == 1 else 6):   # one model: be patient instead of switching
        for model in models:
            if _busy_until.get(model, 0) > time.time():
                continue
            try:
                resp = _call(model, payload)
                break
            except Busy as e:
                _busy_until[model] = time.time() + 30
                print(f"  · {model} is busy (rate limit) → trying the next model", file=sys.stderr, flush=True)
                errors.append(f"{model}: busy")
            except Exception as e:  # noqa: BLE001 - broken or unknown model: skip it for this call
                detail = getattr(e, "body", None) or getattr(e, "message", None) or str(e)
                errors.append(f"{model}: {str(e)[:60]} {str(detail)[:240]}")
        if resp is not None:
            break
        if not any("busy" in x for x in errors[-len(models):]):
            break   # real errors, not rate limits: waiting won't help
        wait = min(_busy_until.values(), default=time.time()) - time.time()
        wait = max(5.0, min(wait, 30.0))
        print(f"  · all models busy, waiting {wait:.0f}s…", file=sys.stderr, flush=True)
        time.sleep(wait)
    if resp is None:
        raise RuntimeError("all models failed: " + " | ".join(errors))

    if not resp.choices:
        raise RuntimeError(f"empty response from {getattr(resp, 'model', '?')}")
    msg = resp.choices[0].message
    usage = resp.usage
    extra_usage = (getattr(usage, "model_extra", None) or {}) if usage else {}
    out = {
        "content": msg.content or "",
        # `extra` keeps provider fields that must be sent back unchanged on the next turn
        # (Gemini 3 returns a "thought signature" in tool_calls[i].extra_content and rejects the next call without it)
        "tool_calls": [{"id": tc.id or f"call_{i}", "name": tc.function.name, "arguments": tc.function.arguments or "{}",
                        "extra": dict(getattr(tc, "model_extra", None) or {})}
                       for i, tc in enumerate(msg.tool_calls or [])],
        "usage": {"prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                  "completion_tokens": getattr(usage, "completion_tokens", 0) or 0},
        "model": getattr(resp, "model", config.MODEL),
    }
    cost = float(extra_usage.get("cost") or 0.0)
    if use_cache:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return {**out, "cost": cost, "cached": False}


def list_models() -> list[str]:
    """Model ids the configured provider offers (for picking AGENT_MODEL)."""
    return sorted(m.id.removeprefix("models/") for m in _get_client().models.list().data)
