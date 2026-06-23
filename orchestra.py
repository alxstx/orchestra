#!/usr/bin/env python3
"""orchestra — automate the Claude (author) <-> Codex (reviewer) loop.

A self-driving pipeline: brief -> Stage A (high-level plan, heavy HITL) ->
Stage B (implementation plan, some HITL) -> Stage C (implementation, autonomous)
-> presented diff/PR. Claude is the *author*; Codex is the *independent reviewer*.
The only channel between fresh sessions is a Markdown/JSON blackboard on disk.

This implements docs/DESIGN.md (v0.10). Stdlib only by design.

Environment note (verified codex-cli 0.139.0): `codex exec review`'s custom
[PROMPT] is mutually exclusive with --uncommitted/--base, so Stage C reviews the
diff via the `codex exec -` path with the diff computed by the orchestrator and
nonce-fenced (DESIGN §3) — stronger than native review, which would bypass the
fence. See `codex_review`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

try:
    import fcntl  # POSIX file locking
except ModuleNotFoundError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
PROMPTS_DIR = ROOT / "prompts"
SCHEMAS_DIR = ROOT / "schemas"
VERDICT_SCHEMA_PATH = SCHEMAS_DIR / "verdict.schema.json"

# Canonical stage names (as written in STATE.json) and their A/B/C display letters.
STAGE_NAMES = ["highlevel", "impl_plan", "implementation"]
STAGE_LETTERS = {"highlevel": "A", "impl_plan": "B", "implementation": "C"}
STAGE_ORDER = {"highlevel": 0, "impl_plan": 1, "implementation": 2}
NEXT_STAGE = {"highlevel": "impl_plan", "impl_plan": "implementation", "implementation": "done"}

ARTIFACT_BASENAME = {
    "highlevel": "10-highlevel-plan",
    "impl_plan": "20-impl-plan",
    "implementation": "30-impl",
}
ARTIFACT_LABEL = {
    "highlevel": "high-level plan",
    "impl_plan": "implementation plan",
    "implementation": "implementation",
}
WORKTREE_DIRNAME = "30-impl"

SEVERITY_WEIGHT = {"critical": 4, "high": 2, "medium": 1}

# Strong negative markers that, on an APPROVE, indicate a prose/decision mismatch (§7).
NEGATIVE_MARKERS = [
    "do not ship", "don't ship", "dont ship", "must fix", "must-fix",
    "is broken", "fundamentally broken", "not ready", "fails to", "does not work",
    "doesn't work", "critical bug", "security hole", "data loss", "will corrupt",
    "not safe", "unsafe to", "should be rejected", "cannot be approved",
]

ANSWERS_SENTINEL = "<!-- DELETE THIS LINE AFTER ANSWERING -->"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class OrchestraError(Exception):
    """Base class for orchestrator errors."""


class RenderError(OrchestraError):
    """A prompt failed to render safely (missing context or nonce-fence breach)."""


class SchemaError(OrchestraError):
    """A JSON document failed schema validation."""


class LockError(OrchestraError):
    """Another orchestrator holds the run lock."""


class ReviewerUnavailable(OrchestraError):
    """Neither Codex nor the Claude reviewer fallback is available."""


class AuthorPaused(OrchestraError):
    """A subprocess hit a subscription usage limit that must pause the run."""


class ReviewerPaused(OrchestraError):
    """on_limit=pause and Codex is unavailable — pause at the in-flight `reviewing`
    state so a later `orchestra resume` retries the SAME review when it resets (§6.2)."""


class FatalCallError(OrchestraError):
    """A non-retryable external-call failure (auth / not-found / config)."""


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.strip().lower()).strip("-")
    return s or "run"


def _norm(s: str) -> str:
    """Normalize a string for content-key comparison (DESIGN §10)."""
    s = (s or "").lower().strip()
    s = s.replace("`", "").replace("*", "").replace("_", "")
    s = re.sub(r"^[\s\-\*\d\.\)]+", "", s)  # strip list/numbering prefixes
    s = re.sub(r"\s+", " ", s)
    s = s.rstrip(" .:;,")
    return s


def read_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# config — defaults mirror orchestra.example.toml; merged with orchestra.toml
# --------------------------------------------------------------------------- #
def default_config() -> dict:
    return {
        "models": {"author": "opus", "reviewer": None},
        "reviewer": {"primary": "codex", "fallback": "claude", "on_limit": "fallback"},
        "max_rounds": {"highlevel": 15, "impl_plan": 15, "implementation": 15},
        "min_confidence": {"highlevel": 0.0, "impl_plan": 0.0, "implementation": 0.6},
        "gate": {"highlevel": "heavy", "impl_plan": "some", "implementation": "none"},
        "sandbox": {"author_plan": "read-only", "author_impl": "acceptEdits", "reviewer": "read-only"},
        "isolation": {
            "claude_safe_mode": True,
            "claude_bare": False,
            "claude_setting_sources": "",
            "claude_strict_mcp": True,
            "claude_no_session_persistence": True,
            "claude_planning_tools": "",
            "claude_brownfield_tools": "Read Grep Glob",
            "codex_ignore_user_config": True,
            "codex_ignore_rules": True,
            "codex_ephemeral": True,
        },
        "budget": {"wall_clock_seconds": 0, "call_timeout_seconds": 600},
        "stage_c": {"test_command": "", "test_timeout_seconds": 600, "sandbox_command": ""},
        "storage": {"runs_dir": ""},
        "target": {"mode": "greenfield", "repo": "", "worktree_path": ""},
        "monitor": {
            "enabled": True,
            "mode": "advisory",
            "interval_seconds": 300,
            "stage_soft_timeout": 1800,
            "intervene_min_confidence": 0.8,
            "watchdog": "none",
            "model": None,
        },
        "behavior": {
            "fresh_author_on_revise": True,
            "stop_on_oscillation": False,
            "dry_run": False,
        },
        "retry": {"max_attempts": 3, "base_delay_seconds": 2.0},
    }


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(config_path: Path | None = None) -> dict:
    """Defaults merged with orchestra.toml (if present). Per-run overrides come
    from STATE.json.config at runtime."""
    cfg = default_config()
    candidates = []
    if config_path:
        candidates.append(Path(config_path))
    candidates += [ROOT / "orchestra.toml", Path.cwd() / "orchestra.toml"]
    for c in candidates:
        if c and c.exists() and tomllib is not None:
            with open(c, "rb") as fh:
                cfg = _deep_merge(cfg, tomllib.load(fh))
            break
    return cfg


def stage_config_blob(cfg: dict) -> dict:
    """The config we persist into STATE.json.config (schema-shaped subset + extras)."""
    return {
        "max_rounds": dict(cfg["max_rounds"]),
        "min_confidence": dict(cfg["min_confidence"]),
        "models": {"author": cfg["models"]["author"], "reviewer": cfg["models"].get("reviewer")},
        "reviewer": dict(cfg["reviewer"]),
        "budget": dict(cfg["budget"]),
        "target": dict(cfg["target"]),
        # extras (config object allows additionalProperties):
        "gate": dict(cfg["gate"]),
        "sandbox": dict(cfg["sandbox"]),
        "isolation": dict(cfg["isolation"]),
        "stage_c": dict(cfg["stage_c"]),
        "storage": dict(cfg["storage"]),
        "monitor": dict(cfg["monitor"]),
        "behavior": dict(cfg["behavior"]),
        "retry": dict(cfg.get("retry", {"max_attempts": 3, "base_delay_seconds": 2.0})),
    }


def effective_config(state: dict) -> dict:
    """Merge defaults under the per-run STATE.json.config so every knob resolves."""
    return _deep_merge(default_config(), state.get("config", {}))


# --------------------------------------------------------------------------- #
# minimal JSON-schema validator (stdlib only) — covers the subset our schemas use
# --------------------------------------------------------------------------- #
def _type_ok(value, t: str) -> bool:
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    return True


def _check_format(value, fmt: str, path: str, errors: list) -> None:
    if fmt == "date-time" and isinstance(value, str):
        v = value.replace("Z", "+00:00")
        try:
            datetime.fromisoformat(v)
        except ValueError:
            errors.append(f"{path}: not a valid date-time: {value!r}")


def _validate(value, schema: dict, path: str, errors: list) -> None:
    if not isinstance(schema, dict):
        return
    if "type" in schema:
        types = schema["type"]
        types = [types] if isinstance(types, str) else list(types)
        if not any(_type_ok(value, t) for t in types):
            errors.append(f"{path}: expected type {schema['type']}, got {type(value).__name__}")
            return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']}")
    if "format" in schema:
        _check_format(value, schema["format"], path, errors)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            errors.append(f"{path}: non-finite number ({value}) not allowed")  # NaN/Inf
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required property '{req}'")
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                _validate(v, props[k], f"{path}.{k}", errors)
            else:
                ap = schema.get("additionalProperties", True)
                if ap is False:
                    errors.append(f"{path}: additional property '{k}' not allowed")
                elif isinstance(ap, dict):
                    _validate(v, ap, f"{path}.{k}", errors)
    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        errors.append(f"{path}: shorter than minLength {schema['minLength']}")
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                _validate(item, items, f"{path}[{i}]", errors)
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
    for sub in schema.get("allOf", []):
        if "if" in sub:
            cond_errs: list = []
            _validate(value, sub["if"], path, cond_errs)
            branch = sub.get("then") if not cond_errs else sub.get("else")
            if branch:
                _validate(value, branch, path, errors)
        else:
            _validate(value, sub, path, errors)
    if "anyOf" in schema:
        ok = False
        for sub in schema["anyOf"]:
            e: list = []
            _validate(value, sub, path, e)
            if not e:
                ok = True
                break
        if not ok:
            errors.append(f"{path}: does not match anyOf")


_SCHEMA_CACHE: dict = {}


def load_schema(path: Path) -> dict:
    p = str(path)
    if p not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[p] = json.loads(read_text(path))
    return _SCHEMA_CACHE[p]


def validate_against(instance, schema_path: Path) -> list:
    errors: list = []
    _validate(instance, load_schema(schema_path), "$", errors)
    return errors


# --------------------------------------------------------------------------- #
# atomic IO — write temp -> fsync(file) -> os.replace -> fsync(dir) (DESIGN §10)
# --------------------------------------------------------------------------- #
def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # fsync the directory so the rename itself is durable.
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    except OSError:  # pragma: no cover - some filesystems disallow dir fsync
        pass
    finally:
        os.close(dfd)


# --------------------------------------------------------------------------- #
# state — load/save STATE.json (schemas/state.schema.json)
# --------------------------------------------------------------------------- #
def new_state(run_id: str, stage: str, cfg: dict, gate: str) -> dict:
    ts = now_iso()
    return {
        "run_id": run_id,
        "created_at": ts,
        "updated_at": ts,
        "started_at": None,
        "stage": stage,
        "status": "authoring",
        "waiting_for": None,
        "stuck_reason": None,
        "round": 0,
        "gate": gate,
        "current_artifact": None,
        "current_step": None,
        "attempts": 0,
        "tokens_spent": 0,
        "config": stage_config_blob(cfg),
        "last_verdict": None,
        "resolved_ledger": [],
        "accepted_deviations": [],
        "open_disputes": [],
        "history": [],
    }


def load_state(run_dir: Path) -> dict:
    """Read and return STATE.json for a run. Defensively ensures the runtime arrays
    that the loop indexes directly always exist (a hand-edited or older state may
    omit them; the schema treats them as optional)."""
    s = json.loads(read_text(Path(run_dir) / "STATE.json"))
    for arr in ("resolved_ledger", "accepted_deviations", "open_disputes", "history"):
        s.setdefault(arr, [])
    return s


# Stage C runtime (worktree path, base commit, round base) is NOT part of the
# STATE.json contract (top-level additionalProperties:false), so it lives in a
# sidecar so STATE.json stays schema-pure.
def load_stage_c(run_dir: Path) -> dict:
    p = Path(run_dir) / ".stage_c.json"
    return json.loads(read_text(p)) if p.exists() else {}


def save_stage_c(run_dir: Path, data: dict) -> None:
    atomic_write_text(Path(run_dir) / ".stage_c.json", json.dumps(data, indent=2, allow_nan=False) + "\n")


def load_carried(run_dir: Path) -> dict:
    """Forward context carried across a stage boundary (approved nits — DESIGN §4.1)."""
    p = Path(run_dir) / ".carried.json"
    return json.loads(read_text(p)) if p.exists() else {}


def save_carried(run_dir: Path, data: dict) -> None:
    atomic_write_text(Path(run_dir) / ".carried.json", json.dumps(data, indent=2, allow_nan=False) + "\n")


def _state_for_disk(state: dict) -> dict:
    """The exact object both validated AND written. started_at is nullable in our
    model but the schema has no null type for it, so a null started_at is OMITTED
    from the file entirely — so what we validate is byte-for-byte what we commit
    (the previous code validated a projection but wrote the null, producing a
    schema-invalid file on disk)."""
    s = dict(state)
    if s.get("started_at") is None:
        s.pop("started_at", None)
    return s


# kept as an alias for tests/back-compat
def _state_for_schema(state: dict) -> dict:
    return _state_for_disk(state)


def save_state(run_dir: Path, state: dict) -> None:
    """Atomically persist STATE.json. STATE.json is the commit point — written last,
    never ahead of the files it references (DESIGN §8/§10). Validates and writes the
    SAME object, so the file on disk is always schema-valid."""
    state["updated_at"] = now_iso()
    on_disk = _state_for_disk(state)
    errors = validate_against(on_disk, SCHEMAS_DIR / "state.schema.json")
    if errors:
        raise SchemaError("STATE.json schema violation:\n  " + "\n  ".join(errors))
    atomic_write_text(Path(run_dir) / "STATE.json", json.dumps(on_disk, indent=2, allow_nan=False) + "\n")


# --------------------------------------------------------------------------- #
# run lock — exclusive flock/pidfile (DESIGN §8/§10)
# --------------------------------------------------------------------------- #
@contextmanager
def acquire_run_lock(run_dir: Path, *, wait: bool = False):
    """Take an EXCLUSIVE lock on <run>/.lock before driving it. A second
    orchestrator refuses (or, with wait=True, blocks). The read-only monitor does
    NOT call this. Releases on exit."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / ".lock"
    fh = open(lock_path, "a+")
    try:
        if fcntl is not None:
            flags = fcntl.LOCK_EX if wait else (fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fcntl.flock(fh.fileno(), flags)
            except OSError:
                fh.seek(0)
                holder = fh.read().strip()
                raise LockError(
                    f"run is locked by another orchestrator (holder: {holder or 'unknown'}). "
                    f"Refusing to act on {run_dir.name}."
                )
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} host={os.uname().nodename} at={now_iso()}\n")
        fh.flush()
        yield fh
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


# --------------------------------------------------------------------------- #
# nonce fencing + prompt rendering (DESIGN §3 — injection-proof framing)
# --------------------------------------------------------------------------- #
class Untrusted(str):
    """Marker for tier-3 (untrusted) values: nonce-fenced by render_prompt."""


def _gen_nonce() -> str:
    # Module-level seam so tests can pin the nonce; 16 random bytes is unguessable.
    return uuid.uuid4().hex + uuid.uuid4().hex[:0] + os.urandom(8).hex()


# Any delimiter requires the literal sequence: untrusted <ws> nonce.  Neutralizing
# that bigram (case-insensitively) anywhere in a value makes it impossible for the
# value to *form* an open/close delimiter, independent of model version.
_DELIM_BIGRAM = re.compile(r"untrusted(\s+)nonce", re.IGNORECASE)


def _neutralize(value: str) -> str:
    return _DELIM_BIGRAM.sub("untrusted-nonce", value)


def _extract_json_object(text: str) -> str | None:
    """Return the FIRST balanced top-level {...} object, or None. Brace-counts while
    respecting strings/escapes, so a valid verdict followed by brace-bearing prose
    ("Note: the {edge case}") isn't over-matched by a greedy first-{…last-} regex (§6.2)."""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _placeholder_names(template: str) -> list:
    return re.findall(r"{{(\w+)}}", template)


def strip_comments(template: str) -> str:
    return re.sub(r"<!--.*?-->\s*", "", template, flags=re.DOTALL)


def render_prompt(template: str, **ctx) -> str:
    """Render a template, substituting {{placeholders}}. Values wrapped in
    Untrusted() are nonce-fenced: the delimiter bigram is neutralized in the value,
    the value is wrapped in <untrusted nonce="..."> ... </untrusted nonce="...">
    with a fresh per-block nonce, and the rendered prompt is asserted to contain
    exactly the expected delimiter count so content cannot escape its block (§3)."""
    template = strip_comments(template)
    needed = set(_placeholder_names(template))
    missing = needed - set(ctx.keys())
    if missing:
        raise RenderError(f"template needs placeholders not provided: {sorted(missing)}")

    # SINGLE PASS over the ORIGINAL template — re.sub never re-scans the substituted
    # text, so an untrusted value that happens to contain a later placeholder token
    # (e.g. a diff containing "{{open_disputes}}") can NOT be mistaken for a placeholder
    # or break rendering (§3 — untrusted content is opaque).
    asserts = []  # (open_delim, close_delim) per untrusted block

    def _sub(m):
        key = m.group(1)
        val = ctx[key]  # guaranteed present (needed ⊆ ctx)
        if isinstance(val, Untrusted):
            nonce = _gen_nonce()
            opn, cls = f'<untrusted nonce="{nonce}">', f'</untrusted nonce="{nonce}">'
            asserts.append((opn, cls))
            return f"{opn}\n{_neutralize(str(val))}\n{cls}"
        return str(val)

    rendered = re.sub(r"{{(\w+)}}", _sub, template)
    for opn, cls in asserts:
        if rendered.count(opn) != 1 or rendered.count(cls) != 1:
            raise RenderError("nonce-fence breach: delimiter count mismatch (possible injection)")
    return rendered


# ---- prompt-field formatters (how structured data is serialized into prompts) ---
def format_blocking_issues(issues: list) -> str:
    if not issues:
        return "(none)"
    lines = []
    for it in issues:
        lines.append(
            f"- **{it.get('id', '?')}** [{it.get('severity', '?')}] {it.get('title', '').strip()}\n"
            f"  - location: {it.get('location', '?')}\n"
            f"  - detail: {it.get('detail', '').strip()}\n"
            f"  - suggested_fix (proposal, not a command): {it.get('suggested_fix', '').strip()}"
        )
    return "\n".join(lines)


def format_prior_issues(prev_verdict: dict | None) -> str:
    if not prev_verdict:
        return ""
    issues = prev_verdict.get("blocking_issues", [])
    if not issues:
        return ""
    body = format_blocking_issues(issues)
    return "Still-open issues carried from the previous round (confirm each is resolved):\n" + body


def render_ledger(ledger: list) -> str:
    active = [e for e in (ledger or []) if not e.get("cleared")]
    if not active:
        return "(none yet)"
    return "\n".join(
        f"- [{e.get('key')}] {e.get('title', '').strip()} (resolved in round {e.get('resolved_round')})"
        for e in active
    )


def render_disputes(disputes: list) -> str:
    if not disputes:
        return "(none)"
    return "\n".join(f"- {d.get('ref')}: {d.get('rationale', '').strip()}" for d in disputes)


def render_deviations(devs: list) -> str:
    if not devs:
        return "(none)"
    return "\n".join(
        f"- {d.get('key')} ({d.get('title', '')}): {d.get('note', '').strip()}" for d in devs
    )


