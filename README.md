# orchestra

Automate the Claude ⇄ Codex review loop.

When you build something, the loop is usually manual: **Claude Code** drafts a
plan, you paste it into **Codex** for review, you paste Codex's feedback back to
Claude, and you babysit the copy-paste while each side thinks. `orchestra` turns
that into a self-driving pipeline where Claude is the **author** and Codex is the
**independent reviewer**, communicating through a shared Markdown blackboard on
disk.

```
 brief ─▶ A: high-level plan ─▶ B: implementation plan ─▶ C: implementation
          HEAVY human-in-loop     SOME human-in-loop        AUTONOMOUS
```

- **Fresh sessions everywhere** — every author step and every review step is a
  brand-new `claude`/`codex` session, so the reviewer never inherits the author's
  blind spots. The only thing carried between steps is the blackboard.
- **Self-terminating loops** — Codex emits a machine-readable JSON verdict
  (`APPROVE` / `REVISE` / `REJECT`), so the loop knows when to stop instead of
  needing you to eyeball every round.
- **Gates where they matter** — heavy human involvement on the high-level plan,
  lighter on the implementation plan, fully autonomous on the implementation.
- **Everything is files** — the whole run is auditable Markdown + JSON under
  `runs/`, and any step can be replayed by pointing a fresh agent at the same
  files.

## Status

✅ **Working** (Stages A/B/C, M1–M5). `orchestra.py` implements the full pipeline:
the §4 convergence loop, `(stage, status)` resume dispatch, the §6.1 isolation
profile, atomic STATE.json + an exclusive run lock, nonce-fenced prompts, the
anti-regression ledger, oscillation guard, dispute/question round-trips, the
Codex→Claude reviewer fallback, the executed-test gate, and the supervisory
monitor + watchdog. Stdlib only; verified against `claude 2.1.185` /
`codex-cli 0.139.0`, with a unit suite (`test_orchestra.py`) and an end-to-end
todo-api run (Stage B converged with Codex, Stage C generated a working,
test-passing implementation presented as a diff — never merged). The optional
N-of-M voting (M6) and the [`Stage T`](docs/STAGE-T-test-phase.md) acceptance-test
phase remain future work. See [`docs/DESIGN.md`](docs/DESIGN.md) for the full design.

## Layout

```
docs/DESIGN.md          full design / spec
orchestra.py            orchestrator skeleton (stubs — not yet functional)
orchestra.example.toml  default config
prompts/claude/         author prompt templates
prompts/codex/          reviewer prompt templates
schemas/verdict.schema.json   Codex's machine-readable verdict contract
schemas/state.schema.json     STATE.json contract
runs/EXAMPLE-todo-api/  an illustrative (fake) run, for shape reference
```

## Requirements

- [`claude`](https://claude.com/claude-code) CLI (author) — headless via `claude -p`
- [`codex`](https://developers.openai.com/codex/cli) CLI (reviewer) — `codex exec` / `codex review`
- Python 3.11+ (stdlib only)

## Quickstart

```bash
# Stage B onward (M1 input contract): seed an approved high-level plan + brief
python3 orchestra.py init todo-api --stage impl_plan \
    --brief brief.md --highlevel-plan highlevel.md \
    --test-command "python3 -m unittest discover"
python3 orchestra.py run todo-api        # drive Stage B to the approval gate
python3 orchestra.py status todo-api     # stage / status / round / last verdict
python3 orchestra.py approve todo-api    # sign off the plan → autonomous Stage C
# ... Stage C implements in an isolated worktree, runs the test gate, and Codex
# reviews the diff until it converges; the final diff is PRESENTED, never merged.

python3 orchestra.py resume todo-api     # continue after any interruption
python3 orchestra.py iterate todo-api --note "..."   # inject feedback / another round
python3 orchestra.py run todo-api --dry-run --stub-verdicts   # print commands, run nothing
python3 orchestra.py watchdog            # independent dead/hung-run detection
```

Run `python3 orchestra.py <cmd> --help` for the full surface. Tests:
`python3 -m unittest test_orchestra`.
