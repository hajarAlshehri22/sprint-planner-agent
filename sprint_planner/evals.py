"""Eval suite: evals/tasks.jsonl x configs A/B x k trials, graded by code (no LLM judge).

Task format (one JSON line, same shape as the academy's tasks.jsonl):
    {"id", "category", "lang", "prompt": <meeting notes>, "sprint"?: "Sprint 25", "memory"?: [...], "graders": [...]}

Graders (the grader type is also the failure category in the error analysis):
- ac_quality          every draft has >= 2 acceptance criteria written as Given/when/then
- fields_valid        every draft: English title, real description, team assignee, allowed labels, estimate 1/2/3/5/8,
                      right milestone
- capacity_ok         nobody goes over capacity in the sprint (existing issues + drafts, in order)
- no_duplicate        no draft repeats an existing open issue
- count_between       {min, max} number of drafts
- issue_for           {keywords (any), assignee_any?, labels_all?}  some draft covers this request
- assignee_not        {values}  nobody in the list gets an issue
- milestone_is        {value}   every draft is in this milestone
- not_contains        {values}  no draft (or the summary) contains these strings
- final_contains_any  {values}  the agent's summary mentions at least one
- tool_called         {name}

Each run uses a fresh copy of data/gitlab_seed.json, a fixed "today" (2026-10-28 -> next sprint is Sprint 24) and
only the task's own memory notes, so results are reproducible.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import config, quality

EVAL_DIR = config.ROOT / "evals"
RESULTS = EVAL_DIR / "results"
SEED = json.loads(config.SEED_FILE.read_text(encoding="utf-8"))
OPEN = [i for i in SEED["issues"] if i["state"] == "opened"]


def load_tasks(path: Path = EVAL_DIR / "tasks.jsonl", ids: list[str] | None = None, limit: int = 0) -> list[dict]:
    tasks = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if ids:
        tasks = [t for t in tasks if t["id"] in ids]
    return tasks[:limit] if limit else tasks


def _text(d: dict) -> str:
    return " ".join([str(d.get("title", "")), str(d.get("description", "")), " ".join(d.get("acceptance_criteria") or [])])


def _seed_load(milestone: str) -> dict[str, int]:
    load: dict[str, int] = {}
    for i in OPEN:
        if i.get("milestone") == milestone and i.get("assignee"):
            load[i["assignee"]] = load.get(i["assignee"], 0) + int(i["weight"])
    return load


def grade(task: dict, run) -> list[dict]:
    drafts, final = run.drafts, run.final or ""
    ms = run.milestone or task.get("sprint") or "Sprint 24"
    out = []
    for g in task["graders"]:
        kind, ok, detail = g["type"], True, ""
        if kind == "ac_quality":
            bad = [d["title"][:40] for d in drafts if any("acceptance criteria" in p for p in quality.problems(d))]
            ok, detail = not bad, f"weak acceptance criteria: {bad}" if bad else ""
        elif kind == "fields_valid":
            issues = []
            for d in drafts:
                p = [x for x in quality.problems(d, milestone=ms) if "acceptance criteria" not in x
                     and "sensitive" not in x]
                if p:
                    issues.append(f"{d.get('title', '')[:30]}: {p[0]}")
            ok, detail = not issues, "; ".join(issues[:3])
        elif kind == "capacity_ok":
            load, over = _seed_load(ms), []
            for d in drafts:
                who, est = d.get("assignee"), d.get("estimate") if isinstance(d.get("estimate"), int) else 0
                load[who] = load.get(who, 0) + est
                cap = quality.MEMBERS.get(who, {}).get("capacity", 0)
                if who in quality.MEMBERS and load[who] > cap:
                    over.append(f"{who} {load[who]}/{cap}")
            ok, detail = not over, f"over capacity: {sorted(set(over))}" if over else ""
        elif kind == "no_duplicate":
            dup = [f"'{d['title'][:35]}' ~ #{o['iid']}" for d in drafts for o in OPEN
                   if quality.similar(d.get("title", ""), o["title"]) >= 0.6]
            ok, detail = not dup, f"duplicates: {dup}" if dup else ""
        elif kind == "count_between":
            ok = g["min"] <= len(drafts) <= g["max"]
            detail = "" if ok else f"{len(drafts)} drafts, expected {g['min']}-{g['max']}"
        elif kind == "issue_for":
            def hit(d):
                if not any(k.lower() in _text(d).lower() for k in g["keywords"]):
                    return False
                if g.get("assignee_any") and d.get("assignee") not in g["assignee_any"]:
                    return False
                return set(g.get("labels_all", [])) <= set(d.get("labels") or [])
            ok = any(hit(d) for d in drafts)
            detail = "" if ok else f"no issue for {g['keywords'][:3]}" + (f" assigned to {g['assignee_any']}" if g.get("assignee_any") else "") + (f" with labels {g['labels_all']}" if g.get("labels_all") else "")
        elif kind == "assignee_not":
            bad = [d["assignee"] for d in drafts if d.get("assignee") in g["values"]]
            ok, detail = not bad, f"assigned to {bad}" if bad else ""
        elif kind == "milestone_is":
            bad = [d.get("milestone") for d in drafts if d.get("milestone") != g["value"]]
            ok = not bad and (drafts or run.milestone == g["value"])
            detail = "" if ok else f"milestone {bad or run.milestone}, expected {g['value']}"
        elif kind == "not_contains":
            blob = " ".join(_text(d) + " " + str(d.get("assignee")) for d in drafts) + " " + final
            hits = [v for v in g["values"] if v.lower() in blob.lower()]
            ok, detail = not hits, f"contains {hits}" if hits else ""
        elif kind == "final_contains_any":
            ok = any(v.lower() in final.lower() for v in g["values"])
            detail = "" if ok else f"summary mentions none of {g['values'][:4]}"
        elif kind == "tool_called":
            ok = any(n == g["name"] for n, _ in run.tool_calls)
            detail = "" if ok else f"{g['name']} not called"
        else:
            raise ValueError(f"unknown grader {kind}")
        out.append({"type": kind, "pass": bool(ok), "detail": detail})
    return out


def run_suite(configs: list[str], trials: int = 1, ids: list[str] | None = None, limit: int = 0, verbose: bool = True):
    import pandas as pd

    from .harness import plan
    from .mcp_bridge import MCPBridge

    tasks = load_tasks(ids=ids, limit=limit)
    # isolated, reproducible environment
    config.MOCK_DB = config.STATE / "eval_gitlab.json"
    shutil.copy(config.SEED_FILE, config.MOCK_DB)
    config.TODAY = __import__("datetime").date(2026, 10, 28)
    model = "FAKE (offline)" if config.FAKE_LLM else config.MODEL
    if verbose:
        print(f"{len(tasks)} tasks x {configs} x {trials} trial(s)  [model: {model}]")
    rows = []
    with MCPBridge() as mcp:
        for c in configs:
            for t in tasks:
                for k in range(trials):
                    run = plan(t["prompt"], config_name=c, sprint=t.get("sprint"), lead_notes=t.get("memory", []),
                               mcp=mcp, use_cache=None if trials == 1 else False)
                    err = "" if run.stopped == "done" else f"stopped: {run.stopped}"
                    grades = grade(t, run)
                    passed = not err and all(g["pass"] for g in grades)
                    failed = [g for g in grades if not g["pass"]]
                    rows.append({"config": c, "id": t["id"], "category": t["category"], "lang": t["lang"],
                                 "trial": k, "passed": passed,
                                 "failed_graders": " | ".join(g["type"] for g in failed) or err,
                                 "details": " | ".join(f"{g['type']}: {g['detail']}" for g in failed) or err,
                                 "drafts": len(run.drafts), "steps": run.steps, "tokens": run.tokens,
                                 "cost_usd": round(run.cost, 5), "seconds": run.seconds,
                                 "tools": " ".join(n for n, _ in run.tool_calls),
                                 "models": " ".join(sorted(run.models)), "final": run.final[:500]})
                    if verbose:
                        print(f"  {'✅' if passed else '❌'} {c} {t['id']:<24} {rows[-1]['details'][:110]}")
    return pd.DataFrame(rows), model


def report(df, trials: int) -> str:
    lines = [f"| Config | pass rate | pass^{trials} | avg requests (model turns) | avg tokens | avg drafts | avg cost $ |",
             "|---|---|---|---|---|---|---|"]
    from .harness import CONFIGS

    for c, g in df.groupby("config"):
        per_task = g.groupby("id").passed.all()
        lines.append(f"| {CONFIGS[c]['label']} | {g.passed.mean():.0%} | {per_task.mean():.0%} | {g.steps.mean():.1f} | "
                     f"{g.tokens.mean():.0f} | {g.drafts.mean():.1f} | {g.cost_usd.mean():.4f} |")
    cats = {}
    for _, r in df.iterrows():
        for cat in filter(None, r["failed_graders"].split(" | ")):
            cats.setdefault(cat, {}).setdefault(r["config"], []).append(r["id"])
    configs = sorted(df.config.unique())
    lines += ["", "### Error analysis (failed runs per root cause)", "",
              "| Root cause (grader) | " + " | ".join(f"Count ({c})" for c in configs) + " | Example task |",
              "|---|" + "---|" * len(configs) + "---|"]
    for cat, by in sorted(cats.items(), key=lambda x: -sum(len(v) for v in x[1].values())):
        example = next(iter(by.values()))[0]
        lines.append(f"| {cat} | " + " | ".join(str(len(by.get(c, []))) for c in configs) + f" | {example} |")
    if len(configs) == 2:
        a, b = configs
        pa = df[df.config == a].groupby("id").passed.all()
        pb = df[df.config == b].groupby("id").passed.all()
        fixed = sorted(i for i in pa.index if not pa[i] and pb.get(i, False))
        broke = sorted(i for i in pa.index if pa[i] and not pb.get(i, True))
        lines += ["", f"Flipped {a}→{b}: fixed {len(fixed)} {fixed}; broke {len(broke)} {broke}"]
    return "\n".join(lines)


FIX_TRIED = {
    "ac_quality": "B's check_issue loop (write, check, fix)",
    "fields_valid": "issue format in AGENTS.md + check_issue",
    "capacity_ok": "points left in get_team + capacity rule in check_issue",
    "no_duplicate": "search_issues before every draft",
    "count_between": "workflow step 3 (split one request per role) in AGENTS.md",
    "issue_for": "skills in get_team; splitting rule in AGENTS.md",
    "assignee_not": "lead memory notes in the prompt",
    "milestone_is": "get_sprint override",
    "not_contains": "notes sanitizer + draft policy (guards.py)",
    "final_contains_any": "summary step in AGENTS.md",
    "tool_called": "workflow order in AGENTS.md",
}
README_START, README_END = "<!-- EVAL_RESULTS:START -->", "<!-- EVAL_RESULTS:END -->"


def readme_block(df, trials: int, model: str) -> str:
    """The results section written into README.md between the markers."""
    from .harness import CONFIGS

    configs = sorted(df.config.unique())
    used = sorted({m for v in df.get("models", []) for m in str(v).split() if m and m != "nan"})
    out = [f"_Last run: {time.strftime('%Y-%m-%d %H:%M')} · model `{model}` via `{config.PROVIDER}` · "
           f"{df.id.nunique()} tasks · {trials} trial(s) per task · filled in automatically by `plan.py eval`._", "",
           f"Models that actually answered: {', '.join(f'`{m}`' for m in used) or '-'}"
           + (" (one model for every task, so A and B are compared fairly)." if len(used) == 1 else
              " — more than one model answered; for a fair A vs B comparison re-run with `AGENT_FALLBACKS=` empty.")
           if used else "", "",
           f"| Config | pass rate | pass^k (k = {trials}) | avg requests | avg tokens |", "|---|---|---|---|---|"]
    rates = {}
    for c in configs:
        g = df[df.config == c]
        rates[c] = g.passed.mean()
        out.append(f"| {CONFIGS[c]['label']} | {g.passed.mean():.0%} | {g.groupby('id').passed.all().mean():.0%} | "
                   f"{g.steps.mean():.1f} | {g.tokens.mean():,.0f} |")
    cats: dict[str, dict[str, list[str]]] = {}
    for _, r in df.iterrows():
        for cat in filter(None, str(r["failed_graders"]).split(" | ")):
            cats.setdefault(cat, {}).setdefault(r["config"], []).append(r["id"])
    out += ["", "### Error analysis", "",
            "| Root cause (failed grader) | " + " | ".join(f"Count ({c})" for c in configs) + " | Example task | Fix tried |",
            "|---|" + "---|" * len(configs) + "---|---|"]
    ranked = sorted(cats.items(), key=lambda x: -sum(len(v) for v in x[1].values()))
    for cat, by in ranked:
        example = next(iter(by.values()))[0]
        out.append(f"| `{cat}` | " + " | ".join(str(len(by.get(c, []))) for c in configs) +
                   f" | `{example}` | {FIX_TRIED.get(cat, 'run stopped (budget / provider error)')} |")
    if not ranked:
        out.append("| none | " + " | ".join("0" for _ in configs) + " | - | - |")
    if len(configs) == 2:
        a, b = configs
        pa = df[df.config == a].groupby("id").passed.all()
        pb = df[df.config == b].groupby("id").passed.all()
        fixed = sorted(i for i in pa.index if not pa[i] and pb.get(i, False))
        broke = sorted(i for i in pa.index if pa[i] and not pb.get(i, True))
        top_a = max(((c, len(v.get(a, []))) for c, v in cats.items()), key=lambda x: x[1], default=(None, 0))
        da, db = df[df.config == a].steps.mean(), df[df.config == b].steps.mean()
        out += ["", "**What flipped between A and B:** "
                f"B fixed {len(fixed)} task(s) ({', '.join(f'`{i}`' for i in fixed) or 'none'}) and broke {len(broke)} "
                f"({', '.join(f'`{i}`' for i in broke) or 'none'}). Pass rate went from {rates[a]:.0%} to {rates[b]:.0%} "
                f"for {db - da:+.1f} model requests per run."
                + (f" The biggest failure cause in A was `{top_a[0]}` ({top_a[1]} run(s))." if top_a[0] else ""),
                "", "**What I'd change next:** _(one or two sentences in your own words)_"]
    out += ["", "Per-task results: [`evals/results/eval_A.csv`](evals/results/eval_A.csv), "
            "[`evals/results/eval_B.csv`](evals/results/eval_B.csv), summary: "
            "[`evals/results/summary.md`](evals/results/summary.md)."]
    return "\n".join(out)


def update_readme(block: str) -> bool:
    path = config.ROOT / "README.md"
    text = path.read_text(encoding="utf-8")
    if README_START not in text or README_END not in text:
        return False
    head, rest = text.split(README_START, 1)
    _, tail = rest.split(README_END, 1)
    path.write_text(f"{head}{README_START}\n{block}\n{README_END}{tail}", encoding="utf-8")
    return True


def main(configs: list[str], trials: int = 1, ids: list[str] | None = None, limit: int = 0) -> int:
    df, model = run_suite(configs, trials, ids, limit)
    RESULTS.mkdir(parents=True, exist_ok=True)
    for c, g in df.groupby("config"):
        g.to_csv(RESULTS / f"eval_{c}.csv", index=False)
    table = report(df, trials)
    md = (f"# Eval results — {time.strftime('%Y-%m-%d %H:%M')}\n\nModel: `{model}` · tasks: {df.id.nunique()} · "
          f"trials: {trials}\n\n{table}\n")
    if config.FAKE_LLM:
        md += "\n> Produced with the offline FAKE model: checks the pipeline only, not real results.\n"
    (RESULTS / "summary.md").write_text(md, encoding="utf-8")
    print("\n" + table + f"\n\nSaved: evals/results/summary.md, " + ", ".join(f"evals/results/eval_{c}.csv" for c in configs))
    if config.FAKE_LLM:
        print("README not updated (offline FAKE model results are not real).")
    elif update_readme(readme_block(df, trials, model)):
        print("README.md: Evaluation results section updated.")
    return 0
