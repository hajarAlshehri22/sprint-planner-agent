"""MCP server "gitlab": the agent's only way to read or write GitLab.

Tools: list_milestones, search_issues, list_open_issues, create_issue.
There is deliberately NO close / delete / edit / add-member tool: the agent cannot do what it isn't given.

Backend: a local mock (state/gitlab_mock.json, seeded from data/gitlab_seed.json) by default, or a real
GitLab project when GITLAB_URL, GITLAB_TOKEN and GITLAB_PROJECT are set.

Server-side policy (enforced here, whatever the agent or a prompt says):
- only the one configured project is used;
- create_issue accepts assignees from data/team.json only, and labels from the allowed list only.

Run standalone:  uv run python -m sprint_planner.gitlab_mcp
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import config

server = MCPServer("gitlab", log_level="ERROR")
TEAM = json.loads(config.TEAM_FILE.read_text(encoding="utf-8"))
MEMBERS = {m["username"] for m in TEAM["members"]}
LABELS = set(TEAM["labels"])


# ---------------------------------------------------------------- backends
class MockGitLab:
    def __init__(self):
        if not config.MOCK_DB.exists():
            config.MOCK_DB.parent.mkdir(parents=True, exist_ok=True)
            config.MOCK_DB.write_text(config.SEED_FILE.read_text(encoding="utf-8"), encoding="utf-8")

    def _load(self) -> dict:
        return json.loads(config.MOCK_DB.read_text(encoding="utf-8"))

    def milestones(self) -> list[dict]:
        return self._load()["milestones"]

    def issues(self, state: str = "opened") -> list[dict]:
        return [i for i in self._load()["issues"] if state == "all" or i["state"] == state]

    def create(self, issue: dict) -> dict:
        db = self._load()
        issue = {**issue, "iid": max(i["iid"] for i in db["issues"]) + 1, "state": "opened"}
        db["issues"].append(issue)
        config.MOCK_DB.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"iid": issue["iid"], "web_url": f"mock://{db['project']}/-/issues/{issue['iid']}"}


class RealGitLab:
    def __init__(self):
        import httpx

        self.http = httpx.Client(base_url=f"{config.GITLAB_URL.rstrip('/')}/api/v4/projects/{quote(config.GITLAB_PROJECT, safe='')}",
                                 headers={"PRIVATE-TOKEN": config.GITLAB_TOKEN}, timeout=20)
        self._users: dict[str, int] = {}

    def milestones(self) -> list[dict]:
        r = self.http.get("/milestones", params={"state": "active", "per_page": 50})
        r.raise_for_status()
        return [{"id": m["id"], "title": m["title"], "start_date": m.get("start_date"), "due_date": m.get("due_date"),
                 "state": m["state"]} for m in r.json()]

    def issues(self, state: str = "opened") -> list[dict]:
        r = self.http.get("/issues", params={"state": state, "per_page": 100})
        r.raise_for_status()
        return [{"iid": i["iid"], "title": i["title"], "state": i["state"],
                 "assignee": (i.get("assignee") or {}).get("username"), "labels": i.get("labels", []),
                 "weight": i.get("weight") or 0, "milestone": (i.get("milestone") or {}).get("title")} for i in r.json()]

    def _user_id(self, username: str) -> int | None:
        if username not in self._users:
            r = self.http.get("/members/all", params={"query": username})
            r.raise_for_status()
            match = [u for u in r.json() if u["username"] == username]
            self._users[username] = match[0]["id"] if match else None
        return self._users[username]

    def create(self, issue: dict) -> dict:
        ms = {m["title"]: m["id"] for m in self.milestones()}
        body = {"title": issue["title"], "description": issue["description"], "labels": ",".join(issue["labels"]),
                "weight": issue["weight"]}
        if issue.get("milestone") in ms:
            body["milestone_id"] = ms[issue["milestone"]]
        uid = self._user_id(issue["assignee"])
        if uid:
            body["assignee_ids"] = [uid]
        r = self.http.post("/issues", json=body)
        r.raise_for_status()
        return {"iid": r.json()["iid"], "web_url": r.json()["web_url"]}


BACKEND = RealGitLab() if config.USE_REAL_GITLAB else MockGitLab()


# ---------------------------------------------------------------- tools
@server.tool()
def list_milestones() -> list[dict]:
    """List the project's active milestones (sprints) with start and due dates."""
    return BACKEND.milestones()


@server.tool()
def search_issues(query: str, state: str = "all") -> list[dict]:
    """Search issues by words in the title. Returns the best matches first (iid, title, state, assignee)."""
    words = {w for w in re.findall(r"\w+", query.lower()) if len(w) > 2}
    scored = []
    for i in BACKEND.issues(state):
        title = set(re.findall(r"\w+", i["title"].lower()))
        score = len(words & title)
        if score:
            scored.append((score, i))
    scored.sort(key=lambda x: (-x[0], x[1]["state"] != "opened"))
    return [{"iid": i["iid"], "title": i["title"], "state": i["state"], "assignee": i["assignee"],
             "points": i.get("weight"), "milestone": i.get("milestone")}
            for _, i in scored[: config.MAX_SEARCH_RESULTS]]


@server.tool()
def list_open_issues() -> list[dict]:
    """All open issues with assignee, weight (points) and milestone."""
    return BACKEND.issues("opened")


@server.tool()
def create_issue(title: str, description: str, assignee: str, labels: list[str], weight: int, milestone: str) -> dict:
    """Create one issue in the configured project. Called by the harness only after the team lead approved it."""
    if assignee not in MEMBERS:
        raise ToolError(f"refused: {assignee!r} is not a member of this team")
    bad = [l for l in labels if l not in LABELS]
    if bad:
        raise ToolError(f"refused: labels not allowed: {bad}")
    if re.search(r"(?<![\w.])@(?!(" + "|".join(map(re.escape, MEMBERS)) + r")\b)[\w.-]+", description):
        raise ToolError("refused: description mentions someone outside the team")
    return BACKEND.create({"title": title[:200], "description": description, "assignee": assignee,
                           "labels": labels, "weight": int(weight), "milestone": milestone})


if __name__ == "__main__":
    server.run()
