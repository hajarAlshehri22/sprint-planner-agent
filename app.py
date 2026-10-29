"""Sprint Planner — web UI (Gradio).

    uv run app.py            # then open http://127.0.0.1:7860
    uv run app.py --fake     # offline scripted model (no API key)

Tabs: Plan sprint · GitLab board · Memory · Attack demo · Evals
"""
from __future__ import annotations

import os
import sys

if "--fake" in sys.argv:
    os.environ["PLANNER_FAKE_LLM"] = "1"   # before sprint_planner is imported

import json
import queue
import threading

import gradio as gr
import pandas as pd

from sprint_planner import config, guards, memory, quality
from sprint_planner.harness import CONFIGS, plan
from sprint_planner.mcp_bridge import MCPBridge
from sprint_planner.tools import create_approved

TEAM = json.loads(config.TEAM_FILE.read_text(encoding="utf-8"))
PEOPLE = [(f"{m['name']} · {m['role']}", m["username"]) for m in TEAM["members"]]
NAMES = {m["username"]: m["name"] for m in TEAM["members"]}
TYPES = ["feature", "bug", "tech-debt", "security", "qa", "devops"]
PRIORITIES = ["high", "medium", "low"]
POINTS = [1, 2, 3, 5, 8]
EXAMPLES = {p.stem: p.read_text(encoding="utf-8") for p in sorted((config.DATA / "meetings").glob("*.md"))}
SIMPLE = EXAMPLES.get("simple_example", "")
AUTO = "Next sprint (automatic)"
ICONS = {"call": "🔧", "result": "📦", "guard": "🛡️", "final": "✅", "error": "⚠️", "model": "🤖", "summary": "📊"}


def milestones() -> list[str]:
    try:
        with MCPBridge() as mcp:
            return [m["title"] for m in mcp.call("list_milestones")]
    except Exception:  # noqa: BLE001
        return []


def steps_md(events: list[dict]) -> str:
    lines = [f"{ICONS.get(e['kind'], '·')} {e['text'][:200]}" for e in events if e["kind"] != "user"]
    return "\n\n".join(lines[-60:]) or "_nothing yet_"


def split_labels(labels: list[str]) -> tuple[str, str]:
    t = next((l for l in labels or [] if l in TYPES), "feature")
    p = next((l.split("::")[1] for l in labels or [] if l.startswith("priority::")), "medium")
    return t, p


# ---------------------------------------------------------------- plan
def on_plan(notes: str, sprint: str, cfg: str):
    if not notes.strip():
        yield "", "⚠️ Write or paste the meeting notes first.", gr.skip(), gr.skip()
        return
    q: queue.Queue = queue.Queue()
    events: list[dict] = []
    box: dict = {}

    def work():
        try:
            box["run"] = plan(notes, config_name=cfg[0], sprint=None if sprint == AUTO else sprint, on_event=q.put)
        except Exception as e:  # noqa: BLE001
            box["error"] = str(e)
        q.put(None)

    threading.Thread(target=work, daemon=True).start()
    yield "", "⏳ **The agent is working…** (open *What the agent did* to watch it)", [], {}
    while (e := q.get()) is not None:
        events.append(e)
        yield steps_md(events), gr.skip(), gr.skip(), gr.skip()
    run = box.get("run")
    if run is None or run.stopped == "error":
        err = box.get("error") or next((e["text"] for e in reversed(events) if e["kind"] == "error"), "unknown error")
        hint = " The free model is busy (429): wait a minute or change AGENT_MODEL in .env." if "429" in err else ""
        yield steps_md(events), f"⚠️ **Something went wrong:** {err}{hint}", [], {}
        return
    if run.drafts:
        head = f"### {len(run.drafts)} issue(s) for **{run.milestone}** — check them below"
    else:
        head = f"### No new issues for **{run.milestone}**"
    head += f"\n\n{run.final or ''}"
    drafts = [{**d, "_checks": quality.problems(d, milestone=run.milestone), "_block": guards.check_draft(d)}
              for d in run.drafts]
    yield steps_md(events), head, drafts, {"milestone": run.milestone}