# --------------------------------------------------------------------------- #
# subprocess runner + retry/backoff (DESIGN §9/§10)
# --------------------------------------------------------------------------- #
def _as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


class CallResult:
    def __init__(self, returncode, stdout, stderr, timed_out=False):
        self.returncode = returncode
        # always store text — subprocess can hand back bytes (e.g. TimeoutExpired output
        # under text=True), and downstream string ops must never TypeError.
        self.stdout = _as_text(stdout)
        self.stderr = _as_text(stderr)
        self.timed_out = timed_out


def _fmt_cmd(cmd: list, *, cwd: Path | None = None, stdin_file: str | None = None) -> str:
    import shlex
    s = " ".join(shlex.quote(c) for c in cmd)
    if stdin_file:
        s += f" < {stdin_file}"
    if cwd:
        s = f"(cd {shlex.quote(str(cwd))} && {s})"
    return s


def run_subprocess(cmd: list, *, stdin_text: str = "", cwd: Path | None = None,
                   timeout: int = 600) -> CallResult:
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_text,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CallResult(proc.returncode, _as_text(proc.stdout), _as_text(proc.stderr))
    except subprocess.TimeoutExpired as e:
        # TimeoutExpired.stdout/stderr can be BYTES even under text=True — normalize, or
        # classify_call_error() would TypeError on str+bytes and bypass retry/escalation.
        return CallResult(124, _as_text(e.stdout), _as_text(e.stderr) + "\n[timeout]", timed_out=True)
    except OSError as e:
        # missing executable / exec failure — return a not-found CallResult so the normal
        # classify→FatalCallError→error transition runs (don't let it escape to main()).
        return CallResult(127, "", f"command not found / exec error: {e}")


