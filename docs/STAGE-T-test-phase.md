# Stage T — the acceptance-test phase (design addition)

> Status: **design addition, v0.23.** A *new phase* to fold into `docs/DESIGN.md` once the core
> pipeline lands. Tests are authored *in the plan manner* — a reviewed **test plan (Tp)** precedes the
> reviewed **test code (Tt)**, frozen into Stage C's gate. Threat model is **non-adversarial** (benign,
> local, no internet); the concerns are **correctness** and **overfitting**, handled by giving the impl
> agent the **acceptance contract** (every requirement) while hiding most concrete **cases**. v0.19
> closes the greenfield **over-declared-call** freeze gap, makes the equivalence-class disjointness
> guard **uniform over everything the agent sees** (visible cases too), defines visible-case **budget
> units**, and — per the review's calibration signal — **trims machinery** toward the pragmatic intent
> (v1 probes = `python`/`cli`; coarse `operation.phase`). `orchestra.py` references by function name.
> **Folding in needs the §11 checklist.**

**Canonical names.** Stages: `test_plan` (letter `Tp`, gate `some`), `tests` (letter `Tt`, gate `none`).
Config: `[stage_t]` (`enabled`, `repeat_runs`, `feedback_granularity`, `reviewer_on_limit`, `limits.*`),
`[test_gate]` (`adapter` | `command` | `report_path` | `normalizer`), per-stage `max_rounds.{test_plan,tests}` /
`min_confidence.{…}` / `gate.{…}`. `feedback_granularity ∈ {per_item}` (the only v1 value; `aggregate`/
`per_test_id` are reserved/unused).

## 1. What & why

The executed-test gate (DESIGN §2/§10) fixes only the test *command*; in greenfield the tests are written by
the implementation author — it grades its own homework. Stage T authors and hardens the tests **independently**
(Tp = what, Tt = how), reviewed twice, then **freezes** them. Stage C is given the full **acceptance contract**
(never graded on an unstated requirement) plus a *policy-bounded* set of visible example cases; the rest are
hidden, so it implements the intent, not the cases.

```
brief ─▶ A: high-level plan ─▶ B: impl plan ─▶ Tp: test plan ─▶ Tt: acceptance tests ─▶ C: implementation
         (heavy HITL)           (some HITL)      (some HITL)       (none)                 (autonomous)
```