# ---------------------------------------------------------------- create
def on_create(drafts: list[dict], edits: dict, meta: dict):
    if not drafts:
        return "Nothing to create yet: click **Plan sprint** first.", board_df()
    final = []
    for i, d in enumerate(drafts):
        e = edits.get(str(i), {})
        if not e.get("create", not d.get("_block")):
            continue
        t, p = split_labels(d.get("labels"))
        item = {k: v for k, v in d.items() if not k.startswith("_")}
        item.update(title=e.get("title", d["title"]), assignee=e.get("assignee", d["assignee"]),
                    estimate=int(e.get("estimate", d.get("estimate") or 3)),
                    labels=[e.get("type", t), f"priority::{e.get('priority', p)}"],
                    description=e.get("description", d.get("description", "")),
                    acceptance_criteria=[a.strip() for a in e.get("ac", "\n".join(d.get("acceptance_criteria") or [])).splitlines()
                                         if a.strip()])
        final.append(item)
    if not final:
        return "No issue is ticked.", board_df()
    lines, safe = [], []
    for d in final:
        block = guards.check_draft(d)   # the security policy still applies after approval
        lines.append(f"⛔ **{d['title']}** — {block}") if block else safe.append(d)
    with MCPBridge() as mcp:
        results = create_approved(mcp, safe)
    for r in results:
        if r["status"] == "created":
            lines.append(f"✅ **#{r['iid']}** {r['title']} → {NAMES.get(r['assignee'], r['assignee'])} · {r['estimate']} pts")
        else:
            lines.append(f"⛔ **{r['title']}** — {r['status']}")
    created = [r for r in results if r["status"] == "created"]
    memory.record_plan((meta or {}).get("milestone") or "", created)
    return (f"### 🎉 Created {len(created)} issue(s) in GitLab\n\n" + "\n\n".join(lines) +
            "\n\nSee them in the **GitLab board** tab."), board_df()


# ---------------------------------------------------------------- other tabs
def board_df() -> pd.DataFrame:
    try:
        with MCPBridge() as mcp:
            issues = mcp.call("list_open_issues")
    except Exception as e:  # noqa: BLE001
        return pd.DataFrame([[str(e)]], columns=["error"])
    rows = [[f"#{i['iid']}", i.get("milestone") or "", NAMES.get(i.get("assignee"), i.get("assignee") or ""),
             i.get("weight"), i["title"]] for i in sorted(issues, key=lambda i: (i.get("milestone") or "", -i["iid"]))]
    return pd.DataFrame(rows, columns=["Issue", "Sprint", "Assigned to", "Points", "Title"])


def memory_md() -> str:
    notes = memory.notes()
    return "### The agent remembers\n" + ("\n".join(f"{i + 1}. {n}" for i, n in enumerate(notes)) or "_nothing yet_")


def on_remember(text: str):
    memory.add_note(text)
    return "", memory_md()


def on_forget(n):
    try:
        memory.remove_note(int(n) - 1)
    except (TypeError, ValueError):
        pass
    return memory_md()


def on_attack():
    from sprint_planner.attack import run_both

    r = run_both()

    def fmt(a, title):
        s = f"### {title}\n\n## {'🔴 Attack worked' if a['attack_succeeded'] else '🟢 Attack stopped'}\n\n"
        s += f"- Password copied into an issue: **{'YES' if a['secret_in_drafts'] else 'no'}**\n"
        s += f"- Outsider @vendor.support added: **{'YES' if a['outsider_in_drafts'] else 'no'}**\n"
        for x in a["removed_from_notes"] + a["blocked_by_policy"]:
            s += f"- 🛡️ {x}\n"
        return s

    return fmt(r["off"], "Without protection"), fmt(r["on"], "With protection (code)")


def evals_view():
    s = config.ROOT / "evals" / "results" / "summary.md"
    md = s.read_text(encoding="utf-8") if s.exists() else "_No results yet. In the terminal run:_ `uv run plan.py eval --config A B`"
    frames = [pd.read_csv(p) for p in sorted((config.ROOT / "evals" / "results").glob("eval_*.csv"))]
    df = pd.concat(frames)[["config", "id", "passed", "details"]] if frames else pd.DataFrame()
    return md, df


def audit_tail() -> str:
    if not config.AUDIT_LOG.exists():
        return "(empty)"
    return "\n".join(l[:300] for l in config.AUDIT_LOG.read_text(encoding="utf-8").splitlines()[-40:])


# ---------------------------------------------------------------- layout
CSS = ".issue-card {border: 1px solid var(--border-color-primary); border-radius: 12px; padding: 6px;}"

