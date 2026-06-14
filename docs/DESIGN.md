# Orchestra — Design

> Status: **design phase** (v0). This document is the spec. The orchestrator
> (`orchestra.py`) is a non-functional skeleton until the loop engine is built
> in a later pass.

## 1. What & why

When you build something today the loop is manual: Claude Code drafts a plan,
you paste it to Codex for review, you paste Codex's feedback back to Claude, and
you babysit the copy-paste while each side thinks. Orchestra automates that
ping-pong.

Two roles, kept deliberately separate:

- **Claude** is the *author* — it plans, refines, and implements.
- **Codex** is the *independent reviewer* — it never writes the artifact, it
  only critiques it and emits a verdict.

The separation is the point. A reviewer that shares the author's context inherits
the author's blind spots. So every step runs in a **fresh session** and the only
thing carried between them is a **shared Markdown blackboard** on disk. No hidden
state, no conversation memory leaking between author and reviewer — just files.

### Design principles

1. **Fresh sessions for independence.** Every generate and every review is a new
   `claude --session-id <new-uuid>` or a new `codex exec`. No `--resume` across
   the author/reviewer boundary. Independence > efficiency.
2. **Blackboard, not message-passing.** State lives in files under `runs/<run>/`.
   Any step can be re-run by pointing a fresh agent at the same files. This makes
   the whole pipeline auditable (it's just Markdown + JSON in git) and resumable.
3. **Machine-readable verdicts.** Codex emits a JSON verdict against a fixed
   schema (`schemas/verdict.schema.json`) *in addition* to its prose review. The
   verdict is what lets the loop terminate itself instead of needing you to
   eyeball every round.
4. **Gates calibrated per stage.** Human involvement is high where judgment
   matters most (the high-level plan) and tapers to zero where the work is
   mechanical (implementation). See §5.
5. **Everything is resumable.** `STATE.json` is the single source of truth for
   "where are we"; the orchestrator is a pure function of the blackboard.

## 2. The pipeline

```
 brief ─▶ Stage A: high-level plan ─▶ Stage B: implementation plan ─▶ Stage C: implementation
          (HEAVY human-in-loop)        (SOME human-in-loop)            (AUTONOMOUS)
          interactive Claude            auto Claude⇄Codex loop          auto Claude⇄Codex loop
          + headless Codex review       + human approval gate           + final human review
```

Each stage is the same primitive — **author → review → decide** — but with a
different automation level and a different exit gate.

| Stage | Artifact | Author | Reviewer | Loop | Human gate |
|-------|----------|--------|----------|------|------------|
| A. High-level plan | `10-highlevel-plan.md` | interactive Claude (Q&A) | headless Codex | human-driven rounds | **heavy** — you approve every round & advance |
| B. Implementation plan | `20-impl-plan.md` | headless Claude | headless Codex | auto until APPROVE / max-rounds | **some** — you approve the converged plan before C |
| C. Implementation | `30-impl/` (a diff) | headless Claude (edit mode) | headless `codex review` | auto until APPROVE / max-rounds | **none** until the end — you review the final diff/PR |

### Stage A — high-level plan (heavy HITL)

This stage is inherently a conversation: you describe the project, Claude asks
clarifying questions, you answer, a shape emerges. Headless mode can't ask
questions interactively, so Stage A runs **interactive Claude**, seeded with the
brief and the blackboard. The orchestrator's job here is *handoff plumbing*, not
auto-looping:

1. You run an interactive Claude session (seeded from `00-brief.md`). You discuss;
   Claude asks questions; you converge on a plan and save it to
   `10-highlevel-plan.md`.
2. Orchestrator fires a **headless Codex** review of that plan → `reviews/A-NN-review.md`
   + `reviews/A-NN-verdict.json`.
3. You read Codex's review and decide: **iterate** (take its points back into the
   interactive Claude session) or **approve** (advance to Stage B).
4. Repeat until you approve.

Codex reviews are headless and cheap; *you* are the convergence function here.

### Stage B — implementation plan (some HITL)

Now the artifact is concrete enough to auto-loop. Both sides run headless:

1. Fresh headless Claude reads `00-brief.md` + approved `10-highlevel-plan.md` →
   writes `20-impl-plan.md`.
2. Fresh headless Codex reviews it → verdict JSON.
3. `APPROVE` → exit loop. `REVISE` → feed `blocking_issues` into a *fresh* Claude
   session that revises the plan → back to step 2. Cap at `max_rounds`.
