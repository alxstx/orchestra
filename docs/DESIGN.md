# Orchestra — Design

> Status: **design phase** (v0.6). This document is the spec. The orchestrator
> (`orchestra.py`) is a non-functional skeleton until the loop engine is built
> (milestone M1). v0.2 folds in a multi-agent adversarial review of v0.1 — see
> the changelog at the end.
>
> Throughout, a 🔧 marks a rule whose *contract* is fixed here now but whose
> *implementation* is a named build milestone.

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

1. **Fresh sessions are the whole value.** Codex catches what Claude misses
   *precisely because* it does not share Claude's context — a reviewer that
   inherits the author's context inherits the author's blind spots. So every
   author step and every review step crosses a fresh-session boundary: a new
   `claude --session-id <new-uuid>` or a new `codex exec`. The **author/reviewer**
   boundary and the **cross-stage** boundary are *always* fresh — never
   `--resume` across them; this independence is non-negotiable. (Intra-stage
   author revisions and Stage A's interactive conversation are the two calibrated
   exceptions; see §2 and §13.)
2. **Blackboard, not message-passing.** State lives in files under `runs/<run>/`.
   Any step can be re-run by pointing a fresh agent at the same files. This makes
   the whole pipeline auditable (it's just Markdown + JSON in git) and resumable.
   Fresh sessions alone don't guarantee this — both CLIs can still load ambient
   user/project config, hooks, rules, and MCP servers. The **isolation profile**
   (§6.1) strips that ambient context so the blackboard is *mechanically* the only
   channel, not just by convention.
3. **Machine-readable verdicts make the loop self-terminating.** Codex emits a
   JSON verdict against a fixed schema (`schemas/verdict.schema.json`) *in
   addition* to its prose review. Without it, "automatic back-and-forth" never
   knows when to stop; with it, the loop advances or halts on its own instead of
   you eyeballing every round. The schema *structurally* enforces the one
   invariant the whole loop rests on — `APPROVE` ⟺ no blocking issues — so a
   self-contradictory verdict can't slip an artifact through (§7).
4. **Gates calibrated per stage.** Human involvement is high where judgment
   matters most (the high-level plan) and tapers to zero where the work is
   mechanical (implementation). See §5.
5. **STATE.json is the commit point.** It is the single source of truth for
   "where are we", written last and atomically (§8, §10). The orchestrator is a
   pure function of the blackboard: kill it anytime, `orchestra resume`, and it
   reconstructs intent from `STATE.json` + the files on disk.

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
| A. High-level plan | `10-highlevel-plan.md` | interactive Claude (Q&A) | headless `codex exec` | human-driven rounds | **heavy** — you approve every round & advance |
| B. Implementation plan | `20-impl-plan.md` | headless Claude | headless `codex exec` | auto until APPROVE / max-rounds | **some** — you approve the converged plan before C |
| C. Implementation | `30-impl/` (a diff) | headless Claude (edit mode) | headless `codex exec review` | auto until APPROVE / max-rounds | **none** until the end — you review the final diff/PR |

**Freshness vs principle 1.** Cross-stage and author/reviewer boundaries are
always fresh, so each of {high-level plan, impl plan, implementation} is its own
fresh Claude lineage and every Codex review is a fresh session — satisfying the
"fresh session for each" requirement. The two deliberate exceptions:

- **Stage A is an interactive conversation**, so its author session is *continuous
  within the stage* by nature (you and Claude are talking). Codex reviews of it
  are still fresh each round.
- **Intra-stage author revisions (B/C)** default to fresh sessions (independence)
  but may resume for cost — a config knob (`behavior.fresh_author_on_revise`),
  see §13.

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
3. You read Codex's review and decide: **iterate** or **approve**. When iterating
   you may *also add your own review* — a human-authored note saved to
   `reviews/A-NN-human.md` — which is fed into the next Claude author round
   alongside Codex's. (This is the user's "human still adding reviews" channel.)
4. Repeat until you approve. `max_rounds.highlevel` is a safety cap covering both
   authoring iterations and Codex review rounds.

Codex reviews are headless and cheap; *you* are the convergence function here.

### Stage B — implementation plan (some HITL)

Now the artifact is concrete enough to auto-loop. Both sides run headless:

1. Fresh headless Claude reads `00-brief.md` + approved `10-highlevel-plan.md` →
   writes `20-impl-plan.md` (round 0 draft).