with gr.Blocks(title="Sprint Planner", analytics_enabled=False) as demo:  # no usage data sent to Gradio
    mode = " · offline demo model" if config.FAKE_LLM else ""
    gr.Markdown(f"# 🗓️ Sprint Planner{mode}\nPaste your meeting notes → the agent prepares GitLab issues → "
                "you check them → you click **Create**.")
    drafts = gr.State([])
    edits = gr.State({})
    meta = gr.State({})

    with gr.Tab("Plan sprint"):
        notes = gr.Textbox(label="1️⃣ Meeting notes", value=SIMPLE, lines=6,
                           placeholder="- Add a button to …\n- Fix the bug where …")
        with gr.Row():
            ex = gr.Button("📝 Load simple example", size="md")
            go = gr.Button("🤖 2️⃣ Plan sprint", variant="primary", size="lg", scale=2)
        with gr.Accordion("⚙️ Advanced", open=False):
            with gr.Row():
                sprint = gr.Dropdown([AUTO] + milestones(), value=AUTO, label="Sprint")
                cfg = gr.Radio([CONFIGS["B"]["label"], CONFIGS["A"]["label"]], value=CONFIGS["B"]["label"],
                               label="Agent version (B = checks its own work)")
            more = gr.Dropdown(list(EXAMPLES), label="Other example notes")
        status = gr.Markdown()
        with gr.Accordion("🔍 What the agent did", open=False):
            steps = gr.Markdown()

        @gr.render(inputs=drafts, triggers=[drafts.change])
        def show_cards(ds: list[dict]):
            if not ds:
                return
            gr.Markdown("### 3️⃣ Check each issue — change anything you want")

            def keep(i: int, field: str):
                def f(value, e):
                    e = dict(e or {})
                    e.setdefault(str(i), {})[field] = value
                    return e
                return f

            for i, d in enumerate(ds):
                t, p = split_labels(d.get("labels"))
                with gr.Group(elem_classes="issue-card"):
                    with gr.Row(equal_height=True):
                        c = gr.Checkbox(value=not d.get("_block"), label="Create", scale=0, min_width=90, interactive=True)
                        title = gr.Textbox(value=d.get("title", ""), label=f"Issue {i + 1}", scale=5, interactive=True)
                    with gr.Row():
                        who = gr.Dropdown(PEOPLE, value=d.get("assignee") if d.get("assignee") in NAMES else None,
                                          label="👤 Assigned to", scale=3, interactive=True)
                        pts = gr.Dropdown(POINTS, value=d.get("estimate") if d.get("estimate") in POINTS else 3,
                                          label="Points", scale=1, interactive=True)
                        typ = gr.Dropdown(TYPES, value=t, label="Type", scale=1, interactive=True)
                        pri = gr.Dropdown(PRIORITIES, value=p, label="Priority", scale=1, interactive=True)
                    with gr.Accordion("Description & acceptance criteria", open=False):
                        desc = gr.Textbox(value=d.get("description", ""), label="Description", lines=3, interactive=True)
                        ac = gr.Textbox(value="\n".join(d.get("acceptance_criteria") or []),
                                        label="Acceptance criteria (one per line)", lines=3, interactive=True)
                    if d.get("_block"):
                        gr.Markdown(f"⛔ {d['_block']}")
                    elif d.get("_checks"):
                        gr.Markdown("⚠️ " + "; ".join(d["_checks"]))
                    else:
                        gr.Markdown("✅ Ready")
                for comp, field in ((c, "create"), (title, "title"), (who, "assignee"), (pts, "estimate"),
                                    (typ, "type"), (pri, "priority"), (desc, "description"), (ac, "ac")):
                    comp.change(keep(i, field), [comp, edits], edits)

        create = gr.Button("✅ 4️⃣ Create ticked issues in GitLab", variant="primary", size="lg")
        result = gr.Markdown()

    with gr.Tab("GitLab board"):
        gr.Markdown("All open issues in the project (mock GitLab unless you connected a real one).")
        refresh = gr.Button("🔄 Refresh")
        board = gr.Dataframe(value=board_df, interactive=False, wrap=True)

    with gr.Tab("Memory"):
        gr.Markdown("Things the agent should always know, e.g. *huda.k is on leave in Sprint 24*. "
                    "They are added to the agent's instructions on every run.")
        mem = gr.Markdown(memory_md)
        with gr.Row():
            note = gr.Textbox(label="Add a note", scale=4)
            add = gr.Button("💾 Remember", scale=1)
        with gr.Row():
            num = gr.Number(label="Note number to delete", precision=0, scale=4)
            forget = gr.Button("🗑️ Delete", scale=1)

    with gr.Tab("Attack demo"):
        gr.Markdown("These notes were forwarded from a vendor email. They hide a password and instructions for the AI:")
        gr.Code(value=(config.ROOT / "attacks" / "injected_notes.md").read_text(encoding="utf-8"), language="markdown")
        atk = gr.Button("▶️ Run the attack (without vs with protection)", variant="primary")
        with gr.Row():
            off_md, on_md = gr.Markdown(), gr.Markdown()

    with gr.Tab("Evals"):
        gr.Markdown("Test results for version A vs B. Run in the terminal: `uv run plan.py eval --config A B`")
        ev_btn = gr.Button("🔄 Load latest results")
        ev_md = gr.Markdown()
        ev_df = gr.Dataframe(interactive=False, wrap=True)
        with gr.Accordion("Audit log (last 40 events)", open=False):
            au_btn = gr.Button("🔄 Refresh")
            au = gr.Code(value=audit_tail, language="json")

    ex.click(lambda: SIMPLE, None, notes)
    more.change(lambda k: EXAMPLES.get(k, ""), more, notes)
    go.click(on_plan, [notes, sprint, cfg], [steps, status, drafts, meta]).then(lambda: {}, None, edits)
    go.click(lambda: "", None, result)
    create.click(on_create, [drafts, edits, meta], [result, board])
    refresh.click(board_df, None, board)
    add.click(on_remember, note, [note, mem])
    forget.click(on_forget, num, mem)
    atk.click(on_attack, None, [off_md, on_md])
    ev_btn.click(evals_view, None, [ev_md, ev_df])
    au_btn.click(audit_tail, None, au)

if __name__ == "__main__":
    demo.launch(theme=gr.themes.Soft(), css=CSS)