_USAGE_LIMIT_RE = re.compile(
    r"usage limit|rate.?limit|too many requests|429|quota|over(loaded|_loaded)|"
    r"capacity|temporarily unavailable|resource_exhausted|service unavailable",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(r"unauthorized|not authenticated|invalid api key|401|403|forbidden|login", re.IGNORECASE)
_NOTFOUND_RE = re.compile(r"command not found|no such file|not found", re.IGNORECASE)


def classify_call_error(result: CallResult) -> str:
    """Return one of: ok | transient | usage_limit | auth | not_found | config."""
    if result.returncode == 0:
        return "ok"
    blob = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.timed_out:
        return "transient"
    # usage/availability is checked BEFORE auth so an availability message that happens
    # to mention "login"/"forbidden" prefers the pause/fallback path over fail-fast.
    if _USAGE_LIMIT_RE.search(blob):
        return "usage_limit"
    if _AUTH_RE.search(blob):
        return "auth"
    if _NOTFOUND_RE.search(blob):
        return "not_found"
    return "transient"


def with_retry(fn, *, cfg: dict, on_attempt=None):
    """Run fn() with bounded exponential backoff + jitter on transient failures.
    fn must raise a RetryableError to trigger a retry, or FatalCallError to stop.
    Returns fn()'s value."""
    rcfg = cfg.get("retry", {"max_attempts": 3, "base_delay_seconds": 2.0})
    attempts = int(rcfg.get("max_attempts", 3))
    base = float(rcfg.get("base_delay_seconds", 2.0))
    last = None
    for i in range(attempts):
        if on_attempt:
            on_attempt(i)
        try:
            return fn()
        except FatalCallError:
            raise
        except RetryableError as e:
            last = e
            if i == attempts - 1:
                break
            delay = base * (2 ** i) + random.uniform(0, base)
            time.sleep(min(delay, 30))
    raise last if last else OrchestraError("retry exhausted")


class RetryableError(OrchestraError):
    pass


# --------------------------------------------------------------------------- #
# claude — author / monitor / reviewer-fallback invocations (DESIGN §6/§6.1)
# --------------------------------------------------------------------------- #
def _claude_isolation_flags(cfg: dict) -> list:
    iso = cfg["isolation"]
    flags = []
    if iso.get("claude_bare"):
        flags.append("--bare")
    elif iso.get("claude_safe_mode", True):
        flags.append("--safe-mode")
    flags += ["--setting-sources", iso.get("claude_setting_sources", "")]
    if iso.get("claude_strict_mcp", True):
        flags.append("--strict-mcp-config")
    if iso.get("claude_no_session_persistence", True):
        flags.append("--no-session-persistence")
    return flags


def build_claude_cmd(cfg: dict, *, mode: str, worktree: Path | None = None,
                     system_prompt_file: Path | None = None) -> list:
    """mode: plan | impl | brownfield | monitor | reviewer."""
    iso = cfg["isolation"]
    model = cfg["models"]["author"]
    cmd = ["claude", "-p", "--session-id", str(uuid.uuid4()), "--model", model,
           "--output-format", "json"]
    if mode in ("plan", "monitor", "reviewer"):
        cmd += ["--tools", iso.get("claude_planning_tools", "")]
    elif mode == "brownfield":
        cmd += ["--tools", iso.get("claude_brownfield_tools", "Read Grep Glob")]
    elif mode == "impl":
        cmd += ["--permission-mode", "acceptEdits"]
        if worktree:
            cmd += ["--add-dir", str(worktree)]
    cmd += _claude_isolation_flags(cfg)
    if system_prompt_file and Path(system_prompt_file).exists():
        cmd += ["--append-system-prompt-file", str(system_prompt_file)]
    return cmd


def parse_claude_json(stdout: str) -> dict:
    data = json.loads(stdout)
    model_id = data.get("model")
    if not model_id:
        mu = data.get("modelUsage") or {}
        if mu:
            model_id = sorted(mu.keys())[0]
    usage = data.get("usage") or {}
    return {
        "result": data.get("result", ""),
        "is_error": bool(data.get("is_error")),
        "subtype": data.get("subtype"),
        "num_turns": data.get("num_turns"),
        "model": model_id or "",
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cost_usd": float(data.get("total_cost_usd") or 0.0),
    }


def claude_generate(prompt: str, *, cfg: dict, mode: str = "plan",
                    worktree: Path | None = None, edit_dir: Path | None = None,
                    cwd: Path | None = None, dry_run: bool = False, on_attempt=None) -> dict:
    """Run a fresh `claude -p` session; return {'result', 'model', 'output_tokens', ...}.
    Carries the full isolation profile (§6.1). edit_dir is an alias for worktree
    (impl mode)."""
    worktree = worktree or edit_dir
    # The author trust-model system prompt applies to AUTHOR modes only — the monitor and
    # the reviewer-fallback carry their own role prompts (in the rendered stdin), so they
    # must not inherit the author's system prompt.
    sysfile = (PROMPTS_DIR / "claude" / "system.md") if mode in ("plan", "impl", "brownfield") else None
    cmd = build_claude_cmd(cfg, mode=mode, worktree=worktree, system_prompt_file=sysfile)
    timeout = int(cfg["budget"]["call_timeout_seconds"])
    if dry_run:
        run_cwd = cwd or (worktree if mode == "impl" else _staging_dir())
        print(f"[dry-run] CLAUDE ({mode}): {_fmt_cmd(cmd, cwd=run_cwd, stdin_file='<prompt>')}")
        return {"result": f"<dry-run stub {mode} artifact>", "model": "dry-run",
                "output_tokens": 0, "cost_usd": 0.0, "is_error": False}

    run_cwd = cwd or (worktree if mode == "impl" else _staging_dir())

    def attempt():
        res = run_subprocess(cmd, stdin_text=prompt, cwd=run_cwd, timeout=timeout)
        kind = classify_call_error(res)
        if kind == "auth":
            raise FatalCallError(f"claude auth failure: {res.stderr[:200]}")
        if kind == "not_found":
            raise FatalCallError("claude CLI not found")
        if kind == "usage_limit":
            raise AuthorPaused("claude hit a usage limit; pausing run (DESIGN §9)")
        if kind == "transient":
            raise RetryableError(f"claude transient failure (rc={res.returncode}): {res.stderr[:200]}")
        try:
            out = parse_claude_json(res.stdout)
        except (json.JSONDecodeError, ValueError) as e:
            raise RetryableError(f"claude returned unparseable JSON: {e}")
        if out["is_error"]:
            raise RetryableError(f"claude is_error=true (subtype={out['subtype']})")
        return out

    return with_retry(attempt, cfg=cfg, on_attempt=on_attempt)


_STAGING = None


def _staging_dir() -> Path:
    """An EMPTY staging cwd for planning authors so they physically cannot read
    the blackboard (DESIGN §3/§6.1)."""
    global _STAGING
    if _STAGING is None or not Path(_STAGING).exists():
        _STAGING = Path(tempfile.mkdtemp(prefix="orchestra-staging-"))
    return Path(_STAGING)


# --------------------------------------------------------------------------- #
# codex — reviewer invocations (DESIGN §6/§6.2)
# --------------------------------------------------------------------------- #
def build_codex_cmd(cfg: dict, *, verdict_path: Path, model: str | None) -> list:
    iso = cfg["isolation"]
    cmd = ["codex", "exec", "-", "--skip-git-repo-check", "--sandbox", "read-only"]
    if iso.get("codex_ignore_user_config", True):
        cmd.append("--ignore-user-config")
    if iso.get("codex_ignore_rules", True):
        cmd.append("--ignore-rules")
    if iso.get("codex_ephemeral", True):
        cmd.append("--ephemeral")
    cmd += ["--output-schema", str(VERDICT_SCHEMA_PATH),
            "--output-last-message", str(verdict_path)]
    if model:
        cmd += ["--model", model]
    return cmd


def codex_review(prompt: str, *, cfg: dict, verdict_path: Path,
                 cwd: Path | None = None, dry_run: bool = False, on_attempt=None) -> dict:
    """Run a fresh Codex review via `codex exec -` and return the parsed verdict
    read from the --output-last-message file (NOT stdout). Raises RetryableError on
    transient failure, AuthorPaused/ReviewerUnavailable on usage limit."""
    model = cfg["models"].get("reviewer")
    cmd = build_codex_cmd(cfg, verdict_path=verdict_path, model=model)
    timeout = int(cfg["budget"]["call_timeout_seconds"])
    run_cwd = cwd or _staging_dir()  # the SAME cwd the live call uses (empty staging dir)
    if dry_run:
        print(f"[dry-run] CODEX review: {_fmt_cmd(cmd, cwd=run_cwd, stdin_file='<review-prompt>')}")
        return _stub_verdict("APPROVE")
    Path(verdict_path).parent.mkdir(parents=True, exist_ok=True)

    # codex writes to a TEMP sibling; we validate, then atomically promote to the real
    # path before STATE references it (temp→fsync→rename→fsync-dir, DESIGN §10) so a
    # crash can't leave STATE pointing at a half-written verdict.
    tmp_out = Path(str(verdict_path) + ".raw")
    cmd = build_codex_cmd(cfg, verdict_path=tmp_out, model=model)

    def attempt():
        if tmp_out.exists():
            tmp_out.unlink()
        res = run_subprocess(cmd, stdin_text=prompt, cwd=run_cwd, timeout=timeout)
        kind = classify_call_error(res)
        # §6.2: usage-limit / rate / AUTH / availability — incl. a MISSING codex binary
        # (an availability problem) — switch the round to the Claude reviewer rather than
        # hard-erroring; only when BOTH are unavailable does the run get stuck.
        if kind in ("usage_limit", "auth", "not_found"):
            raise CodexUnavailable(f"codex {kind}: {res.stderr[:160]}")
        if not tmp_out.exists():
            # transport failure (no output) — retryable; persistent ⇒ availability (below)
            raise RetryableError(f"codex wrote no verdict (rc={res.returncode}): {res.stderr[:200]}")
        try:
            verdict = json.loads(read_text(tmp_out))
        except (json.JSONDecodeError, ValueError) as e:
            raise VerdictInvalid(f"codex verdict unparseable: {e}")
        verr = validate_against(verdict, VERDICT_SCHEMA_PATH)
        if verr:
            raise VerdictInvalid("codex verdict schema violation: " + "; ".join(verr[:4]))
        atomic_write_text(verdict_path, json.dumps(verdict, indent=2, allow_nan=False) + "\n")
        try:
            tmp_out.unlink()
        except OSError:
            pass
        return verdict

    try:
        return with_retry(attempt, cfg=cfg, on_attempt=on_attempt)
    except RetryableError as e:
        # persistent transport failure (e.g. connection refused) is an availability
        # problem → fall back to the Claude reviewer rather than erroring (§6.2).
        raise CodexUnavailable(f"codex unavailable after retries: {e}")


class CodexUnavailable(OrchestraError):
    """Codex specifically is unavailable (limit/rate/auth/availability) — triggers fallback."""


class VerdictInvalid(OrchestraError):
    """The reviewer produced a present-but-malformed verdict (unparseable / schema-
    invalid) — a verdict-quality problem: re-prompt once, then stuck(error) (§7).
    Distinct from CodexUnavailable (which falls back to the other reviewer)."""


def _stub_verdict(decision: str, *, blockers=None) -> dict:
    blockers = blockers or ([] if decision == "APPROVE" else [{
        "id": "B1", "severity": "high", "title": "stub blocker",
        "detail": "stub", "location": "stub", "suggested_fix": "stub"}])
    return {
        "decision": decision, "confidence": 0.9,
        "review_markdown": f"## Stub review\n\n{decision} (dry-run/stub).",
        "summary": f"{decision} stub.", "blocking_issues": blockers,
        "non_blocking_suggestions": [], "reject_reason": None,
        "addressed_previous": [], "regressions": [], "dispute_rulings": [],
    }


# --------------------------------------------------------------------------- #
# reviewer abstraction — Codex primary, Claude fallback (DESIGN §6.2)
# --------------------------------------------------------------------------- #
def claude_reviewer(prompt: str, *, cfg: dict, verdict_path: Path,
                    dry_run: bool = False, on_attempt=None) -> dict:
    """A fresh, isolated Claude session acting as reviewer (read-only). Same
    reviewer-agnostic prompt + a JSON-only instruction; validated identically."""
    instr = (
        "\n\nYou are acting as the independent reviewer. Output ONLY a single JSON "
        "object conforming exactly to the verdict schema (decision, confidence, "
        "review_markdown, summary, blocking_issues, non_blocking_suggestions, "
        "reject_reason, addressed_previous, regressions, dispute_rulings). No prose "
        "outside the JSON, no code fences.\n"
    )
    if dry_run:
        print("[dry-run] CLAUDE reviewer (fallback)")
        return _stub_verdict("APPROVE")

    # NO outer with_retry here — claude_generate already owns the bounded transient retry
    # (a second wrapper would cube the attempts to 3x3=9). A malformed-but-present verdict
    # is a VerdictInvalid → handle_review does the single corrective re-prompt (§7).
    out = claude_generate(prompt + instr, cfg=cfg, mode="reviewer", on_attempt=on_attempt)
    text = out["result"].strip()
    obj = _extract_json_object(text)
    if obj is None:
        raise VerdictInvalid("claude reviewer produced no JSON object")
    try:
        verdict = json.loads(obj)
    except (json.JSONDecodeError, ValueError) as e:
        raise VerdictInvalid(f"claude reviewer JSON parse failed: {e}")
    verr = validate_against(verdict, VERDICT_SCHEMA_PATH)
    if verr:
        raise VerdictInvalid("claude reviewer verdict schema violation: " + "; ".join(verr[:3]))
    atomic_write_text(verdict_path, json.dumps(verdict, indent=2, allow_nan=False) + "\n")
    return verdict


def review(prompt: str, *, cfg: dict, verdict_path: Path, cwd: Path | None = None,
           dry_run: bool = False, on_attempt=None) -> dict:
    """Dispatch a review to the configured reviewer with fallback (§6.2).
    Returns {'verdict', 'actor', 'fallback', 'model'}."""
    primary = cfg["reviewer"].get("primary", "codex")
    on_limit = cfg["reviewer"].get("on_limit", "fallback")

    if primary == "claude":  # single-vendor mode (data egress) — deliberate, not fallback
        verdict = claude_reviewer(prompt, cfg=cfg, verdict_path=verdict_path, dry_run=dry_run,
                                  on_attempt=on_attempt)
        return {"verdict": verdict, "actor": "claude", "fallback": False,
                "model": cfg["models"]["author"]}

    try:
        verdict = codex_review(prompt, cfg=cfg, verdict_path=verdict_path, cwd=cwd,
                               dry_run=dry_run, on_attempt=on_attempt)
        return {"verdict": verdict, "actor": "codex", "fallback": False,
                "model": cfg["models"].get("reviewer") or "codex-default"}
    except CodexUnavailable as e:
        if on_limit == "pause":
            # operator chose to wait rather than degrade — pause at `reviewing` so a
            # later resume retries the SAME review when the limit resets (§6.2).
            raise ReviewerPaused(f"Codex unavailable and on_limit=pause: {e}")
        if not cfg["reviewer"].get("fallback"):
            raise ReviewerUnavailable(f"Codex unavailable, no fallback configured: {e}")
        try:
            verdict = claude_reviewer(prompt, cfg=cfg, verdict_path=verdict_path,
                                      dry_run=dry_run, on_attempt=on_attempt)
        except VerdictInvalid:
            raise  # a malformed fallback verdict → re-prompt/stuck(error), not unavailable
        except Exception as e2:
            raise ReviewerUnavailable(f"Codex AND Claude reviewer unavailable: {e}; {e2}")
        return {"verdict": verdict, "actor": "claude", "fallback": True,
                "model": cfg["models"]["author"]}


# --------------------------------------------------------------------------- #
# verdict semantics — consistent() + quality guards (DESIGN §7)
# --------------------------------------------------------------------------- #
def confidence_valid(verdict: dict) -> bool:
    c = verdict.get("confidence")
    return isinstance(c, (int, float)) and not isinstance(c, bool) and 0.0 <= c <= 1.0


def regression_verifiable(reg: dict, artifact_delta: str) -> bool:
    """A claimed regression is verifiable iff its evidence is grounded in the
    artifact delta (DESIGN §3 E2). Unverifiable -> downgraded to a normal finding."""
    ev = _norm(reg.get("evidence", ""))
    if not ev or not artifact_delta:
        return False
    delta = _norm(artifact_delta)
    if ev in delta:
        return True
    # token-overlap fallback for paraphrased evidence
    toks = [t for t in re.split(r"\W+", ev) if len(t) >= 4]
    if not toks:
        return False
    hits = sum(1 for t in toks if t in delta)
    return hits / len(toks) >= 0.6


def validated_regressions(verdict: dict, artifact_delta: str, active_ledger_keys=None) -> list:
    """Regressions that actually count toward forbidding APPROVE: evidence grounded in
    the artifact delta AND (when a ledger is supplied) the key belongs to an active,
    uncleared resolved-ledger entry (a "regression" is by definition a regressed ledger
    item — §3). When active_ledger_keys is None, fall back to evidence-only."""
    out = []
    for r in verdict.get("regressions", []):
        if not regression_verifiable(r, artifact_delta):
            continue
        if active_ledger_keys is not None and r.get("key") not in active_ledger_keys:
            continue  # not a tracked (uncleared) ledger item → treat as a normal finding
        out.append(r)
    return out


def consistent(verdict: dict, ledger: list | None = None, artifact_delta: str = "",
               accepted_keys=()) -> bool:
    """Enforce the verdict invariants the strict schema can't (DESIGN §7).
    APPROVE <=> no EFFECTIVE blockers AND no validated regression; REVISE <=> >=1
    effective blocker; REJECT <=> >=1 effective blocker or reject_reason. Confidence
    outside [0,1] (or non-numeric) is a failure — never a silent clamp. Blockers whose
    content key was conceded (accepted_deviations) don't count (§5); a regression only
    counts if its key is an active, uncleared ledger entry (§3, when ledger supplied)."""
    decision = verdict.get("decision")
    if decision not in ("APPROVE", "REVISE", "REJECT"):
        return False
    if not confidence_valid(verdict):
        return False
    # reject_reason is the REJECT signal — a non-REJECT verdict carrying one is
    # self-contradictory (§7: REJECT ⟺ ... reject_reason), so it's inconsistent.
    if verdict.get("reject_reason") and decision != "REJECT":
        return False
    blockers = verdict.get("blocking_issues")
    if not isinstance(blockers, list):
        return False
    accepted = set(accepted_keys or ())
    effective_blockers = [b for b in blockers if content_key(b) not in accepted]
    active_keys = None
    if ledger is not None:
        active_keys = {e["key"] for e in ledger if not e.get("cleared")}
    n_reg = len(validated_regressions(verdict, artifact_delta, active_keys))
    effective = len(effective_blockers) + n_reg
    if decision == "APPROVE":
        return effective == 0
    if decision == "REVISE":
        return effective >= 1
    if decision == "REJECT":
        return effective >= 1 or bool(verdict.get("reject_reason"))
    return False


def is_nontrivial_artifact(text: str) -> bool:
    t = (text or "").strip()
    return len(t) > 280 or t.count("\n") >= 8


def has_prose_decision_mismatch(verdict: dict) -> bool:
    if verdict.get("decision") != "APPROVE":
        return False
    md = (verdict.get("review_markdown", "") or "").lower()
    return any(marker in md for marker in NEGATIVE_MARKERS)


def low_confidence(verdict: dict, cfg: dict, stage: str) -> bool:
    floor = float(cfg["min_confidence"].get(stage, 0.0))
    return floor > 0.0 and float(verdict.get("confidence", 0.0)) < floor


# --------------------------------------------------------------------------- #
# anti-regression ledger + oscillation (DESIGN §3/§10)
# --------------------------------------------------------------------------- #
def content_key(issue: dict) -> str:
    return f"{_norm(issue.get('location', ''))}::{_norm(issue.get('title', ''))}"


def blocking_score(blockers: list, exclude_keys=()) -> int:
    ex = set(exclude_keys)
    return sum(
        SEVERITY_WEIGHT.get(b.get("severity"), 1)
        for b in blockers if content_key(b) not in ex
    )


def is_oscillating(prev_blockers: list, cur_blockers: list, exclude_keys=()) -> bool:
    """Severity-weighted score does NOT strictly decrease AND a prior key recurs
    (DESIGN §10). 'fixed K, found 1 new' (all keys new) does not trip it."""
    ex = set(exclude_keys)
    prev_keys = {content_key(b) for b in prev_blockers} - ex
    cur_keys = {content_key(b) for b in cur_blockers} - ex
    if not prev_keys or not cur_keys:
        return False
    cur_score = blocking_score(cur_blockers, ex)
    prev_score = blocking_score(prev_blockers, ex)
    recurs = bool(prev_keys & cur_keys)
    return (cur_score >= prev_score) and recurs


def excluded_keys(state: dict) -> set:
    keys = {e["key"] for e in state.get("accepted_deviations", [])}
    keys |= {e["key"] for e in state.get("resolved_ledger", []) if e.get("cleared")}
    return keys


def update_ledger(state: dict, prev_verdict: dict | None, cur_verdict: dict, round_: int) -> None:
    """Append content keys of blockers resolved this round (present last round,
    gone this round) to the append-only ledger (DESIGN §3)."""
    if not prev_verdict:
        return
    prev_blockers = prev_verdict.get("blocking_issues", [])
    cur_keys = {content_key(b) for b in cur_verdict.get("blocking_issues", [])}
    existing = {e["key"] for e in state["resolved_ledger"]}
    # A conceded blocker (accepted_deviation) "disappears" but was NOT fixed — never
    # ledger it as resolved (§5), else its won't-fix status flips to must-stay-fixed.
    conceded = {e["key"] for e in state.get("accepted_deviations", [])}
    for b in prev_blockers:
        k = content_key(b)
        if k not in cur_keys and k not in existing and k not in conceded:
            state["resolved_ledger"].append({
                "key": k, "title": b.get("title", ""), "resolved_round": round_,
            })
            existing.add(k)


# --------------------------------------------------------------------------- #
# disputes & questions parsing (DESIGN §5)
# --------------------------------------------------------------------------- #
def _extract_block(text: str, header: str) -> list:
    """Find a `HEADER:` block (optionally inside a ``` fence) and return its lines
    until a blank line / fence close / next header."""
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        if re.match(rf"^\s*`*\s*{re.escape(header)}\s*:?\s*$", lines[i], re.IGNORECASE) or \
           re.match(rf"^\s*{re.escape(header)}\s*:", lines[i], re.IGNORECASE):
            # consume any inline content after the header on the same line
            inline = re.sub(rf"^\s*{re.escape(header)}\s*:\s*", "", lines[i], flags=re.IGNORECASE)
            j = i + 1
            if inline.strip() and inline.strip() not in ("```",):
                out.append(inline.rstrip())
            while j < len(lines):
                ln = lines[j]
                if ln.strip().startswith("```"):
                    break
                if not ln.strip():
                    break
                if re.match(r"^\s*#", ln):
                    break
                out.append(ln.rstrip())
                j += 1
            return out
        i += 1
    return out


def parse_disputes(text: str, round_: int) -> list:
    """Parse a `DISPUTES:` block into [{ref, rationale, raised_round}]."""
    out = []
    for ln in _extract_block(text, "DISPUTES"):
        m = re.match(r"^\s*[-*]?\s*([^:]+?)\s*:\s*(.+)$", ln)
        if m:
            out.append({"ref": m.group(1).strip(), "rationale": m.group(2).strip(),
                        "raised_round": round_})
    return out


def parse_questions(text: str) -> list:
    """Parse a `QUESTIONS:` block into a list of question strings."""
    out = []
    for ln in _extract_block(text, "QUESTIONS"):
        q = re.sub(r"^\s*[-*\d\.\)]+\s*", "", ln).strip()
        if q:
            out.append(q)
    return out


def _questions_gen(questions: list) -> str:
    return sha256_text("\n".join(questions))[:12]


def write_questions(run_dir: Path, questions: list) -> None:
    """Write questions.md + an answers.md template, tagged with a generation hash of
    THIS question set. A new/different question cycle regenerates the answers template
    (with a fresh sentinel) so a prior cycle's answers can't be silently reused
    (DESIGN §13). The same questions re-emitted (idempotent retry) won't clobber answers
    the human already provided for that generation."""
    gen = _questions_gen(questions)
    gen_marker = f"<!-- gen:{gen} -->"
    atomic_write_text(Path(run_dir) / "questions.md",
                      "\n".join([f"# Questions from the author {gen_marker}", ""] +
                                [f"{i+1}. {q}" for i, q in enumerate(questions)]) + "\n")
    answers_path = Path(run_dir) / "answers.md"
    same_gen = answers_path.exists() and gen_marker in read_text(answers_path)
    if not same_gen:
        tmpl = [f"# Answers {gen_marker}", "",
                "Answer each question below, then DELETE the sentinel line and run "
                "`orchestra resume`.", "", ANSWERS_SENTINEL, ""]
        for i, q in enumerate(questions):
            tmpl += [f"## {i+1}. {q}", "", "<your answer here>", ""]
        atomic_write_text(answers_path, "\n".join(tmpl) + "\n")


def answers_ready(run_dir: Path) -> bool:
    """True iff answers.md exists, the sentinel is deleted, has real content, AND its
    generation marker matches the current questions.md — so stale answers from a prior
    question cycle don't count (DESIGN §13)."""
    p = Path(run_dir) / "answers.md"
    if not p.exists():
        return False
    txt = read_text(p)
    if ANSWERS_SENTINEL in txt:
        return False
    # An unfilled placeholder means the human hasn't answered every question — deleting
    # only the sentinel must NOT count as filled (DESIGN §5/§13).
    if "<your answer here>" in txt:
        return False
    qp = Path(run_dir) / "questions.md"
    if qp.exists():
        m = re.search(r"<!-- gen:([0-9a-f]+) -->", read_text(qp))
        a_markers = set(re.findall(r"<!-- gen:([0-9a-f]+) -->", txt))
        # reject only if the answers carry a gen marker that doesn't match the current
        # questions (a stale prior cycle). No marker (a clean human overwrite) is fine.
        if m and a_markers and m.group(1) not in a_markers:
            return False
    # strip headings, the gen marker, and the template's instruction line, then require
    # real remaining content.
    stripped = re.sub(r"^#.*$", "", txt, flags=re.MULTILINE)
    stripped = re.sub(r"<!-- gen:[0-9a-f]+ -->", "", stripped)
    stripped = stripped.replace("Answer each question below, then DELETE the sentinel line "
                                "and run `orchestra resume`.", "")
    return len(stripped.strip()) > 0


# --------------------------------------------------------------------------- #
# LOG.md + notifications + telemetry (DESIGN §10/M4)
# --------------------------------------------------------------------------- #
def log_line(run_dir: Path, message: str) -> None:
    """Append a human-readable line to LOG.md (human-facing only; never fed to agents)."""
    p = Path(run_dir) / "LOG.md"
    prefix = "" if p.exists() else f"# Log — {Path(run_dir).name}\n\n"
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(f"{prefix}- **{now_iso()}** {message}\n")


def notify(run_dir: Path, message: str) -> None:
    """Surface an awaiting_human / stuck event (DESIGN §10 notification hook)."""
    banner = f"[orchestra:{Path(run_dir).name}] {message}"
    print("\n" + "=" * 8 + " NOTICE " + "=" * 8, file=sys.stderr)
    print(banner, file=sys.stderr)
    print("=" * 24 + "\n", file=sys.stderr)
    atomic_write_text(Path(run_dir) / "NOTIFY.txt", f"{now_iso()} {message}\n")


def add_tokens(state: dict, out: dict) -> None:
    state["tokens_spent"] = int(state.get("tokens_spent", 0)) + int(out.get("output_tokens", 0))


def append_history(state: dict, entry: dict) -> None:
    entry.setdefault("ts", now_iso())
    state["history"].append(entry)


class _CheckpointAbort(OrchestraError):
    """A budget/HALT checkpoint fired DURING a retry loop — abort and commit the
    terminal state it produced (DESIGN §9/§10.1 — enforce before each external call)."""
    def __init__(self, terminal_state: dict):
        super().__init__("checkpoint abort")
        self.terminal_state = terminal_state


def _attempt_hb(run_dir: Path, state: dict, cfg: dict | None = None):
    """An on_attempt callback: records the retry count + bumps updated_at as a heartbeat,
    AND (before each retry) re-runs the budget/HALT checkpoint so a wall-clock stop or a
    freshly-written HALT aborts the retry loop instead of waiting it out (§9/§10.1)."""
    def hb(i: int) -> None:
        state["attempts"] = int(i)  # 0 = first try, >=1 = retry
        try:
            save_state(run_dir, state)
        except (SchemaError, OSError):
            pass
        if cfg is not None:  # enforce the budget/HALT checkpoint BEFORE every attempt
            term = checkpoint(run_dir, load_state(run_dir), cfg)
            if term is not None:
                raise _CheckpointAbort(term)
    return hb


def review_count(state: dict, stage: str) -> int:
    """Round is derived from history: the count of reviewer entries for the stage
    (DESIGN §4) — never a stale counter."""
    return sum(1 for h in state["history"]
               if h["stage"] == stage and h["actor"] in ("codex", "claude") and "verdict" in h)


# --------------------------------------------------------------------------- #
# checkpoints — budget / wall-clock / monitor HALT (DESIGN §9/§10.1)
# --------------------------------------------------------------------------- #
def elapsed_seconds(state: dict) -> int:
    started = state.get("started_at") or state.get("created_at")
    try:
        t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0
    return int((datetime.now(timezone.utc) - t0).total_seconds())


def checkpoint(run_dir: Path, state: dict, cfg: dict) -> dict | None:
    """Safe-checkpoint guards run at the top of the loop and before each call.
    Returns a new terminal state to commit, or None to proceed."""
    # wall-clock stop
    wall = int(cfg["budget"].get("wall_clock_seconds", 0))
    if wall > 0 and elapsed_seconds(state) > wall:
        state["status"] = "stuck"
        state["stuck_reason"] = "budget_exceeded"
        state["current_step"] = None
        log_line(run_dir, f"WALL-CLOCK budget exceeded ({elapsed_seconds(state)}s > {wall}s) → stuck.")
        return state
    # monitor HALT — honored ONLY when ALL hold (else rejected + quarantined, §10.1):
    #   enforcing mode; valid JSON payload; ts EXACTLY binds the current assessment.json;
    #   that assessment independently re-passes (intervene/halt/confidence); and the
    #   orchestrator still corroborates a concerning trusted signal right now.
    halt = Path(run_dir) / "monitor" / "HALT"
    if halt.exists():
        def _reject(reason: str):
            try:
                halt.rename(Path(run_dir) / "monitor" / "HALT.rejected")
            except OSError:
                try:
                    halt.unlink()
                except OSError:
                    pass
            log_line(run_dir, f"monitor/HALT rejected and quarantined: {reason}")
            return None

        if cfg.get("monitor", {}).get("mode") != "enforcing":
            return _reject("monitor not in enforcing mode")
        try:
            payload = json.loads(read_text(halt))
        except (json.JSONDecodeError, ValueError):
            return _reject("HALT is not valid JSON (foreign/manual file)")
        halt_ts = payload.get("ts")
        ass_path = Path(run_dir) / "monitor" / "assessment.json"
        if not (isinstance(payload, dict) and halt_ts and ass_path.exists()):
            return _reject("HALT has no assessment binding")
        try:
            assess = json.loads(read_text(ass_path))
        except (json.JSONDecodeError, ValueError):
            return _reject("assessment.json unreadable")
        if not isinstance(assess, dict) or assess.get("ts") != halt_ts:
            return _reject("HALT does not bind the current assessment (stale/invalid)")
        # schema-validate the bound assessment before trusting any of its fields — a
        # poisoned/malformed assessment must be rejected, never crash the checkpoint.
        if validate_against(assess, SCHEMAS_DIR / "monitor.schema.json"):
            return _reject("bound assessment fails its schema")
        floor = float(cfg.get("monitor", {}).get("intervene_min_confidence", 0.8))
        try:
            conf = float(assess.get("confidence", 0))
        except (TypeError, ValueError):
            return _reject("bound assessment confidence is not numeric")
        if not (assess.get("assessment") == "intervene"
                and assess.get("recommended_action") == "halt" and conf >= floor):
            return _reject("bound assessment does not justify a halt")
        if not _halt_corroborated(run_dir, state, cfg):
            return _reject("no corroborating trusted signal at checkpoint")
        # CONSUME the HALT so the same file can't re-stick the run after human recovery (§8)
        try:
            halt.rename(Path(run_dir) / "monitor" / "HALT.honored")
        except OSError:
            try:
                halt.unlink()
            except OSError:
                pass
        rationale = payload.get("rationale", "")
        state["status"] = "stuck"
        state["stuck_reason"] = "monitor"
        state["current_step"] = None
        append_history(state, {"stage": state["stage"], "round": state["round"],
                               "actor": "orchestrator", "note": f"monitor HALT: {rationale[:160]}"})
        log_line(run_dir, f"MONITOR HALT honored (consumed) → stuck(monitor). Rationale: {rationale[:200]}")
        notify(run_dir, f"Monitor halted the run: {rationale[:160]}")
        return state
    return None


# --------------------------------------------------------------------------- #
# the supervisory monitor (DESIGN §10.1)
# --------------------------------------------------------------------------- #
def _verdicts_digest(run_dir: Path, state: dict) -> str:
    """A bounded, structured per-round digest the monitor can reason about: decision,
    confidence, severity-weighted blocker score, and the recurring content keys (so it
    can see a semantic loop the content metric also tracks) — read from the validated
    verdict files, not just the history decision (DESIGN §10.1)."""
    rev_dir = Path(run_dir) / "reviews"
    rows = []
    for h in state["history"]:
        if "verdict" not in h:
            continue
        letter = STAGE_LETTERS.get(h["stage"], "?")
        v = _load_round_verdict(run_dir, letter, h["round"])
        tag = f"{h['stage']} r{h['round']} [{h.get('actor')}{'/fallback' if h.get('fallback') else ''}]"
        if v:
            bl = v.get("blocking_issues", [])
            keys = ",".join(sorted(content_key(b) for b in bl))[:200]
            rows.append(f"- {tag} → {v.get('decision')} conf={v.get('confidence')} "
                        f"blockers={len(bl)} score={blocking_score(bl)} keys=[{keys}]")
        else:
            rows.append(f"- {tag} → {h['verdict']}")
    return "\n".join(rows) or "(no reviews yet)"


def _timings_digest(state: dict) -> str:
    return (f"elapsed_seconds={elapsed_seconds(state)}\n"
            f"round={state['round']} stage={state['stage']} status={state['status']}\n"
            f"attempts={state.get('attempts', 0)} tokens_spent={state.get('tokens_spent', 0)}\n"
            f"updated_at={state['updated_at']} started_at={state.get('started_at')}\n"
            f"resolved_ledger={len(state.get('resolved_ledger', []))} "
            f"open_disputes={len(state.get('open_disputes', []))}")


def _prior_reports(run_dir: Path) -> str:
    mon = Path(run_dir) / "monitor"
    if not mon.exists():
        return "(none)"
    reps = sorted(mon.glob("report-*.md"))
    if not reps:
        return "(none)"
    return "\n\n".join(read_text(r)[:1500] for r in reps[-2:])


def run_monitor(run_dir: Path, cfg: dict, *, dry_run: bool = False) -> dict | None:
    """Fresh read-only Claude overseer judging run HEALTH (DESIGN §10.1). Reads only
    structured trusted telemetry (STATE.json/verdicts/timings/prior reports), NOT
    LOG.md or raw artifacts. Writes only under monitor/."""
    mon_cfg = cfg.get("monitor", {})
    if mon_cfg.get("mode", "advisory") == "off" or not mon_cfg.get("enabled", True):
        return None
    state = load_state(run_dir)
    tmpl = read_text(PROMPTS_DIR / "monitor" / "health-check.md")
    prompt = render_prompt(
        tmpl,
        state_json=Untrusted(json.dumps(_state_for_monitor(state), indent=2)),
        verdicts_digest=Untrusted(_verdicts_digest(run_dir, state)),
        timings=_timings_digest(state),
        prior_reports=Untrusted(_prior_reports(run_dir)),
    )
    mon_dir = Path(run_dir) / "monitor"
    mon_dir.mkdir(parents=True, exist_ok=True)
    if dry_run:
        cmd = build_claude_cmd(cfg, mode="monitor")
        print(f"[dry-run] MONITOR: {_fmt_cmd(cmd, stdin_file='<health-check>')}")
        return None
    instr = ("\n\nOutput ONLY a JSON object conforming to schemas/monitor.schema.json "
             "(assessment, recommended_action, progressing, summary, findings, ts; "
             "rationale required for halt). No prose outside JSON, no fences.\n")
    mcfg = dict(cfg)
    if mon_cfg.get("model"):
        mcfg = _deep_merge(cfg, {"models": {"author": mon_cfg["model"]}})
    try:
        out = claude_generate(prompt + instr, cfg=mcfg, mode="monitor")
    except OrchestraError as e:
        _mon_log(run_dir, f"monitor invocation failed (non-fatal): {e}")
        return None
    text = out["result"].strip()
    obj = _extract_json_object(text)
    if obj is None:
        return None
    try:
        assess = json.loads(obj)
    except (json.JSONDecodeError, ValueError):
        return None
    assess["ts"] = now_iso()
    # OVERWRITE observed from trusted state — never let the model supply its own
    # telemetry (fabricated `observed` could otherwise feign progress/justify a halt).
    assess["observed"] = {"stage": state["stage"], "round": state["round"],
                          "status": state["status"], "tokens_spent": state.get("tokens_spent", 0),
                          "elapsed_seconds": elapsed_seconds(state)}
    # Light normalization so a minor formatting slip in advisory findings doesn't waste
    # the call. This does NOT touch the safety-critical halt gate (assessment/rationale/
    # confidence are never defaulted), only optional finding metadata.
    for f in assess.get("findings", []) or []:
        if isinstance(f, dict):
            f.setdefault("severity", "info")
            f.setdefault("title", "(finding)")
            f.setdefault("detail", "")
    # Decide the HALT verdict NOW (before persisting) so assessment.json's halt_requested
    # truthfully mirrors whether monitor/HALT is written this cycle (§10.1 / schema).
    corro = _halt_corroborated(run_dir, state, cfg)
    will_halt = bool(mon_cfg.get("mode") == "enforcing" and assess.get("assessment") == "intervene"
                     and assess.get("recommended_action") == "halt"
                     and float(assess.get("confidence", 0) or 0) >= float(mon_cfg.get("intervene_min_confidence", 0.8))
                     and (assess.get("rationale") or "").strip() and corro)
    assess["halt_requested"] = will_halt
    errs = validate_against(assess, SCHEMAS_DIR / "monitor.schema.json")
    if errs:
        _mon_log(run_dir, f"monitor assessment invalid, ignoring: {errs[0]}")
        return None
    # The monitor is single-writer-safe: it writes ONLY under monitor/ (it takes no run
    # lock, DESIGN §10.1). Human-facing LOG.md/NOTIFY.txt surfacing is left to the
    # lock-holding orchestrator (see maybe_run_monitor).
    atomic_write_text(mon_dir / "assessment.json", json.dumps(assess, indent=2, allow_nan=False) + "\n")
    n = len(list(mon_dir.glob("report-*.md"))) + 1
    if assess["assessment"] in ("warning", "intervene"):
        atomic_write_text(mon_dir / f"report-{n:02d}.md",
                          f"# Monitor report {n} — {assess['ts']}\n\n"
                          f"**Assessment:** {assess['assessment']} / "
                          f"action: {assess['recommended_action']} / "
                          f"progressing: {assess.get('progressing')}\n\n"
                          f"{assess.get('summary', '')}\n\n"
                          f"Rationale: {assess.get('rationale', '(n/a)')}\n")
    # Write the HALT (the gate was decided above as will_halt). The model's prose alone
    # can never halt — it required orchestrator corroboration from trusted telemetry (§10.1).
    if will_halt:
        # bind the HALT to THIS assessment so checkpoint can reject a stale/foreign HALT
        payload = {"ts": assess["ts"], "mode": "enforcing",
                   "rationale": assess.get("rationale", ""),
                   "corroboration": corro, "observed": assess["observed"]}
        atomic_write_text(mon_dir / "HALT", json.dumps(payload, indent=2, allow_nan=False) + "\n")
        _mon_log(run_dir, f"monitor (enforcing) wrote monitor/HALT (corroborated: {corro}).")
    elif mon_cfg.get("mode") == "enforcing" and assess.get("recommended_action") == "halt":
        _mon_log(run_dir, "monitor recommended halt but the orchestrator found NO corroborating "
                          "trusted signal (or below confidence floor) — HALT withheld.")
    return assess


def _halt_corroborated(run_dir: Path, state: dict, cfg: dict) -> list:
    """Independently confirm a halt-worthy condition from TRUSTED state — a LACK OF
    PROGRESS, not merely elapsed work (§10.1). Returns corroborating signals (empty ⇒
    not corroborated). A healthy run that's simply deep into its rounds is NOT halted;
    there must be a demonstrated stall."""
    reasons = []
    excl = excluded_keys(state)  # accepted/cleared keys don't count toward a "loop"

    # severity-weighted blocking score across the last few rounds — flat/rising = stalled
    letter = STAGE_LETTERS.get(state["stage"], "?")
    scores = []
    key_rounds: dict = {}
    for h in state.get("history", []):
        if h.get("stage") == state["stage"] and "verdict" in h:
            v = _load_round_verdict(run_dir, letter, h["round"])
            if v is not None:
                scores.append((h["round"], blocking_score(v.get("blocking_issues", []), excl)))
                for b in v.get("blocking_issues", []):
                    k = content_key(b)
                    if k not in excl:
                        key_rounds.setdefault(k, set()).add(h["round"])
    recent = [s for _, s in scores[-3:]]
    stalled = len(recent) >= 3 and recent[-1] >= recent[0] and recent[-1] > 0
    if stalled:
        reasons.append(f"severity-weighted blocker score not decreasing over last "
                       f"{len(recent)} rounds ({recent})")
    recurring = [k for k, rs in key_rounds.items() if len(rs) >= 3]
    if recurring:
        reasons.append(f"{len(recurring)} active blocking key(s) recurred across >=3 rounds")

    # a genuinely hung call: stale heartbeat while in-flight
    in_flight = state["status"] in ("authoring", "authored", "reviewing", "deciding")
    soft = int(cfg.get("monitor", {}).get("stage_soft_timeout", 0) or 0)
    if in_flight and soft > 0:
        try:
            upd = datetime.fromisoformat(state["updated_at"].replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - upd).total_seconds() > 2 * soft:
                reasons.append(f"heartbeat stale > 2x soft timeout while in-flight ({soft}s)")
        except (ValueError, KeyError):
            pass
    return reasons


def _mon_log(run_dir: Path, message: str) -> None:
    """Monitor-scoped log — stays UNDER monitor/ so the lock-free monitor never writes
    a shared run-root file (single-writer safety, DESIGN §10.1)."""
    mon = Path(run_dir) / "monitor"
    mon.mkdir(parents=True, exist_ok=True)
    with open(mon / "monitor.log", "a", encoding="utf-8") as fh:
        fh.write(f"- {now_iso()} {message}\n")


def _state_for_monitor(state: dict) -> dict:
    """A trusted-telemetry projection of STATE for the monitor (no raw artifacts)."""
    keep = ("run_id", "stage", "status", "round", "gate", "waiting_for", "stuck_reason",
            "attempts", "tokens_spent", "updated_at", "started_at", "created_at")
    s = {k: state.get(k) for k in keep}
    s["resolved_ledger_keys"] = [e["key"] for e in state.get("resolved_ledger", [])]
    s["open_disputes"] = state.get("open_disputes", [])
    lv = state.get("last_verdict") or {}
    s["last_verdict_digest"] = {
        "decision": lv.get("decision"), "confidence": lv.get("confidence"),
        "n_blocking": len(lv.get("blocking_issues", [])),
        "severities": [b.get("severity") for b in lv.get("blocking_issues", [])],
    }
    s["history"] = [{k: h.get(k) for k in ("stage", "round", "actor", "verdict", "ts", "fallback")}
                    for h in state.get("history", [])]
    return s


def _stage_elapsed_seconds(run_dir: Path) -> int:
    """Wall-clock seconds since the CURRENT stage began (earliest history ts for it)."""
    state = load_state(run_dir)
    stage = state["stage"]
    stamps = [h["ts"] for h in state.get("history", []) if h.get("stage") == stage and h.get("ts")]
    ref = min(stamps) if stamps else state.get("started_at") or state.get("created_at")
    try:
        t0 = datetime.fromisoformat(ref.replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - t0).total_seconds())
    except (ValueError, AttributeError):
        return 0