2. Fresh headless Codex reviews it → verdict JSON.
3. `APPROVE` → exit loop. `REVISE` → feed `blocking_issues` (and the brief +
   high-level plan, so the author can't drift while closing blockers) into a
   *fresh* Claude session that revises the plan → back to step 2. Cap at
   `max_rounds.impl_plan`.
4. On exit, **status = awaiting_human, waiting_for = approval**: you review the
   converged plan (and `LOG.md`) and either approve → Stage C, or inject feedback
   (`orchestra iterate --note`) for another round.

"Some HITL" = the loop runs itself, but a human signs off on the plan before any
code is written, and may interject between rounds.

### Stage C — implementation (autonomous)

1. Fresh headless Claude, in **edit mode** inside an isolated git worktree
   (`30-impl/`), implements against the approved `20-impl-plan.md`.
2. 🔧 The orchestrator runs the plan's **test command** in the worktree itself and
   captures the exit code + output. That result is fed into the review prompt as a
   *trusted, orchestrator-produced* field — so "tests pass" is a real, executed
   gate, not the author's self-claim (see §6, §10).
3. `codex exec review --base <branch>` reviews the actual diff → verdict JSON.
4. `APPROVE` (with green tests) → done. `REVISE` → a fresh Claude session reads the
   diff + review + test output and fixes it → re-test → re-review. Cap at
   `max_rounds.implementation`.
5. On `APPROVE` or cap, present the final diff / open a PR for your review. This is
   the only human touchpoint in Stage C.

## 3. The blackboard layout

A "run" is one project going through the pipeline. Everything about it is one
folder:

```
runs/<YYYY-MM-DD>-<slug>/
  STATE.json            # the state machine — single source of truth + commit point
  LOG.md                # human-readable transcript (human-facing only; not fed to agents)
  00-brief.md           # your seed: what the project is
  10-highlevel-plan.md  # Stage A output (current); per-round snapshots 10-highlevel-plan.rNN.md
  20-impl-plan.md       # Stage B output (current); per-round snapshots 20-impl-plan.rNN.md
  30-impl/              # Stage C output (a git worktree / working dir)
  questions.md          # Claude → human (clarifying questions), when paused
  answers.md            # human → Claude (your answers)
  reviews/
    A-01-review.md   A-01-verdict.json   A-01-human.md   # human review note (optional)
    B-01-review.md   B-01-verdict.json
    C-01-review.md   C-01-verdict.json   C-01-tests.txt  # captured test run for the round
    ...
  monitor/                # written ONLY by the supervisory overseer (§10.1)
    assessment.json       # latest health assessment (schemas/monitor.schema.json)
    report-01.md ...      # accumulating human-readable health reports
    HALT                  # presence = overseer requests a halt (enforcing mode)
```

Naming: `<stage-letter>-<round:02d>-{review.md,verdict.json,...}`. Stages are
`A`/`B`/`C`; rounds are 1-based per stage (see "Round semantics" in §4).

### The agent-to-agent "memory"

The shared md memory you have in mind is this folder. But not all of it is fed to
the agents — feeding everything would bloat context and re-introduce the coupling
fresh sessions exist to avoid. Each round, the **memory handed to an agent** is a
specific slice:

- **Author (revise):** brief + approved upstream plan + the *current* artifact +
  the *latest* verdict's blocking issues (+ any human review note).
- **Reviewer:** brief + approved upstream plan + the artifact under review + a
  short digest of still-open prior issues (`{{prior_issues}}`).
- **`LOG.md` is human-facing only** — it is never fed back into an agent prompt.

The handoff is sequential, not concurrent: the orchestrator detects "artifact
written and persisted" (author invocation returned + the file committed per §10),
then triggers the reviewer. "Signal Codex to review" = that sequencing; there is
no separate IPC channel.

## 4. The convergence loop (core algorithm)

The same primitive drives Stages B and C (Stage A is the human-driven variant):

```
# Resume DISPATCHES ON (stage, status) — never on round alone. A crash at `deciding`
# with a REVISE verdict must go to author_revise, NOT run another review. (§8)
def resume(run):
    s = load_state(run)                               # STATE.json is the source of truth
    match s.status:
        case "authoring":      return author_step(run)            # idempotent: redo draft/revise
        case "authored":       return review_step(run)            # artifact committed → review it
        case "reviewing":      return review_step(run)            # idempotent: redo the review
        case "deciding":       return decide(run, s.last_verdict) # re-branch from persisted verdict
        case "awaiting_human": return await_human(run, s.waiting_for)   # approval vs answers (§5)
        case "converged":      return advance_stage(run)          # gate logic (§8)
        case "stuck"|"error"|"done": return s.status              # terminal until human / done
        case _:                return author_step(run)            # fresh stage → round-0 draft

def author_step(run):                                 # round 0 = draft; round k = revise
    set_status("authoring"); check_budget_or_escalate(run)        # before every external call (§9)
    artifact = author_generate_or_revise(run)         # fresh Claude (or resume per cfg)
    commit_artifact(run, artifact)                    # snapshot + hash → STATE.json (atomic, §10)
    set_status("authored", current_artifact=...)
    return review_step(run)

def review_step(run):
    set_status("reviewing"); check_budget_or_escalate(run)
    verdict = review(run)                             # fresh Codex, schema-validated
    set_status("deciding", last_verdict=verdict)      # commit point BEFORE branching
    return decide(run, verdict)

def decide(run, verdict):
    if not consistent(verdict):  return STUCK("error")            # APPROVE w/ blockers etc. — §7
    if verdict.decision == "APPROVE":
        if low_confidence(verdict): return AWAITING_HUMAN("approval")   # §7 confidence rule
        return CONVERGED(nits=verdict.non_blocking_suggestions)  # nits carried fwd, never looped
    if verdict.decision == "REJECT": return STUCK("rejected")     # fundamental flaw → human
    annotate_oscillation(run)                                     # always surface the signal (§10)
    if cfg.stop_on_oscillation and oscillating(run): return STUCK("oscillation")  # opt-in; default OFF
    if round(run) >= cfg.max_rounds[stage]:                       # ceiling check BEFORE revising
        return STUCK("max_rounds")                                # still REVISE at ceiling → stop now
    return author_step(run)                                       # next round: fresh revise
```

- **CONVERGED** → APPROVE+gate logic (§8) decides the next status: `awaiting_human`
  if the stage's gate ∈ {heavy, some}, else advance / `done`.
- **STUCK** → terminal-until-human, tagged with `stuck_reason` ∈ {rejected,
  max_rounds, budget_exceeded, oscillation, error}, surfaced by `orchestra status`.

**Round semantics.** `round` is **per-stage** and resets to 0 when a stage begins.
Round 0 is the initial author draft (no review yet); round *k* (k ≥ 1) is the
*k*-th review plus the revise that produced the artifact it reviewed. With
`max_rounds[stage] = N`, the loop performs **at most N reviews** and therefore at
most **N−1 revisions** after the initial draft. On resume, the starting round is
derived as the max round recorded in `history` for the current stage — never a
possibly-stale counter.

### 4.1 Convergence ceiling & settle rules

The loop is bounded by a **hard ceiling** — `max_rounds[stage]`, default **15**
for the automatic stages — plus a fixed decision applied at every round and
decisively at the ceiling. Read each round's verdict as one of four states:
`APPROVE` (clean), `APPROVE` **+ nits** (no blockers but ≥1
`non_blocking_suggestion`), `REVISE` (≥1 blocker), or `REJECT`.

1. **APPROVE — clean or with nits — settles immediately.** At *any* round an
   APPROVE ends the loop. **Nits never block and never add a round**: they are
   recorded and *carried forward* (surfaced to the human at the gate, and into the
   next stage's author context / the final PR), and the orchestrator logs
   `converged (clean)` vs `converged (with N nits)`. (The §7 confidence gate may
   still downgrade a low-confidence *autonomous* APPROVE to `awaiting_human`.)
   → *honors "after the cap, if it's approved with nits, just proceed."*
2. **Ceiling reached without APPROVE → stop immediately, flag.** If a stage runs
   its full `max_rounds` reviews and the last verdict is still `REVISE`, the loop
   does **not** run another round and does **not** auto-advance with open
   blockers. It halts at `stuck(max_rounds)` and flags loudly (status + `LOG.md` +
   the M4 notification hook). "Stop immediately" = no grace round.
   → *honors "after 15 max back-and-forths, if not approved, flag and stop."*
3. **Still revising at the ceiling = probably a loop.** `stuck(max_rounds)` after a
   full ceiling of `REVISE` verdicts *is* the "it's just running a loop" signal.
   The flag carries the oscillation digest — which blocking issues recurred and how
   often (orchestrator-computed content keys, §10) — so you can see *why* it never
   settled. → *honors "if it's still revised, stop — maybe it's just looping."*

**Early stop vs full allotment.** By default the loop spends a stage's full ceiling
trying to settle (`behavior.stop_on_oscillation = false`): oscillation is surfaced
as a per-round warning but does not cut the attempt short — you asked it to get up
to 15 tries before concluding it's stuck. Flip `stop_on_oscillation = true` to bail
the moment a loop is detected (ending at `stuck(oscillation)` rather than
`stuck(max_rounds)`) — cheaper, but fewer chances to recover. Either way the
**budget** (§9) is the hard cost backstop beneath the round ceiling.

**"Proceed to next stage" ≠ skip the human.** These rules govern the *automatic
loop* only. A settled (APPROVE) Stage B still hands off to its **approval gate**
(you sign off the plan) before Stage C; only the `none`-gate Stage C proceeds
straight to done (§5, §8). `REJECT`, where enabled, can end a loop before the
ceiling (`stuck(rejected)`); whether the automatic stages may emit REJECT at all is
an open item (§13).

## 5. Human-in-the-loop mechanism

A headless pipeline still has to stop and wait for you. Three mechanisms, all
discriminated so resume is never ambiguous:

1. **Approval gates.** At a gate the orchestrator sets `status = awaiting_human,
   waiting_for = approval` and **exits** (it does not hold a blocking process —
   see "idle model" below). You resume with `orchestra approve <run>` /
   `orchestra iterate <run> --note "..."`.
2. **Question round-trip** (Stage A, or any headless author that's unsure). The
   author writes `questions.md`; orchestrator sets `status = awaiting_human,
   waiting_for = answers`; you fill in `answers.md`; the next author invocation
   gets both appended. On resume, `waiting_for` tells the orchestrator whether to
   look for `answers.md` or wait for an approve command.
3. **Interactive escape hatch.** Stage A is interactive by default; any stage can
   be dropped to interactive Claude via `--interactive` for a hands-on round.

The three gate tiers (`heavy` / `some` / `none`) map onto the stages and are
configurable per-run in `STATE.json.gate`.

**Idle model.** For human gates the orchestrator **exits and persists** rather
than holding a process for minutes/hours — idle cost is ~zero and an external
trigger (`orchestra approve/resume`, or a future watcher) resumes it. For the
*automated* B/C loop, idle = the orchestrator blocking on the in-flight
`claude`/`codex` subprocess (also ~zero compute, bounded by the per-call timeout
in §6).

## 6. CLI invocation reference

The exact commands the orchestrator shells out to. **This is the contract; verify
against the installed CLIs before building (flags marked ⚠️ need empirical
confirmation).** Every invocation runs under a per-call subprocess **timeout**
(`budget` / default 600s); a child that exceeds it is killed and the round is
retried or escalated (§9, §10).

### 6.1 Isolation profile (blackboard-only, mechanically)

Fresh `--session-id` / `codex exec` only stops *conversation* carryover. Both CLIs
otherwise load ambient context — global/project `CLAUDE.md`, settings, hooks,
plugins, MCP servers (Claude); `~/.codex/config.toml`, rules (Codex) — which would
leak state the blackboard is supposed to own, and make runs non-reproducible. Every
agent invocation in §6 therefore carries an **isolation profile** (verified flags,
`claude 2.1.177` / `codex-cli 0.139.0`):

- **Claude:** `--bare` (skip hooks/LSP/plugins) or `--safe-mode` (disable all
  customizations); `--setting-sources ""` (load no user/project/local settings);
  `--strict-mcp-config` (ignore ambient MCP); an explicit `--system-prompt` /
  `--append-system-prompt-file`; `--no-session-persistence`; run from a minimal
  `cwd` (the run dir or worktree) and pass only the needed files in the prompt.
- **Codex:** `--ignore-user-config`, `--ignore-rules`, `--ephemeral`; run with
  `-C <run-dir>`; reviewer adds `--sandbox read-only` (on `codex exec`).

Caveat: `claude --add-dir` *grants* a writable dir — it is **not** a read sandbox.
True read confinement comes from the minimal `cwd` + stripped ambient context (+
Codex `--sandbox read-only`). With the profile applied, "the blackboard is the only
channel" is mechanical, not aspirational. Config knobs live in `[isolation]`.

### Claude — author, planning stages (read-only; orchestrator owns the file)

```bash
claude -p \
  --session-id "$(uuidgen)" \           # fresh session every call
  --model opus \                        # alias, tracks latest Opus (pin only for reproducibility)
  --output-format json \               # capture .result as the artifact text
  --allowed-tools "Read Grep Glob" \    # read-only (verified): produces text, can't edit files
  --append-system-prompt-file prompts/claude/system.md \
  < rendered_prompt.md
# orchestrator writes the returned .result to 20-impl-plan.md
```

For planning stages Claude stays read-only and its **stdout (`.result`) is the
artifact** — the orchestrator persists it. ✅ **Verified empirically** (2026-06-13,
`claude 2.1.177`): the read-only invocation above returns clean plan Markdown in
`.result` (`is_error:false`, `subtype:success`, 1 turn, no files written). **Do
not** use `--permission-mode plan` here: tested headless, plan mode instead runs
many turns, tries `AskUserQuestion`/`ExitPlanMode`, and leaves `.result` **empty** —
the plan ends up trapped inside the `ExitPlanMode` tool-input plus a stray side-file
under `~/.claude/plans/`. Token/cost accounting for the budget (§9) reads from the
same JSON: `.usage.output_tokens` (+ cache fields) and `.total_cost_usd`.

### Claude — author, implementation stage (edit mode, isolated)

```bash
claude -p \
  --session-id "$(uuidgen)" \
  --model opus \
  --permission-mode acceptEdits \      # may edit files...
  --add-dir runs/<run>/30-impl \       # ...only inside the worktree
  --output-format json \
  < rendered_prompt.md
```

Run inside a dedicated git worktree so iterative edits are isolated and the diff
is clean for review.

### Codex — reviewer, plan stages (prose + machine verdict)

```bash
codex exec - \                         # `-` = read the prompt from stdin
  --skip-git-repo-check \
  --sandbox read-only \                # reviewer never mutates the workspace
  --ignore-user-config --ignore-rules --ephemeral \   # isolation profile (§6.1)
  --output-schema schemas/verdict.schema.json \        # forces JSON verdict shape
  --output-last-message runs/<run>/reviews/B-01-verdict.json \
  [--model <id>] \                     # omit to use the operator's configured Codex default
  < rendered_review_prompt.md
# the human-readable review is verdict.review_markdown → rendered to B-01-review.md
```

`--output-schema` makes Codex's *final message* conform to the verdict schema, and
`--output-last-message` writes exactly that message to a file. There is **no
separate prose channel**: under `--output-schema` (and `--json`) stdout is JSON, not
prose — so the human-readable review travels *inside* the verdict's
`review_markdown` field, and the orchestrator renders `B-01-review.md` from it.

### Codex — implementation review (native diff review)

```bash
codex exec review - \                              # `review` SUBCOMMAND of exec; `-` = prompt on stdin
  --uncommitted --base <main-branch> \             # --uncommitted captures Claude's WORKTREE edits
  --skip-git-repo-check --ignore-user-config --ignore-rules \   # isolation (§6.1)
  --output-schema schemas/verdict.schema.json \
  --output-last-message runs/<run>/reviews/C-01-verdict.json \
  [--model <id>] \
  < rendered_review_prompt.md
# review.md rendered from verdict.review_markdown (no prose on stdout).
# ALTERNATIVE (deterministic, snapshot-friendly): the orchestrator COMMITS the round
# in the worktree, then reviews that commit with `--commit <sha>` (or --base).
```

> ⚠️ **Correctness note (fixed in v0.2):** the machine-verdict flags
> (`--output-schema`, `--output-last-message`, `--model`) live on **`codex exec`**,
> including its `review` subcommand — **not** on the bare `codex review` command,
> which accepts only `-c/--config/--enable/--disable/--uncommitted/--base/--commit/--title`.
> Likewise `--sandbox` is a `codex exec` flag; the `review` path relies on review
> mode's intrinsic read-only behavior (or `-c sandbox_mode=...`).
>
> **Verified (v0.6, codex-cli 0.139.0):** `codex exec review` accepts
> `--output-schema`, `-o/--output-last-message`, `--uncommitted`, `--base`,
> `--commit`, `--json`, `-m/--model`, `--ephemeral`, `--ignore-user-config`,
> `--ignore-rules`, `--skip-git-repo-check`. Its `[PROMPT]` is read from **stdin
> only when `-` is passed** — so the invocation uses `codex exec review - < prompt`.
> Use `--uncommitted` (or commit-then-`--commit`) because Claude leaves an
> *uncommitted* worktree diff that `--base` alone can miss (untracked/unstaged).

### Why these flags

- `--session-id "$(uuidgen)"` — deterministic *fresh* sessions; independence.
- read-only author (planning) vs `acceptEdits` + `--add-dir` (impl) — least
  privilege per stage.
- `--sandbox read-only` on the `codex exec` reviewer — a reviewer must never alter
  what it reviews.
- `--output-schema` / `--output-last-message` — the self-termination contract.
- `--model opus` (alias) for Claude and **omitting** `--model` for Codex — avoid
  hardcoding ids that rot; pin only for reproducibility (§13).
- `--append-system-prompt-file` (not `--append-system-prompt "$(cat …)"`) — avoids
  argv-size and shell-quoting hazards. ⚠️ confirm the exact flag name.

## 7. The verdict contract

`schemas/verdict.schema.json` (full schema in repo). Shape:

```json
{
  "decision": "APPROVE | REVISE | REJECT",
  "confidence": 0.0,                       // REQUIRED — gates a low-confidence APPROVE
  "review_markdown": "full human-readable review → rendered to <stage>-<round>-review.md",
  "summary": "one-paragraph digest",
  "blocking_issues": [
    { "id": "B1", "severity": "critical|high|medium",
      "title": "...", "detail": "...", "location": "section / file:line (REQUIRED)",
      "suggested_fix": "..." }
  ],
  "non_blocking_suggestions": [ { "id": "N1", "title": "...", "detail": "..." } ],
  "reject_reason": "REQUIRED for REJECT unless blockers itemize the flaw",
  "addressed_previous": ["B1", "B2"]
}
```

**Structural invariant.** The schema uses an `allOf`/`if-then` so that `APPROVE`
*requires* `blocking_issues` to be empty, `REVISE` *requires* at least one, and
`REJECT` *requires* justification (≥1 blocking issue **or** a `reject_reason`) — so
a verdict can't reject without explaining why, nor approve with open blockers.
`confidence` and each blocker's `location` are **required**. The human-readable
review lives in `review_markdown` (there is **no** prose-on-stdout channel — §6).
The orchestrator treats any verdict that is invalid, unparseable, or still
inconsistent as an **error to re-prompt once, then escalate** (`stuck_reason =
error`) — never as CONVERGED.

Loop interpretation:

- `APPROVE` → converged (blocking_issues empty, guaranteed by schema). **Confidence
  gate:** if `confidence` is below `cfg.min_confidence[stage]` (most relevant for
  the `none`-gate Stage C), the APPROVE downgrades to `awaiting_human` instead of
  auto-advancing. If you don't want this safeguard, set the threshold to 0; the
  field then records confidence without gating.
- **Nits never block.** `non_blocking_suggestions` ("nits") add no rounds and don't
  hold up an APPROVE. The orchestrator distinguishes `converged (clean)` from
  `converged (with N nits)` and **carries the nits forward** — to the human at the
  gate and into the next stage / final PR — but never iterates to polish them
  (§4.1).
- `REVISE` → ≥1 `blocking_issue`; feed them (and the brief/plan) to the next author
  round.
- `REJECT` → fundamental flaw iteration won't fix → escalate immediately
  (`stuck_reason = rejected`, distinct from a burned-out `max_rounds`).
- `addressed_previous` lets the orchestrator check the author resolved last round's
  issues; it is **validated against the actual prior verdict's ids** before being
  trusted (a fresh reviewer can't be assumed to reuse ids — see §10).

## 8. State machine

Every external call has a **persisted phase on both sides**, so a crash is always
resumable to the right next action (not a re-run of completed work):

```
            author in flight        artifact committed         review in flight
  (enter)──▶ authoring ──────────▶ authored ──────────────▶ reviewing ──────┐
     ▲           │  crash⇒retry        │  crash⇒review          │ crash⇒retry │
     │           ▼                     ▼                        ▼             ▼
     │        (error)              (error)                  (error)      deciding   ← last_verdict persisted
     │                                                                       │
 answers/iterate          ┌──────────── APPROVE+gate ───────────────────────┤
 /approve/resume          │                          REVISE(<max) ──────────┼──▶ authoring (round+1)
     │                    ▼                                                  │
 ┌─────────────────┐  ┌───────────┐   gate∈{heavy,some}  ┌────────────────┐ │ REJECT / max_rounds /
 │ awaiting_human  │◀─│ converged │──────────────────────│ awaiting_human │ │ budget / oscillation
 │ waiting_for:    │  └─────┬─────┘   gate==none          │ (approval)     │ ▼
 │  approval|answers│       │ advance stage (reset round & gate) / done    └─▶ stuck (stuck_reason)
 └────────┬────────┘       ▼                                                       │
          │            next stage … → done                                         │
          └───────────────────────────────────◀── iterate --note / resume ─────────┘
```

- `authoring`/`reviewing` are written **before** the respective subprocess call;
  on resume the call is idempotently retried (the author overwrites the round's
  draft; the reviewer re-reviews).
- `authored` records the artifact path + content hash, so a crash before review
  resumes at *review*, not a wasteful re-author.
- `deciding` is written **after** review and **before** branching (with
  `last_verdict`), so the branch decision is itself resumable.
- `error` is reachable from any in-flight phase; `resume` performs an idempotent
  retry of the failed invocation (bounded by §10 retry policy).
- `stuck` carries `stuck_reason`. Human exit edges: `orchestra iterate --note`
  (inject guidance, raise the cap if reason was `max_rounds`/`budget`, → back to
  `authoring` at the recorded round so it doesn't instantly re-trip the cap) or
  `resume` after the operator edits the blackboard.

`STATE.json` (schema in `schemas/state.schema.json`):

```json
{
  "run_id": "2026-06-13-todo-api",
  "created_at": "2026-06-13T10:00:00Z",
  "updated_at": "2026-06-13T10:48:00Z",
  "started_at": "2026-06-13T10:00:00Z",
  "stage": "impl_plan",
  "status": "deciding",
  "waiting_for": null,
  "stuck_reason": null,
  "round": 2,
  "gate": "some",
  "current_step": "review",
  "attempts": 0,
  "tokens_spent": 41234,
  "config": {
    "max_rounds": { "highlevel": 15, "impl_plan": 15, "implementation": 15 },
    "min_confidence": { "highlevel": 0, "impl_plan": 0, "implementation": 0.6 }
  },
  "last_verdict": { "decision": "REVISE", "...": "..." },
  "history": [ { "stage": "highlevel", "round": 1, "actor": "codex", "verdict": "APPROVE", "ts": "..." } ]
}
```

**Stage advance** atomically resets `round` to 0 and `gate` to the new stage's
configured tier in the same STATE.json write.

## 9. Budgets, timeouts & enforcement

`max_rounds` bounds *iteration count*, not *cost*. Cost is bounded separately and
mechanically:

- **Token budget.** The Claude side is exact — `.usage.output_tokens` (+ cache
  fields) and `.total_cost_usd` from `claude --output-format json` (verified, §6).
  The **Codex side is best-effort**: parse usage from `codex exec --json` JSONL
  events *if they expose it* (confirm the field names empirically before relying);
  otherwise the Codex side is bounded by wall-clock + per-call timeout +
  `max_rounds`, and the token ceiling enforces the Claude side only. Accumulated
  into `tokens_spent`, checked at the **top of every loop iteration and before
  every external call**. On breach → `stuck(budget_exceeded)`.
- **Wall-clock budget.** `started_at` + elapsed checked at the same points.
- **Per-call timeout.** Every `claude`/`codex` subprocess runs under a timeout
  (default 600s); a hung child is killed → retry/escalate.
- **Soft stage/run time-budget.** A *separate, non-fatal* threshold: when a stage
  (or the whole run) runs longer than its soft budget, the orchestrator kills
  nothing — it **wakes the monitor** (§10.1) to judge whether the over-run is
  benign ("slow but progressing") or wedged ("stop it"). The hard per-call timeout
  handles hung *processes*; the soft budget handles suspicious *slowness*.
- 🔧 The *enforcement points* are fixed here; the accounting code is M1/M3.
  `orchestra.example.toml` ships a **non-zero default budget** (not "unlimited"),
  so a runaway is capped out of the box. With the default 15-round ceiling (§4.1),
  the budget — not the round count — is the real cost guardrail: a genuinely
  looping stage normally trips the token/wall-clock cap before it reaches round 15.

## 10. Failure modes & safeguards

> The table states the *contract*. A 🔧 row's mechanism is a named milestone;
> the rule is binding now.

| Risk | Safeguard |
|------|-----------|
| **Loop runaway / cost blowup** | hard `max_rounds` per stage **and** token + wall-clock budgets with defined check points (§9); `--dry-run` prints commands without spending. 🔧 |
| **Oscillation / endless disagreement** | orchestrator-computed metric, **not** reviewer-chosen ids (a fresh reviewer emits fresh ids each round). The orchestrator derives a content key per blocking issue (normalized `location` + normalized `title`) and detects non-improvement when, over a 2-round window, the blocking-issue count does **not** strictly decrease **and** a prior issue's key recurs unchanged (the author claims a fix the reviewer keeps re-raising). The "fixed K, found 1 genuinely new" case (count flat but all keys new) does **not** trip it. `addressed_previous` is validated against the prior verdict's real ids before being trusted. **Advisory by default** (`behavior.stop_on_oscillation = false`): the signal is surfaced as a per-round warning and rolled into the final flag, but the loop still runs to its `max_rounds` ceiling so a stage gets its full allotment of attempts (§4.1); set it `true` to bail early to `stuck(oscillation)`. The hard backstops are the ceiling and the budget (§9). 🔧 |
| **Cross-agent prompt injection** | one agent's output is another's input. Artifacts (and code diffs, including comments/strings/tests) are **untrusted data**, wrapped in clearly delimited blocks; every author and reviewer prompt carries an untrusted-content clause; reviewer runs read-only; author runs least-privilege; never `--dangerously-bypass-*` by default. A reviewer's `suggested_fix` is a *proposal*, not a command the author must execute verbatim (§7, prompts). |
| **Author edits outside scope (Stage C)** | `--add-dir` + worktree isolation; review is diff-scoped via `codex exec review --base`. |
| **Reviewer mutates workspace** | reviewer always read-only (`codex exec --sandbox read-only`; review subcommand is intrinsically read-only). |
| **"Tests pass" is self-attested** | 🔧 the **orchestrator** runs the plan's test command in the worktree and feeds the exit code + output into the review prompt as a trusted field; the reviewer only claims what it can verify by reading. Green tests are an *executed* gate, not a prose claim. |
| **Self-contradictory verdict** | schema enforces APPROVE⟺no-blockers; inconsistent/invalid/unparseable verdict → re-prompt once → `stuck(error)`; never CONVERGED (§7). |
| **Torn cross-file state on crash** | strict commit order: write artifact/verdict to temp → fsync → atomic rename into place → **then** atomic-rename the updated `STATE.json` that references them. STATE.json is never ahead of the files it points to. Round artifacts are **snapshotted** (`20-impl-plan.r2.md`), never overwritten in place, so a partial revise can't destroy the last-good version; "latest" is read from STATE.json, not by globbing. 🔧 |
| **CLI/API errors, rate limits, bad verdict** | bounded retries with exponential backoff + jitter (default 3) on {timeout, rate-limit, transient subprocess failure, schema-invalid verdict}; fail-fast on {auth, not-found, config}. On terminal failure → `status = error`, blackboard preserved, STATE.json never corrupted (write-temp-then-rename). 🔧 |
| **Secrets leakage** | `.gitignore` excludes run contents by default; briefs/plans may be sensitive — opt in to committing runs. |
| **Stuck with no human around** | `stuck` is terminal-until-human and tagged with `stuck_reason`; `orchestra status` surfaces it and flags staleness via `updated_at`. 🔧 notification hook (see §12). |
| **Hung vs working run** | the loop writes a `LOG.md` line on **entry and exit** of each invocation and bumps `updated_at`/`current_step`/`attempts`, so a tail distinguishes mid-call from hung; `orchestra status` flags a stale `updated_at`. 🔧 |
| **Subtle/semantic failure the static guards miss** | an optional concurrent **monitor** (§10.1) judges run *health* — real progress vs spinning, recurring errors, semantic loops, spend-vs-progress — and, in enforcing mode, can halt and flag you *earlier* than the hard caps, or judge an over-run benign and let it continue. Augments the mechanical floor; never replaces it. 🔧 |

### 10.1 The monitor — a supervisory overseer

The deterministic guards above (per-call timeout, the §4.1 round ceiling, budget
caps, the oscillation metric) are the always-on *floor*: cheap, mechanical, dumb —
they can't tell "slow but progressing" from "wedged," and they fire only at fixed
thresholds. The **monitor** is an optional, concurrent *smart layer* on top: an
independent agent that watches a run and judges whether the **system itself** is
working — distinct from Codex, which judges the *artifact*. It is an LLM-as-judge
over the *process*. Most of the time it should conclude "healthy, continue";
occasionally it should step in.

**Independence & single-writer safety.** The monitor is itself a **fresh session**
each time it wakes — it never shares the author's or reviewer's context; it reads
their output as data. It reads the whole blackboard (`STATE.json`, `LOG.md`,
`reviews/`, prior monitor reports) but **writes only under `monitor/`**. The
orchestrator stays the sole writer of `STATE.json` (preserving the §10
single-writer / commit-order discipline) and simply **reads `monitor/HALT` +
`monitor/assessment.json` at its safe checkpoints** (top of the loop and before
each external call — the same points as the budget checks).

**When it wakes.** Periodically (`monitor.interval_seconds`) and/or on events: a
call that blew the hard timeout, a stage past its **soft time-budget** (§9), N
consecutive errors, budget crossing a threshold, accumulating oscillation
warnings, or a stale `updated_at` (a hung run). This is the real answer to "when
it's running longer than expected" — a soft over-run wakes a *judge*; it doesn't
blindly kill.

**What it judges (rubric).** Is the run making *genuine progress* (blocking issues
trending down, stage advancing) or spinning? Recurring errors/retries? Hung (stale
heartbeat)? A *semantic* loop the content metric can't catch (the reviewer
re-raising the same concern in new words; the author misreading the brief)? Spend
disproportionate to progress? Is a long run benign or wedged?

**Output → an auditable health trail.** Each wake it updates
`monitor/assessment.json` (schema: `schemas/monitor.schema.json`) and, at least on
any `warning`/`intervene`, writes a human-readable `monitor/report-NN.md`. These
accumulate, so you can answer "is the system working correctly?" at a glance —
exactly the report you asked for.

**Authority tiers (`monitor.mode`)** — calibrated so it rarely acts:
- `off` — no monitor.
- `advisory` (default) — observes, writes reports, raises warnings to you, but
  **never halts**; the run continues. Good for building trust in its judgment.
- `enforcing` (the "harder" setting) — may **intercept**. When it returns
  `intervene` with confidence ≥ `monitor.intervene_min_confidence`, it writes
  `monitor/HALT` (+ assessment + rationale); at its next checkpoint the
  orchestrator records `stuck(reason="monitor")`, persists the rationale, and
  **flags you**. It does *not* act on `healthy`/`warning` — "most times it should
  not; in some cases it should."

**It augments, never replaces, the floor.** The mechanical guards still run; the
monitor can act *earlier* than a hard cap when something is clearly wrong, or
*hold back* and let a benign over-run finish — judgment the static caps can't make.
A halt is always surfaced (the M4 notification hook); in `advisory` mode it can
flag you without halting.

**It's just a fresh Claude overseer.** Mechanically the monitor is the same as any
other step: a fresh, read-only `claude -p` session (like the planning author)
pointed at the run's blackboard, returning an assessment the orchestrator persists
to `monitor/assessment.json`. The only real choice is *who launches it and when*:
by default the **local orchestrator** spawns it on the interval / on triggers — no
different from how it spawns the author and reviewer. Running it as a **scheduled
cloud routine** only matters if you want oversight to keep going while your machine
is off (a long unattended run on a server): same prompt, same role, just a remote
wake-up. Keep its cadence modest and feed it summaries (`LOG.md`, `STATE.json`,
latest verdicts), so oversight stays a small fraction of the run's cost.

## 11. Configuration

Per-run config lives in `STATE.json.config`; defaults in `orchestra.toml`
(see `orchestra.example.toml`):

- `models`: Claude alias / optional Codex id per role (omit Codex to use its
  configured default).
- `max_rounds`, `min_confidence`: per stage.
- `budget`: wall-clock seconds, token ceiling, per-call timeout (non-zero
  defaults).
- `sandbox` / permission mode: per stage (least privilege).
- `gate`: `heavy` / `some` / `none` per stage (defaults follow §2).
- `behavior.fresh_author_on_revise`, `behavior.stop_on_oscillation`,
  `behavior.dry_run`.
- `monitor`: `enabled`, `mode` (`off`/`advisory`/`enforcing`), `interval_seconds`,
  `stage_soft_timeout`, `intervene_min_confidence`, `model` (§10.1).

## 12. Roadmap

- **M1 — Loop engine + minimum safety kit.** Implement the §4 loop for one
  headless stage (Stage B) end-to-end: author → schema-validated Codex verdict →
  revise → converge, with status-dispatch resume (§8) and the isolation profile
  (§6.1). Ships *with* the non-negotiable safety basics: default budget, per-call
  timeout, oscillation guard, atomic STATE.json commit, `--dry-run`.
  **M1 input contract:** Stage B presupposes an approved `10-highlevel-plan.md` and
  an initialized run, so M1 provides `orchestra init <slug> --stage impl_plan
  --brief <file> --highlevel-plan <file>` — it seeds the run dir, writes those
  artifacts, and generates `STATE.json` at `stage=impl_plan, status=authoring,
  round=0`. That makes M1 runnable before Stages A and C exist.
- **M2 — Full pipeline.** Stage A (interactive + question round-trip + human
  review note) and Stage C (worktree edit mode + orchestrator-run tests +
  `codex exec review`). Approval gates, `waiting_for` discrimination.
- **M3 — Resilience.** Retry/backoff, full budget accounting, per-round artifact
  snapshots, error/resume edges, `stuck_reason` surfacing.
- **M4 — Observability.** Heartbeat/`updated_at` staleness, richer `LOG.md`,
  `orchestra status` dashboard, notification on `awaiting_human` / `stuck`.
- **M5 — Supervisory monitor (§10.1).** Advisory first (concurrent health reports
  + warnings, no halting), then enforcing (intercept on high-confidence `intervene`
  → halt + flag). Soft time-budget trigger; `stuck_reason = monitor`.
- **M6 — Quality (optional).** N-of-M reviewer voting; swappable reviewer/author
  models; multi-perspective reviewers.

## 13. Open questions

- **Planning-author capture — RESOLVED (2026-06-13).** Verified: read-only
  `claude -p --output-format json` returns clean plan Markdown in `.result`
  (1 turn, no files written); `--permission-mode plan` is correctly *avoided*
  (headless, it traps the plan in an `ExitPlanMode` tool-input and leaves
  `.result` empty). See §6.
- **Fresh vs resumed author on revision** — fresh = independence but re-reads each
  round (slower/costlier); resume = cheaper but carries context. Default fresh
  (`behavior.fresh_author_on_revise`). This applies to Stage B/C revisions **and**
  to whether Stage A re-seeds a fresh interactive session per round vs continues
  the conversation (current default: continuous within Stage A).
- **Where runs live long-term** — committed here, a sibling data repo, or
  gitignored local-only? Default gitignored; revisit.
- **Codex model** — standardize via the operator's Codex default (don't hardcode);
  document the recommended id once confirmed.
- **PR automation in Stage C** — open a real PR via `gh`, or just present the diff?
- **Monitor cadence & default mode** — the overseer is just a fresh read-only
  `claude` session; the open knobs are its wake cadence (cost vs coverage), whether
  to launch it locally (default) or as a scheduled cloud routine for unattended
  runs, and keeping `advisory` the default until its judgment is trusted enough for
  `enforcing`.

## Changelog

- **v0.6** — addressed an external design review (8 findings, CLI facts
  re-verified against the installed tools). Resume now **dispatches on (stage,
  status)**, not round alone, with per-status schema invariants (§4/§8). Added an
  **isolation profile** (§6.1) so "blackboard-only" is mechanical (ambient
  config/hooks/MCP/rules stripped). Corrected the Codex contract: the
  human-readable review moves into the verdict's `review_markdown` (no prose on
  stdout); Stage C uses `codex exec review - --uncommitted` (stdin needs `-`;
  `--uncommitted` captures Claude's worktree diff). Tightened the verdict schema
  (required `confidence`/`location`, REJECT needs justification). Made Codex token
  accounting honest (§9). Defined the M1 input contract (`orchestra init --stage
  impl_plan`).
- **v0.5** — empirically verified the planning-author capture (§6): read-only
  `claude -p --output-format json` returns clean plan Markdown in `.result`;
  `--permission-mode plan` confirmed unsuitable headless (empties `.result`).
  Confirmed token/cost fields for the budget. Clarified the monitor is *just a
  fresh read-only Claude overseer* (local-spawned by default; cloud only for
  unattended runs) — "cloud vs local" is a launch detail, not a design fork.
- **v0.4** — added the supervisory **monitor** (§10.1): an optional concurrent
  overseer that judges run *health* (progress vs spinning, errors, semantic loops,
  spend-vs-progress), writes an accumulating health trail, and — only in enforcing
  mode, on a high-confidence call — can intercept, halt, and flag the user.
  Single-writer-safe (writes only under `monitor/`; the orchestrator reads
  `monitor/HALT`). Added a soft stage/run time-budget that wakes the monitor
  instead of killing (§9), `stuck_reason = monitor`, `[monitor]` config, and
  `schemas/monitor.schema.json`.
- **v0.3** — added the convergence ceiling & settle rules (§4.1): a hard per-stage
  round ceiling (default 15) with explicit terminal behavior — APPROVE (clean or
  with nits) settles immediately and carries nits forward; hitting the ceiling
  while still revising stops immediately and flags as a likely loop. Made the
  oscillation guard advisory-by-default (`behavior.stop_on_oscillation`) so a stage
  gets its full allotment of attempts, with the budget as the hard cost backstop.
  Sharpened the two load-bearing principles in §1 (fresh-sessions-as-the-point;
  verdict-as-self-termination).
- **v0.2** — hardened against a 5-lens multi-agent adversarial review of v0.1
  (state-machine correctness, prompt-injection/trust boundaries, cost/operability,
  fidelity to the intended workflow, and CLI-flag verification against ground-truth
  `--help`). Notable fixes: corrected the Stage C Codex invocation to
  `codex exec review` (the v0.1 `codex review --output-schema` is not a real flag
  combination); made the APPROVE⟺no-blockers invariant structural in the schema;
  added persisted intermediate states (`authoring`/`authored`/`reviewing`/
  `deciding`) and `error` edges for unambiguous resume; redefined the oscillation
  guard as an orchestrator-computed metric; replaced the unverified
  `--permission-mode plan` capture assumption with a read-only-tools approach
  flagged for verification; made budgets/timeouts mechanical; added an
  orchestrator-run test gate for Stage C; standardized untrusted-content clauses
  across prompts; de-hardcoded model ids.
- **v0.1** — initial design.
