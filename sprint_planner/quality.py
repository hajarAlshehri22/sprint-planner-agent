"""check_issue: the quality rules for one issue draft, in plain code.

Used three ways: as the agent's `check_issue` tool (config B), to show warnings in the approval table,
and by the eval graders. Same rules everywhere, so "what good looks like" is defined once.
"""
from __future__ import annotations

import json
import re

from . import config
from .guards import find_secrets

TEAM = json.loads(config.TEAM_FILE.read_text(encoding="utf-8"))
MEMBERS = {m["username"]: m for m in TEAM["members"]}
TYPE_LABELS = {"feature", "bug", "tech-debt", "security", "qa", "devops"}
PRIORITY_LABELS = {"priority::high", "priority::medium", "priority::low"}
ESTIMATES = set(TEAM["estimates"])


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2}


def similar(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    return len(wa & wb) / max(1, min(len(wa), len(wb)))


def problems(issue: dict, *, milestone: str | None = None, load: dict[str, int] | None = None,
             open_issues: list[dict] | None = None) -> list[str]:
    """Everything wrong with one draft. Empty list = good to go.

    load: points already planned per username in this sprint (existing issues + drafts queued before this one).
    """
    p = []
    title = (issue.get("title") or "").strip()
    desc = (issue.get("description") or "").strip()
    ac = issue.get("acceptance_criteria") or []
    if isinstance(ac, str):
        ac = [a for a in re.split(r"\n+|(?<=\.)\s+(?=Given)", ac) if a.strip()]
    if not 5 <= len(title) <= 100:
        p.append("title must be 5-100 characters")
    if re.search(r"[؀-ۿ]", title + desc + " ".join(ac)):
        p.append("write the issue in English (Arabic text found)")
    if len(desc.split()) < 12:
        p.append("description too short: explain what and why in 2-4 sentences")
    good_ac = [a for a in ac if re.search(r"\bgiven\b", a, re.I) and re.search(r"\bthen\b", a, re.I)]
    if len(good_ac) < 2:
        p.append(f"needs at least 2 acceptance criteria written as 'Given ... when ... then ...' (has {len(good_ac)})")
    who = issue.get("assignee")
    if who not in MEMBERS:
        p.append(f"assignee {who!r} is not on the team; use one of {sorted(MEMBERS)}")
    labels = set(issue.get("labels") or [])
    if not labels & TYPE_LABELS:
        p.append(f"add one type label: {sorted(TYPE_LABELS)}")
    if len(labels & PRIORITY_LABELS) != 1:
        p.append("add exactly one priority label: priority::high / priority::medium / priority::low")
    unknown = labels - TYPE_LABELS - PRIORITY_LABELS
    if unknown:
        p.append(f"labels not allowed: {sorted(unknown)}")
    est = issue.get("estimate")
    if est not in ESTIMATES:
        p.append(f"estimate must be one of {sorted(ESTIMATES)} (got {est!r})")
    if milestone and issue.get("milestone") != milestone:
        p.append(f"milestone must be {milestone!r}")
    if who in MEMBERS and load is not None and isinstance(est, int):
        cap, used = MEMBERS[who]["capacity"], load.get(who, 0)
        if used + est > cap:
            p.append(f"{who} would be over capacity ({used}+{est} > {cap} points); pick someone with points left "
                     "or say in the summary that this should move to the next sprint")
    secrets = find_secrets(" ".join([title, desc, *ac]))
    if secrets:
        p.append(f"remove sensitive data from the issue: {', '.join(secrets)}")
    for o in open_issues or []:
        if similar(title, o["title"]) >= 0.6:
            p.append(f"looks like existing open issue #{o['iid']} '{o['title']}': don't duplicate it")
            break
    return p


def gitlab_description(issue: dict) -> str:
    """The text that goes into GitLab: description + acceptance criteria as a checklist."""
    ac = issue.get("acceptance_criteria") or []
    if isinstance(ac, str):
        ac = [a for a in ac.splitlines() if a.strip()]
    lines = [issue.get("description", "").strip(), "", "### Acceptance criteria"] + [f"- [ ] {a.strip()}" for a in ac]
    lines += ["", f"_Estimate: {issue.get('estimate')} points · drafted by Sprint Planner, approved by the team lead_"]
    return "\n".join(lines)
