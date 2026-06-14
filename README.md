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

🚧 **Design phase.** This repo currently contains the spec, prompt templates,
schemas, and a non-functional orchestrator skeleton. See
[`docs/DESIGN.md`](docs/DESIGN.md) for the full design. The loop engine is the
next build milestone (M1).

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

## Quickstart (planned)

```bash
orchestra init todo-api --brief brief.md   # create a run
orchestra run todo-api                      # drive the pipeline
orchestra status todo-api                   # see where it's at
```

> These commands are specified in `docs/DESIGN.md §9` but not yet implemented.