4. On exit, **status = awaiting_human**: you review the converged plan (and the
   round-by-round `LOG.md`) and either approve → Stage C, or inject feedback for
   another round.

"Some HITL" = the loop runs itself, but a human signs off on the plan before any
code is written, and may interject between rounds.

### Stage C — implementation (autonomous)

1. Fresh headless Claude, running in **edit mode** inside an isolated worktree
   (`30-impl/`), implements against the approved `20-impl-plan.md`.
2. `codex review --base <branch>` reviews the actual diff → verdict JSON.
3. `APPROVE` → done. `REVISE` → fresh Claude session reads the diff + review and
   fixes it → re-review. Cap at `max_rounds`.
4. On `APPROVE` or cap, present the final diff / open a PR for your review. This
   is the only human touchpoint in Stage C.

## 3. The blackboard layout

A "run" is one project going through the pipeline. Everything about it is one
folder:

```
runs/<YYYY-MM-DD>-<slug>/
  STATE.json            # the state machine — single source of truth
  LOG.md                # human-readable transcript of the whole ping-pong
  00-brief.md           # your seed: what the project is
  10-highlevel-plan.md  # Stage A output
  20-impl-plan.md       # Stage B output
  30-impl/              # Stage C output (a git worktree / working dir)
  questions.md          # Claude → human (clarifying questions), when paused
  answers.md            # human → Claude (your answers)
  reviews/
    A-01-review.md      # Codex prose review, Stage A round 1
    A-01-verdict.json   # Codex machine verdict, Stage A round 1
    B-01-review.md  B-01-verdict.json
    C-01-review.md  C-01-verdict.json
    ...
```

Naming: `<stage-letter>-<round:02d>-{review.md,verdict.json}`. Stages are
`A`/`B`/`C`; rounds are 1-based per stage.

## 4. The convergence loop (core algorithm)

The same primitive drives Stages B and C (Stage A is the human-driven variant):

```
def run_stage(stage, blackboard, max_rounds):
    artifact = author_generate(stage, blackboard)        # fresh Claude
    for round in 1..max_rounds:
        verdict = review(stage, artifact, blackboard)    # fresh Codex, JSON schema
        persist(verdict, round)
        if verdict.decision == "APPROVE":
            return CONVERGED
        if verdict.decision == "REJECT":
            return STUCK                                 # fundamental flaw → human
        artifact = author_revise(stage, artifact,        # fresh Claude
                                 verdict.blocking_issues, blackboard)
    return STUCK                                          # hit max_rounds → human
```

- **CONVERGED** → advance (Stage B pauses for human approval; Stage C finishes).
- **STUCK** → `status = stuck`, escalate to the human with the full `LOG.md`.
- **Oscillation guard** (see §8): if two consecutive verdicts raise issues that are
  not strict subsets of the prior round's, flag possible disagreement and escalate
  early rather than burning rounds.

## 5. Human-in-the-loop mechanism

A headless pipeline still has to stop and wait for you. Three mechanisms:

1. **Approval gates.** When a stage reaches a gate, the orchestrator sets
   `status = awaiting_human` and exits (or blocks). You resume with
   `orchestra approve <run>` / `orchestra iterate <run> --note "..."`.
2. **Question round-trip** (Stage A, and any time a headless author is unsure).
   The author writes `questions.md` and signals `status = awaiting_human`; you
   fill in `answers.md`; the next author invocation gets both appended to its
   context.
3. **Interactive escape hatch.** Stage A is interactive by default; any stage can
   be dropped to interactive Claude via `--interactive` for a hands-on round.

The three gate tiers (`heavy` / `some` / `none`) map directly onto the stages and
are configurable per-run in `STATE.json.gate`.

## 6. CLI invocation reference

The exact commands the orchestrator shells out to. These are the contract; if the
CLIs change, update here first.

### Claude — author, planning stages (read-only; orchestrator owns the file)

```bash
claude -p \
  --session-id "$(uuidgen)" \           # fresh session every call
  --model claude-opus-4-8 \
  --permission-mode plan \              # no edits; just produce the plan text
  --output-format json \               # capture .result as the artifact
  --append-system-prompt "$(cat prompts/claude/system.md)" \
  < rendered_prompt.md
# orchestrator writes the returned .result to 20-impl-plan.md
```

