"""Offline tests (no key, no network, no cost):  uv run pytest -q"""
import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp())
os.environ.update(PLANNER_FAKE_LLM="1", PLANNER_TODAY="2026-10-28", GITLAB_MOCK_DB=str(_tmp / "gitlab.json"),
                  MEMORY_FILE=str(_tmp / "memory.json"), PLANNER_CACHE="0")

import pytest  # noqa: E402

from sprint_planner import config, guards, quality  # noqa: E402
from sprint_planner.harness import permission, plan  # noqa: E402
from sprint_planner.mcp_bridge import MCPBridge, MCPToolError  # noqa: E402
from sprint_planner.tools import create_approved  # noqa: E402

GOOD = {"title": "Add search by permit number", "assignee": "omar.h", "labels": ["feature", "priority::medium"],
        "estimate": 3, "milestone": "Sprint 24",
        "description": "Citizens want to find a permit quickly. Add a search box on the permits list page that "
                       "filters by permit number.",
        "acceptance_criteria": ["Given a valid permit number, when I search, then only that permit is shown",
                                "Given an unknown number, when I search, then 'no results' is shown"]}


@pytest.fixture(scope="module")
def mcp():
    with MCPBridge() as b:
        yield b


# --- quality rules -------------------------------------------------------------------------
def test_good_issue_has_no_problems():
    assert quality.problems(GOOD, milestone="Sprint 24", load={}) == []


def test_quality_catches_weak_issue():
    bad = {**GOOD, "acceptance_criteria": ["works"], "labels": ["feature"], "estimate": 4, "assignee": "bob"}
    p = " ".join(quality.problems(bad, milestone="Sprint 24"))
    for word in ("acceptance criteria", "priority", "estimate", "not on the team"):
        assert word in p


def test_capacity_and_duplicates():
    p = quality.problems({**GOOD, "assignee": "reem.s", "estimate": 5}, load={"reem.s": 10})
    assert any("over capacity" in x for x in p)
    p = quality.problems({**GOOD, "title": "Nafath login fails on Safari iOS"},
                         open_issues=[{"iid": 41, "title": "Nafath login fails on Safari iOS"}])
    assert any("#41" in x for x in p)


# --- guards ----------------------------------------------------------------------------------
def test_sanitizer_removes_hidden_instructions_and_secrets():
    raw = (config.ROOT / "attacks" / "injected_notes.md").read_text()
    clean, removed = guards.sanitize_notes(raw)
    assert "vendor.support" not in clean and "Riy@dh" not in clean
    assert any("HTML comment" in r for r in removed) and any("password" in r for r in removed)


def test_draft_policy():
    assert guards.check_draft(GOOD) is None
    assert "not on the team" in guards.check_draft({**GOOD, "assignee": "vendor.support"})
    assert "outside the team" in guards.check_draft({**GOOD, "description": GOOD["description"] + " cc @vendor.x"})
    assert "sensitive" in guards.check_draft({**GOOD, "description": "password: Hunter2024!"})


def test_permission_policy():
    tools = ["get_sprint", "search_issues", "create_issue"]
    assert permission("search_issues", tools) == "allow"
    assert permission("create_issue", tools) == "ask"        # queued for the human
    assert permission("check_issue", tools) == "deny"        # not offered in config A
    assert permission("delete_issue", tools) == "deny"


# --- MCP server ------------------------------------------------------------------------------
def test_mcp_tools_and_server_policy(mcp):
    assert set(mcp.tool_names()) == {"list_milestones", "search_issues", "list_open_issues", "create_issue"}
    with pytest.raises(MCPToolError):
        mcp.call("create_issue", {"title": "x" * 10, "description": "d", "assignee": "vendor.support",
                                  "labels": ["feature"], "weight": 1, "milestone": "Sprint 24"})
    assert mcp.call("search_issues", {"query": "Nafath login Safari"})[0]["iid"] == 41


# --- the agent loop ----------------------------------------------------------------------------
def test_plan_queues_drafts_and_creates_nothing(mcp):
    before = len(mcp.call("list_open_issues"))
    run = plan((config.DATA / "meetings" / "sprint24_planning.md").read_text(), config_name="B", mcp=mcp, lead_notes=[])
    assert run.stopped == "done" and run.milestone == "Sprint 24" and run.drafts
    assert len(mcp.call("list_open_issues")) == before          # nothing written without approval
    names = [n for n, _ in run.tool_calls]
    assert names[:2] == ["get_sprint", "get_team"] and "search_issues" in names


def test_config_b_fixes_what_a_misses(mcp):
    notes = "- Add search by permit number on the permits list page\n- New API endpoint to cancel a pending appointment"
    a = plan(notes, config_name="A", mcp=mcp, lead_notes=[])
    b = plan(notes, config_name="B", mcp=mcp, lead_notes=[])
    weak = lambda r: [d for d in r.drafts if any("acceptance" in p for p in quality.problems(d))]  # noqa: E731
    assert weak(a) and not weak(b)
    assert not any(n == "check_issue" for n, _ in a.tool_calls) and any(n == "check_issue" for n, _ in b.tool_calls)


def test_sprint_override(mcp):
    run = plan("- Add an FAQ page to the portal with common questions", sprint="Sprint 25", mcp=mcp, lead_notes=[])
    assert run.milestone == "Sprint 25" and all(d["milestone"] == "Sprint 25" for d in run.drafts)


def test_attack_blocked_only_with_guards(mcp):
    from sprint_planner.attack import assess

    notes = (config.ROOT / "attacks" / "injected_notes.md").read_text()
    off = assess(plan(notes, guards_on=False, mcp=mcp, lead_notes=[]))
    on = assess(plan(notes, guards_on=True, mcp=mcp, lead_notes=[]))
    assert off["attack_succeeded"] and not on["attack_succeeded"]


def test_approved_drafts_are_created(mcp):
    [r] = create_approved(mcp, [GOOD])
    assert r["status"] == "created" and r["iid"] > 45


def test_budget_stops_the_loop(mcp, monkeypatch):
    monkeypatch.setattr(config, "MAX_STEPS", 3)
    run = plan("- Add search by permit number on the permits list page", mcp=mcp, lead_notes=[])
    assert run.stopped == "max_steps" and run.steps == 3
