"""The 5 tools the agent can call. Read tools run directly; create_issue only queues a draft for approval.

| tool           | reads / writes                | where            |
|----------------|-------------------------------|------------------|
| get_sprint     | read  milestones              | GitLab over MCP  |
| get_team       | read  team.json + open issues | local + MCP      |
| search_issues  | read  existing issues         | GitLab over MCP  |
| check_issue    | read  (quality rules in code) | local (config B) |
| create_issue   | WRITE -> queued for approval  | GitLab over MCP, after the lead approves |
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from . import config, quality
from .mcp_bridge import MCPBridge

TEAM = json.loads(config.TEAM_FILE.read_text(encoding="utf-8"))

ISSUE_FIELDS = {
    "title": {"type": "string", "description": "Short title in English, starting with a verb"},
    "description": {"type": "string", "description": "2-4 sentences: what and why"},
    "acceptance_criteria": {"type": "array", "items": {"type": "string"},
                            "description": "At least 2 items, each 'Given ... when ... then ...'"},
    "assignee": {"type": "string", "description": "username from get_team"},
    "labels": {"type": "array", "items": {"type": "string"},
               "description": "one type label + one priority label, e.g. ['bug', 'priority::high']"},
    "estimate": {"type": "integer", "description": "story points: 1, 2, 3, 5 or 8"},
    "milestone": {"type": "string", "description": "milestone title from get_sprint, e.g. 'Sprint 24'"},
}

SCHEMAS = {
    "get_sprint": {"description": "Get the sprint (GitLab milestone) to plan: the next one that hasn't started, "
                                  "unless the lead chose another. Call once, first.",
                   "parameters": {"type": "object", "properties": {}, "required": []}},
    "get_team": {"description": "Team members with role, skills, capacity, and points already planned / left in "
                                "the sprint (existing issues plus drafts you queued). Call once, after get_sprint.",
                 "parameters": {"type": "object", "properties": {}, "required": []}},
    "search_issues": {"description": "Search existing GitLab issues by keywords (English), to avoid duplicates. "
                                     "Returns up to 5 matches with iid, title, state (opened/closed), assignee, points and milestone.",
                      "parameters": {"type": "object", "properties": {
                          "query": {"type": "string", "description": "3-6 English keywords describing the task"}},
                          "required": ["query"]}},
    "check_issue": {"description": "Check one issue draft against the team's rules (acceptance criteria, labels, "
                                   "estimate, assignee capacity, duplicates, sensitive data). Returns the problems "
                                   "to fix; call again until it returns ok=true, then create_issue.",
                    "parameters": {"type": "object", "properties": ISSUE_FIELDS, "required": list(ISSUE_FIELDS)}},
    "create_issue": {"description": "Queue one issue for the team lead's approval (nothing is created in GitLab "
                                    "until the lead approves). Returns a draft id.",
                     "parameters": {"type": "object", "properties": ISSUE_FIELDS, "required": list(ISSUE_FIELDS)}},
}

READ_TOOLS = {"get_sprint", "get_team", "search_issues", "check_issue"}
WRITE_TOOLS = {"create_issue"}


def schemas(names: list[str]) -> list[dict]:
    return [{"type": "function", "function": {"name": n, **SCHEMAS[n]}} for n in names]


@dataclass
class Session:
    """State for one planning run: the MCP connection, the chosen sprint, and the queued drafts."""
    mcp: MCPBridge
    sprint_override: str | None = None
    today: date = field(default_factory=lambda: config.TODAY)
    milestone: str | None = None
    drafts: list[dict] = field(default_factory=list)
    _open: list[dict] | None = None

    # ---------------------------------------------------------------- helpers
    def open_issues(self) -> list[dict]:
        if self._open is None:
            self._open = self.mcp.call("list_open_issues")
        return self._open

    def pick_milestone(self) -> dict:
        ms = sorted(self.mcp.call("list_milestones"), key=lambda m: m.get("start_date") or "")
        if self.sprint_override:
            for m in ms:
                if m["title"].lower() == self.sprint_override.lower() or str(m["id"]) == str(self.sprint_override):
                    return m
        upcoming = [m for m in ms if (m.get("start_date") or "") > self.today.isoformat()]
        return upcoming[0] if upcoming else ms[-1]

    def load(self) -> dict[str, int]:
        """Points per person in the planned sprint: existing open issues + drafts already queued."""
        ms = self.milestone or self.pick_milestone()["title"]
        load: dict[str, int] = {}
        for i in self.open_issues():
            if i.get("milestone") == ms and i.get("assignee"):
                load[i["assignee"]] = load.get(i["assignee"], 0) + int(i.get("weight") or 0)
        for d in self.drafts:
            if isinstance(d.get("estimate"), int):
                load[d["assignee"]] = load.get(d["assignee"], 0) + d["estimate"]
        return load

    # ---------------------------------------------------------------- tools
    def get_sprint(self) -> dict:
        m = self.pick_milestone()
        self.milestone = m["title"]
        return {"milestone": m["title"], "start_date": m.get("start_date"), "due_date": m.get("due_date"),
                "today": self.today.isoformat(), "chosen_by": "team lead" if self.sprint_override else "next sprint"}

    def get_team(self) -> dict:
        load = self.load()
        return {"milestone": self.milestone or self.pick_milestone()["title"], "members": [
            {"username": m["username"], "role": m["role"], "skills": m["skills"], "capacity": m["capacity"],
             "points_planned": load.get(m["username"], 0), "points_left": m["capacity"] - load.get(m["username"], 0)}
            for m in TEAM["members"]]}

    def search_issues(self, query: str) -> list[dict]:
        return self.mcp.call("search_issues", {"query": query})

    def check_issue(self, **issue) -> dict:
        p = quality.problems(issue, milestone=self.milestone, load=self.load(), open_issues=self.open_issues())
        return {"ok": not p, "problems": p}

    def create_issue(self, **issue) -> dict:
        """Queue a draft (the harness has already applied the security policy)."""
        issue = {k: issue.get(k) for k in ISSUE_FIELDS}
        if isinstance(issue.get("estimate"), str) and issue["estimate"].isdigit():
            issue["estimate"] = int(issue["estimate"])
        issue["draft_id"] = len(self.drafts) + 1
        self.drafts.append(issue)
        return {"queued": True, "draft_id": issue["draft_id"], "note": "waiting for the team lead's approval"}

    def call(self, name: str, args: dict):
        if name == "get_sprint":
            return self.get_sprint()
        if name == "get_team":
            return self.get_team()
        if name == "search_issues":
            return self.search_issues(str(args.get("query", "")))
        if name == "check_issue":
            return self.check_issue(**args)
        if name == "create_issue":
            return self.create_issue(**args)
        raise KeyError(name)


def create_approved(mcp: MCPBridge, drafts: list[dict]) -> list[dict]:
    """After the lead approves: create each draft in GitLab through MCP. Returns one result per draft."""
    out = []
    for d in drafts:
        try:
            r = mcp.call("create_issue", {"title": d["title"], "description": quality.gitlab_description(d),
                                          "assignee": d["assignee"], "labels": list(d["labels"]),
                                          "weight": int(d["estimate"]), "milestone": d["milestone"]})
            out.append({**d, "iid": r["iid"], "url": r["web_url"], "status": "created"})
        except Exception as e:  # noqa: BLE001 - one refused issue shouldn't stop the others
            out.append({**d, "status": f"refused: {e}"})
    return out