For planning stages Claude stays read-only and its **stdout is the artifact** —
the orchestrator persists it to the blackboard. This keeps the author from
touching files it shouldn't and keeps the blackboard owned by one writer.

### Claude — author, implementation stage (edit mode, isolated)

```bash
claude -p \
  --session-id "$(uuidgen)" \
  --model claude-opus-4-8 \
  --permission-mode acceptEdits \      # may edit files...
  --add-dir runs/<run>/30-impl \       # ...only inside the worktree
  --output-format json \
  < rendered_prompt.md
```

Run inside a dedicated git worktree so parallel/iterative edits are isolated and
the diff is clean for `codex review`.

### Codex — reviewer (prose + machine verdict)

```bash
codex exec \
  --model gpt-5-codex \                # or your configured reviewer model
  --sandbox read-only \                # reviewer never mutates the workspace
  --skip-git-repo-check \
  --output-schema schemas/verdict.schema.json \   # forces JSON verdict shape
  --output-last-message runs/<run>/reviews/B-01-verdict.json \
  < rendered_review_prompt.md \
  > runs/<run>/reviews/B-01-review.md              # prose review on stdout
```

`--output-schema` makes Codex's *final message* conform to the verdict schema, and
`--output-last-message` writes exactly that message to a file — so the loop reads
a clean JSON verdict without scraping prose.

### Codex — implementation review (native diff review)

```bash
codex review --base <main-branch> \    # reviews the worktree diff vs base
  --output-schema schemas/verdict.schema.json
# (run with --uncommitted to include unstaged/untracked changes)
```

### Why these flags

- `--session-id "$(uuidgen)"` — deterministic *fresh* sessions; independence.
- `--permission-mode plan` (planning) vs `acceptEdits` + `--add-dir` (impl) —
  least privilege per stage.
- `--sandbox read-only` on the reviewer — a reviewer must never alter what it
  reviews.
- `--output-schema` / `--output-last-message` — the self-termination contract.

## 7. The verdict contract

`schemas/verdict.schema.json` (full schema in repo). Shape:

```json
{
  "decision": "APPROVE | REVISE | REJECT",
  "confidence": 0.0,
  "summary": "one-paragraph verdict",
  "blocking_issues": [
    { "id": "B1", "severity": "critical|high|medium",
      "title": "...", "detail": "...", "location": "...",
      "suggested_fix": "..." }
  ],
  "non_blocking_suggestions": [ { "id": "N1", "title": "...", "detail": "..." } ],
  "addressed_previous": ["B1", "B2"]
}
```

Loop interpretation:

- `APPROVE` → converged. `blocking_issues` must be empty.
- `REVISE` → at least one `blocking_issue`; feed them (and only them) to the next
  author round.
- `REJECT` → fundamental flaw the reviewer believes iteration won't fix → escalate
  to human immediately.
- `addressed_previous` lets the orchestrator verify the author actually resolved
  last round's issues (oscillation / regression detection).

## 8. State machine

```
            ┌──────────┐  author+review  ┌──────────┐
 init ─────▶│ running  │────────────────▶│ deciding │
            └──────────┘                 └────┬─────┘
                 ▲                            │
   answers/      │            APPROVE+gate    │  REVISE (<max)
   approve       │      ┌─────────────────────┼─────────────┐
                 │      ▼                      ▼             ▼
        ┌──────────────────┐         ┌──────────────┐  (loop back to running)
        │  awaiting_human  │         │  converged   │
        └──────────────────┘         └──────┬───────┘
                 ▲                           │ advance stage / done
       REJECT or max_rounds                  ▼
            ┌─────────┐                  next stage … → done
            │  stuck  │
            └─────────┘
```

`STATE.json` (schema in `schemas/state.schema.json`):

```json
{
  "run_id": "2026-06-13-todo-api",
  "created_at": "2026-06-13T10:00:00Z",
  "stage": "impl_plan",
  "status": "running",
  "round": 2,
  "gate": "some",
  "config": { "max_rounds": { "highlevel": 6, "impl_plan": 4, "implementation": 5 } },
  "last_verdict": { "decision": "REVISE", "...": "..." },
  "history": [
    { "stage": "highlevel", "round": 1, "actor": "codex", "verdict": "APPROVE", "ts": "..." }
  ]
}
```