**Threat model — non-adversarial.** Benign code, local, no network. Precautions are **light** (timeout,
rlimits, scratch, network-off for determinism, ~free nonce fencing). The gate runs as a **child process**
(argv) whose single interpreter holds the **combined test+app environment** — the app is *imported within that
child* (no separate app server), not run in the orchestrator's own process — so the §4.2 controls (process-group
kill, `setrlimit`) apply to it. (First
field of the run's **review charter**, §9.1.) **Closes:** test-authoring self-attestation and overfitting.

## 2. Stage Tp — the test plan (reviewed in the plan manner)

A **planning (read-only) author** like Stage B — it keeps the DESIGN isolation profile (`--tools ""` from an
empty staging cwd, so it *cannot* read or write the blackboard; its single **stdout** is the artifact). Inputs:
`00-brief.md`, approved `10-highlevel-plan.md` **and** `20-impl-plan.md`, deviations/nits.

**Multi-artifact stdout protocol (Tp emits one stdout, not three files).** Because a plan-author has no file
tools, Tp emits a **single stdout with three fenced, named sections** — `=== 25-test-plan.md ===`,
`=== 25-interface.json ===`, `=== 25-acceptance-contract.json ===` — which the **orchestrator splits** into the
three files and schema-validates (T5). *Do not* make Tp an edit-mode stage to write files: that would grant it
read access and destroy the §3 mechanical memory-slice isolation. The three artifacts:
- `25-test-plan.md` — coverage map: each behaviour → criterion + **oracle**.
- `25-interface.json` — **interface manifest**: public surface, stable **entry IDs**, each with **declared
  instrumentation** (a wrapper/probe recording entry; §4.1). Visible.
- `25-acceptance-contract.json` — **visible normative contract**: stable **item IDs** + behaviour/oracle
  semantics + **structured wire examples** (§2.2); carries the **visibility policy** (§2.1).

**Tp-gate validation (don't defer to Tt freeze).** At the Tp `some` gate — *before* human approval — the
orchestrator schema-validates `25-interface.json` and `25-acceptance-contract.json` and runs the **intra-contract
invariants that need no Tt artifacts** (`counts_as_visible` rule, budget-sum sanity, equivalence-class ref
resolution, §2.2). A malformed contract is rejected at Tp, not discovered a whole stage later at Tt freeze.

**2.1 Visibility policy (human-approved, enforced at freeze).** Declares, human-approved at the Tp gate:
**default `hidden`**; `max_visible_global`/`max_visible_per_item` **counting collected visible cases *and*
case-like contract examples** (a **visible case = 1 unit against each credited
item's `max_visible_per_item` and 1 against `max_visible_global`** — so a brownfield multi-item visible case
charges every item it credits; an example contributes its `visibility_budget_units` **(≥ 1, §2.2) against each
of its `contract_items` and 1 global** — the same per-item charging rule as cases, so a multi-item example (in
greenfield too) can't dodge per-item caps); explicit per-item exceptions; and per §3.5: **every item has ≥1 hidden case**
(or approved `visible_only`), and **a visible anchor** for convergence (all-hidden only by explicit approval).
**Anchor strength matters:** a **visible *case*** gives a real executable red→green signal, whereas an
**example-only** anchor gives only an illustration to read plus the per-item bit — prefer a visible case where
convergence matters. Items are **behavior-sized**, and **≥2 distinct hidden cases per item is the soft default**
(promoted from a preference) so a singleton hidden group can't leak its boundary via repeated probing.

**2.2 Structured contract examples + equivalence classes.** Wire examples are public cases, so they are
structured: `{example_id, contract_items, example_kind ∈ {case_like, normative}, counts_as_visible: bool,
visibility_budget_units: int, equivalence_class_id}`. **`counts_as_visible` is not free:** `validate_test_bundle()`
**requires `counts_as_visible = true` for any `case_like` example** (concrete input/output behaviour) **and
`visibility_budget_units ≥ 1` whenever `counts_as_visible = true`** (rejecting 0/negative — otherwise a
visible-but-uncounted example adds 0 to the cap and unlimited concrete I/O leaks past the budget at zero cost).
Only a purely descriptive `normative` example may set it false. **Caveat (not machine-closable):** `example_kind`
is author-declared and the `case_like`↔`normative` line is semantic, so mislabelling concrete I/O as `normative`
bypasses the rule — this is a **Tt-review responsibility**, listed in §12 residuals, *not* a machine guarantee. **Equivalence classes are first-class contract objects**
— `{id, description, boundary/rationale}`; the **per-case/per-example `equivalence_class_id` is authoritative**
and the class's membership (`covered_*`) is **derived** from it (no two-way disagreement) — so distinctness isn't just string
equality: **machine** validation checks ID references resolve and that an item's hidden classes are **disjoint
(by id) from *every class the agent can see* — visible-example *and* visible-case classes** (so an item anchored
by a visible case can't have redundant hidden coverage in that same class), while **semantic distinctness** (two
ids that actually describe the same class) is an explicit **Tt-review** responsibility. **Every case — hidden
*and* visible — declares its `equivalence_class_id`** (examples too).

Review: several Codex rounds, `max_rounds.test_plan`, **gate `some`**.

## 3. Stage Tt — the test code (an edit-mode stage)

Tt writes files → an **edit-mode stage** needing a **stage-kind abstraction** (§10): `stage_kind ∈ {plan, edit}`;
**typed `current_artifact`** `{kind:file|tree, path, hash, commit}`; per-round base commit, `reset --hard`+`clean`
on `authoring` resume, commit-before-STATE, review diff, freeze.

**3.1 Author** → the suite (under **visible/hidden roots**, §3.4) + an interface stub (signatures from
`25-interface.json`; bodies `raise StubSentinel(<interface_entry_id>)`) + an author **test-definition manifest**
(`28-tests-manifest.json`): per-definition fields (`declared_public_calls`, `red_first_call`, declared
dependencies §3.4) **plus a per-case table keyed by the deterministic `ids=` label** (which the §3.2 collection
pass maps to `case_id`) carrying each case's `contract_items`, `visibility`, `equivalence_class_id`, and
brownfield `expected_baseline`. Per-case keying is required because a parametrized definition routinely **spans
multiple equivalence classes / items** (testing boundaries A, B, C), so a single per-definition
`equivalence_class_id` is too coarse; `validate_test_bundle()` joins manifest rows to collected cases by that
`ids=` label.

**Stub partition (get this wrong and the gate deadlocks).** The stub is **committed in `tt_commit`** (so freeze
can import/collect against it), **excluded from the frozen test closure** (§3.5 — it is *not* a frozen test
file), assigned to the **implementation side** of the ephemeral tree (Stage C completes its bodies; it is
**implementation-owned and editable**), and **regenerated pristine from `25-interface.json`** at amendment
re-freeze (§7's sub-run reopens blind to the Stage C patch, so the stub must reset to signatures-only, not carry
Stage C's edits). Freezing the stub *into* the closure (Stage C can't edit it → unsatisfiable gate) or excluding
it from `tt_commit` (freeze can't collect) each deadlock; neither is permitted.

**Atomicity on the *item*, not the call (multi-call workflows allowed).** Greenfield cases are **atomic on the
contract item** (exactly one `contract_item`) but may have **multiple `declared_public_calls`** — so real
workflows ("create then fetch", "login then access", "write then read back") are expressible and the hidden
suite can catch end-to-end bugs. `red_first_call` **must be the first public interface entry the case
invokes** (the stub aborts there, so freeze enforces this); it is used **only** to prove **non-vacuity at
freeze**, and the **full** declared-call set is verified at implementation-run time (§4.3). **Multi-*item* credit** (one case → several items) is allowed **only in
brownfield**, where every credited item's call has reachability evidence via its probe; a failure projects to
all mapped items.

**3.2 Case identity — deterministic collected cases.** Parametrized definitions expand into **cases** (the unit).
Freeze's **collection pass** → the normalized immutable **`28-cases.json`** keyed by **`case_id`**. Pytest node
IDs can drift, so **parametrization must use explicit deterministic `ids=`**, and `validate_test_bundle()`
**statically (AST) verifies every `parametrize` has an `ids=` that is a literal list of string literals**,
rejecting implicit (no `ids=`) or env-derived (dynamic-expression) identity. The **collected *set*** must also be
stable, so **parametrize value sources must live in the declared/frozen closure** (§3.4) — no import-time reads of
unfrozen data/snapshot files. **`frozen_case_ids`** is the unique key set; equality is `Counter(collected_ids) ==
{each frozen id: 1}` (§4.3).

**3.3 Red-first / vacuity** — greenfield: collection succeeds; each case's **terminal state is `sentinel_reached`,
not `passed`** — so a case that swallows `StubSentinel` (e.g. `pytest.raises(Exception)` or a bare `except:`) and
*passes* against the bare stub **fails freeze** (vacuous). The rest of the declared-call set isn't provable
against the stub and is deferred to §4.3. Brownfield: each
case **reaches its declared call(s)** via probes regardless of pass/fail baseline; a per-revision baseline matches
each case's `expected_baseline` — the **freeze-time/pre-impl** expectation (some cases legitimately *red*,
documenting current behaviour). Stage C must then drive **all** cases to `passed` (§4.3); `expected_baseline` is
the starting state, the Stage C gate is the end state.

**3.4 Visibility closure — a declared topology, recorded as `28-visibility-closure.json`.** Arbitrary pytest
deps aren't precisely discoverable, so the closure is **declared and verified**: **visible/hidden roots** at
pinned paths — visible tests under `28-tests/visible/`, hidden under `28-tests/hidden/`, shared declared deps
under `28-tests/lib/`, and the **impl-owned stub** under the implementation side (e.g. `30-impl/<pkg>/`), so
§5's frozen-test/impl path-overlap check has concrete paths to compare; each
case/file **declares** its fixture/helper/snapshot deps; a **visible case's declared closure may not reach a
hidden root** or a dependency carrying hidden parameters/snapshots/expected-values/case-generators; **no
undeclared crossing**; **no dynamic imports in test/fixture/helper code within the declared closure** (the
framework — pytest, its plugins, the canonical-report plugin — uses dynamic import and is exempt); **conservative
fallback** (undeclared/mixed/dynamic →
classified hidden, or fails freeze if declared visible). Freeze **verifies** against the static import/fixture
graph and writes the frozen **`28-visibility-closure.json`** (`schemas/visibility-closure.schema.json`: per
case/source the dependency closure, visibility class, digests, exposure decision) — the concrete object Stage C
exposure, review, and resume comparison use.

**3.5 Review → freeze.** **Dispatch slot:** Tt converging (stage `tests`, status `converged`) triggers the
**freeze `operation`** (§10.3); a `validating` failure routes **backward** to stage `tests`, status `authoring`
(recording the deterministic errors as the next author round's input) — the only non-monotonic edge in the loop;
only a clean freeze advances to `implementation`. On APPROVE, **`validate_test_bundle()`** runs all cross-artifact
invariants — unique/deterministic ids; refs resolve; collection expansion; visibility caps over cases **+ counted contract examples**;
item-atomic greenfield (§3.1); closure (§3.4); **every item has ≥1 case, ≥1 hidden case (or approved
`visible_only`), a visible anchor (or approved all-hidden), and hidden cases in classes distinct from **all
visible (example *and* case) classes** (§2.2). **Reachability is split by `subject_kind`** (the stub aborts at
the first call, so the full set can't be proven at freeze): **Tt greenfield freeze** requires `red_first_call ∈
declared_public_calls`, the report reached `red_first_call`, **and each `declared_public_call` is statically
referenced in the case's reachable source** (cheap via the §3.4 graph) — so an over-declared call the test never
makes is rejected at freeze, not discovered a full impl-cycle later as an unsatisfiable Stage C gate; **Tt brownfield / Stage C / final gate** require `reached_calls ⊇
declared_public_calls` (§4.3). "Every interface entry has conformance coverage" is checked as **declared**
coverage at greenfield freeze (a probe + ≥1 case declares it), with full **runtime** reachability enforced at
the brownfield/implementation runs. Deterministic errors before freeze. Then record the immutable closure (tests, fixtures, `conftest.py`, test-only config + lock,
canonical-report plugin + normalizer) + the manifests + `28-cases.json` + `28-visibility-closure.json` +
`frozen_case_ids` + the **test-tool env digest** (§8). Reject unsafe special files.

## 4. The executed gate

**4.1 Canonical report & reachability.** Operator-selected, orchestrator-run (never model-derived): built-in
`pytest` adapter or operator command (§4.4). The harness installs **per-interface-entry instrumentation** (the
declared probe) recording reached entries. Each `25-interface.json` entry **declares its probe shape**:
`{entry_id, kind, target, install_phase, reach_marker, failure_mode}` where **v1 ships `kind ∈ {python, cli}`**
(`http`/`fs` deferred until a run needs them — most greenfield targets are `python`), and `validate_test_bundle()`
requires **every interface entry to have a compatible probe** (else its reachability can't be asserted). The report (`schemas/test-report.schema.json`) has a required **header** `{run_nonce, suite_digest, subject_kind,
subject_revision, schema_version}` and **one record per collected case**: `{id, phases:{setup,call,teardown},
reached_calls, terminal_status}` + collection errors + a **runner envelope** (§4.2). At freeze (`subject_kind =
stub`) the stub raises `StubSentinel(id)` so only `red_first_call` is reachable; at implementation/brownfield runs
the real bodies execute and the instrumentation records the **full** `reached_calls`. `collected_ids` is compared
to `frozen_case_ids` **separately** (§4.3). **Freshness:** delete `{report_path}` before each invocation and
**compare the report header to the run's expected `run_nonce`/`suite_digest`/`subject_kind`/`subject_revision`**,
rejecting a mismatch (a custom command/normalizer **must populate the header** from the §4.4 placeholders).

**4.2 Runner envelope + light hygiene.** Every gate run yields an envelope `{process_exit, timed_out, killed,
report_complete, report_truncated, report_valid}`, so a stale/partial/contradictory report never reads as green.
A **launch failure, timeout, kill, invalid/truncated report, or adapter crash → an infra `error`** (not test
pass/fail). The built-in pytest adapter **translates pytest's nonzero exit into per-case terminal statuses** —
process exit alone is not decisive. Accident controls (config `[stage_t].limits.*`, defaults suggestive):
**wall_timeout_seconds** (600) killing the **whole process group**; **cpu_seconds** (600); **address_space_bytes**
(~2 GiB) where available; **file_size_bytes** (256 MiB); **open_files** (1024); **processes** (256);
**report_cap_bytes** (16 MiB) + **stdout/stderr_cap_bytes** (4 MiB) — all enforced **while streaming**.
`HOME`/`TMPDIR`/caches → `{scratch_dir}`; sources read-only; network off where possible. Argv array.

**4.3 Gate predicate — separate from `consistent()`.** A stage-aware **`test_gate_ok()`** (generalize the live
`stage_c_tests_green`, where the empty-`test_command`→green path also lives — that path applies **only** when
Stage T is disabled) at decide/freeze/finalize. Requires: a clean envelope (§4.2); `Counter(collected_ids)`
equals `{each frozen id:1}`; per-case expected terminal state — Stage C → `passed` **and `reached_calls ⊇
declared_public_calls`** (the full workflow ran); Tt greenfield → `sentinel_reached` on `red_first_call`; Tt
brownfield → `expected_baseline` with declared calls reached; `skip|xfail|xpass|not-run` rejected unless modeled.
**`test_gate_ok()` branches on `[stage_t].enabled`:** disabled → the legacy `[stage_c].test_command` path
(an empty command stays green, the live behaviour); enabled → the `[test_gate]` adapter/command, of which
**exactly one must be set** (empty = config error). **Non-waivable:** `cmd_approve`
already refuses a non-green Stage C gate; Stage T preserves that. A reviewer APPROVE over a red gate →
**`stuck(tests_failed)`** (human-mediated recovery). Invalid Tt evidence → Tt authoring.

**4.4 Operator command.** Argv-token array with placeholders `{report_path} {scratch_dir} {run_nonce}
{repeat_index} {suite_digest} {subject_kind} {subject_revision} {test_tool_env_digest} {app_env_digest}`; each
repeat gets a fresh nonce + report path; a custom command emits the canonical schema or names a **normalizer**
(part of the frozen toolchain digest, §8). **Exit policy** (benign adapters commonly exit nonzero on test
failures): the **built-in pytest adapter may accept a nonzero exit when the report is complete and valid**; a
**custom command defaults to requiring `process_exit == 0`** unless its frozen **normalizer declares which exit
codes are report-bearing** — any other nonzero exit is an infra `error` (§4.2), not a red test report.

## 5. Stage C with test-visibility (the anti-overfitting core)

The impl agent implements the **contract**, graded by cases it mostly cannot see.

- **Sees:** brief + plans + `25-interface.json` + the **full `25-acceptance-contract.json`** + the policy-bounded
  **visible cases** (and only their audited visible closure). Not the hidden cases.
- **Two feedback channels:** **full diagnostics for visible cases** (ids, tracebacks, pytest selection — public,
  so Stage C gets a real red→green signal) and **redacted per-item status for hidden cases** (`per_item`): per
  item `{contract_item_id, status}`, `status ∈ {pass, fail, error, visible_only}` (+ a bounded `error_class ∈
  {collection_error, setup_error, teardown_error, timeout, env_error, report_invalid, impl_import_error}` for
  `error`) — no hidden ids/counts/parameters/traceback/expected/raw log; **same redaction in the reviewer
  prompt.** Aggregation over an item's **hidden** cases across all repeats: `error` if any infra-error/not-run;
  else `fail` if any hidden case fails any repeat; else `pass`; approved zero-hidden → `visible_only`. A hidden
  failure projects to all the case's `contract_items`.
- **Disjoint paths + ephemeral gate tree:** Stage C owns implementation paths only (incl. the stub as a seed);
  visible cases exposed **read-only** with **post-author-call hash verification**; each gate run builds an
  **ephemeral tree** = `deliverable_base` (the pipeline's base commit — greenfield: the empty initial commit;
  brownfield: the target repo's base) + impl revision + the exact frozen closure, verifies digests, runs
  there. Frozen-test/impl path overlap is **rejected**.
- **Review (a *replaced* prompt, not an extended one):** the live `prompts/codex/review-implementation.md` is
  built around a single trusted `{{test_exit}}` and a "tests present/meaningful — missing tests are blocking"
  dimension. Under Stage T that is **wrong on both counts**: the frozen tests are excluded from the reviewed diff
  (`deliverable_base → HEAD` **minus frozen-test paths**), so a reviewer running the old prompt would flag
  "missing tests" → REVISE → the author *can't* add tests (they're frozen) → the loop can't converge; and there
  is **no single exit code** (the gate is the per-case canonical report + envelope). So the Stage C review prompt
  is **replaced**: **remove** the author-writes-tests framing, the "missing tests blocking" dimension, and
  `{{test_exit}}`; **add** the trusted mechanical **gate verdict** (`test_gate_ok` green/red) as the pass signal,
  the **redacted `per_item`** feedback, and a new primary job — the **overfitting / contract-fidelity** check on
  the **impl-only** diff. Run via `codex exec - --sandbox read-only`. Stage C re-runs the gate `repeat_runs` times.

## 6. Composition & final re-test

Assemble the ephemeral tree, verify digests, **reuse the same resolved env** (§8), **re-run the full gate** (a
clean envelope §4.2). `finalize_run` marks `done` **only** on a green, revision-bound final report. A **red final
gate → `stuck(tests_failed)`** (amendable, §7); an **assembly/digest/environment/infra failure →
`stuck(composition_failed)`** (fix assembly/env). Neither commits `done`.

## 7. Amendment (fixing a wrong test)

The impl agent cannot see a hidden case to dispute it, so: **`test_dispute`** — when Stage C believes it
implements an item correctly but its hidden cases keep failing, it disputes **at the item level**.

**This is a *distinct* channel from DESIGN §5's author↔reviewer `DISPUTES:` block.** That existing block routes
to the **Codex reviewer** (`STATE.open_disputes` → `dispute_rulings`) — wrong here, since the reviewer also can't
see the hidden case. So Stage C emits a separate **`TEST_DISPUTE:` block keyed by `contract_item_id`**, and the
orchestrator routes it to **`awaiting_human, waiting_for = test_dispute`** — *bypassing* the reviewer
`dispute_rulings` path entirely. **"Persistently fails" is orchestrator-judged:** a `TEST_DISPUTE:` is only
honored after the same item has been `fail`/`error` for **≥ N consecutive Stage C rounds** (config), so a
premature self-declared dispute doesn't pause the loop early. The human resolves **`resolve-test-dispute
--amend`** or **`--continue --note`** (→ authoring, recording the rejected dispute so it doesn't re-pause).
Otherwise a wrong hidden case surfaces as `stuck(max_rounds | oscillation | tests_failed)`. **`amend-tests`** is
valid from any eligible test-related pause.

**Resume model.** Amendment is a full Tp/Tt run, so while `amend.active = true` the orchestrator runs the
**normal Tp/Tt state machine** in a sub-context (`amend.subrun` — the resume dispatch key); **`amend.phase ∈
{snapshot, rebase, reset}`** is reserved for the atomic operations. **Resume precedence:** `resume()` checks
**`if amend.active`, dispatch on `amend.phase` (an atomic op in flight) else on `amend.subrun`, *before*
consulting the top-level `(stage, status)` table.** `amend.*` (including `saved_stage_c`) persists in
`STATE.json` — **not** the `.stage_c.json` sidecar. The sub-run reopens **blind to the Stage C patch**. Old
Stage C state is scoped/cleared by `suite_epoch`.
**Successful return:** a conflict-free rebase **increments `suite_epoch`**, invalidates prior-epoch state, and
starts a **fresh Stage C `authoring` round** with **per-epoch round accounting**. Since DESIGN derives `round`
from append-only `history` (you can't delete old entries), **`history` items gain a `suite_epoch` field** and
round is derived as `count(history where stage == S and suite_epoch == current)` — so prior-epoch review entries
don't over-count the new epoch (§10.3). A conflict → `stuck(amendment_conflict)`, exit only via a **human edge**
→ re-authoring.

## 8. Dependency environments (one resolved environment)

The gate child imports the app **into pytest's interpreter** (no separate app server), so the test tooling and
app deps share **one interpreter**; `sys.path` precedence doesn't *resolve* a version conflict. Provisioning
**resolves one combined environment under the frozen test-tool constraints**; the app layer is resolved within
those, recorded per impl revision, digest bound into the report. An **unsatisfiable** set is a **provisioning
failure**: the orchestrator **surfaces the concrete conflict** (the offending app dependency + the constraining
test-tool pin) to Stage C as **trusted diagnostics** (not hidden-case content, so safe) so the autonomous agent
can pick a compatible dependency instead of re-failing round after round; if it remains unsatisfiable it is a
**deliberate human-amendment exit** (toolchain refreeze) — acceptable under benign/local, and stated here so it
isn't read as autonomously recoverable. The same resolved environment is **reused across `repeat_runs` and
reproduced for the final re-test**. A `set-config` change
affecting the adapter/toolchain **invalidates the freeze** (requires amendment/refreeze). A mid-run `set-config`
change to a **frozen suite's** visibility policy / `max_visible_*` / `[stage_t].enabled` is **rejected** — the
freeze is immutable and enablement is pinned at init (T3); changing them requires amendment/re-init, not a live edit.

## 9. Review governance — charter & independence

### 9.1 Review charter (prompt-level) — to reviewers, authors, and the monitor

A reviewer optimizes whatever objective it's given; unanchored it drifts to the most **escalatable** axis. The
**review charter** — **operating/threat model, ranked priorities, non-goals, blocking bar** — is **elicited in
Stage A** (propose-then-confirm via `QUESTIONS`), finalized at the A gate, stored as **`05-charter.md`**,
human-owned and amendable. **Prompt-level, not a verdict field:** passed as tier-2 context to **every reviewer,
author/reviser, and the monitor**. Reviewers raise out-of-scope observations as **nits**, never blockers — so no
fragile `out_of_charter` boolean; `consistent()` unchanged. A **general** orchestra mechanism (governs A/B too).

### 9.2 Reviewer independence (per-stage, sticky)

`reviewer.on_limit` is global; add a **per-stage** `stage_t.reviewer_on_limit` (default `pause`). A
fallback-reviewed Tp/Tt sets **sticky `fallback_reviewed`** through freeze and escalates Tt's gate to `some`,
sticky for that frozen suite.

## 10. Implementation, schema, config, init (functions by name)

1. **Stage-kind abstraction:** `stage_kind`, typed `current_artifact`, Tt edit handlers — generalize
   `artifact_paths`/`handle_author`/resume-integrity; resume tests for authoring/provisioning/freeze/review.
2. **Schemas + `validate_test_bundle()`:** `interface` (entry instrumentation), `acceptance-contract` (visibility
   policy + **structured examples** §2.2), author `tests-manifest`, frozen **`cases`**, **`visibility-closure`**,
   `test-report` (+ runner envelope). `validate_test_bundle()` runs all §3.5 invariants at freeze.
3. **`state.schema.json` + operation model:** stages; hashes/commits/digests; **`suite_epoch`** (top-level **and
   on each `history` item**, so epoch-aware round derivation works, T2); `amend.{active, subrun, phase,
   saved_stage_c}`; `waiting_for += {test_dispute}`; `stuck_reason += {amendment_conflict, tests_failed,
   composition_failed}`; `fallback_reviewed`. For the side-effecting non-LLM operations define
   **`operation = {kind ∈ {provision,freeze,compose,final_gate}, phase, suite_epoch, input_digests, output_paths,
   completed_marker, error}`** with an **idempotence table** (provision: key=input digests→env digest, done when
   recorded, retry=re-resolve; freeze: key=tt_commit+bundle hash, done when frozen artifacts+digests written,
   retry=re-validate+emit; compose: key=deliverable_base+impl rev+suite digest, done when ephemeral tree
   built+verified, retry=rebuild; final_gate: key=composed-tree digest+nonce, done when a valid report, retry=
   rerun with fresh nonce). Resume **re-runs the in-flight operation idempotently from its `completed_marker`** —
   the idempotence table (done-when / retry / digest key), **not** fine-grained sub-phases, is what makes this
   crash-safe, so `operation.phase` is an **optional, coarse liveness hint**, not a required dispatch key. The
   `operation` object replaces the small `current_step` enum for these operations.
4. **Prompts:** Tp/Tt author+review; Stage C author gets contract + visible cases + **two-channel feedback**,
   never hidden cases. The **Stage C *review* prompt is replaced** (not extended) — drop `{{test_exit}}` + the
   "tests present/meaningful / missing-tests-blocking" dimensions; add the `test_gate_ok` verdict, `per_item`, and
   the overfitting/contract-fidelity check on the impl-only diff (§5). `{{charter}}` to **all reviewers,
   authors/revisers, and the monitor**; Stage A charter elicitation.
5. **Gate runner:** replace **`_run_test_command`** with a dedicated **argv runner** producing the **runner
   envelope** (§4.2) + canonical report; rlimits/process-group-kill/streaming caps/HOME-TMPDIR→scratch;
   placeholders + fresh nonce. Exactly-one-gate-source config rule.
6. **Gate predicate:** stage-aware **`test_gate_ok()`** (generalize `stage_c_tests_green`) with envelope check +
   `Counter` id equality + per-case expected state **+ `reached_calls ⊇ declared_public_calls` for passing impl
   runs**; red APPROVE / red final → `tests_failed`; preserve `cmd_approve` non-waivability.
7. **Visibility split & topology (§5):** partition by case `visibility`; expose visible cases + closure artifact
   read-only with post-call hash verify; ephemeral gate tree; reject overlap; two feedback channels.
8. **Routing & advance:** replace static `NEXT_STAGE` with **`next_stage(stage, STATE.config)`** — it reads the
   **per-run config snapshot, not live `orchestra.toml`** (T3), so Stage T enablement is **pinned at init** and a
   later `[stage_t].enabled` toggle / `set-config` can't reshape an in-flight pipeline (disabled ⇒ `impl_plan →
   implementation`). Migration snapshots enablement into `STATE.config` for pre-Stage-T runs (default: stays
   disabled unless re-init'd). `advance_stage` persists frozen suite/digests/contract/`suite_epoch`; `finalize_run`
   composes + re-tests (§6). **Greenfield seeding change:** the live `setup_worktree` seeds Stage C as an empty
   `git init` + empty base commit (DESIGN §6); with Stage T it must seed from the **`tt_commit` tree** (which
   carries the impl-owned stub) so the frozen tests import a real module — same code area as §10.1's
   `artifact_paths`/`handle_author` generalization.
9. **Config:** the **Canonical names** block — incl. `feedback_granularity ∈ {per_item}` (v1), `repeat_runs >= 1`
   (default 3; `1` flagged no-flake), `limits.*` (§4.2).
10. **Init contracts (required seed artifacts per start stage):**
    - **`--stage test_plan`:** `00-brief.md`, approved `10-highlevel-plan.md`, `20-impl-plan.md`, `05-charter.md`
      (or `--charter`).
    - **`--stage tests`:** the above **+** `25-test-plan.md`, `25-interface.json`, `25-acceptance-contract.json`.
    - **`--stage implementation`:** the above **+** `28-tests-manifest.json`, `28-cases.json`,
      `28-visibility-closure.json`, `frozen_case_ids` + suite digest, the env digest, and the `[test_gate]` config.
    Verbs: `amend-tests`, `resolve-test-dispute --amend|--continue`, `set-config`; resume **re-runs the in-flight
    `operation` idempotently** and dispatches `amend.subrun`/`amend.phase` for amendment (§7).

## 11. DESIGN.md fold-in checklist
- **§2 pipeline**; **§3 blackboard/naming + curated-context** (`05-charter.md`, `25-*`, `28-*` incl.
  `28-visibility-closure.json`, schemas); **§4 loop/edit-mode** (stage-kind, typed artifact, `amend.*` +
  **operation model** + `suite_epoch`, `test_gate_ok` vs `consistent()`, `next_stage`); **§5 gates/HITL** (Tp
  `some`/Tt `none`, `test_dispute`, charter prompt-level); **§6 CLI** (gate argv runner + envelope, `codex exec -`,
  per-stage `reviewer_on_limit`, `--charter`, `set-config`, **init contracts**); **§7 verdict** (off-charter⇒nit);
  **§9/§10 safeguards** (enumerated hygiene; non-waivable gate; exactly-one-gate-source); **§10.1 monitor** —
  make it **Stage-T-aware** (treat `operation.phase` / `suite_epoch` advancement as *progress* so a long
  provision/freeze/compose isn't misjudged as wedged and HALTed in enforcing mode; teach it the new stages and
  `stuck_reason`s; **add `operation`/`phase` to `monitor.schema.json`'s `observed` block** — today it captures
  only `stage/round/status/tokens/elapsed`, so it literally can't represent a freeze-in-progress and its
  "blockers-trending-down / stage-advancing" heuristic would read a legitimate provision as a wedge, V3);
  **§11 config**; **artifact-layout** (pin the stub +
  visible/hidden suite-root paths, T8); **roadmap/prompts/init/migration**.

## 12. Notes / open
- **Overfitting is mitigated, not proven closed** — visibility policy over collected cases **+ structured
  contract examples with equivalence classes**, a declared/audited closure artifact, mandatory hidden coverage
  **and** a visible anchor per item, two feedback channels. Three **review-backstopped residuals** (machine can't
  close them; Tt review does): a singleton hidden group leaks a little; a brownfield multi-item hidden case makes
  its items flip together so the agent can infer they share a case; and **`example_kind` is author-declared**, so
  mislabelling concrete I/O as `normative` bypasses the visible budget (the *units floor* §2.2 closes the machine
  side; correct `case_like`/`normative` classification is a Tt-review guarantee). All bounded and benign-acceptable.
- **`feedback_granularity`** is `per_item` in v1.

## Changelog
- **v0.23** — fifth in-intent pass (live-prompt integration gaps): **`test_dispute` is now a distinct
  `TEST_DISPUTE:` channel** keyed by `contract_item_id`, routed by the orchestrator to `awaiting_human` —
  *bypassing* DESIGN §5's Codex `dispute_rulings` path (which would misroute a hidden-test dispute to a reviewer
  that can't see the case); "persistently fails" is orchestrator-judged after ≥N consecutive rounds (V1). The
  **Stage C review prompt is *replaced*, not extended**: drop `{{test_exit}}` + the "tests present/meaningful /
  missing-tests-blocking" dimensions (which would spuriously block a Stage T impl whose tests are frozen and
  excluded from the diff), add the `test_gate_ok` verdict + `per_item` + the overfitting/contract-fidelity check
  on the impl-only diff (V2). Added **`operation`/`phase` to the monitor `observed` schema** so a freeze-in-
  progress isn't read as a wedge (V3, sharpening T4).
- **v0.22** — fourth in-intent pass; first **blocking-eligible** finding, fixed: the visibility budget was
  bypassable because `visibility_budget_units` had no floor — a `case_like` example with `units = 0` is
  visible-but-uncounted, leaking unlimited concrete I/O past the cap. **`validate_test_bundle()` now requires
  `visibility_budget_units ≥ 1` when `counts_as_visible = true`** (R1a), and the doc **stops claiming examples
  "can't leak"**: `example_kind` mislabelling is an acknowledged **review-backstopped residual** (R1b, §12).
  Also: the author manifest's **per-case fields are keyed by the deterministic `ids=` label** (a parametrized
  definition spans multiple classes/items, so per-case not per-definition) (R2); **multi-item charging defined
  for examples too** (each `contract_items` + global), not just brownfield cases (R3); **"no dynamic imports"
  scoped** to test/fixture/helper code, framework exempt (R4); and the **greenfield `setup_worktree` must seed
  Stage C from the `tt_commit` tree** (the impl-owned stub) so frozen tests import a real module (R5).
- **v0.21** — third in-intent pass (sound-with-revisions): resolved the headline **Stage-T↔DESIGN contradiction**
  — Tp is a tool-less **planning author** that emits a **single stdout with three fenced named sections** the
  orchestrator splits + schema-validates, preserving the `--tools ""` mechanical-isolation invariant (not an
  edit-mode stage) (T1); **Tp-gate validation** of the two JSON artifacts before human approval (T5); **`history`
  items gain `suite_epoch`** so per-epoch round derivation is representable against append-only history (T2);
  **`next_stage` reads `STATE.config`** (per-run snapshot), pinning Stage T enablement at init so a live toggle
  can't reshape an in-flight run (T3); **monitor made Stage-T-aware** in the fold-in (operation/epoch advancement
  = progress; new stages/stuck reasons; `monitor.schema.json`) (T4); defined **`deliverable_base`** (T6),
  clarified brownfield **`expected_baseline` = start state, gate = end state** (T7), pinned **stub + suite-root
  paths** (T8), made mid-run `set-config` against a frozen suite **rejected** (T9), and added the brownfield
  multi-item **correlated-flip** leak to §12 residuals (T10).
- **v0.20** — second in-intent pass (sound-with-revisions; cluster in §4/§8): resolved the **"in-process" vs
  argv/process-group/rlimits** wording contradiction — the gate runs as a **child process** importing the app
  into pytest's interpreter (no separate server), so the §4.2 controls apply (N#2); the **report now carries a
  header** `{run_nonce, suite_digest, subject_kind, subject_revision, schema_version}` that its own freshness
  check compares against (N#1); **env-conflict diagnostics are surfaced to Stage C** (offending dep + constraining
  pin) so it can recover, with the human-refreeze exit stated explicitly (N#3); the **interface-stub partition**
  is pinned (committed in `tt_commit`, excluded from the closure, impl-owned/editable, regenerated pristine at
  amendment) so neither misreading deadlocks (N#4); **multi-item visible-case budget arithmetic** (charges each
  credited item + global, N#5); **collection-*set* determinism** (parametrize sources in the frozen closure) +
  an **AST check** that `ids=` is a literal list (N#6/N#7); the **freeze dispatch slot + backward edge** to Tt
  authoring (N#8); a **stub-passing case fails freeze** (`terminal != sentinel_reached`, N#9); and the **per-case
  `equivalence_class_id` is authoritative**, class membership derived (N#10).
- **v0.19** — per an in-intent review (no blocking; fixed the four should-fix gaps + trimmed per the
  calibration signal): **greenfield freeze now statically checks each `declared_public_call` is referenced in
  the case's reachable source** (§3.4 graph), so an over-declared call is rejected at freeze instead of becoming
  an unsatisfiable Stage C gate a full impl-cycle later (S1); the **equivalence-class disjointness guard is now
  uniform** — visible cases carry an `equivalence_class_id` and hidden classes must be disjoint from *all* visible
  (example + case) classes, closing the asymmetric hole for visible-case-anchored items (S2); a **visible case =
  1 budget unit** (S3); **`red_first_call` must be the case's first-invoked call**, stated explicitly (S4).
  **Trims (calibration):** v1 probes are `python`/`cli` (`http`/`fs` deferred); `operation.phase` is coarse/
  optional (the idempotence table carries crash-safety); anchor-strength distinction stated (a visible case gives
  real red→green, an example-only anchor doesn't); `≥2 hidden cases/item` promoted to a soft default. Plus the
  amend resume-dispatch precedence + `STATE.json` location (N3), the `test_gate_ok()` enabled/disabled regime
  branch (N5), and the §3.3 non-vacuity reword (N4).
- **v0.18** — per an in-intent review: **reachability split by `subject_kind`** — greenfield freeze requires
  only `red_first_call ∈ declared_public_calls` reached (the stub aborts at the first call), while
  brownfield/Stage C/final require `reached_calls ⊇ declared_public_calls`; greenfield conformance coverage is
  **declared**, full reachability is **runtime** (#blocking, fixing a §3.5↔§3.3 contradiction); **`counts_as_visible`
  forced true for any `case_like` example** (concrete I/O), with an `example_kind` classifier (#1; **corrected in
  v0.22** — the machine side needs a `visibility_budget_units ≥ 1` floor and the classification is
  review-backstopped, not "can't leak"); **equivalence classes are first-class objects** — machine checks id disjointness,
  Tt review judges semantic distinctness (#2); a **custom-command exit policy** (built-in pytest may accept
  nonzero+valid report; custom defaults to exit 0 unless a normalizer declares report-bearing codes) (#3);
  **interface-probe schema** `{entry_id, kind, target, install_phase, reach_marker, failure_mode}` with a
  validate check (#4); **per-kind `operation.phase` enums** (#nit).
- **v0.17** — per an in-intent review: **multi-call workflows restored** — greenfield cases are atomic on the
  *item* but may declare **multiple calls**; `red_first_call` proves non-vacuity at freeze, while the **full
  call set is verified at impl-run** (`reached_calls ⊇ declared_public_calls`); reachability unified via
  per-interface-entry **instrumentation** (#blocking); **structured contract examples** with
  `equivalence_class_id` + machine budget, hidden cases must cover distinct classes (#1); a **runner envelope**
  (`process_exit/timed_out/killed/report_complete/report_truncated/report_valid`) so partial/stale reports →
  infra `error`, and pytest exit translated per-case (#2); the **operation model** `{kind,phase,suite_epoch,
  input_digests,output_paths,completed_marker,error}` + idempotence table replacing `current_step` for
  provision/freeze/compose/final_gate (#3); **enumerated init contracts** per start stage (#4); fixed the
  `stage_c_tests_green` reference (#nit); defined the `feedback_granularity` enum (`per_item` v1) (#nit).
- **v0.16** — greenfield atomicity (now relaxed to item-atomic); visible diagnostics channel; deterministic case
  ids; visible anchor; `28-visibility-closure.json`; enumerated rlimits.
- **v0.15** — item/hidden coverage; declared closure; amendment resume; exactly-one-gate-source; multi-item; repeat_runs.
- **v0.14** — collected-case normalization; report-per-case; `suite_epoch`; `next_stage`. **v0.13** — visibility
  policy; `test_dispute`; charter prompt-level. **v0.12** — concrete machine model. **v0.11** — charter in Stage A.
  **v0.10** — non-adversarial reset; visibility core. **v0.9–v0.1** — adversarial peak (rolled back); Tp→Tt; initial.
