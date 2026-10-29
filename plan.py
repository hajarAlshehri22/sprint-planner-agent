"""Sprint Planner — command line.

    uv run plan.py notes data/meetings/sprint24_planning.md      # plan a sprint, approve in the terminal
    uv run plan.py notes my_notes.md --sprint "Sprint 25"        # choose the milestone yourself
    uv run plan.py eval --config A B                             # eval suite: A vs B (writes CSVs + summary)
    uv run plan.py attack                                        # prompt-injection demo: guards off vs on
    uv run plan.py remember "huda.k is on leave in Sprint 24"    # add a memory note
    uv run plan.py memory | board | tools | models

Add --fake to any command to run offline with a scripted model (no API key, no cost).
The web UI is app.py:  uv run app.py
"""
from __future__ import annotations

import argparse
import os
import sys
import textwrap


def show_drafts(drafts: list[dict], problems_for) -> None:
    for i, d in enumerate(drafts, 1):
        print(f"\n[{i}] {d['title']}")
        print(f"    assignee: {d['assignee']} · labels: {', '.join(d['labels'] or [])} · "
              f"points: {d['estimate']} · milestone: {d['milestone']}")
        print(textwrap.indent(textwrap.fill(d.get("description") or "", 100), "    "))
        for a in d.get("acceptance_criteria") or []:
            print(f"    ☐ {a}")
        for p in problems_for(d):
            print(f"    ⚠️  {p}")


def cmd_notes(args) -> int:
    from sprint_planner import guards, memory, quality
    from sprint_planner.harness import plan
    from sprint_planner.mcp_bridge import MCPBridge
    from sprint_planner.tools import create_approved

    notes = open(args.file, encoding="utf-8").read()
    icons = {"call": "🔧", "result": "📦", "guard": "🛡️", "final": "✅", "error": "⚠️", "model": "🤖"}
    with MCPBridge() as mcp:
        run = plan(notes, config_name=args.config, sprint=args.sprint, mcp=mcp,
                   on_event=lambda e: print(f"{icons.get(e['kind'], '·')} {e['text'][:150]}") if e["kind"] in icons else None)
        print(f"\nSprint: {run.milestone} · {len(run.drafts)} draft(s) · {run.steps} steps · ${run.cost:.4f} · {run.stopped}")
        print(f"\nAgent summary:\n{run.final}")
        if not run.drafts:
            return 0
        show_drafts(run.drafts, lambda d: quality.problems(d, milestone=run.milestone))
        if args.yes:
            choice = "all"
        else:
            choice = input("\nCreate which issues in GitLab? [all / 1,3 / none] > ").strip().lower()
        if choice in ("", "none", "n", "no"):
            print("Nothing created.")
            return 0
        pick = run.drafts if choice in ("all", "a", "y", "yes") else \
            [run.drafts[int(x) - 1] for x in choice.replace(" ", "").split(",") if x.isdigit() and 0 < int(x) <= len(run.drafts)]
        safe = []
        for d in pick:
            block = guards.check_draft(d)
            print(f"  ⛔ not created: {block}") if block else safe.append(d)
        results = create_approved(mcp, safe)
        for r in results:
            print(f"  {'✅' if r['status'] == 'created' else '⛔'} #{r.get('iid', '-')} {r['title'][:70]}  {r.get('url', r['status'])}")
        memory.record_plan(run.milestone, [r for r in results if r["status"] == "created"])
    return 0


def cmd_eval(args) -> int:
    from sprint_planner.evals import main

    return main(args.config, trials=args.trials, ids=args.ids, limit=args.limit)


def cmd_attack(args) -> int:
    from sprint_planner.attack import main

    return main()


def cmd_remember(args) -> int:
    from sprint_planner import memory

    memory.add_note(args.text)
    print("Saved. Memory notes:", *[f"  - {n}" for n in memory.notes()], sep="\n")
    return 0


def cmd_memory(args) -> int:
    from sprint_planner import memory

    print("Memory notes:", *[f"  {i}. {n}" for i, n in enumerate(memory.notes())] or ["  (none)"], sep="\n")
    print("Approved plans:", *[f"  {h['date']} {h['milestone']}: {len(h['issues'])} issue(s)" for h in memory.history()]
          or ["  (none)"], sep="\n")
    return 0


def cmd_board(args) -> int:
    from sprint_planner.mcp_bridge import MCPBridge

    with MCPBridge() as mcp:
        for i in sorted(mcp.call("list_open_issues"), key=lambda i: (i.get("milestone") or "", i["iid"])):
            print(f"#{i['iid']:<4} {i.get('milestone') or '-':<10} {i.get('assignee') or '-':<10} {i.get('weight')}pt  {i['title']}")
    return 0


def cmd_models(args) -> int:
    from sprint_planner import config
    from sprint_planner.llm import list_models

    print(f"Provider: {config.PROVIDER} ({config.BASE_URL})")
    for m in list_models():
        if not args.free or m.endswith(":free") or config.PROVIDER != "openrouter":
            print(" ", m)
    return 0


def cmd_tools(args) -> int:
    from sprint_planner.mcp_bridge import MCPBridge
    from sprint_planner.tools import SCHEMAS

    with MCPBridge() as mcp:
        print("Agent tools :", ", ".join(SCHEMAS))
        print("MCP server  :", ", ".join(mcp.tool_names()), "(gitlab)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fake", action="store_true", help="offline scripted model (no key, no cost)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("notes", help="plan a sprint from meeting notes")
    n.add_argument("file")
    n.add_argument("--sprint", default=None, help="milestone title, e.g. 'Sprint 25' (default: next sprint)")
    n.add_argument("--config", default="B", choices=["A", "B"])
    n.add_argument("--yes", action="store_true", help="approve all drafts without asking")
    n.set_defaults(fn=cmd_notes)
    e = sub.add_parser("eval", help="run the eval suite")
    e.add_argument("--config", nargs="+", default=["A", "B"])
    e.add_argument("--trials", type=int, default=1)
    e.add_argument("--ids", nargs="*", default=None)
    e.add_argument("--limit", type=int, default=0)
    e.set_defaults(fn=cmd_eval)
    sub.add_parser("attack", help="prompt-injection demo").set_defaults(fn=cmd_attack)
    r = sub.add_parser("remember", help="add a memory note")
    r.add_argument("text")
    r.set_defaults(fn=cmd_remember)
    sub.add_parser("memory", help="show memory").set_defaults(fn=cmd_memory)
    sub.add_parser("board", help="list open GitLab issues").set_defaults(fn=cmd_board)
    sub.add_parser("tools", help="list tools").set_defaults(fn=cmd_tools)
    mo = sub.add_parser("models", help="list the model ids your provider offers")
    mo.add_argument("--free", action="store_true", help="OpenRouter: only :free models")
    mo.set_defaults(fn=cmd_models)
    args = ap.parse_args()
    if args.fake:
        os.environ["PLANNER_FAKE_LLM"] = "1"   # must be set before sprint_planner is imported
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
