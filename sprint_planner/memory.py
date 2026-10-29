"""Long-term memory: short notes the team lead wants the agent to remember across sprints
("Huda is on leave in Sprint 24", "every new feature also needs a QA issue"), plus a history of approved plans.

Notes are added by the lead in the UI (or CLI) and put into the agent's context on every run.
"""
from __future__ import annotations

import json
import time

from . import config


def _load() -> dict:
    if config.MEMORY_FILE.exists():
        return json.loads(config.MEMORY_FILE.read_text(encoding="utf-8"))
    return {"notes": [], "history": []}


def _save(m: dict) -> None:
    config.MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def notes() -> list[str]:
    return _load()["notes"]


def add_note(text: str) -> None:
    text = " ".join(text.split())[:300]
    if text:
        m = _load()
        m["notes"].append(text)
        _save(m)


def remove_note(index: int) -> None:
    m = _load()
    if 0 <= index < len(m["notes"]):
        m["notes"].pop(index)
        _save(m)


def record_plan(milestone: str, created: list[dict]) -> None:
    m = _load()
    m["history"].append({"date": time.strftime("%Y-%m-%d %H:%M"), "milestone": milestone,
                         "issues": [{"iid": c.get("iid"), "title": c.get("title"), "assignee": c.get("assignee")}
                                    for c in created]})
    m["history"] = m["history"][-20:]
    _save(m)


def history() -> list[dict]:
    return _load()["history"]
