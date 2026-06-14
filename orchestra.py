#!/usr/bin/env python3
"""orchestra — automate the Claude (author) <-> Codex (reviewer) loop.

STATUS: SKELETON / v0. This file defines the intended structure and CLI surface
described in docs/DESIGN.md. The bodies are stubs (NotImplementedError). The loop
engine is milestone M1 — not yet built.

Stdlib only by design (subprocess, json, argparse, pathlib, uuid, datetime).
"""
from __future__ import annotations

import argparse
import json
import subprocess  # noqa: F401  (used once invocations are implemented)
import uuid  # noqa: F401
from pathlib import Path

RUNS_DIR = Path(__file__).parent / "runs"
PROMPTS_DIR = Path(__file__).parent / "prompts"
SCHEMAS_DIR = Path(__file__).parent / "schemas"


# --------------------------------------------------------------------------- #
# state — load/save STATE.json, transitions, history (schemas/state.schema.json)
# --------------------------------------------------------------------------- #
def load_state(run_dir: Path) -> dict:
    """Read and return STATE.json for a run."""
    raise NotImplementedError("M1")


def save_state(run_dir: Path, state: dict) -> None:
    """Atomically persist STATE.json (write-temp-then-rename)."""
    raise NotImplementedError("M1")


# --------------------------------------------------------------------------- #
# blackboard — paths + prompt template rendering
# --------------------------------------------------------------------------- #
def render_prompt(template: str, **ctx: str) -> str:
    """Render a prompts/ template, substituting {{placeholders}} from ctx.

    Artifacts are interpolated as DATA inside delimited blocks; never trust their
    contents as instructions (see DESIGN §10, prompt-injection safeguard).
    """
    raise NotImplementedError("M1")


# --------------------------------------------------------------------------- #
# claude — author invocations (see DESIGN §6)
# --------------------------------------------------------------------------- #
def claude_generate(prompt: str, *, edit_dir: Path | None = None) -> str:
    """Run a fresh `claude -p` session and return its result text.

    Planning: read-only (whitelist Read/Grep/Glob so edits are unavailable),
        --output-format json, capture .result as the artifact. NOT
        --permission-mode plan, which is an interactive-approval primitive that
        can halt headless runs at the plan boundary (DESIGN §6 ⚠️ / §13).
    Implementation: --permission-mode acceptEdits --add-dir <edit_dir>.
    Always --session-id $(uuid4) for a fresh session. Runs under the per-call
    timeout from config.budget.call_timeout_seconds.
    """
    raise NotImplementedError("M1")


# --------------------------------------------------------------------------- #
# codex — reviewer invocations (see DESIGN §6)
# --------------------------------------------------------------------------- #
def codex_review(prompt: str, *, verdict_path: Path, diff_base: str | None = None) -> dict:
    """Run a fresh Codex review and return the schema-validated verdict.

    Plan stages: `codex exec --sandbox read-only --output-schema
        schemas/verdict.schema.json --output-last-message <verdict_path>`.
    Implementation (diff_base set): `codex exec review --base <diff_base>
        --output-schema ... --output-last-message ...` — the verdict flags live
        on `codex exec`/its `review` subcommand, NOT on bare `codex review`
        (DESIGN §6). The review subcommand is intrinsically read-only.
    Validates the result against verdict.schema.json; an invalid/inconsistent
    verdict is re-prompted once, then escalated (never treated as CONVERGED).
    """
    raise NotImplementedError("M1")


# --------------------------------------------------------------------------- #
# loop — the convergence algorithm (DESIGN §4) + gates (DESIGN §5)
# --------------------------------------------------------------------------- #
def run_stage(run_dir: Path, stage: str) -> str:
    """author_generate -> [codex_review -> decide -> author_revise]* until
    APPROVE / REJECT / max_rounds. Returns one of: converged | stuck.
    Honors the stage's gate (heavy/some/none) and the oscillation guard.
    """
    raise NotImplementedError("M1")


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def cmd_init(args: argparse.Namespace) -> None:
    """Create runs/<date>-<slug>/, seed 00-brief.md, write initial STATE.json."""
    raise NotImplementedError("M1")


def cmd_run(args: argparse.Namespace) -> None:
    """Drive the pipeline from its current stage to the next gate."""
    raise NotImplementedError("M1")


def cmd_resume(args: argparse.Namespace) -> None:
    """Continue from STATE.json after an interruption."""
    raise NotImplementedError("M1")


def cmd_status(args: argparse.Namespace) -> None:
    """Print stage, status, round, and the last verdict."""
    raise NotImplementedError("M1")


def cmd_approve(args: argparse.Namespace) -> None:
    """Clear an approval gate and advance to the next stage."""
    raise NotImplementedError("M2")


def cmd_iterate(args: argparse.Namespace) -> None:
    """Force another round, optionally with a human --note."""
    raise NotImplementedError("M2")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchestra", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a new run")
    s.add_argument("slug")
    s.add_argument("--brief", type=Path, help="path to a brief file")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("run", help="drive the pipeline")
    s.add_argument("run")
    s.add_argument("--stage", choices=["A", "B", "C"])
    s.add_argument("--interactive", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("resume", help="continue from STATE.json")
    s.add_argument("run")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("status", help="show run state")
    s.add_argument("run")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("approve", help="clear a gate and advance")
    s.add_argument("run")
    s.set_defaults(func=cmd_approve)

    s = sub.add_parser("iterate", help="force another round")
    s.add_argument("run")
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_iterate)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