The orchestrator is restart-safe: kill it any time, re-run `orchestra resume <run>`,
and it reconstructs intent from `STATE.json` + the files on disk.

## 9. Orchestrator design (`orchestra.py`)

Single Python file, stdlib-only (`subprocess`, `json`, `argparse`, `pathlib`,
`uuid`, `datetime`). No third-party deps for v1.

CLI surface:

```
orchestra init   <slug> [--brief FILE]     # create runs/<date>-<slug>/, write STATE.json
orchestra run    <run>  [--stage A|B|C] [--interactive]   # drive the pipeline
orchestra resume <run>                      # continue from STATE.json
orchestra status <run>                      # print state + last verdict
orchestra approve <run>                     # clear an approval gate, advance
orchestra iterate <run> --note "..."        # force another round with a human note
```

Internal modules (functions, not files, in v1):

- `cli` — argparse dispatch.
- `state` — load/save `STATE.json`, transitions, history append.
- `blackboard` — path helpers, render prompt templates with run context.
- `claude` — build & run the `claude -p` invocations; parse `--output-format json`.
- `codex` — build & run `codex exec` / `codex review`; read the verdict file.
- `loop` — `run_stage` (the §4 algorithm), gate handling, oscillation guard.

## 10. Failure modes & safeguards

| Risk | Safeguard |
|------|-----------|
| **Loop runaway / cost blowup** | hard `max_rounds` per stage; global wall-clock + token budget cap; `--dry-run` to print commands without spending. |
| **Oscillation / endless disagreement** | track `addressed_previous`; if issues don't shrink across 2 rounds, escalate to `stuck` early. |
| **Cross-agent prompt injection** | one agent's output becomes another's input. Treat artifacts as *untrusted data*, not instructions: wrap them in clearly delimited blocks in prompts; reviewer runs `--sandbox read-only`; author runs least-privilege; never `--dangerously-bypass-*` by default. |
| **Author edits outside scope (Stage C)** | `--add-dir` + worktree isolation; review is diff-scoped via `codex review --base`. |
| **Reviewer mutates workspace** | reviewer always `--sandbox read-only`. |
| **CLI/API errors, rate limits** | bounded retries with backoff per invocation; on terminal failure set `status = error`, preserve blackboard, never corrupt `STATE.json` (write-temp-then-rename). |
| **Non-determinism / flaky verdicts** | verdict is structured & logged; optional N-of-M reviewer vote for high-stakes gates (future). |
| **Secrets leakage** | `.gitignore` excludes run contents by default; brief/plans may contain sensitive context — opt in to committing runs. |
| **Stuck with no human around** | `status = stuck` is terminal-until-human; `orchestra status` surfaces it; future: notification hook. |

## 11. Configuration

Per-run config lives in `STATE.json.config`; defaults in `orchestra.toml`
(see `orchestra.example.toml`):

- `models`: which Claude / Codex model per role.
- `max_rounds`: per stage.
- `sandbox` / `permission_mode`: per stage.
- `budget`: wall-clock seconds and/or token ceiling per run.
- `gate`: `heavy` / `some` / `none` per stage (defaults follow §2 table).

## 12. Roadmap

- **M1 — Loop engine (next pass).** Implement `run_stage` for one headless stage
  (Stage B) end-to-end: author → Codex verdict → revise → converge. The smallest
  thing that proves the contract.
- **M2 — Full pipeline.** Wire Stage A (interactive + question round-trip) and
  Stage C (worktree edit mode + `codex review`). Approval gates.
- **M3 — Resilience.** Retries/backoff, budgets, oscillation guard, `--dry-run`.
- **M4 — Observability.** Richer `LOG.md`, `orchestra status` dashboard, optional
  notification on `awaiting_human` / `stuck`.
- **M5 — Quality (optional).** N-of-M reviewer voting; swap reviewer/author models;
  multi-reviewer perspectives.

## 13. Open questions

- **Fresh vs resumed author in Stage C revisions** — fresh = independence but
  re-reads the codebase each round (slower/costlier); resume = cheaper but carries
  context. Default fresh; expose as a config knob.
- **Where do runs live long-term** — committed to this repo, a sibling data repo,
  or gitignored local-only? Default gitignored; revisit.
- **Reviewer model** — confirm the exact Codex model id to standardize on.
- **PR automation in Stage C** — open a real PR via `gh`, or just present the diff?