def maybe_run_monitor(run_dir: Path, cfg: dict, *, force: bool = False, dry_run: bool = False) -> None:
    """Run the monitor at a checkpoint if due (interval / soft-timeout) or forced."""
    mon_cfg = cfg.get("monitor", {})
    if mon_cfg.get("mode", "advisory") == "off" or not mon_cfg.get("enabled", True):
        return
    if dry_run:
        run_monitor(run_dir, cfg, dry_run=True)  # print the command only
        return
    stamp = Path(run_dir) / "monitor" / ".last_run"
    due = force
    # Trigger: the current stage has run past its SOFT time-budget (§9/§10.1) — wake the
    # monitor to judge benign-slow vs wedged (it does not kill; the hard caps do that).
    soft = int(mon_cfg.get("stage_soft_timeout", 0) or 0)
    if not due and soft > 0 and _stage_elapsed_seconds(run_dir) > soft:
        due = True
    if not due and stamp.exists():
        try:
            last = datetime.fromisoformat(read_text(stamp).strip().replace("Z", "+00:00"))
            due = (datetime.now(timezone.utc) - last).total_seconds() >= int(mon_cfg.get("interval_seconds", 300))
        except (ValueError, OSError):
            due = True
    elif not due:
        due = True  # first time
    if not due:
        return
    (Path(run_dir) / "monitor").mkdir(parents=True, exist_ok=True)
    atomic_write_text(stamp, now_iso() + "\n")
    assess = run_monitor(run_dir, cfg)
    # We hold the run lock here, so the ORCHESTRATOR (not the monitor) surfaces any
    # warning to the shared LOG.md/NOTIFY.txt — keeping the monitor monitor/-only.
    if assess and assess.get("assessment") in ("warning", "intervene"):
        log_line(run_dir, f"monitor: {assess['assessment']} — {assess.get('summary', '')[:120]}")
        notify(run_dir, f"monitor {assess['assessment']}: {assess.get('summary', '')[:140]} "
                        f"(see monitor/)")


# --------------------------------------------------------------------------- #
# Stage C — worktree, test gate, diff (DESIGN §2/§6)
# --------------------------------------------------------------------------- #
def _git(args: list, cwd: Path, check=True) -> CallResult:
    res = run_subprocess(["git"] + args, cwd=cwd, timeout=120)
    if check and res.returncode != 0:
        raise OrchestraError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res


def setup_worktree(run_dir: Path, cfg: dict, state: dict, *, dry_run: bool = False) -> dict:
    """Greenfield: git init 30-impl/ with an empty base commit. Brownfield: git
    worktree add from the user's repo, store a pointer (DESIGN §6). Returns
    {'worktree', 'base_commit'}."""
    target = state["config"].get("target", cfg["target"])
    mode = target.get("mode", "greenfield")
    if dry_run:
        wt = (Path(target["worktree_path"]) if mode == "brownfield" and target.get("worktree_path")
              else Path(run_dir) / WORKTREE_DIRNAME)
        if mode == "brownfield":
            print(f"[dry-run] GIT: git -C {target.get('repo')} worktree add -b orchestra/{state['run_id']} {wt} <base>")
        else:
            print(f"[dry-run] GIT: git init -b main {wt} && git commit --allow-empty -m 'orchestra: empty base'")
        return {"worktree": str(wt), "base_commit": "<dry-run-base>", "base_branch": "main", "mode": mode}
    if mode == "brownfield":
        repo_s = (target.get("repo") or "").strip()
        wt_s = (target.get("worktree_path") or "").strip()
        if not repo_s or not wt_s:
            raise OrchestraError("brownfield requires non-empty [target].repo AND "
                                 "[target].worktree_path (refusing to default to cwd — "
                                 "Stage C git reset/clean would wipe it)")
        repo = Path(repo_s).expanduser().resolve()
        wt = Path(wt_s).expanduser().resolve()
        if not (repo / ".git").exists():
            raise OrchestraError(f"[target].repo is not a git repository: {repo}")
        # Refuse to operate on any protected/ambient directory — Stage C edits, commits,
        # `git reset --hard` and `git clean -fdx` the worktree, so a wrong path is
        # destructive (CRITICAL). The worktree must be a dedicated path the user controls.
        protected = {ROOT.resolve(), repo, Path(run_dir).resolve(), Path.cwd().resolve(),
                     Path.home().resolve(), Path(wt.anchor)}
        if wt in protected:
            raise OrchestraError(f"refusing brownfield worktree at a protected path: {wt}")
        for p in (ROOT.resolve(), repo, Path(run_dir).resolve()):
            if p == wt or p in wt.parents or wt in p.parents:
                raise OrchestraError(f"brownfield worktree {wt} overlaps protected dir {p}")
        branch = f"orchestra/{state['run_id']}"
        base_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip() or "main"
        # An existing path must be a REGISTERED worktree of THIS repo — proving ownership,
        # so we never git-reset a foreign/unexpected directory.
        listing = _git(["worktree", "list", "--porcelain"], repo, check=False).stdout
        registered = {Path(ln.split(" ", 1)[1]).resolve()
                      for ln in listing.splitlines() if ln.startswith("worktree ")}
        if wt.exists():
            # Only an idempotent recovery of OUR worktree is accepted: registered with the
            # repo, on the expected orchestra/<run> branch, and clean. An arbitrary
            # pre-existing worktree (foreign branch / dirty index) is refused, else the
            # next `git add -A` would commit unrelated user changes (§6).
            if wt not in registered:
                raise OrchestraError(
                    f"{wt} exists but is not a registered git worktree of {repo}; refusing")
            cur_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt).stdout.strip()
            if cur_branch != branch:
                raise OrchestraError(
                    f"{wt} is on branch '{cur_branch}', not this run's '{branch}'; refusing "
                    f"(point [target].worktree_path at a fresh path)")
            dirty = _git(["status", "--porcelain"], wt, check=False).stdout.strip()
            if dirty:
                raise OrchestraError(f"{wt} has uncommitted changes; refusing to drive a dirty "
                                     f"worktree (commit/stash them first)")
        else:
            _git(["worktree", "add", "-b", branch, str(wt), base_branch], repo)
        base_commit = _git(["rev-parse", "HEAD"], wt).stdout.strip()
        atomic_write_text(Path(run_dir) / WORKTREE_DIRNAME,
                          f"# Stage C worktree pointer (brownfield)\n{wt}\n")
        return {"worktree": str(wt), "base_commit": base_commit, "base_branch": base_branch,
                "mode": "brownfield"}
    # greenfield: default in-run-dir (runs/<run>/30-impl, per §3's one-folder layout). The
    # edit-mode author can't use --tools "" (it needs Edit/Write), so its read-containment
    # rests on its cwd, NOT on the toolset (§6.1). When that default cwd is under the run
    # dir, a relative "../reviews/…" reaches the blackboard — so for MECHANICAL independence
    # an operator can set [target].worktree_path to an EXTERNAL path (a pointer is stored
    # under runs/, like brownfield). See DESIGN §14.
    ext = (target.get("worktree_path") or "").strip()
    if ext:
        wt = Path(ext).expanduser().resolve()
        protected = {ROOT.resolve(), Path(run_dir).resolve(), Path.cwd().resolve()}
        if wt in protected or any(p == wt or p in wt.parents for p in (Path(run_dir).resolve(),)):
            raise OrchestraError(f"refusing greenfield worktree under the run dir: {wt}")
        wt.mkdir(parents=True, exist_ok=True)
        atomic_write_text(Path(run_dir) / WORKTREE_DIRNAME,
                          f"# Stage C worktree pointer (greenfield, external)\n{wt}\n")
    else:
        wt = Path(run_dir) / WORKTREE_DIRNAME
        wt.mkdir(parents=True, exist_ok=True)
    if not (wt / ".git").exists():
        _git(["init", "-b", "main"], wt)
        _git(["config", "user.email", "orchestra@local"], wt)
        _git(["config", "user.name", "orchestra"], wt)
        _git(["commit", "--allow-empty", "-m", "orchestra: empty base"], wt)
    base_commit = _git(["rev-parse", "HEAD"], wt).stdout.strip()
    return {"worktree": str(wt), "base_commit": base_commit, "base_branch": "main",
            "mode": "greenfield"}


def worktree_diff(wt: Path, base_commit: str) -> str:
    """Cumulative diff against the base commit, including untracked files."""
    _git(["add", "-N", "."], wt, check=False)  # intent-to-add so untracked show in diff
    res = _git(["diff", base_commit], wt, check=False)
    return res.stdout


def commit_round(wt: Path, round_: int) -> str:
    _git(["add", "-A"], wt)  # a staging failure must not look like an empty round
    # --allow-empty: a genuinely empty round still commits, so ANY non-zero exit is a
    # REAL failure (hook rejection, index/auth/config error) — never inferred away from
    # post-hook index state, which a hook could have reset (§8).
    res = run_subprocess(["git", "commit", "--allow-empty", "-m", f"orchestra: round {round_}"],
                         cwd=wt, timeout=60)
    if res.returncode != 0:
        raise OrchestraError(f"git commit failed (round {round_}): "
                             f"{(res.stderr or res.stdout).strip()[:300]}")
    return _git(["rev-parse", "HEAD"], wt).stdout.strip()


def reset_worktree(wt: Path, base_commit: str) -> None:
    """Stage C resume: reset to the round's base commit (DESIGN §8). Fails LOUDLY —
    silently proceeding on a dirty/wrong tree would re-author on corrupt state."""
    # A real git worktree ALWAYS has its OWN .git (dir for greenfield, file/gitdir-pointer
    # for a `git worktree add`). If it's missing, the path is wrong/corrupt — refuse, never
    # fall back to a parent repo's .git (a `git reset --hard` there would wipe the PARENT
    # repository's working tree).
    if not (Path(wt) / ".git").exists():
        raise OrchestraError(f"refusing to reset {wt}: not a git worktree (no .git)")
    _git(["reset", "--hard", base_commit], wt)
    _git(["clean", "-fd"], wt)  # -fd (NOT -x): keep ignored files like .venv out of scope


