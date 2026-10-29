# Eval results — 2026-10-29 03:17

Model: `gemini-3.5-flash-lite` · tasks: 15 · trials: 1

| Config | pass rate | pass^1 | avg requests (model turns) | avg tokens | avg drafts | avg cost $ |
|---|---|---|---|---|---|---|
| A: write once (no check_issue) | 80% | 80% | 5.1 | 9022 | 2.1 | 0.0000 |
| B: write -> check_issue -> fix loop | 87% | 87% | 7.7 | 18353 | 2.1 | 0.0000 |

### Error analysis (failed runs per root cause)

| Root cause (grader) | Count (A) | Count (B) | Example task |
|---|---|---|---|
| capacity_ok | 2 | 2 | t06_capacity_frontend |
| assignee_not | 1 | 0 | t11_memory_leave |

Flipped A→B: fixed 1 ['t11_memory_leave']; broke 0 []
