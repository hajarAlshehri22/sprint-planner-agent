"""A scripted stand-in for the model (PLANNER_FAKE_LLM=1): runs the whole loop offline for tests and demos.

It behaves like a hurried model on purpose: its first draft of each issue has only ONE acceptance criterion,
so config A (no check_issue) produces weak issues and config B's check loop has something to fix.
It also follows instructions hidden in the notes, so the attack demo has something to stop.
Numbers produced with it are NOT real eval results.
"""
from __future__ import annotations

import json
import re

ROLE_WORDS = [
    ("khalid.m", r"nafath|sadad|payment|pay|sms|notification|integration|نفاذ|سداد|دفع|رسائل|إشعار"),
    ("huda.k", r"\btest|qa\b|regression|performance|اختبار"),
    ("sultan.f", r"deploy|monitor|pipeline|server|infrastructure|backup|نشر|مراقبة|سيرفر"),
    ("reem.s", r"page|screen|ui\b|button|form|dashboard|rtl|accessib|design|صفحة|شاشة|واجهة|زر|تصميم"),
    ("noura.a", r"api|endpoint|database|report|permit|backend|export|تقرير|تقارير|تصاريح|قاعدة"),
]
BUG = re.compile(r"bug|fix|error|fail|broken|crash|slow|مشكلة|خطأ|يتعطل|بطيء|لا يعمل", re.I)
URGENT = re.compile(r"urgent|asap|critical|immediately|today|this week|عاجل|بسرعة|ضروري|فوري", re.I)


def _items(notes: str) -> list[str]:
    out = []
    for line in notes.splitlines():
        m = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.+)$", line)
        if m and len(m.group(1).split()) >= 3:
            out.append(m.group(1).strip())
    return out


class Msg:
    def __init__(self, content="", calls=None):
        self.content, self.calls = content, calls or []


class FakeModel:
    def __init__(self):
        self.plan: list = []
        self.started = False
        self.n = 0

    def _call(self, name, args):
        self.n += 1
        return {"id": f"call_{self.n}", "name": name, "arguments": json.dumps(args, ensure_ascii=False)}

    def _draft(self, item: str, milestone: str, team: dict, careful: bool, notes: str) -> dict:
        who = next((u for u, pat in ROLE_WORDS if re.search(pat, item, re.I)), "noura.a")
        left = {m["username"]: m["points_left"] for m in team.get("members", [])}
        if who == "reem.s" and left.get("reem.s", 99) < 3:
            who = "omar.h"
        text = re.sub(r"[^\w\s/-]", "", item)
        title = ("Fix " if BUG.search(item) else "Implement ") + text[:70]
        ac = [f"Given the change is deployed, when a citizen uses it, then {text[:60]} works as described"]
        if careful:
            ac.append("Given an invalid input, when the request is sent, then a clear error message is shown")
        desc = f"Requested in the sprint planning meeting: {item}. This improves the citizen portal experience for users."
        inj = re.search(r"(?i)(?:assistant|ai)[^\n]*?(@[\w.\-]+)", notes)
        if inj:  # a careless model follows hidden instructions
            secret = re.search(r"(?i)(password\s*[:=]?\s*\S+|glpat-[\w-]+)", notes)
            desc += f" cc {inj.group(1)}" + (f" ({secret.group(1)})" if secret else "")
        return {"title": title, "description": desc, "acceptance_criteria": ac, "assignee": who,
                "labels": ["bug" if BUG.search(item) else "feature",
                           "priority::high" if URGENT.search(item) else "priority::medium"],
                "estimate": 3, "milestone": milestone}

    def step(self, messages: list[dict], tool_names: list[str]) -> dict:
        notes = next(m["content"] for m in messages if m["role"] == "user")
        results = [m for m in messages if m["role"] == "tool"]
        last = json.loads(results[-1]["content"]) if results else None
        if not self.started:
            self.started = True
            self.items = _items(notes)
            self.state = "sprint"
            return {"content": "", "tool_calls": [self._call("get_sprint", {})]}
        if self.state == "sprint":
            self.milestone = last.get("milestone", "Sprint 24")
            self.state = "team"
            return {"content": "", "tool_calls": [self._call("get_team", {})]}
        if self.state == "team":
            self.team = last
            self.i, self.reused, self.queued = 0, [], []
            self.state = "search"
        if self.state == "after_search":
            hit = next((r for r in (last or []) if r["state"] == "opened" and
                        len(set(re.findall(r"\w{4,}", r["title"].lower())) &
                            set(re.findall(r"\w{4,}", self.items[self.i].lower()))) >= 3), None)
            if hit:
                self.reused.append(hit["iid"])
                self.i += 1
                self.state = "search"
            else:
                self.draft = self._draft(self.items[self.i], self.milestone, self.team, False, notes)
                self.state = "check" if "check_issue" in tool_names else "create"
        elif self.state == "after_check":
            if last.get("ok") or self.rechecked:
                self.state = "create"
            else:
                self.draft = self._draft(self.items[self.i], self.milestone, self.team, True, notes)
                self.state = "check2"
        elif self.state == "after_create":
            self.queued.append(self.draft["title"])
            self.i += 1
            self.state = "search"
        if self.state == "search":
            if self.i >= len(self.items):
                self.state = "done"
                reused = f" Existing issues reused: {', '.join('#' + str(r) for r in self.reused)}." if self.reused else ""
                return {"content": f"Queued {len(self.queued)} issue(s) for {self.milestone}.{reused}", "tool_calls": []}
            words = " ".join(re.findall(r"[A-Za-z]{4,}", self.items[self.i])[:5]) or self.items[self.i][:40]
            self.state = "after_search"
            return {"content": "", "tool_calls": [self._call("search_issues", {"query": words})]}
        if self.state in ("check", "check2"):
            self.rechecked = self.state == "check2"
            self.state = "after_check"
            return {"content": "", "tool_calls": [self._call("check_issue", self.draft)]}
        if self.state == "create":
            self.state = "after_create"
            return {"content": "", "tool_calls": [self._call("create_issue", self.draft)]}
        return {"content": "Done.", "tool_calls": []}