def _run_test_command(cmd: str, cwd: Path, timeout: int, sandbox_command: str = "") -> CallResult:
    """Run a model-authored test command under least privilege (§2/§10). Always: a
    minimal environment + its own process group (killed on timeout). For real OS
    isolation, the operator sets [stage_c].sandbox_command (e.g. `firejail --net=none
    --private`, `sandbox-exec -f profile.sb`, or `docker run ...`) — prepended to the
    invocation; a stdlib tool cannot portably impose a container/seccomp itself, so
    untrusted Stage C code should run orchestra inside a sandbox OR set this."""
    import shlex
    import signal
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(cwd),  # temp HOME = the worktree, not the user's real HOME
           "LANG": os.environ.get("LANG", "C.UTF-8"),
           "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
    argv = (shlex.split(sandbox_command) if sandbox_command else []) + ["/bin/sh", "-c", cmd]
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            out, err = proc.communicate(timeout=timeout)
            return CallResult(proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            out, err = proc.communicate()
            return CallResult(124, out or "", (err or "") + "\n[timeout: process group killed]",
                              timed_out=True)
    except OSError as e:
        return CallResult(127, "", f"failed to launch test command: {e}")


def stage_c_tests_green(run_dir: Path, state: dict, cfg: dict, round_: int | None = None) -> tuple:
    """Return (green: bool, reason). When a test_command IS configured, the gate FAILS
    CLOSED: it is green ONLY if the sidecar has a matching current-round result with
    exit code exactly 0. Missing / stale / non-zero telemetry all count as NOT green
    (§2/§10), so neither absent telemetry nor a human approve can waive the gate."""
    round_ = state["round"] if round_ is None else round_
    sc = load_stage_c(run_dir)
    if sc.get("dry_run"):
        return True, "dry-run"
    # 1) NO-DRIFT (always, even with no test_command): the worktree must still BE exactly
    # the reviewed commit, in a valid repo — so the presented diff == the reviewed diff and
    # a hand-edit during awaiting_human can't sneak in unreviewed code (§2/§3). Fail closed.
    rc = sc.get("reviewed_commit")
    wt = Path(sc.get("worktree", ""))
    if not rc:
        return False, "no reviewed commit recorded for this round"
    if not (wt / ".git").exists():
        return False, "worktree git repository missing/unverifiable"
    head = _git(["rev-parse", "HEAD"], wt, check=False)
    if head.returncode != 0:
        return False, "worktree HEAD unreadable"
    if head.stdout.strip() != rc:
        return False, f"worktree HEAD {head.stdout.strip()[:8]} != reviewed commit {rc[:8]} (drifted)"
    if _git(["status", "--porcelain"], wt, check=False).stdout.strip():
        return False, "worktree has uncommitted changes since the reviewed commit (drifted)"
    # 2) EXECUTED TESTS (when configured): green = current-round exit 0 on the reviewed commit.
    sc_cfg = state["config"].get("stage_c", cfg["stage_c"])
    if not (sc_cfg.get("test_command") or "").strip():
        return True, "no test_command configured; worktree matches reviewed commit"
    if not sc.get("test_configured"):
        return False, "tests configured but never executed for this run"
    if sc.get("last_test_round") != round_:
        return False, f"test telemetry is for round {sc.get('last_test_round')}, not {round_} (stale)"
    if sc.get("last_test_exit") != 0:
        return False, f"executed tests exited {sc.get('last_test_exit')} (not 0)"
    if sc.get("last_test_commit") != rc:
        return False, "test telemetry is bound to a different commit than the reviewed one"
    return True, "executed tests exited 0 on the reviewed commit"


def run_test_gate(run_dir: Path, cfg: dict, state: dict, wt: Path, round_: int) -> str:
    """The orchestrator runs [stage_c].test_command (operator-config ONLY) in the
    worktree under a timeout, capturing exit code + output as a TRUSTED field
    (DESIGN §2/§10). Persists the STRUCTURED exit code to the sidecar so the decide
    step can mechanically gate APPROVE — "tests pass" is never inferred from prose.
    Writes reviews/C-NN-tests.txt."""
    cmd = (state["config"].get("stage_c", cfg["stage_c"]).get("test_command") or "").strip()
    tests_path = Path(run_dir) / "reviews" / f"C-{round_:02d}-tests.txt"
    sc = load_stage_c(run_dir)
    if not cmd:
        body = ("No executed test gate configured for this run (operator opted out via empty "
                "[stage_c].test_command). Evaluate the diff on inspection; absence of an "
                "executed gate is itself a risk to note.")
        atomic_write_text(tests_path, body + "\n")
        sc.update({"last_test_round": round_, "last_test_exit": None, "test_configured": False})
        save_stage_c(run_dir, sc)
        return body
    sc_cfg = state["config"].get("stage_c", cfg["stage_c"])
    timeout = int(sc_cfg.get("test_timeout_seconds", 600))
    tested_commit = _git(["rev-parse", "HEAD"], wt, check=False).stdout.strip()  # the round commit
    res = _run_test_command(cmd, wt, timeout, sc_cfg.get("sandbox_command", ""))
    out = (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")
    body = (f"$ {cmd}\n[tested commit: {tested_commit[:12]}]\n"
            f"[exit code: {res.returncode}{' (TIMEOUT)' if res.timed_out else ''}]\n\n"
            f"{out[-6000:]}")
    atomic_write_text(tests_path, body + "\n")
    # Bind the telemetry to the exact commit tested (so a later worktree drift is detected).
    sc.update({"last_test_round": round_, "last_test_exit": int(res.returncode),
               "test_configured": True, "last_test_commit": tested_commit, "dry_run": False})
    save_stage_c(run_dir, sc)
    return body


# --------------------------------------------------------------------------- #
# loop — author / review / decide (DESIGN §4) + gates (DESIGN §5)
# --------------------------------------------------------------------------- #
def artifact_paths(run_dir: Path, stage: str, round_: int) -> tuple:
    base = ARTIFACT_BASENAME[stage]
    if stage == "implementation":
        snap = Path(run_dir) / f"{base}.r{round_}.diff"
        current = Path(run_dir) / f"{base}.diff"
    else:
        snap = Path(run_dir) / f"{base}.r{round_}.md"
        current = Path(run_dir) / f"{base}.md"
    return snap, current


def upstream_plan_text(run_dir: Path, stage: str) -> str:
    if stage == "impl_plan":
        p = Path(run_dir) / "10-highlevel-plan.md"
    elif stage == "implementation":
        p = Path(run_dir) / "20-impl-plan.md"
    else:
        return ""
    return read_text(p) if p.exists() else ""


def _latest_human_note(run_dir: Path, stage: str) -> str:
    notes = sorted((Path(run_dir) / "reviews").glob(f"{STAGE_LETTERS[stage]}-*-human.md"))
    return read_text(notes[-1]) if notes else ""


def _author_extra_context(run_dir: Path, state: dict) -> str:
    """Trusted (tier-1/tier-2) context appended to EVERY author prompt: the human's
    answers, the latest human review note, and (Stage C revise) the prior round's
    executed test output — content the design says the next author must see
    (§2/§3/§5). Trusted, so not nonce-fenced."""
    stage = state["stage"]
    parts = []
    # Accepted deviations (conceded won't-fix items, §5) — tier-3 reviewer-derived, so
    # nonce-fenced. Tell the author NOT to re-litigate or re-introduce work for these.
    devs = state.get("accepted_deviations", [])
    if devs:
        parts.append(render_prompt(
            "Already-accepted deviations (won't-fix; do NOT re-fix or re-raise these):\n"
            "<accepted_deviations untrusted=\"true\">\n{{d}}\n</accepted_deviations>",
            d=Untrusted(render_deviations(devs))))
    if (Path(run_dir) / "answers.md").exists() and answers_ready(run_dir):
        # answers.md interleaves the author's own echoed questions (tier-3) with the
        # human's answers (tier-2). Neutralize the echoed `## N. <question>` headings so
        # only genuinely human-authored text wears the trust="spec" label (and can't
        # escape the fixed tag with `</answers>`).
        raw = read_text(Path(run_dir) / "answers.md")
        human = re.sub(r"(?m)^(##\s*\d+\.).*$", r"\1 (answer)", raw)  # drop echoed question text
        human = re.sub(r"<!--.*?-->", "", human, flags=re.DOTALL)
        parts.append("The human's answers to the author's questions (tier-2 spec — binding):\n"
                     "<answers trust=\"spec\">\n" + human + "\n</answers>")
    note = _latest_human_note(run_dir, stage)
    if note.strip():
        parts.append("Human review note for this round (tier-2 spec):\n"
                     "<human_note trust=\"spec\">\n" + note + "\n</human_note>")
    # carried-forward nits from the prior stage, for the round-0 author only (§4.1).
    # These are REVIEWER-derived (tier-3) — nonce-fence them like any agent content (§3),
    # not the trusted human/orchestrator context above.
    if state.get("current_artifact") is None and state.get("round", 0) == 0:
        carried = load_carried(run_dir)
        cnits = carried.get("nits") or []
        if cnits and carried.get("from_stage") and carried["from_stage"] != stage:
            lines = "\n".join(f"- {n.get('title', '')}: {n.get('detail', '')}" for n in cnits)
            fenced = render_prompt(
                "Carried-forward non-blocking suggestions from the prior stage "
                "(address where natural; they do not block):\n"
                "<carried_nits untrusted=\"true\">\n{{nits}}\n</carried_nits>",
                nits=Untrusted(lines))
            parts.append(fenced)
    if stage == "implementation" and state.get("round", 0) >= 1:
        # the diff is worktree-confined and the author can't read reviews/; hand it the
        # prior round's executed-test output (§2 revise context). The exit code is the
        # trusted signal; the captured output is author-written → nonce-fence it (§3).
        prev = state["round"]
        tp = Path(run_dir) / "reviews" / f"C-{prev:02d}-tests.txt"
        if not tp.exists():
            cands = sorted((Path(run_dir) / "reviews").glob("C-*-tests.txt"))
            tp = cands[-1] if cands else tp
        if tp.exists():
            exit_code = load_stage_c(run_dir).get("last_test_exit")
            parts.append(render_prompt(
                f"Previous round's executed tests — trusted exit code: "
                f"{exit_code if exit_code is not None else 'n/a'} (0=pass). Fix any failures. "
                f"The captured output below is your own test code's output (untrusted):\n"
                "<test_output untrusted=\"true\">\n{{t}}\n</test_output>",
                t=Untrusted(read_text(tp))))
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def build_author_prompt(run_dir: Path, state: dict, cfg: dict) -> str:
    stage = state["stage"]
    brief = read_text(Path(run_dir) / "00-brief.md")
    round_ = state["round"]
    label = ARTIFACT_LABEL[stage]
    base = None
    if state.get("current_artifact") is None and round_ == 0:
        # round-0 draft
        if stage == "impl_plan":
            tmpl = read_text(PROMPTS_DIR / "claude" / "impl-plan.md")
            base = render_prompt(tmpl, brief=brief, highlevel_plan=upstream_plan_text(run_dir, stage))
        elif stage == "implementation":
            tmpl = read_text(PROMPTS_DIR / "claude" / "implement.md")
            base = render_prompt(tmpl, brief=brief, impl_plan=upstream_plan_text(run_dir, stage))
        elif stage == "highlevel":
            tmpl = read_text(PROMPTS_DIR / "claude" / "highlevel-plan.md")
            ctx = {"run_id": state["run_id"], "brief": brief}
            base = render_prompt(tmpl, **{k: v for k, v in ctx.items()
                                          if k in _placeholder_names(strip_comments(tmpl))})
    if base is None:
        # revise round
        prev_text = ""
        ca = state.get("current_artifact")
        if ca and Path(ca["path"]).exists():
            prev_text = read_text(Path(ca["path"]))
        # EXCLUDE conceded blockers (accepted_deviations) from the "must resolve" list —
        # the won't-fix ledger means the author must NOT be told to re-fix them (§5/§7).
        accepted_keys = {e["key"] for e in state.get("accepted_deviations", [])}
        blockers = [b for b in (state.get("last_verdict") or {}).get("blocking_issues", [])
                    if content_key(b) not in accepted_keys]
        tmpl = read_text(PROMPTS_DIR / "claude" / "revise.md")
        base = render_prompt(
            tmpl,
            artifact_label=label,
            round=str(round_),
            brief=brief,
            upstream_plan=upstream_plan_text(run_dir, stage) or "(none — this is the top-level plan)",
            prev_artifact=Untrusted(prev_text),
            blocking_issues=Untrusted(format_blocking_issues(blockers)),
            resolved_ledger=Untrusted(render_ledger(state.get("resolved_ledger", []))),
        )
    return base + _author_extra_context(run_dir, state)


def build_review_prompt(run_dir: Path, state: dict, cfg: dict, *, artifact_text: str,
                        diff: str = "", test_results: str = "") -> str:
    stage = state["stage"]
    brief = read_text(Path(run_dir) / "00-brief.md")
    prev_round = review_count(state, stage)  # previous reviews
    prev_verdict = _load_round_verdict(run_dir, STAGE_LETTERS[stage], prev_round)
    prior = format_prior_issues(prev_verdict)
    ledger = render_ledger(state.get("resolved_ledger", []))
    disputes = render_disputes(state.get("open_disputes", []))
    devs = render_deviations(state.get("accepted_deviations", []))
    common = dict(
        prior_issues=(Untrusted(prior) if prior else ""),
        resolved_ledger=Untrusted(ledger),
        open_disputes=Untrusted(disputes),
        # accepted_deviations text is tier-3 (autonomously conceded author/reviewer
        # prose, no human gate) → nonce-fence it like any agent content (DESIGN §3).
        accepted_deviations=Untrusted(devs),
    )
    if stage == "highlevel":
        tmpl = read_text(PROMPTS_DIR / "codex" / "review-highlevel-plan.md")
        return render_prompt(tmpl, brief=brief, highlevel_plan=Untrusted(artifact_text), **common)
    if stage == "impl_plan":
        tmpl = read_text(PROMPTS_DIR / "codex" / "review-impl-plan.md")
        return render_prompt(tmpl, brief=brief,
                             highlevel_plan=upstream_plan_text(run_dir, stage),
                             impl_plan=Untrusted(artifact_text), **common)
    # implementation: the exit code is the trusted signal (from the sidecar); the captured
    # output is author-influenceable → nonce-fence it (§3).
    tmpl = read_text(PROMPTS_DIR / "codex" / "review-implementation.md")
    exit_code = load_stage_c(run_dir).get("last_test_exit")
    test_exit = (str(exit_code) if exit_code is not None
                 else "not available (no executed test gate configured)")
    return render_prompt(tmpl, impl_plan=upstream_plan_text(run_dir, stage),
                         test_exit=test_exit,
                         test_results=Untrusted(test_results or "not available"),
                         diff=Untrusted(diff or "(no diff produced)"), **common)


def _load_round_verdict(run_dir: Path, letter: str, round_: int) -> dict | None:
    if round_ < 1:
        return None
    p = Path(run_dir) / "reviews" / f"{letter}-{round_:02d}-verdict.json"
    if p.exists():
        try:
            return json.loads(read_text(p))
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def commit_artifact(run_dir: Path, state: dict, stage: str, round_: int, text: str) -> dict:
    """Snapshot the artifact atomically, write the 'current' copy, return
    {path, hash}. STATE.json (the commit point) is written by the caller AFTER."""
    snap, current = artifact_paths(run_dir, stage, round_)
    atomic_write_text(snap, text)
    atomic_write_text(current, text)
    return {"path": str(snap), "hash": sha256_text(text)}


# ---- handlers: each does its work, persists the next status, returns ----
def handle_author(run_dir: Path, state: dict, cfg: dict, *, dry_run=False, stub=None) -> None:
    stage = state["stage"]
    round_ = state["round"]
    state["status"] = "authoring"
    state["current_step"] = "author"
    state["attempts"] = 0
    save_state(run_dir, state)
    hb = _attempt_hb(run_dir, state, cfg)
    log_line(run_dir, f"→ author {STAGE_LETTERS[stage]} round {round_} "
                      f"({'draft' if round_ == 0 and not state.get('current_artifact') else 'revise'})")

    model_id = "claude"
    prose = ""  # the author's natural-language output (for QUESTIONS:/DISPUTES: parsing)
    if stage == "implementation":
        text, model_id, prose = _author_implementation(run_dir, state, cfg, round_,
                                                        dry_run=dry_run, stub=stub, on_attempt=hb)
    else:
        prompt = build_author_prompt(run_dir, state, cfg)
        if dry_run and stub is None:
            claude_generate(prompt, cfg=cfg, mode="plan", dry_run=True)
            text = f"<dry-run stub {stage} artifact round {round_}>"
        elif stub is not None:
            text = stub.get("author", f"<stub {stage} artifact r{round_}>")
        else:
            # brownfield planning author gets Read/Grep/Glob confined to the CODEBASE
            # cwd (never the run dir), so it can survey existing code (DESIGN §6.1).
            target = state["config"].get("target", cfg["target"])
            if target.get("mode") == "brownfield" and target.get("repo"):
                out = claude_generate(prompt, cfg=cfg, mode="brownfield",
                                      cwd=Path(target["repo"]), on_attempt=hb)
            else:
                out = claude_generate(prompt, cfg=cfg, mode="plan", on_attempt=hb)
            add_tokens(state, out)
            text = out["result"]
            model_id = out.get("model") or "claude"
        prose = text

    # QUESTIONS:/DISPUTES: handling for EVERY headless author (plans AND Stage C, §5).
    if stub is None:
        for d in parse_disputes(prose, round_):
            if d["ref"] not in {x["ref"] for x in state["open_disputes"]}:
                state["open_disputes"].append(d)
        questions = parse_questions(prose)
        if questions:
            if stage == "implementation":
                # discard the (possibly partial) round so the post-answers re-author starts clean
                sc = load_stage_c(run_dir)
                if sc.get("round_base"):
                    reset_worktree(Path(sc["worktree"]), sc["round_base"])
            write_questions(run_dir, questions)
            state["status"] = "awaiting_human"
            state["waiting_for"] = "answers"
            state["current_step"] = None
            save_state(run_dir, state)
            log_line(run_dir, f"author asked {len(questions)} question(s) → awaiting_human(answers)")
            notify(run_dir, f"Author needs answers: see questions.md ({len(questions)} question(s)).")
            return

    art = commit_artifact(run_dir, state, stage, round_, text)
    state["current_artifact"] = {"path": art["path"], "hash": art["hash"], "verdict_path": None}
    append_history(state, {"stage": stage, "round": round_, "actor": "claude",
                           "artifact": art["path"], "artifact_hash": art["hash"],
                           "model": model_id})
    state["status"] = "authored"
    state["current_step"] = None
    save_state(run_dir, state)
    log_line(run_dir, f"← author committed {Path(art['path']).name}")


def _author_implementation(run_dir, state, cfg, round_, *, dry_run=False, stub=None, on_attempt=None):
    """Stage C edit-mode author inside the worktree; returns (diff_text, model_id, result_text).
    result_text is Claude's prose (for QUESTIONS:/DISPUTES: parsing, §5)."""
    sc = load_stage_c(run_dir)
    wt = Path(sc["worktree"])
    prompt = build_author_prompt(run_dir, state, cfg)
    model_id, result_text = "claude", ""
    if dry_run and stub is None:
        claude_generate(prompt, cfg=cfg, mode="impl", worktree=wt, dry_run=True)
        return ("<dry-run Stage C diff>", "dry-run", "")
    # Edit-mode authoring is NOT atomic — a crash mid-edit leaves partial edits in the
    # worktree. On resume of THIS round, git reset --hard + clean back to the round's
    # base commit before re-running (DESIGN §8). First attempt: record the base.
    if sc.get("round_base_round") == round_ and sc.get("round_base"):
        reset_worktree(wt, sc["round_base"])
        log_line(run_dir, f"Stage C resume: reset worktree to round-{round_} base {sc['round_base'][:8]}")
    else:
        sc["round_base"] = _git(["rev-parse", "HEAD"], wt).stdout.strip()
        sc["round_base_round"] = round_
        save_stage_c(run_dir, sc)
    if stub is not None and "impl_files" in stub:
        for rel, content in stub["impl_files"].items():
            atomic_write_text(wt / rel, content)
    else:
        out = claude_generate(prompt, cfg=cfg, mode="impl", worktree=wt, cwd=wt, on_attempt=on_attempt)
        add_tokens(state, out)
        model_id = out.get("model") or "claude"
        result_text = out.get("result", "")
    authored = commit_round(wt, round_)
    # Record the EXACT commit whose diff is the artifact, so review/test/finalize all bind
    # to it — not to a live HEAD that could drift afterward (§2/§3 critical).
    sc = load_stage_c(run_dir)
    sc["authored_commit"], sc["authored_round"] = authored, round_
    save_stage_c(run_dir, sc)
    diff = worktree_diff(wt, sc["base_commit"])
    return diff, model_id, result_text


def _artifact_delta(run_dir: Path, state: dict) -> str:
    """A real previous→current delta for regression-evidence validation (§3) — so
    evidence about DELETED text is visible (a full document/cumulative diff hides it).
    Plans: a unified diff between the prior review's snapshot and this one. Stage C: the
    round-over-round git diff (round_base..HEAD), which already shows +/- lines."""
    import difflib
    stage = state["stage"]
    round_ = state["round"]  # review round; the artifact under review is r(round-1)
    if stage == "implementation":
        sc = load_stage_c(run_dir)
        wt = Path(sc.get("worktree", ""))
        base = sc.get("round_base") or sc.get("base_commit")
        if base and (wt / ".git").exists():
            return _git(["diff", base, "HEAD"], wt, check=False).stdout
        ca = state.get("current_artifact")
        return read_text(Path(ca["path"])) if ca and Path(ca["path"]).exists() else ""
    cur_snap, _ = artifact_paths(run_dir, stage, round_ - 1)
    prev_snap, _ = artifact_paths(run_dir, stage, round_ - 2)
    cur = read_text(cur_snap) if cur_snap.exists() else ""
    if round_ - 2 < 0 or not prev_snap.exists():
        return cur  # first review round — everything is "added"
    prev = read_text(prev_snap)
    return "\n".join(difflib.unified_diff(prev.splitlines(), cur.splitlines(), lineterm=""))


def handle_review(run_dir: Path, state: dict, cfg: dict, *, dry_run=False, stub=None) -> None:
    stage = state["stage"]
    state["round"] = review_count(state, stage) + 1
    round_ = state["round"]
    state["status"] = "reviewing"
    state["current_step"] = "review"
    state["attempts"] = 0
    save_state(run_dir, state)
    hb = _attempt_hb(run_dir, state, cfg)
    letter = STAGE_LETTERS[stage]
    log_line(run_dir, f"→ review {letter} round {round_}")

    ca = state["current_artifact"]
    artifact_text = read_text(Path(ca["path"])) if Path(ca["path"]).exists() else ""
    diff = ""
    test_results = ""
    if stage == "implementation":
        wt = Path(load_stage_c(run_dir)["worktree"])
        if dry_run and stub is None:
            tcmd = (state["config"].get("stage_c", cfg["stage_c"]).get("test_command") or "").strip()
            print(f"[dry-run] TEST GATE: {('(cd %s && %s)' % (wt, tcmd)) if tcmd else '(no test_command configured)'}")
            # synthesize GREEN telemetry (clearly marked) so the dry pass clears the
            # mechanical test gate and keeps rendering later commands (§10). This mutates
            # only the throwaway dry-run COPY's sidecar.
            if tcmd:
                _sc = load_stage_c(run_dir)
                _sc.update({"test_configured": True, "last_test_round": round_,
                            "last_test_exit": 0, "last_test_commit": "dry-run", "dry_run": True})
                save_stage_c(run_dir, _sc)
        else:
            state["current_step"] = "test"
            save_state(run_dir, state)
            # The reviewed artifact (current_artifact) is the diff of the AUTHORED commit.
            # Pin the worktree to that exact commit BEFORE testing, so the tested code and
            # the presented diff are precisely what was reviewed — never a drifted HEAD
            # (§2/§3 critical). authored_commit was recorded by _author_implementation.
            _sc = load_stage_c(run_dir)
            authored = _sc.get("authored_commit")
            if authored and (wt / ".git").exists():
                if _git(["rev-parse", "HEAD"], wt, check=False).stdout.strip() != authored \
                        or _git(["status", "--porcelain"], wt, check=False).stdout.strip():
                    reset_worktree(wt, authored)  # discard any drift before reviewing/testing
            if stub is not None:
                test_results = stub.get("tests", "stub tests pass\n[exit code: 0]")
            else:
                test_results = run_test_gate(run_dir, cfg, state, wt, round_)
                # Tests can MUTATE the worktree — including CREATING A COMMIT (advancing
                # HEAD). Reset to the AUTHORED commit (not live HEAD), so neither the
                # presented diff nor the next round's base carries unreviewed test-created
                # code (§2/§3). Fail LOUDLY if the restore fails or HEAD doesn't land back.
                if authored and (wt / ".git").exists():
                    reset_worktree(wt, authored)
                    if _git(["rev-parse", "HEAD"], wt, check=False).stdout.strip() != authored:
                        raise OrchestraError("failed to restore worktree to the authored commit "
                                             "after tests — refusing to proceed on a poisoned tree")
                else:
                    _git(["reset", "--hard", "HEAD"], wt)
                    _git(["clean", "-fd"], wt)
            # reviewed_commit == the AUTHORED commit (whose diff was reviewed), NOT live HEAD.
            _sc = load_stage_c(run_dir)
            _sc.update({"reviewed_commit": (authored or _git(["rev-parse", "HEAD"], wt).stdout.strip()),
                        "reviewed_round": round_})
            save_stage_c(run_dir, _sc)
        diff = artifact_text  # the snapshot IS the diff for Stage C

    verdict_path = Path(run_dir) / "reviews" / f"{letter}-{round_:02d}-verdict.json"
    prompt = build_review_prompt(run_dir, state, cfg, artifact_text=artifact_text,
                                 diff=diff, test_results=test_results)

    artifact_delta = _artifact_delta(run_dir, state)  # real prev→cur delta (§3)

    if stub is not None and "verdict" in stub:
        verdicts = stub["verdict"]
        v = verdicts.pop(0) if isinstance(verdicts, list) else verdicts
        rev = {"verdict": v, "actor": "codex", "fallback": False, "model": "stub"}
        atomic_write_text(verdict_path, json.dumps(v, indent=2, allow_nan=False) + "\n")
    else:
        # A present-but-malformed verdict (unparseable / schema-invalid) gets ONE
        # corrective re-prompt, then stuck(error) — never CONVERGED (§7).
        try:
            rev = review(prompt, cfg=cfg, verdict_path=verdict_path, cwd=None,
                         dry_run=dry_run, on_attempt=hb)
        except VerdictInvalid as e:
            log_line(run_dir, f"verdict invalid ({e}) — re-prompting once")
            try:
                rev = review(prompt + "\n\nYour previous output was not a valid verdict JSON. "
                             "Re-emit ONLY a single JSON object conforming exactly to the verdict "
                             "schema.", cfg=cfg, verdict_path=verdict_path, cwd=None, dry_run=dry_run,
                             on_attempt=hb)
            except VerdictInvalid as e2:
                _set_stuck(run_dir, state, "error",
                           f"Reviewer produced an invalid verdict twice ({e2}).")
                return

    verdict = rev["verdict"]
    accepted = {e["key"] for e in state.get("accepted_deviations", [])}
    # re-prompt once on inconsistency, then stuck(error) — never CONVERGED (§7)
    if not consistent(verdict, state.get("resolved_ledger"), artifact_delta, accepted_keys=accepted):
        log_line(run_dir, f"verdict inconsistent (decision={verdict.get('decision')}, "
                          f"blockers={len(verdict.get('blocking_issues', []))}, "
                          f"conf={verdict.get('confidence')}) — re-prompting once")
        if stub is not None and "verdict" in stub and isinstance(stub["verdict"], list) and stub["verdict"]:
            verdict = stub["verdict"].pop(0)
            atomic_write_text(verdict_path, json.dumps(verdict, indent=2, allow_nan=False) + "\n")
            rev["verdict"] = verdict
        elif not dry_run:
            try:
                rev = review(prompt + "\n\nYour previous verdict was internally inconsistent "
                             "(e.g. APPROVE with blockers, or out-of-range confidence). Re-emit a "
                             "consistent verdict.", cfg=cfg, verdict_path=verdict_path,
                             cwd=None, dry_run=dry_run, on_attempt=hb)
                verdict = rev["verdict"]
            except OrchestraError as e:
                log_line(run_dir, f"re-prompt failed: {e}")
        if not consistent(verdict, state.get("resolved_ledger"), artifact_delta, accepted_keys=accepted):
            state["status"] = "stuck"
            state["stuck_reason"] = "error"
            state["current_step"] = None
            state["last_verdict"] = verdict
            save_state(run_dir, state)
            log_line(run_dir, "verdict still inconsistent after re-prompt → stuck(error)")
            notify(run_dir, "Reviewer produced an inconsistent verdict twice → stuck(error).")
            return

    # render review.md + persist verdict + record telemetry
    atomic_write_text(Path(run_dir) / "reviews" / f"{letter}-{round_:02d}-review.md",
                      verdict.get("review_markdown", "") + "\n")
    append_history(state, {"stage": stage, "round": round_, "actor": rev["actor"],
                           "verdict": verdict["decision"], "fallback": rev.get("fallback", False),
                           "model": rev.get("model", "")})
    if rev.get("fallback"):
        notify(run_dir, f"Codex unavailable — used Claude reviewer fallback for {letter} round {round_} (degraded independence).")
        log_line(run_dir, f"⚠️ reviewer fallback to Claude (Codex unavailable), round {round_}")
    ca["verdict_path"] = str(verdict_path)
    state["current_artifact"] = ca
    state["last_verdict"] = verdict
    state["status"] = "deciding"
    state["current_step"] = "decide"
    save_state(run_dir, state)
    log_line(run_dir, f"← review {letter} round {round_} → {verdict['decision']} "
                      f"(confidence {verdict.get('confidence')})")


def handle_decide(run_dir: Path, state: dict, cfg: dict, *, dry_run=False, stub=None) -> None:
    stage = state["stage"]
    round_ = state["round"]
    verdict = state["last_verdict"]
    letter = STAGE_LETTERS[stage]

    # apply reviewer dispute rulings (DESIGN §5)
    escalated = _apply_dispute_rulings(run_dir, state, verdict, round_)
    # update anti-regression ledger from resolved blockers
    prev_verdict = _load_round_verdict(run_dir, letter, round_ - 1)
    update_ledger(state, prev_verdict, verdict, round_)
    # Validate addressed_previous against the prior verdict's REAL ids (§7/§10) — advisory:
    # a fresh reviewer emits fresh ids, so a mismatch is informational, surfaced in LOG.md,
    # not a gate (the resolved-ledger + regressions machinery carries the binding signal).
    if prev_verdict is not None and verdict.get("addressed_previous"):
        prior_ids = {b.get("id") for b in prev_verdict.get("blocking_issues", [])}
        unknown = [i for i in verdict["addressed_previous"] if i not in prior_ids]
        if unknown:
            log_line(run_dir, f"addressed_previous lists ids not in the prior verdict "
                              f"{sorted(unknown)} (ignored — ids aren't stable across fresh reviews)")
    if escalated:
        save_state(run_dir, state)  # a persistent upheld dispute → awaiting_human
        return

    decision = verdict["decision"]
    artifact_text = ""
    ca = state.get("current_artifact")
    if ca and Path(ca["path"]).exists():
        artifact_text = read_text(Path(ca["path"]))

    # A blocker CONCEDED in THIS verdict must stop blocking the CURRENT decision (§5).
    # Recompute effective blockers/regressions after the rulings: a REVISE whose only
    # blockers were just conceded (none left) is effectively an APPROVE — converge now
    # rather than spending another round (or burning the ceiling) on a won't-fix.
    accepted_keys = {e["key"] for e in state.get("accepted_deviations", [])}
    eff_blockers = [b for b in verdict.get("blocking_issues", [])
                    if content_key(b) not in accepted_keys]
    active_ledger = {e["key"] for e in state.get("resolved_ledger", []) if not e.get("cleared")}
    eff_regs = validated_regressions(verdict, _artifact_delta(run_dir, state), active_ledger)
    if decision == "REVISE" and not eff_blockers and not eff_regs:
        log_line(run_dir, "all REVISE blockers were conceded this round → effective APPROVE")
        decision = "APPROVE"

    if decision == "REJECT":
        _set_stuck(run_dir, state, "rejected",
                   f"Reviewer REJECTED: {verdict.get('reject_reason') or 'see blocking_issues'}")
        return

    # The hard ceiling covers EVERY stage, including Stage A's human-driven loop —
    # max_rounds is "a safety cap covering both authoring iterations and Codex review
    # rounds" (§2/§4.1). Checked before the heavy-gate park so repeated iterate commands
    # can't run past it; the human raises the cap via `iterate` to continue.
    if decision != "APPROVE" and round_ >= int(cfg["max_rounds"][stage]):
        # carry the oscillation digest on the flag so the human can see WHY it never
        # settled — which blocking-issue keys recurred and how often (§4.1).
        annotate_oscillation(run_dir, state, verdict)
        _set_stuck(run_dir, state, "max_rounds",
                   f"Hit max_rounds={cfg['max_rounds'][stage]} for {stage} still in {decision}. "
                   f"{_oscillation_digest(run_dir, state)}")
        return

    # Stage A (heavy gate) is human-driven: surface the verdict and park; the human
    # is the convergence function (iterate/approve), not the auto-loop (DESIGN §2/§5).
    if state["gate"] == "heavy" and decision != "APPROVE":
        state["status"] = "awaiting_human"
        state["waiting_for"] = "approval"
        state["current_step"] = None
        save_state(run_dir, state)
        log_line(run_dir, f"Stage {letter} review → {decision}; awaiting human (iterate/approve)")
        notify(run_dir, f"Stage {letter} reviewed ({decision}). Read reviews/, then "
                        f"`orchestra iterate --note ...` or `orchestra approve`.")
        return

    if decision == "APPROVE":
        # MECHANICAL executed-test gate (§2/§10), fail-CLOSED: an autonomous Stage C
        # APPROVE is honored only if the orchestrator-run tests for THIS round exited
        # zero. Missing/stale/non-zero telemetry all block. A reviewer APPROVE cannot
        # override a red (or unverified) executed gate — "green tests" is a fact.
        if stage == "implementation":
            green, why = stage_c_tests_green(run_dir, state, cfg, round_)
            if not green:
                state["status"] = "stuck"
                state["stuck_reason"] = "tests_failed"
                state["current_step"] = None
                save_state(run_dir, state)
                log_line(run_dir, f"reviewer APPROVE but executed-test gate not green ({why}) "
                                  f"→ stuck(tests_failed)")
                notify(run_dir, f"Stage C APPROVE blocked — test gate not green: {why}. "
                                f"Fix and `orchestra iterate`, or re-run the round.")
                return
        # persistent upheld disputes escalate to human even on APPROVE path handled in rulings
        downgrade_reason = None
        if low_confidence(verdict, cfg, stage):
            downgrade_reason = f"confidence {verdict['confidence']} < floor {cfg['min_confidence'][stage]}"
        elif has_prose_decision_mismatch(verdict):
            downgrade_reason = "APPROVE but review prose carries strong negative markers"
        elif round_ == 1 and is_nontrivial_artifact(artifact_text) and state["gate"] == "none":
            downgrade_reason = "first-round APPROVE on a non-trivial artifact (suspicious easy pass)"
        if downgrade_reason and state["gate"] == "none":
            state["status"] = "awaiting_human"
            state["waiting_for"] = "approval"
            state["current_step"] = None
            save_state(run_dir, state)
            log_line(run_dir, f"APPROVE downgraded to awaiting_human: {downgrade_reason}")
            notify(run_dir, f"Auto-stage APPROVE held for human review: {downgrade_reason}")
            return
        # settled (APPROVE) — apply the gate (DESIGN §4/§8)
        nits = verdict.get("non_blocking_suggestions", [])
        tag = "clean" if not nits else f"with {len(nits)} nit(s)"
        if state["gate"] in ("heavy", "some"):
            state["status"] = "awaiting_human"
            state["waiting_for"] = "approval"
            state["current_step"] = None
            save_state(run_dir, state)
            log_line(run_dir, f"converged ({letter}, {tag}) → awaiting_human(approval) [gate={state['gate']}]")
            notify(run_dir, f"{ARTIFACT_LABEL[stage].title()} converged ({tag}) — approve to advance "
                            f"(orchestra approve) or iterate (orchestra iterate --note).")
            return
        # none-gate → converged (the loop advances without a human)
        state["status"] = "converged"
        state["current_step"] = None
        save_state(run_dir, state)
        log_line(run_dir, f"CONVERGED ({letter}, {tag}) — auto-advancing [gate=none]")
        return

    # REVISE (ceiling already enforced above for every stage)
    annotate_oscillation(run_dir, state, verdict)
    if cfg["behavior"].get("stop_on_oscillation") and _oscillating_now(run_dir, state, verdict):
        _set_stuck(run_dir, state, "oscillation",
                   "Oscillation detected and stop_on_oscillation=true.")
        return
    # next round: fresh revise (round stays; review_step will ++ on next entry)
    state["status"] = "authoring"
    state["current_step"] = "author"
    save_state(run_dir, state)
    log_line(run_dir, f"REVISE ({letter}) → author revision for round {round_}")


def _dispute_content_key(run_dir: Path, state: dict, dispute: dict) -> str | None:
    """Resolve a dispute (whose ref is a blocker id) to the disputed blocker's stable
    content key, by looking it up in the verdict from the round it was raised against.
    Ids are fresh per round, so this is how a concession joins the oscillation-excluded
    key space (DESIGN §5/§10)."""
    v = _load_round_verdict(run_dir, STAGE_LETTERS[state["stage"]], dispute.get("raised_round", 0))
    if v:
        for b in v.get("blocking_issues", []):
            if b.get("id") == dispute.get("ref"):
                return content_key(b)
    return None


def _apply_dispute_rulings(run_dir: Path, state: dict, verdict: dict, round_: int) -> bool:
    """Apply the reviewer's dispute_rulings (DESIGN §5). Conceded → accepted_deviations
    (stops blocking, excluded from oscillation). Upheld twice → escalate to human.
    Persistence is tracked via history notes (open_disputes items are schema-strict)."""
    rulings = verdict.get("dispute_rulings", []) or []
    if not state.get("open_disputes") and not rulings:
        return False
    open_by_ref = {d["ref"]: d for d in state.get("open_disputes", [])}
    conceded_refs = set()
    escalated = False
    ruled_this_verdict = set()  # at most ONE ruling per dispute per verdict (§5)
    for r in rulings:
        ref = r.get("ref")
        if ref not in open_by_ref or ref in ruled_this_verdict:
            continue  # ignore duplicate rulings for the same ref in one verdict
        ruled_this_verdict.add(ref)
        if r.get("ruling") == "conceded":
            d = open_by_ref[ref]
            # Store the conceded item under the disputed blocker's CONTENT KEY (not the
            # dispute id) so excluded_keys() lines up with content_key()/is_oscillating()
            # and the concession is actually excluded from oscillation scoring (DESIGN §5/§10).
            dev_key = _dispute_content_key(run_dir, state, d) or (_norm(ref) or ref)
            state.setdefault("accepted_deviations", []).append({
                "key": dev_key, "title": ref,
                "note": (r.get("note") or d.get("rationale", "")), "conceded_round": round_,
            })
            conceded_refs.add(ref)
            log_line(run_dir, f"dispute {ref} CONCEDED → accepted_deviations (key {dev_key})")
        elif r.get("ruling") == "upheld":
            append_history(state, {"stage": state["stage"], "round": round_,
                                   "actor": "orchestrator", "note": f"dispute_upheld:{ref}:{round_}"})
            # persistence = upheld in >= 2 DISTINCT rounds (not duplicate notes / a re-run)
            upheld_rounds = {h["note"].rsplit(":", 1)[-1] for h in state["history"]
                             if h.get("note", "").startswith(f"dispute_upheld:{ref}:")}
            if len(upheld_rounds) >= 2:
                log_line(run_dir, f"dispute {ref} UPHELD across rounds → escalate to human")
                notify(run_dir, f"Persistent author↔reviewer disagreement on '{ref}'. See open_disputes.")
                state["status"] = "awaiting_human"
                state["waiting_for"] = "approval"
                state["current_step"] = None
                escalated = True
    state["open_disputes"] = [d for d in state.get("open_disputes", [])
                              if d["ref"] not in conceded_refs]
    return escalated


def annotate_oscillation(run_dir: Path, state: dict, verdict: dict) -> None:
    if _oscillating_now(run_dir, state, verdict):
        keys = [content_key(b) for b in verdict.get("blocking_issues", [])]
        log_line(run_dir, f"⚠️ oscillation signal: recurring blocking-issue keys {keys[:3]} "
                          f"(severity-weighted score not decreasing)")


def _oscillation_digest(run_dir: Path, state: dict) -> str:
    """Which blocking-issue content keys recurred across this stage's rounds, and how
    often — the digest the §4.1 max_rounds flag carries so a human can see why it never
    settled (excludes accepted/cleared keys)."""
    excl = excluded_keys(state)
    counts: dict = {}
    letter = STAGE_LETTERS.get(state["stage"], "?")
    for h in state.get("history", []):
        if h.get("stage") == state["stage"] and "verdict" in h:
            v = _load_round_verdict(run_dir, letter, h["round"])
            for b in (v or {}).get("blocking_issues", []):
                k = content_key(b)
                if k not in excl:
                    counts[k] = counts.get(k, 0) + 1
    recurring = sorted(((c, k) for k, c in counts.items() if c >= 2), reverse=True)
    if not recurring:
        return "Oscillation digest: no blocking-issue key recurred across rounds."
    top = "; ".join(f"{k} ×{c}" for c, k in recurring[:5])
    return f"Oscillation digest (recurring blocking keys): {top}."


def _oscillating_now(run_dir: Path, state: dict, verdict: dict) -> bool:
    stage = state["stage"]
    prev = _load_round_verdict(run_dir, STAGE_LETTERS[stage], state["round"] - 1)
    if not prev:
        return False
    return is_oscillating(prev.get("blocking_issues", []), verdict.get("blocking_issues", []),
                          excluded_keys(state))


def _set_stuck(run_dir: Path, state: dict, reason: str, msg: str) -> None:
    state["status"] = "stuck"
    state["stuck_reason"] = reason
    state["current_step"] = None
    save_state(run_dir, state)
    log_line(run_dir, f"STUCK({reason}): {msg}")
    notify(run_dir, f"Run stuck ({reason}): {msg}")


def handle_converged(run_dir: Path, state: dict, cfg: dict, *, dry_run=False, stub=None) -> None:
    """`converged` = settled-and-approved → advance (DESIGN §8 resume table). The gate
    decision was already made in handle_decide (none-gate auto-converges here; a
    heavy/some gate reaches converged only via an explicit human `approve`)."""
    advance_stage(run_dir, state, cfg, dry_run=dry_run, stub=stub)


def advance_stage(run_dir: Path, state: dict, cfg: dict, *, dry_run=False, stub=None) -> None:
    cur = state["stage"]
    nxt = NEXT_STAGE[cur]
    log_line(run_dir, f"advance: {cur} → {nxt}")
    # Carry the converged stage's approved nits forward to the next stage's author
    # (DESIGN §4.1 — nits never loop but are surfaced into the next stage / final PR).
    lv = state.get("last_verdict") or {}
    nits = lv.get("non_blocking_suggestions", []) or []
    save_carried(run_dir, {"from_stage": cur, "nits": nits})
    if nxt == "done":
        finalize_run(run_dir, state, cfg)
        return
    # reset per-stage state on entry (the resolved-ledger is anti-regression for the
    # CURRENT artifact, so it is per-stage by design; cross-stage content keys don't match)
    state["stage"] = nxt
    state["round"] = 0
    state["gate"] = cfg["gate"][nxt]
    state["status"] = "authoring"
    state["waiting_for"] = None
    state["current_step"] = None
    state["current_artifact"] = None
    state["last_verdict"] = None
    state["resolved_ledger"] = []
    state["accepted_deviations"] = []
    state["open_disputes"] = []
    if nxt == "implementation":
        sc = setup_worktree(run_dir, cfg, state, dry_run=dry_run)
        save_stage_c(run_dir, sc)
        log_line(run_dir, f"Stage C worktree ready: {sc['worktree']} (base {sc['base_commit'][:8]})")
    save_state(run_dir, state)


def finalize_run(run_dir: Path, state: dict, cfg: dict) -> None:
    """Present the final diff / PR — Stage C never merges unattended (DESIGN §2)."""
    state["stage"] = "done"
    state["status"] = "done"
    state["current_step"] = None
    state["waiting_for"] = None
    summary = []
    sc = load_stage_c(run_dir)
    if sc:
        wt = Path(sc["worktree"])
        # Present the diff of the REVIEWED commit (base..reviewed_commit), NEVER the live
        # working tree or symbolic HEAD — so what's presented is exactly what was reviewed
        # (§2/§3). reviewed_commit is always set by the time we converge (the gate requires
        # it); guard defensively if somehow absent.
        target = sc.get("reviewed_commit")
        if not target:
            log_line(run_dir, "WARNING: no reviewed_commit at finalize — presenting current HEAD")
            target = "HEAD"
        diff = _git(["diff", sc["base_commit"], target], wt, check=False).stdout if wt.exists() else ""
        atomic_write_text(Path(run_dir) / "30-impl.final.diff", diff)
        stat = _git(["diff", "--stat", sc["base_commit"], target], wt, check=False).stdout if wt.exists() else ""
        summary.append("Final diff presented at 30-impl.final.diff (NOT merged):")
        summary.append(stat.strip())
    save_state(run_dir, state)
    log_line(run_dir, "DONE — pipeline complete. Final implementation diff PRESENTED (never merged).")
    notify(run_dir, "Run DONE. Review the presented diff (30-impl.final.diff). Nothing was merged.")
    if summary:
        print("\n".join(summary))


# --------------------------------------------------------------------------- #
# resume / drive — dispatch on (stage, status), never on round (DESIGN §8)
# --------------------------------------------------------------------------- #
HANDLERS = {
    "authoring": handle_author,
    "authored": handle_review,
    "reviewing": handle_review,
    "deciding": handle_decide,
    "converged": handle_converged,
}


def verify_resume_integrity(run_dir: Path, state: dict) -> None:
    """Hash check vs human edits (DESIGN §8): mismatch at a human-reachable pause is
    an intentional edit (re-hash, proceed); mismatch in-flight is corruption (error)."""
    ca = state.get("current_artifact")
    if not ca or not ca.get("path"):
        return
    human_pause = state["status"] in ("awaiting_human", "stuck", "converged")
    # in-flight = any non-human phase that references a committed artifact (incl. `authored`)
    in_flight = state["status"] in ("authoring", "authored", "reviewing", "deciding")
    p = Path(ca["path"])
    if not p.exists():
        if in_flight:
            log_line(run_dir, f"referenced artifact missing in-flight ({p.name}) → error")
            state["status"] = "error"
            state["current_step"] = None
            save_state(run_dir, state)
        return
    actual = sha256_text(read_text(p))
    if actual == ca.get("hash"):
        return
    if human_pause:
        log_line(run_dir, f"artifact {p.name} edited by hand at a human pause — re-hashing, proceeding")
        ca["hash"] = actual
        state["current_artifact"] = ca
        # Sync the stage's "current" alias (10-/20-impl-plan.md) to the edited snapshot —
        # downstream stages read the alias, so it must not drift from the accepted edit (§8).
        if state["stage"] in ARTIFACT_BASENAME and state["stage"] != "implementation":
            _, alias = artifact_paths(run_dir, state["stage"], state["round"])
            try:
                atomic_write_text(alias, read_text(p))
            except OSError:
                pass
        save_state(run_dir, state)
    elif in_flight:  # authoring / authored / reviewing / deciding
        log_line(run_dir, f"artifact hash mismatch in-flight ({p.name}) — treating as corruption → error")
        state["status"] = "error"
        state["current_step"] = None
        save_state(run_dir, state)


def resume(run_dir: Path, *, cfg: dict | None = None, dry_run=False, stub=None,
           stub_verdicts=False, max_steps: int = 200) -> str:
    """Drive the run from STATE.json, dispatching on (stage, status). Returns the
    terminal status reached (awaiting_human / stuck / error / done)."""
    run_dir = Path(run_dir)
    state = load_state(run_dir)
    if cfg is None:
        cfg = effective_config(state)
    if not state.get("started_at"):
        state["started_at"] = now_iso()
        save_state(run_dir, state)
    if state.get("paused_reason"):  # we're driving again — clear the prior pause marker
        state["paused_reason"] = None
        save_state(run_dir, state)
    if cfg.get("behavior", {}).get("fresh_author_on_revise") is False and not (dry_run or stub):
        log_line(run_dir, "note: behavior.fresh_author_on_revise=false is not implemented "
                          "(the author always runs a fresh session — the safe default); ignoring.")
    verify_resume_integrity(run_dir, state)

    steps = 0
    while True:
        steps += 1
        if steps > max_steps:
            raise OrchestraError("resume exceeded max_steps (possible infinite loop)")
        state = load_state(run_dir)
        status = state["status"]
        stage = state["stage"]

        if stage == "done" or status == "done":
            return "done"
        if status in ("awaiting_human", "stuck", "error"):
            # a full dry pass (--stub-verdicts) walks through gates to show every
            # stage's commands (DESIGN §10/§13); a plain --dry-run stops at the gate.
            if dry_run and stub_verdicts and status == "awaiting_human" \
                    and state.get("waiting_for") == "approval":
                print(f"[dry-run] auto-advancing past {stage} approval gate (--stub-verdicts)")
                state["status"] = "converged"
                state["waiting_for"] = None
                save_state(run_dir, state)
                continue
            return status

        try:
            # Safe checkpoint BEFORE any external call — including Stage A (budget/HALT
            # must gate every author, §9). Re-checked after the monitor runs so a HALT the
            # monitor JUST wrote stops THIS iteration's call, not the next one (§10.1).
            term = checkpoint(run_dir, state, cfg)
            if term is not None:
                save_state(run_dir, term)
                return term["status"]
            if stub is None:  # the monitor is a real claude call; never in stubbed/unit runs
                maybe_run_monitor(run_dir, cfg, dry_run=dry_run)
                term = checkpoint(run_dir, state, cfg)  # honor a freshly-written HALT now
                if term is not None:
                    save_state(run_dir, term)
                    return term["status"]

            # Stage A (interactive/headless plumbing) — author phase only
            if stage == "highlevel" and status == "authoring":
                _drive_stage_a_author(run_dir, state, cfg, dry_run=dry_run, stub=stub)
                continue

            handler = HANDLERS.get(status, handle_author)
            handler(run_dir, state, cfg, dry_run=dry_run, stub=stub)
        except _CheckpointAbort as ab:
            # a budget/HALT checkpoint fired mid-retry — commit its terminal state.
            save_state(run_dir, ab.terminal_state)
            log_line(run_dir, f"checkpoint fired during a retry → {ab.terminal_state['status']}"
                              f"({ab.terminal_state.get('stuck_reason')})")
            return ab.terminal_state["status"]
        except (AuthorPaused, ReviewerPaused) as e:
            # subscription usage limit / on_limit=pause — pause and stay resumable (§9/§6.2).
            # Status stays in-flight (authoring/reviewing) so a later `orchestra resume`
            # idempotently retries the SAME call; paused_reason marks it as an intentional
            # pause (not a hang) for the watchdog.
            st = load_state(run_dir)
            st["paused_reason"] = str(e)[:200]
            save_state(run_dir, st)
            log_line(run_dir, f"PAUSED: {e}")
            notify(run_dir, f"Run paused (usage limit): {e}. `orchestra resume` when it resets.")
            return st["status"]
        except ReviewerUnavailable as e:
            st = load_state(run_dir)
            _set_stuck(run_dir, st, "reviewer_unavailable", str(e))
            return "stuck"
        except (FatalCallError, RenderError, SchemaError) as e:
            st = load_state(run_dir)
            st["status"] = "error"
            st["current_step"] = None
            save_state(run_dir, st)
            log_line(run_dir, f"FATAL → error: {e}")
            notify(run_dir, f"Run errored: {e}")
            return "error"
        except OrchestraError as e:
            st = load_state(run_dir)
            st["status"] = "error"
            st["current_step"] = None
            save_state(run_dir, st)
            log_line(run_dir, f"unrecoverable error → error: {e}")
            notify(run_dir, f"Run errored: {e}")
            return "error"
        except Exception as e:
            # any UNEXPECTED handler exception (e.g. a mid-flight deleted brief, a stray
            # OSError) → preserve the blackboard and transition to a recoverable error,
            # rather than escaping to main() and wedging the run in a crash loop (§10).
            try:
                st = load_state(run_dir)
                st["status"] = "error"
                st["current_step"] = None
                save_state(run_dir, st)
            except Exception:
                pass
            log_line(run_dir, f"unexpected {type(e).__name__} → error: {e}")
            notify(run_dir, f"Run errored ({type(e).__name__}): {e}")
            return "error"


def run_stage(run_dir: Path, stage: str, *, cfg: dict | None = None, dry_run=False, stub=None) -> str:
    """Ensure the run is at `stage` then drive it to its next gate/terminal."""
    state = load_state(run_dir)
    if cfg is None:
        cfg = effective_config(state)
    if stage and state["stage"] != stage and STAGE_ORDER.get(stage, 9) < STAGE_ORDER.get(state["stage"], 9):
        raise OrchestraError(f"cannot rewind from {state['stage']} to {stage}")
    return resume(run_dir, cfg=cfg, dry_run=dry_run, stub=stub)


# --------------------------------------------------------------------------- #
# Stage A — interactive / headless plumbing (DESIGN §2/§5)
# --------------------------------------------------------------------------- #
def _drive_stage_a_author(run_dir: Path, state: dict, cfg: dict, *, dry_run=False, stub=None) -> None:
    """Stage A author phase only — interactive by default, headless (read-only, may
    emit QUESTIONS) when no TTY or scripted. Sets status=authored; the main loop then
    runs review→decide, which parks at awaiting_human for the heavy gate (the human is
    the convergence function, DESIGN §2/§5)."""
    plan_path = Path(run_dir) / "10-highlevel-plan.md"
    round_ = state["round"]

    # Stage A is an interactive conversation by design (§2): default to interactive
    # whenever attached to a TTY, unless the operator passed --headless (or there's no
    # TTY, e.g. scripted/CI — then fall back to the headless read-only author).
    interactive = sys.stdin.isatty() and not cfg.get("_headless", False)
    if interactive and not dry_run and stub is None:
        _run_interactive_claude(run_dir, state, cfg)
        if not plan_path.exists():
            # No plan saved — park. `run`/`resume` won't re-drive an awaiting_human run,
            # so point the operator at the path that actually continues: `iterate` (it
            # re-enters Stage A; if you've saved the plan, just exit the session to proceed).
            state["status"] = "awaiting_human"
            state["waiting_for"] = "approval"
            save_state(run_dir, state)
            notify(run_dir, "Stage A interactive session ended without 10-highlevel-plan.md. "
                            "Save the plan, then run `orchestra iterate <run>` to continue.")
            return
        text = read_text(plan_path)
    else:
        prompt = build_author_prompt(run_dir, state, cfg)  # answers/notes folded in centrally
        if stub is not None:
            text = stub.get("author", "# High-level plan\n\nStub plan.")
        elif dry_run:
            claude_generate(prompt, cfg=cfg, mode="plan", dry_run=True)
            text = f"# High-level plan (dry-run stub, round {round_})"
        else:
            out = claude_generate(prompt, cfg=cfg, mode="plan", on_attempt=_attempt_hb(run_dir, state, cfg))
            add_tokens(state, out)
            text = out["result"]
        # route any author DISPUTES: to the reviewer (same as every other headless author, §5)
        for d in parse_disputes(text, round_):
            if d["ref"] not in {x["ref"] for x in state["open_disputes"]}:
                state["open_disputes"].append(d)
        questions = parse_questions(text)
        if questions:
            write_questions(run_dir, questions)
            state["status"] = "awaiting_human"
            state["waiting_for"] = "answers"
            save_state(run_dir, state)
            log_line(run_dir, f"Stage A author asked {len(questions)} question(s) → awaiting_human(answers)")
            notify(run_dir, f"Stage A: author needs answers (questions.md, {len(questions)} q).")
            return
        atomic_write_text(plan_path, text)

    art = commit_artifact(run_dir, state, "highlevel", round_, text)
    state["current_artifact"] = {"path": art["path"], "hash": art["hash"], "verdict_path": None}
    append_history(state, {"stage": "highlevel", "round": round_, "actor": "claude",
                           "artifact": art["path"], "model": "claude"})
    state["status"] = "authored"
    save_state(run_dir, state)
    log_line(run_dir, f"Stage A plan saved (round {round_}) → review")


def _run_interactive_claude(run_dir: Path, state: dict, cfg: dict) -> None:
    """Spawn interactive Claude WITHOUT the isolation profile (DESIGN §2/§6.1 —
    deliberately un-isolated; a human is steering)."""
    brief = read_text(Path(run_dir) / "00-brief.md")
    tmpl = read_text(PROMPTS_DIR / "claude" / "highlevel-plan.md")
    seed = render_prompt(tmpl, run_id=state["run_id"], brief=brief) if "{{run_id}}" in strip_comments(tmpl) \
        else render_prompt(tmpl, brief=brief)
    print("Launching interactive Claude for Stage A (un-isolated; your CLAUDE.md/skills apply).")
    print(f"Save the converged plan to: {run_dir / '10-highlevel-plan.md'}\n")
    log_line(run_dir, "→ Stage A interactive Claude session launched")
    # Interactive Stage A is intentionally UNBOUNDED (a human is driving — no per-call
    # timeout, §14). But a launch failure (e.g. claude missing) must NOT escape to main()
    # leaving STATE in-flight — convert it to a recoverable FatalCallError → error (§9).
    try:
        subprocess.run(["claude", "--model", cfg["models"]["author"], seed], cwd=str(run_dir))
    except OSError as e:
        raise FatalCallError(f"failed to launch interactive claude: {e}")
    log_line(run_dir, "← Stage A interactive session ended")


# --------------------------------------------------------------------------- #
# watchdog — independent dead/hung-orchestrator detection (DESIGN §10.1 E4)
# --------------------------------------------------------------------------- #
def watchdog_check(run_dir: Path, *, stale_seconds: int = 1800) -> dict:
    """Independent staleness check: an in-flight run whose updated_at is older than
    stale_seconds is flagged as possibly dead/hung (a child monitor can't detect a
    dead parent — §10.1)."""
    state = load_state(run_dir)
    # a usage-limit pause is in-flight but INTENTIONALLY idle — not a hang.
    paused = bool(state.get("paused_reason"))
    in_flight = state["status"] in ("authoring", "authored", "reviewing", "deciding") and not paused
    try:
        upd = datetime.fromisoformat(state["updated_at"].replace("Z", "+00:00"))
        age = int((datetime.now(timezone.utc) - upd).total_seconds())
    except (ValueError, KeyError):
        age = -1
    stale = in_flight and age > stale_seconds
    result = {"run": Path(run_dir).name, "status": state["status"], "age_seconds": age,
              "in_flight": in_flight, "paused": paused, "stale": stale}
    if stale:
        notify(run_dir, f"WATCHDOG: run appears dead/hung — status={state['status']}, "
                        f"updated_at {age}s ago (> {stale_seconds}s). Investigate or resume.")
    return result


# --------------------------------------------------------------------------- #
# run-dir resolution
# --------------------------------------------------------------------------- #
def _user_data_runs_dir() -> Path:
    """Default runs location: a user data dir, NEVER the install directory (DESIGN §6).
    Honors XDG on Linux and Application Support on macOS."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "orchestra"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "orchestra"
    return base / "runs"


def runs_root(cfg: dict | None = None) -> Path:
    if cfg and cfg.get("storage", {}).get("runs_dir"):
        return Path(cfg["storage"]["runs_dir"]).expanduser()
    env = os.environ.get("ORCHESTRA_RUNS_DIR")
    if env:
        return Path(env).expanduser()
    # Only a real, WRITABLE source checkout uses the repo-local runs/ (convenient for the
    # committed EXAMPLE). A pip-installed package (no .git, possibly read-only site-
    # packages) defaults to a user data dir — never the install directory (DESIGN §6).
    if (ROOT / ".git").exists() and (RUNS_DIR / "EXAMPLE-todo-api").exists() and os.access(RUNS_DIR, os.W_OK):
        return RUNS_DIR
    return _user_data_runs_dir()


def resolve_run_dir(run_arg: str, cfg: dict | None = None) -> Path:
    p = Path(run_arg)
    if p.exists() and (p / "STATE.json").exists():
        return p
    root = runs_root(cfg)
    cand = root / run_arg
    if (cand / "STATE.json").exists():
        return cand
    # convenience: match a dated run id by its slug suffix (e.g. `todo-api` → 2026-..-todo-api)
    if root.exists():
        matches = [d for d in sorted(root.glob(f"*-{run_arg}")) if (d / "STATE.json").exists()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise OrchestraError(f"ambiguous run '{run_arg}': {[m.name for m in matches]}")
    if p.exists():
        return p
    raise OrchestraError(f"run not found: {run_arg} (looked in {root})")


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def cmd_init(args: argparse.Namespace) -> int:
    cfg = load_config(getattr(args, "config", None))
    if getattr(args, "test_command", None) is not None:
        cfg["stage_c"]["test_command"] = args.test_command
    if getattr(args, "target", None):
        cfg["target"]["mode"] = args.target
    if getattr(args, "repo", None):
        cfg["target"]["repo"] = args.repo
    if getattr(args, "worktree", None):
        cfg["target"]["worktree_path"] = args.worktree

    # ALWAYS sanitize the full run id (slugify strips path separators and ./..) — a slug
    # like "2026-06-22-x/../../escape" must not traverse outside runs_dir (§3/§6).
    if re.match(r"^\d{4}-\d{2}-\d{2}-", args.slug):
        run_id = slugify(args.slug)                 # keeps the date, strips traversal
    else:
        run_id = f"{today_str()}-{slugify(args.slug)}"
    root = runs_root(cfg).resolve()
    run_dir = (root / run_id).resolve()
    if run_dir.parent != root:                       # defense in depth
        raise OrchestraError(f"refusing run id that escapes runs_dir: {args.slug!r}")
    if (run_dir / "STATE.json").exists():
        raise OrchestraError(f"run already exists: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "reviews").mkdir(exist_ok=True)

    brief_text = (read_text(Path(args.brief)) if args.brief
                  else f"# Brief: {run_id}\n\n(TODO: describe the project)\n")
    atomic_write_text(run_dir / "00-brief.md", brief_text)

    stage = args.stage
    if args.highlevel_plan:
        atomic_write_text(run_dir / "10-highlevel-plan.md", read_text(Path(args.highlevel_plan)))
    if getattr(args, "impl_plan", None):
        atomic_write_text(run_dir / "20-impl-plan.md", read_text(Path(args.impl_plan)))
    if stage == "impl_plan":
        if not (run_dir / "10-highlevel-plan.md").exists():
            raise OrchestraError("--stage impl_plan requires --highlevel-plan (M1 input contract, §12)")
    elif stage == "implementation":
        # Stage C implements against the approved 20-impl-plan.md — require it.
        if not (run_dir / "20-impl-plan.md").exists():
            raise OrchestraError("--stage implementation requires --impl-plan "
                                 "(the approved 20-impl-plan.md to build against)")

    gate = cfg["gate"][stage]
    state = new_state(run_id, stage, cfg, gate)
    # A run seeded DIRECTLY at Stage C never goes through advance_stage, so set up the
    # isolated worktree + sidecar now — otherwise the first author indexes a missing
    # sidecar and crashes (DESIGN §6).
    if stage == "implementation":
        sc = setup_worktree(run_dir, cfg, state)
        save_stage_c(run_dir, sc)
        log_line(run_dir, f"init: Stage C worktree ready: {sc['worktree']} (base {sc['base_commit'][:8]})")
    save_state(run_dir, state)
    log_line(run_dir, f"init: run {run_id} seeded at stage={stage}, status=authoring, round=0")
    print(f"Initialized run: {run_dir}")
    print(f"  stage={stage} status=authoring gate={gate}")
    print(f"  next: orchestra run {run_id}")
    return 0


@contextmanager
def _dry_run_target(run_dir: Path, dry_run: bool):
    """A --dry-run must NOT mutate the authoritative run. When dry, drive a throwaway
    COPY (stub artifacts/verdicts/state land there and are discarded), so the real run
    is untouched (DESIGN §10)."""
    if not dry_run:
        yield run_dir
        return
    tmp = Path(tempfile.mkdtemp(prefix="orchestra-dryrun-"))
    target = tmp / Path(run_dir).name
    shutil.copytree(run_dir, target, ignore=shutil.ignore_patterns(".lock"))
    print(f"[dry-run] driving a throwaway copy — the real run is not modified")
    try:
        yield target
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_run(args: argparse.Namespace) -> int:
    file_cfg = load_config(getattr(args, "config", None))
    run_dir = resolve_run_dir(args.run, file_cfg)
    with acquire_run_lock(run_dir, wait=getattr(args, "wait", False)):
        # derive config from state ONLY while holding the lock, so a concurrent driver
        # can't have us act on stale config/state (DESIGN §8).
        cfg = effective_config(load_state(run_dir))
        dry_run = bool(getattr(args, "dry_run", False) or cfg["behavior"].get("dry_run"))
        if getattr(args, "headless", False):
            cfg["_headless"] = True  # transient — force headless Stage A even on a TTY
        if getattr(args, "interactive", False) and load_state(run_dir)["stage"] != "highlevel":
            print("note: --interactive applies to Stage A (high-level plan) only; "
                  "Stages B/C run headless.", file=sys.stderr)
        with _dry_run_target(run_dir, dry_run) as target:
            resume(target, cfg=cfg, dry_run=dry_run, stub_verdicts=getattr(args, "stub_verdicts", False))
            if dry_run:
                _print_status(target)  # the COPY's end state; the real run is untouched
        if not dry_run:
            _print_status(run_dir)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    file_cfg = load_config(getattr(args, "config", None))
    run_dir = resolve_run_dir(args.run, file_cfg)
    with acquire_run_lock(run_dir):
        state = load_state(run_dir)
        cfg = effective_config(state)
        dry_run = bool(getattr(args, "dry_run", False) or cfg["behavior"].get("dry_run"))
        # if waiting for answers, only proceed once answers are filled
        if state["status"] == "awaiting_human" and state.get("waiting_for") == "answers":
            if not answers_ready(run_dir):
                print("answers.md is not filled yet (sentinel still present or empty). "
                      "Edit answers.md, then resume.")
                return 1
            if not dry_run:
                state["status"] = "authoring"
                state["waiting_for"] = None
                save_state(run_dir, state)
                log_line(run_dir, "answers detected as filled → resuming author")
        with _dry_run_target(run_dir, dry_run) as target:
            resume(target, cfg=cfg, dry_run=dry_run)
            if dry_run:
                _print_status(target)
    if not dry_run:
        _print_status(run_dir)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    cfg = load_config(getattr(args, "config", None))
    run_dir = resolve_run_dir(args.run, cfg)
    with acquire_run_lock(run_dir):
        state = load_state(run_dir)
        cfg = effective_config(state)
        if not (state["status"] == "awaiting_human" and state.get("waiting_for") == "approval"):
            print(f"Nothing to approve: status={state['status']} waiting_for={state.get('waiting_for')}")
            return 1
        # There must actually BE a reviewed artifact to approve — a committed artifact AND a
        # completed reviewer entry for this stage (§2). Guards e.g. a Stage A interactive
        # session that exited without saving a plan (no artifact, no Codex review).
        if not state.get("current_artifact") or not state.get("last_verdict") \
                or review_count(state, state["stage"]) < 1:
            print("Nothing reviewed to approve: this stage has no committed artifact and "
                  "completed reviewer verdict yet. Produce/save the artifact and let it be "
                  "reviewed first (e.g. re-run the stage), then approve.")
            return 1
        # A human approve CANNOT waive the executed-test gate (§3 — a tier-2 input can't
        # waive a verification gate). Defense in depth: refuse to converge Stage C with a
        # non-green gate (the recovery is to fix the code and `orchestra iterate`).
        if state["stage"] == "implementation":
            green, why = stage_c_tests_green(run_dir, state, cfg)
            if not green:
                print(f"Refusing to approve: Stage C test gate is not green ({why}). "
                      f"Fix the code and `orchestra iterate` — the executed-test gate "
                      f"is not waivable by approval (§3).")
                return 1
        log_line(run_dir, "human APPROVED the gate")
        append_history(state, {"stage": state["stage"], "round": state["round"],
                               "actor": "human", "note": "approved"})
        state["status"] = "converged"
        state["waiting_for"] = None
        save_state(run_dir, state)
        resume(run_dir, cfg=cfg)
    _print_status(run_dir)
    return 0


def cmd_iterate(args: argparse.Namespace) -> int:
    cfg = load_config(getattr(args, "config", None))
    run_dir = resolve_run_dir(args.run, cfg)
    with acquire_run_lock(run_dir):
        state = load_state(run_dir)
        cfg = effective_config(state)
        # Accept any hand-edit to the artifact WHILE still at the pause status, so it's
        # treated as an intentional edit (re-hash + sync alias), not in-flight corruption
        # once we flip to authoring (§8).
        verify_resume_integrity(run_dir, state)
        state = load_state(run_dir)
        note = args.note or ""
        # human override: clear a contested ledger entry (E2)
        if getattr(args, "clear_ledger", None):
            for e in state["resolved_ledger"]:
                if e["key"] == args.clear_ledger:
                    e["cleared"] = True
                    log_line(run_dir, f"human cleared ledger entry {args.clear_ledger} (E2)")
        if state["status"] not in ("awaiting_human", "stuck"):
            print(f"iterate only valid at awaiting_human/stuck (status={state['status']})")
            return 1
        # raise the cap if we burned out on it
        if state.get("stuck_reason") == "max_rounds":
            bump = getattr(args, "add_rounds", 5)
            cfg["max_rounds"][state["stage"]] = int(cfg["max_rounds"][state["stage"]]) + bump
            state["config"]["max_rounds"][state["stage"]] = cfg["max_rounds"][state["stage"]]
            log_line(run_dir, f"iterate: raised max_rounds[{state['stage']}] by {bump}")
        elif state.get("stuck_reason") == "budget_exceeded":
            # the wall-clock bound, not max_rounds, is what tripped — extend IT, else the
            # next checkpoint re-stucks immediately (§8).
            add = int(getattr(args, "add_wall_seconds", 3600))
            base = int(cfg["budget"].get("wall_clock_seconds", 0)) or 0
            new_wall = max(base, elapsed_seconds(state)) + add
            cfg["budget"]["wall_clock_seconds"] = new_wall
            state["config"].setdefault("budget", {})["wall_clock_seconds"] = new_wall
            log_line(run_dir, f"iterate: extended wall_clock to {new_wall}s (+{add}s past elapsed)")
        # human review note → reviews/<L>-NN-human.md for EVERY stage, so the next
        # author actually receives it (DESIGN §3 — "+ any human review note").
        if note:
            n = state["round"] + 1
            atomic_write_text(run_dir / "reviews" / f"{STAGE_LETTERS[state['stage']]}-{n:02d}-human.md",
                              note + "\n")
        if note:
            append_history(state, {"stage": state["stage"], "round": state["round"],
                                   "actor": "human", "note": note[:200]})
        state["status"] = "authoring"
        state["waiting_for"] = None
        state["stuck_reason"] = None
        save_state(run_dir, state)
        log_line(run_dir, f"human iterate{' with note' if note else ''} → authoring round {state['round']}")
        resume(run_dir, cfg=cfg)
    _print_status(run_dir)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(getattr(args, "config", None))
    run_dir = resolve_run_dir(args.run, cfg)
    _print_status(run_dir, verbose=True)
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    cfg = load_config(getattr(args, "config", None))
    run_dir = resolve_run_dir(args.run, cfg)
    cfg = effective_config(load_state(run_dir))
    assess = run_monitor(run_dir, cfg, dry_run=getattr(args, "dry_run", False))
    if assess:
        print(f"monitor: {assess['assessment']} / {assess['recommended_action']} "
              f"(progressing={assess.get('progressing')})")
        print(assess.get("summary", ""))
    else:
        print("monitor: disabled or produced no assessment")
    return 0


def cmd_watchdog(args: argparse.Namespace) -> int:
    cfg = load_config(getattr(args, "config", None))
    stale = int(getattr(args, "stale_seconds", 1800))
    if getattr(args, "run", None):
        run_dir = resolve_run_dir(args.run, cfg)
        r = watchdog_check(run_dir, stale_seconds=stale)
        print(json.dumps(r))
        return 2 if r["stale"] else 0
    # all runs
    any_stale = False
    root = runs_root(cfg)
    for d in sorted(root.glob("*")):
        if (d / "STATE.json").exists():
            r = watchdog_check(d, stale_seconds=stale)
            print(json.dumps(r))
            any_stale = any_stale or r["stale"]
    return 2 if any_stale else 0


def _print_status(run_dir: Path, verbose: bool = False) -> None:
    state = load_state(run_dir)
    letter = STAGE_LETTERS.get(state["stage"], state["stage"])
    try:
        upd = datetime.fromisoformat(state["updated_at"].replace("Z", "+00:00"))
        age_s = int((datetime.now(timezone.utc) - upd).total_seconds())
    except (ValueError, KeyError):
        age_s = -1
    stale_flag = ""
    if state["status"] in ("authoring", "authored", "reviewing", "deciding") and age_s > 1800:
        stale_flag = f"  ⚠️ STALE ({age_s}s since update — possible hang)"
    print(f"\n=== {state['run_id']} ===")
    print(f"stage:   {state['stage']} ({letter})   gate: {state['gate']}")
    print(f"status:  {state['status']}" +
          (f"  waiting_for={state['waiting_for']}" if state.get("waiting_for") else "") +
          (f"  stuck_reason={state['stuck_reason']}" if state.get("stuck_reason") else "") + stale_flag)
    print(f"round:   {state['round']}   tokens_spent: {state.get('tokens_spent', 0)}   "
          f"updated_at: {state['updated_at']}")
    lv = state.get("last_verdict")
    if lv:
        print(f"verdict: {lv.get('decision')} (confidence {lv.get('confidence')}) — "
              f"{len(lv.get('blocking_issues', []))} blocker(s), "
              f"{len(lv.get('non_blocking_suggestions', []))} nit(s)")
        if verbose and lv.get("blocking_issues"):
            for b in lv["blocking_issues"]:
                print(f"   - [{b.get('severity')}] {b.get('title')} @ {b.get('location')}")
    if verbose:
        print(f"ledger:  {len([e for e in state.get('resolved_ledger', []) if not e.get('cleared')])} "
              f"must-stay-fixed; disputes: {len(state.get('open_disputes', []))}; "
              f"accepted_deviations: {len(state.get('accepted_deviations', []))}")
        mon = Path(run_dir) / "monitor" / "assessment.json"
        if mon.exists():
            try:
                a = json.loads(read_text(mon))
                print(f"monitor: {a.get('assessment')} / {a.get('recommended_action')} "
                      f"(progressing={a.get('progressing')})")
            except (json.JSONDecodeError, ValueError):
                pass
        if (Path(run_dir) / "monitor" / "HALT").exists():
            print("monitor: HALT present")
    if state["status"] == "awaiting_human":
        if state.get("waiting_for") == "approval":
            print("→ orchestra approve <run>   |   orchestra iterate <run> --note '...'")
        else:
            print(f"→ fill {run_dir/'answers.md'}, then: orchestra resume <run>")
    print()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchestra", description=__doc__.splitlines()[0])
    p.add_argument("--config", type=Path, help="path to orchestra.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a new run")
    s.add_argument("slug")
    s.add_argument("--brief", type=Path, help="path to a brief file")
    s.add_argument("--stage", choices=STAGE_NAMES, default="highlevel",
                   help="seed the run at this stage (M1 input contract, e.g. impl_plan)")
    s.add_argument("--highlevel-plan", type=Path,
                   help="pre-approved high-level plan (required when --stage=impl_plan)")
    s.add_argument("--impl-plan", type=Path,
                   help="pre-approved implementation plan (required when --stage=implementation)")
    s.add_argument("--target", choices=["greenfield", "brownfield"])
    s.add_argument("--repo", help="brownfield: path to target git repo")
    s.add_argument("--worktree", help="brownfield: path for git worktree add")
    s.add_argument("--test-command", help="Stage C executed-test gate command (operator-provided)")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("run", help="drive the pipeline")
    s.add_argument("run")
    s.add_argument("--stage", choices=STAGE_NAMES,
                   help="run a specific stage (A/B/C map to highlevel/impl_plan/implementation)")
    s.add_argument("--interactive", action="store_true",
                   help="Stage A is interactive by default on a TTY; this is a no-op hint")
    s.add_argument("--headless", action="store_true",
                   help="force headless Stage A (read-only author) even on a TTY")
    s.add_argument("--dry-run", action="store_true", help="print commands without executing")
    s.add_argument("--stub-verdicts", action="store_true",
                   help="with --dry-run, auto-advance gates to show every stage's commands")
    s.add_argument("--wait", action="store_true", help="wait for the run lock instead of refusing")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("resume", help="continue from STATE.json")
    s.add_argument("run")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("status", help="show run state")
    s.add_argument("run")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("approve", help="clear an approval gate and advance")
    s.add_argument("run")
    s.set_defaults(func=cmd_approve)

    s = sub.add_parser("iterate", help="force another round")
    s.add_argument("run")
    s.add_argument("--note", default="")
    s.add_argument("--add-rounds", type=int, default=5,
                   help="raise max_rounds by N when un-sticking a max_rounds stop")
    s.add_argument("--add-wall-seconds", type=int, default=3600,
                   help="extend the wall-clock budget by N when un-sticking budget_exceeded")
    s.add_argument("--clear-ledger", help="human override: retire a contested ledger key (E2)")
    s.set_defaults(func=cmd_iterate)

    s = sub.add_parser("monitor", help="run the supervisory monitor once")
    s.add_argument("run")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_monitor)

    s = sub.add_parser("watchdog", help="independent dead/hung detection (cron/launchd)")
    s.add_argument("run", nargs="?", help="a run, or omit to scan all runs")
    s.add_argument("--stale-seconds", type=int, default=1800)
    s.set_defaults(func=cmd_watchdog)

    return p


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except (OrchestraError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"error: corrupt JSON (e.g. a hand-edited STATE.json): {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
