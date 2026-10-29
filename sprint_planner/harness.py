"""The harness: one agent loop with the controls around it.

    notes --sanitize--> model <--> tools (permission policy, guards, result cap, audit log)
                          |  loop until the model stops calling tools, or a budget is hit
                          v
                     drafts queued for the team lead's approval  -->  create_approved() over MCP

Configs for the eval (ONE change between them):
  A: the agent writes each issue and queues it (no check_issue tool)
  B: the agent must call check_issue and fix every problem before queuing (write -> check -> fix loop)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable

from . import config, guards, memory
from .mcp_bridge import MCPBridge
from .tools import READ_TOOLS, WRITE_TOOLS, Session, schemas

CONFIGS = {
    "A": {"tools": ["get_sprint", "get_team", "search_issues", "create_issue"],
          "extra": "", "label": "A: write once (no check_issue)"},
    "B": {"tools": ["get_sprint", "get_team", "search_issues", "check_issue", "create_issue"],
          "extra": ("\n\n## Quality loop\nBefore every create_issue, call check_issue with the same fields. If it "
                    "returns problems, fix them and call check_issue again. Only call create_issue when it returns "
                    "ok=true (or when the only problem is capacity and you mention it in your summary)."),
          "label": "B: write -> check_issue -> fix loop"},
}
KEEP_RECENT_RESULTS = 6   # compaction: older tool results are shortened before each model call


@dataclass
class Run:
    final: str = ""
    drafts: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)
    steps: int = 0
    stopped: str = "done"          # done | max_steps | max_cost | error
    cost: float = 0.0
    tokens: int = 0
    milestone: str | None = None
    removed: list[str] = field(default_factory=list)   # what the notes sanitizer removed
    blocked: list[str] = field(default_factory=list)   # what the policy blocked
    models: set = field(default_factory=set)            # models that actually answered (for the eval report)
    seconds: float = 0.0


def system_prompt(config_name: str, lead_notes: list[str]) -> str:
    spec = config.SPEC_FILE.read_text(encoding="utf-8")
    if lead_notes:
        spec += "\n\n## The team lead asked you to remember\n" + "\n".join(f"- {n}" for n in lead_notes)
    return spec + CONFIGS[config_name]["extra"]


def permission(name: str, allowed: list[str]) -> str:
    """Read tools: allow. create_issue: 'ask' = queue for the lead's approval. Anything else: deny."""
    if name not in allowed:
        return "deny"
    if name in WRITE_TOOLS:
        return "ask"
    return "allow" if name in READ_TOOLS else "deny"


def compact(messages: list[dict]) -> list[dict]:
    """Context rule: keep the last few tool results in full, shorten older ones."""
    tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    old = set(tool_idx[:-KEEP_RECENT_RESULTS])
    out = []
    for i, m in enumerate(messages):
        if i in old and len(m["content"]) > 300:
            m = {**m, "content": m["content"][:200] + " …[older result shortened]"}
        out.append(m)
    return out


def audit(event: dict) -> None:
    with open(config.AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **event}, ensure_ascii=False, default=str) + "\n")


def plan(notes: str, *, config_name: str = "B", sprint: str | None = None, guards_on: bool = True,
         lead_notes: list[str] | None = None, mcp: MCPBridge | None = None, use_cache: bool | None = None,
         on_event: Callable[[dict], None] | None = None) -> Run:
    """Run the agent on one set of meeting notes. Returns the drafts waiting for approval (nothing is created)."""
    t0 = time.time()
    run = Run()
    cfg = CONFIGS[config_name]
    lead_notes = memory.notes() if lead_notes is None else lead_notes

    def emit(kind: str, text: str, **data):
        e = {"kind": kind, "text": text, **data}
        run.events.append(e)
        audit({"config": config_name, **e})
        if on_event:
            on_event(e)

    # 1. untrusted input: clean the notes before the model sees them
    if guards_on:
        clean, run.removed = guards.sanitize_notes(notes)
        for r in run.removed:
            emit("guard", f"removed from notes: {r}")
    else:
        clean = guards.plain_notes(notes)

    own_mcp = mcp is None
    mcp = mcp or MCPBridge().__enter__()
    try:
        session = Session(mcp=mcp, sprint_override=sprint)
        messages = [{"role": "system", "content": system_prompt(config_name, lead_notes)},
                    {"role": "user", "content": f"Meeting notes:\n{clean}\n\nPrepare the sprint issues."}]
        tool_defs = schemas(cfg["tools"])
        fake = None
        if config.FAKE_LLM:
            from .fake_llm import FakeModel

            fake = FakeModel()
        emit("user", clean[:300])

        for step in range(1, config.MAX_STEPS + 1):
            run.steps = step
            if fake:
                reply = {**fake.step(messages, cfg["tools"]), "cost": 0.0, "usage": {}, "model": "fake-offline"}
            else:
                reply = __import__("sprint_planner.llm", fromlist=["chat"]).chat(compact(messages), tool_defs,
                                                                                 use_cache=use_cache)
            run.cost += reply.get("cost", 0.0)
            if reply.get("model"):
                run.models.add(str(reply["model"]).removeprefix("models/"))
            run.tokens += sum((reply.get("usage") or {}).values())
            calls = reply["tool_calls"]
            if reply["content"] and calls:
                emit("model", reply["content"])
            if not calls:
                run.final = reply["content"]
                emit("final", reply["content"])
                break
            messages.append({"role": "assistant", "content": reply["content"] or "", "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]},
                 **(c.get("extra") or {})}
                for c in calls]})
            for c in calls:
                try:
                    args = json.loads(c["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = None
                run.tool_calls.append((c["name"], args or {}))
                emit("call", f"{c['name']}({json.dumps(args, ensure_ascii=False)[:300]})", name=c["name"])
                if args is None:
                    out = {"error": "arguments were not valid JSON; send a JSON object"}
                else:
                    decision = permission(c["name"], cfg["tools"])
                    if decision == "deny":
                        out = {"error": f"{c['name']} is not available"}
                        emit("guard", f"denied {c['name']}")
                    else:
                        block = guards.check_draft(args) if (guards_on and c["name"] == "create_issue") else None
                        if block:
                            out = {"error": block}
                            run.blocked.append(block)
                            emit("guard", block)
                        else:
                            try:
                                out = session.call(c["name"], args)
                            except Exception as e:  # noqa: BLE001 - tool errors go back to the model
                                out = {"error": f"{type(e).__name__}: {e}"}
                text = json.dumps(out, ensure_ascii=False, default=str)
                if len(text) > config.MAX_RESULT_CHARS:   # context budget for tool results
                    text = text[: config.MAX_RESULT_CHARS] + " …[cut]"
                emit("error" if '"error"' in text[:12] else "result", text[:400], name=c["name"])
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": text})
            if run.cost > config.MAX_COST:
                run.stopped = "max_cost"
                emit("guard", f"cost budget ${config.MAX_COST} reached; stopping")
                break
        else:
            run.stopped = "max_steps"
            emit("guard", f"step budget ({config.MAX_STEPS}) reached; stopping")
        run.drafts = session.drafts
        run.milestone = session.milestone
    except Exception as e:  # noqa: BLE001
        run.stopped = "error"
        emit("error", f"{type(e).__name__}: {e}")
    finally:
        if own_mcp:
            mcp.__exit__(None, None, None)
    run.seconds = round(time.time() - t0, 1)
    emit("summary", f"{len(run.drafts)} draft(s), {run.steps} steps, ${run.cost:.4f}, stopped={run.stopped}")
    return run
