#!/usr/bin/env python3
"""rival-review: Cross-model review runner.

Claude Code (planner) + Codex CLI (reviewer) automated consensus loop.
This runner handles transport, parsing, validation, and archiving.
Claude handles user interaction, drafting, and decision-making.

Usage:
    rival-review init
    rival-review review [--resume] [--timeout SEC] [--model MODEL] [--reasoning-effort EFFORT]
    rival-review status
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORK_DIR = ".rival-review"
STATE_FILE = "state.json"
CONTRACT_FILE = "contract.json"
SCHEMA_FILE = "review-schema.json"
GOAL_FILE = "goal.md"
DRAFT_FILE = "current-draft.md"
REVISION_SUMMARY_FILE = "revision-summary.md"
LATEST_REVIEW_FILE = "latest-review.json"
SOURCES_DIR = "sources"
HISTORY_DIR = "history"

EXIT_APPROVED = 0
EXIT_NEEDS_REVISION = 1
EXIT_ERROR = 2
EXIT_BLOCKED = 3
EXIT_INSUFFICIENT_CONTEXT = 4

VALID_VERDICTS = {"approved", "needs_revision", "insufficient_context"}
VALID_SEVERITIES = {"major", "minor", "nit"}

# System criterion IDs always valid regardless of contract
SYSTEM_CRITERION_IDS = {"source_availability"}

REQUIRED_REVIEW_FIELDS = {"approved", "verdict", "round", "issues", "summary", "confidence"}
REQUIRED_ISSUE_FIELDS = {
    "issue_id", "severity", "criterion_id",
    "location", "excerpt", "description", "suggestion",
}

DEFAULT_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "approved": {"type": "boolean"},
        "verdict": {"type": "string", "enum": ["approved", "needs_revision", "insufficient_context"]},
        "round": {"type": "integer"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "issue_id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["major", "minor", "nit"]},
                    "criterion_id": {"type": "string"},
                    "location": {"type": "string"},
                    "excerpt": {"type": "string"},
                    "description": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": list(REQUIRED_ISSUE_FIELDS),
            },
        },
        "summary": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": list(REQUIRED_REVIEW_FIELDS),
}

DEFAULT_CONTRACT = {
    "document_type": "other",
    "reviewer_role": "",
    "review_criteria": [],
    "source_manifest": [],
    "transport": {
        "allow_resume": True,
        "timeout_sec": 1800,
        "sandbox": "read-only",
        "model": "gpt-5.4",
        "reasoning_effort": "xhigh",
    },
    "max_rounds": 0,  # 0 = unlimited (until consensus)
    "after_approval": "present_plan_only",
}

GOAL_TEMPLATE = """# Review Goal

## Objective


## Definition of Done


## After Approval
present_plan_only | execute_plan

## Constraints


## Non-goals


## Stop Conditions
- Codex returns approved=true with zero major issues
- Repeated disagreement (2+ rounds on same issue) → ask user
- max_rounds reached → ask user (set max_rounds=0 for unlimited)
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def work_path(*parts: str) -> Path:
    return Path(WORK_DIR).joinpath(*parts)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def read_text(path: Path) -> str:
    with open(path) as f:
        return f.read()


def round_dir(n: int) -> Path:
    return work_path(HISTORY_DIR, f"round-{n:03d}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_contract(contract: dict) -> list[str]:
    """Validate contract.json structure. Returns list of errors."""
    errors = []
    if not contract.get("reviewer_role"):
        errors.append("reviewer_role is empty")
    criteria = contract.get("review_criteria", [])
    if not criteria:
        errors.append("review_criteria is empty (need at least 1)")
    for i, c in enumerate(criteria):
        if "id" not in c:
            errors.append(f"review_criteria[{i}]: missing 'id'")
        if "description" not in c:
            errors.append(f"review_criteria[{i}]: missing 'description'")
    transport = contract.get("transport", {})
    if "timeout_sec" not in transport:
        errors.append("transport.timeout_sec is missing")
    return errors


def validate_sources(contract: dict, workspace: Path) -> list[str]:
    """Preflight check on source_manifest. Returns list of errors."""
    errors = []
    workspace_real = workspace.resolve()
    for src in contract.get("source_manifest", []):
        if not isinstance(src, str):
            errors.append(f"source_manifest: entry is not a string: {src!r}")
            continue
        if src.startswith("/") or "://" in src:
            errors.append(f"source_manifest: absolute path or URL not allowed: {src}")
            continue
        resolved = (workspace / src).resolve()
        if not resolved.is_relative_to(workspace_real):
            errors.append(f"source_manifest: path escapes workspace: {src}")
            continue
        if not resolved.exists():
            errors.append(f"source_manifest: path does not exist: {src}")
        elif resolved.is_file() and not os.access(resolved, os.R_OK):
            errors.append(f"source_manifest: path not readable: {src}")
    return errors


def validate_readiness(workspace: Path) -> list[str]:
    """Pre-review fail-fast checks. Returns list of errors."""
    errors = []

    # contract.json
    contract_path = work_path(CONTRACT_FILE)
    if not contract_path.exists():
        errors.append(f"{CONTRACT_FILE} does not exist")
        return errors  # can't continue without contract
    try:
        contract = load_json(contract_path)
    except (json.JSONDecodeError, OSError) as e:
        errors.append(f"{CONTRACT_FILE} is not valid JSON: {e}")
        return errors
    errors.extend(validate_contract(contract))

    # goal.md
    goal_path = work_path(GOAL_FILE)
    if not goal_path.exists():
        errors.append(f"{GOAL_FILE} does not exist")
    elif goal_path.stat().st_size == 0:
        errors.append(f"{GOAL_FILE} is empty")

    # current-draft.md
    draft_path = work_path(DRAFT_FILE)
    if not draft_path.exists():
        errors.append(f"{DRAFT_FILE} does not exist")
    elif draft_path.stat().st_size == 0:
        errors.append(f"{DRAFT_FILE} is empty")

    # source manifest
    errors.extend(validate_sources(contract, workspace))

    return errors


def validate_review(obj: dict, contract: dict) -> list[str]:
    """Strict validation of review JSON. Returns list of errors."""
    errors = []

    for f in REQUIRED_REVIEW_FIELDS:
        if f not in obj:
            errors.append(f"missing required field: {f}")

    if "approved" in obj and not isinstance(obj["approved"], bool):
        errors.append("approved must be boolean")
    if "verdict" in obj and obj["verdict"] not in VALID_VERDICTS:
        errors.append(f"verdict must be one of {VALID_VERDICTS}, got: {obj.get('verdict')}")
    if "round" in obj and not isinstance(obj["round"], int):
        errors.append("round must be integer")
    if "confidence" in obj and not isinstance(obj.get("confidence"), (int, float)):
        errors.append("confidence must be number")

    # Verdict/approved consistency check
    verdict = obj.get("verdict")
    approved = obj.get("approved")
    if verdict is not None and approved is not None:
        if verdict == "approved" and not approved:
            errors.append("verdict is 'approved' but approved is false — contradictory")
        if verdict != "approved" and approved:
            errors.append(f"verdict is '{verdict}' but approved is true — contradictory")

    valid_criteria = (
        {c["id"] for c in contract.get("review_criteria", [])}
        | SYSTEM_CRITERION_IDS
    )

    for i, issue in enumerate(obj.get("issues", [])):
        if not isinstance(issue, dict):
            errors.append(f"issues[{i}]: not an object")
            continue
        for f in REQUIRED_ISSUE_FIELDS:
            if f not in issue:
                errors.append(f"issues[{i}]: missing {f}")
        sev = issue.get("severity")
        if sev and sev not in VALID_SEVERITIES:
            errors.append(f"issues[{i}]: invalid severity '{sev}'")
        cid = issue.get("criterion_id")
        if valid_criteria and cid and cid not in valid_criteria:
            errors.append(f"issues[{i}]: criterion_id '{cid}' not in contract")

    return errors


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

def git_workspace_snapshot() -> str:
    """Capture git status excluding .rival-review/."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        filtered = [l for l in lines if f"{WORK_DIR}/" not in l]
        return "\n".join(filtered)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


# ---------------------------------------------------------------------------
# JSONL Parsing
# ---------------------------------------------------------------------------

def parse_codex_output(jsonl_path: Path) -> tuple[str | None, dict | None]:
    """Parse Codex JSONL output.

    Returns (thread_id, review_dict).
    Scans agent_messages from last to first, takes first that parses as
    a valid JSON object with 'approved' field.
    """
    events = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Extract thread_id
    thread_id = None
    for e in events:
        if e.get("type") == "thread.started":
            thread_id = e.get("thread_id")
            break

    # Extract review — scan from last to first item.completed agent_message
    agent_messages = [
        e for e in events
        if (e.get("type") == "item.completed"
            and isinstance(e.get("item"), dict)
            and e["item"].get("type") == "agent_message")
    ]

    for msg in reversed(agent_messages):
        text = msg["item"].get("text", "")
        try:
            obj = json.loads(text) if isinstance(text, str) else text
            if isinstance(obj, dict) and "approved" in obj:
                return thread_id, obj
        except (json.JSONDecodeError, TypeError):
            continue

    return thread_id, None


# ---------------------------------------------------------------------------
# Prompt Generation
# ---------------------------------------------------------------------------

def build_review_prompt(contract: dict, current_round: int, is_revision: bool = False) -> str:
    """Build Codex review prompt dynamically from contract.json."""
    role = contract.get("reviewer_role", "reviewer")
    doc_type = contract.get("document_type", "document")
    criteria = contract.get("review_criteria", [])
    sources = contract.get("source_manifest", [])

    criteria_text = "\n".join(
        f"  - {c['id']}: {c.get('description', c.get('label', ''))}"
        for c in criteria
    )

    source_instructions = ""
    if sources:
        source_list = "\n".join(f"  - {s}" for s in sources)
        source_instructions = f"""Step 3 — Read required sources:
  These are listed in the Source Manifest. Read each one:
{source_list}
  If any source is missing or unreadable, report it as a major issue
  with criterion_id "source_availability".
  Do NOT search beyond these sources and the draft itself."""
    else:
        source_instructions = """Step 3 — Context scope:
  No Source Manifest is provided. Review based solely on the draft
  and materials explicitly referenced within it.
  If context is insufficient to assess a criterion, report it as a
  minor issue rather than guessing. Do NOT freely search the workspace."""

    issue_format = """Every issue must have:
- issue_id: stable kebab-case identifier (e.g. "pacing-slow-middle-act")
- severity: major | minor | nit
- criterion_id: must match one of the criterion IDs above
- location: where in the draft (section, step, paragraph, line)
- excerpt: relevant quote from the draft
- description: what is wrong
- suggestion: concrete fix

Set verdict to one of: approved, needs_revision, insufficient_context.
Set approved=true ONLY if verdict is "approved" (zero major issues).
Set round to {round}.""".format(round=current_round)

    if is_revision:
        return f"""You are a {role} reviewing a revised {doc_type}.
This is round {current_round}.

Step 1 — Read the shared contract:
  Read {WORK_DIR}/goal.md

Step 2 — Read the revised draft:
  Read {WORK_DIR}/{DRAFT_FILE}
  Also read {WORK_DIR}/{REVISION_SUMMARY_FILE} (what changed and why)
  Also read {WORK_DIR}/{LATEST_REVIEW_FILE} (your previous review)

{source_instructions}

Step 4 — Review against each criterion:
{criteria_text}

Focus on:
- Were your previous major/minor issues resolved?
- Did the revision introduce NEW issues?
- Does the draft still meet the objectives in goal.md?

{issue_format}"""

    return f"""You are a {role} reviewing a {doc_type}.
This is round {current_round}.

Step 1 — Read the shared contract:
  Read {WORK_DIR}/goal.md

Step 2 — Read the draft:
  Read {WORK_DIR}/{DRAFT_FILE}

{source_instructions}

Step 4 — Review against each criterion:
{criteria_text}

{issue_format}"""


def build_retry_prompt() -> str:
    return (
        "Your last output could not be parsed as a valid JSON object. "
        "Do NOT re-review or change your assessment. "
        "Just output your previous conclusion again as a single valid JSON "
        "object with fields: approved (boolean), verdict (string), "
        "round (integer), issues (array), summary (string), "
        "confidence (number between 0 and 1). "
        "Each issue needs: issue_id, severity, criterion_id, location, "
        "excerpt, description, suggestion."
    )


# ---------------------------------------------------------------------------
# Transport (streaming with live progress)
# ---------------------------------------------------------------------------

def _stream_codex(
    cmd: list[str],
    prompt: str,
    timeout_sec: int,
    output_path: Path,
) -> int:
    """Run a codex command, stream JSONL, print live progress.

    Returns exit code (-1 on timeout).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        print("Error: codex command not found")
        save_text(output_path, "")
        return -1

    # Send prompt and close stdin
    if proc.stdin:
        proc.stdin.write(prompt)
        proc.stdin.close()

    lines: list[str] = []
    last_heartbeat = start
    files_read: list[str] = []
    commands_run: list[str] = []

    try:
        while True:
            # Check timeout
            elapsed = time.monotonic() - start
            if elapsed > timeout_sec:
                proc.kill()
                proc.wait()
                save_text(output_path, "\n".join(lines))
                print(f"\n  Timed out after {int(elapsed)}s")
                return -1

            line = proc.stdout.readline() if proc.stdout else ""
            if not line and proc.poll() is not None:
                break
            if not line:
                continue

            lines.append(line.rstrip())

            # Parse event for live progress
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            # Heartbeat every 15 seconds
            if time.monotonic() - last_heartbeat >= 15:
                elapsed_s = int(time.monotonic() - start)
                print(f"  ... reviewing ({elapsed_s}s elapsed)", flush=True)
                last_heartbeat = time.monotonic()

            # Track file reads
            if etype == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "command_execution":
                    cmd_text = item.get("command", "")
                    # Detect file reads (cat, sed, head, etc.)
                    if any(r in cmd_text for r in ["cat ", "sed ", "head ", "tail "]):
                        short = cmd_text.split('"')[-2] if '"' in cmd_text else cmd_text[:80]
                        if short not in files_read:
                            files_read.append(short)
                            print(f"  [read] {short}", flush=True)
                    elif cmd_text and cmd_text not in commands_run:
                        commands_run.append(cmd_text)
                        short_cmd = cmd_text[:100] + ("..." if len(cmd_text) > 100 else "")
                        print(f"  [exec] {short_cmd}", flush=True)

    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        save_text(output_path, "\n".join(lines))
        return -1

    save_text(output_path, "\n".join(lines) + "\n" if lines else "")
    elapsed_s = int(time.monotonic() - start)
    print(f"  Codex finished in {elapsed_s}s", flush=True)
    return proc.returncode or 0


def run_codex_fresh(
    prompt: str,
    schema_path: Path,
    timeout_sec: int,
    sandbox: str,
    output_path: Path,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> int:
    """Run fresh codex exec with live progress."""
    cmd = ["codex", "exec"]
    cmd.extend(["--sandbox", sandbox])
    cmd.append("--json")
    cmd.extend(["--output-schema", str(schema_path)])
    if model:
        cmd.extend(["--model", model])
    if reasoning_effort:
        cmd.extend(["--reasoning-effort", reasoning_effort])
    cmd.append("-")

    print(f"  Starting fresh review (sandbox: {sandbox})...", flush=True)
    return _stream_codex(cmd, prompt, timeout_sec, output_path)


def run_codex_resume(
    prompt: str,
    thread_id: str,
    timeout_sec: int,
    output_path: Path,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> int:
    """Run codex exec resume with live progress."""
    cmd = ["codex", "exec", "resume"]
    cmd.append("--json")
    if model:
        cmd.extend(["--model", model])
    if reasoning_effort:
        cmd.extend(["--reasoning-effort", reasoning_effort])
    cmd.append(thread_id)
    cmd.append("-")

    print(f"  Resuming session {thread_id[:12]}...", flush=True)
    return _stream_codex(cmd, prompt, timeout_sec, output_path)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init() -> int:
    """Scaffold .rival-review/ with empty templates."""
    base = Path(WORK_DIR)
    if base.exists():
        print(f"Error: {WORK_DIR}/ already exists. Remove it first or use existing setup.")
        return EXIT_ERROR

    # Create directories
    base.mkdir()
    (base / HISTORY_DIR).mkdir()
    (base / SOURCES_DIR).mkdir()

    # Write templates
    save_json(work_path(CONTRACT_FILE), DEFAULT_CONTRACT)
    save_json(work_path(SCHEMA_FILE), DEFAULT_REVIEW_SCHEMA)
    save_text(work_path(GOAL_FILE), GOAL_TEMPLATE)
    save_text(work_path(DRAFT_FILE), "")

    state = {
        "current_round": 0,
        "status": "initialized",
        "last_verdict": None,
        "active_thread_id": None,
        "last_transport": None,
        "last_review_path": None,
        "current_draft_path": DRAFT_FILE,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    save_json(work_path(STATE_FILE), state)

    # Add to .git/info/exclude
    exclude_path = Path(".git/info/exclude")
    if exclude_path.exists():
        content = read_text(exclude_path)
        if f"{WORK_DIR}/" not in content:
            with open(exclude_path, "a") as f:
                f.write(f"\n{WORK_DIR}/\n")

    print(f"Initialized {WORK_DIR}/")
    print(f"Next: fill in {WORK_DIR}/{GOAL_FILE} and {WORK_DIR}/{CONTRACT_FILE}")
    return EXIT_APPROVED


def cmd_review(
    use_resume: bool = False,
    timeout_override: int | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> int:
    """Run one round of review."""
    workspace = Path.cwd()

    # --- Readiness validation ---
    errors = validate_readiness(workspace)
    if errors:
        print("Review readiness check failed:")
        for e in errors:
            print(f"  - {e}")
        return EXIT_ERROR

    contract = load_json(work_path(CONTRACT_FILE))
    state = load_json(work_path(STATE_FILE))

    current_round = state["current_round"] + 1
    max_rounds = contract.get("max_rounds", 0)  # 0 = unlimited (until consensus)

    if max_rounds > 0 and current_round > max_rounds:
        print(f"Max rounds ({max_rounds}) reached.")
        state["status"] = "blocked"
        state["last_verdict"] = "blocked"
        state["updated_at"] = now_iso()
        save_json(work_path(STATE_FILE), state)
        return EXIT_BLOCKED

    transport_cfg = contract.get("transport", {})
    timeout_sec = timeout_override or transport_cfg.get("timeout_sec", 1800)
    sandbox = transport_cfg.get("sandbox", "read-only")
    allow_resume = transport_cfg.get("allow_resume", True)

    # Model: CLI param > contract > None (Codex default)
    effective_model = model or transport_cfg.get("model")
    effective_reasoning = reasoning_effort or transport_cfg.get("reasoning_effort")

    is_revision = current_round > 1
    prompt = build_review_prompt(contract, current_round, is_revision=is_revision)

    # --- Pre-review guard ---
    pre_snapshot = git_workspace_snapshot()

    # --- Prepare round directory ---
    rdir = round_dir(current_round)
    rdir.mkdir(parents=True, exist_ok=True)

    # Save prompt and contract snapshot
    save_text(rdir / "prompt.md", prompt)
    shutil.copy2(work_path(CONTRACT_FILE), rdir / "contract.json")
    if work_path(GOAL_FILE).exists():
        shutil.copy2(work_path(GOAL_FILE), rdir / "goal.md")

    # Save draft snapshot
    draft_path = work_path(DRAFT_FILE)
    if draft_path.exists():
        shutil.copy2(draft_path, rdir / "draft.md")

    # Save revision summary if exists
    rev_summary = work_path(REVISION_SUMMARY_FILE)
    if rev_summary.exists():
        shutil.copy2(rev_summary, rdir / "revision-summary.md")

    # --- Transport ---
    schema_path = work_path(SCHEMA_FILE)

    requested_transport = "resume" if use_resume else "fresh"
    actual_transport = "fresh"
    fell_back = False
    retry_count = 0
    thread_id = state.get("active_thread_id")
    review = None

    started_at = now_iso()

    if use_resume and allow_resume and thread_id:
        # Try resume — each attempt gets its own raw file
        resume_raw = rdir / "raw-resume.jsonl"
        exit_code = run_codex_resume(
            prompt, thread_id, timeout_sec, resume_raw,
            model=effective_model, reasoning_effort=effective_reasoning,
        )
        if exit_code == 0:
            actual_transport = "resume"
            tid, review = parse_codex_output(resume_raw)
            if review:
                val_errors = validate_review(review, contract)
                if val_errors:
                    # Invalid, retry once in same session
                    retry_raw = rdir / "raw-retry.jsonl"
                    retry_prompt = build_retry_prompt()
                    retry_count = 1
                    exit_code = run_codex_resume(
                        retry_prompt, thread_id, timeout_sec, retry_raw,
                        model=effective_model, reasoning_effort=effective_reasoning,
                    )
                    if exit_code == 0:
                        tid, review = parse_codex_output(retry_raw)
                        if review and validate_review(review, contract):
                            review = None  # still invalid
                    else:
                        review = None
            if review is None:
                fell_back = True
        else:
            fell_back = True

        if fell_back:
            # Fallback to fresh — gets its own raw file
            actual_transport = "fresh"
            fresh_raw = rdir / "raw-fresh.jsonl"
            exit_code = run_codex_fresh(
                prompt, schema_path, timeout_sec, sandbox, fresh_raw,
                model=effective_model, reasoning_effort=effective_reasoning,
            )
            if exit_code >= 0:
                thread_id_new, review = parse_codex_output(fresh_raw)
                if thread_id_new:
                    thread_id = thread_id_new
    else:
        # Default: fresh exec
        fresh_raw = rdir / "raw.jsonl"
        exit_code = run_codex_fresh(
            prompt, schema_path, timeout_sec, sandbox, fresh_raw,
            model=effective_model, reasoning_effort=effective_reasoning,
        )
        if exit_code >= 0:
            thread_id_new, review = parse_codex_output(fresh_raw)
            if thread_id_new:
                thread_id = thread_id_new

    finished_at = now_iso()

    # --- Build transport metadata (saved on all paths) ---
    transport_meta = {
        "round": current_round,
        "requested_transport": requested_transport,
        "actual_transport": actual_transport,
        "fell_back_from_resume": fell_back,
        "retry_count": retry_count,
        "thread_id": thread_id,
        "model": effective_model,
        "reasoning_effort": effective_reasoning,
        "timeout_sec": timeout_sec,
        "sandbox": sandbox if actual_transport == "fresh" else None,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": finished_at,
    }
    save_json(rdir / "transport.json", transport_meta)

    # --- Error: timeout ---
    if exit_code == -1:
        print(f"Error: Codex call timed out after {timeout_sec}s")
        # Do NOT increment current_round on transport failure
        state["status"] = "error"
        state["last_verdict"] = "error"
        state["updated_at"] = now_iso()
        save_json(work_path(STATE_FILE), state)
        return EXIT_ERROR

    # --- Error: no review extracted ---
    if review is None:
        print("Error: Could not extract valid review from Codex output.")
        print(f"Raw output saved to: {rdir}/")
        # Do NOT increment current_round on parse failure
        state["status"] = "error"
        state["last_verdict"] = "error"
        state["updated_at"] = now_iso()
        save_json(work_path(STATE_FILE), state)
        return EXIT_ERROR

    # --- Strict validation: ALL errors block ---
    val_errors = validate_review(review, contract)
    if val_errors:
        print("Error: Review failed strict validation:")
        for e in val_errors:
            print(f"  - {e}")
        # Save the invalid review for debugging but do NOT advance round
        save_json(rdir / "review.json", review)
        state["status"] = "error"
        state["last_verdict"] = "error"
        state["updated_at"] = now_iso()
        save_json(work_path(STATE_FILE), state)
        return EXIT_ERROR

    # --- Post-review guard ---
    post_snapshot = git_workspace_snapshot()
    if pre_snapshot != post_snapshot:
        print("ABORT: Workspace was modified during review.")
        print("Pre-review and post-review git status differ.")
        state["status"] = "error"
        state["last_verdict"] = "error"
        state["updated_at"] = now_iso()
        save_json(work_path(STATE_FILE), state)
        return EXIT_ERROR

    # --- Success: save results and advance round ---
    save_json(rdir / "review.json", review)
    save_json(work_path(LATEST_REVIEW_FILE), review)

    # --- Determine verdict ---
    verdict = review.get("verdict", "needs_revision")
    approved = review.get("approved", False)

    if verdict == "approved" and approved:
        status = "approved"
        exit_rc = EXIT_APPROVED
    elif verdict == "insufficient_context":
        status = "insufficient_context"
        exit_rc = EXIT_INSUFFICIENT_CONTEXT
    else:
        status = "needs_revision"
        exit_rc = EXIT_NEEDS_REVISION

    # --- Update state ---
    review_path = str(rdir / "review.json")
    state.update({
        "current_round": current_round,
        "status": status,
        "last_verdict": verdict,
        "active_thread_id": thread_id,
        "last_transport": actual_transport,
        "last_review_path": review_path,
        "updated_at": now_iso(),
    })
    save_json(work_path(STATE_FILE), state)

    # --- Output ---
    confidence = review.get("confidence", 0)
    issues = review.get("issues", [])
    n_issues = len(issues)
    major_count = sum(1 for i in issues if i.get("severity") == "major")
    minor_count = sum(1 for i in issues if i.get("severity") == "minor")
    nit_count = sum(1 for i in issues if i.get("severity") == "nit")

    print()
    print(f"{'=' * 60}")
    print(f"Round {current_round} | {verdict} | confidence: {confidence:.2f}")
    print(f"Issues: {major_count} major, {minor_count} minor, {nit_count} nit")
    print(f"{'=' * 60}")

    if issues:
        for issue in issues:
            sev = issue.get("severity", "?").upper()
            iid = issue.get("issue_id", "?")
            desc = issue.get("description", "")
            # Truncate long descriptions
            if len(desc) > 120:
                desc = desc[:117] + "..."
            print(f"  [{sev}] {iid}")
            print(f"        {desc}")
        print()

    summary = review.get("summary", "")
    if summary:
        print(f"Summary: {summary}")
        print()

    print(f"Review saved: {review_path}")

    return exit_rc


def cmd_status() -> int:
    """Show current review status."""
    state_path = work_path(STATE_FILE)
    if not state_path.exists():
        print(f"Not initialized. Run: rival-review init")
        return EXIT_ERROR

    state = load_json(state_path)
    r = state.get("current_round", 0)
    status = state.get("status", "unknown")
    verdict = state.get("last_verdict", "-")
    tid = state.get("active_thread_id", "-")
    transport = state.get("last_transport", "-")
    review_path = state.get("last_review_path", "-")

    tid_short = tid[:12] + "..." if tid and len(tid) > 12 else (tid or "-")

    print(f"Round {r} | {status} | verdict: {verdict} | "
          f"transport: {transport} | thread: {tid_short}")
    print(f"Last review: {review_path}")

    return EXIT_APPROVED


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="rival-review",
        description="Cross-model review runner: Claude + Codex consensus loop",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Scaffold .rival-review/ directory")

    review_p = sub.add_parser("review", help="Run one round of review")
    review_p.add_argument("--resume", action="store_true",
                          help="Attempt to resume previous Codex session")
    review_p.add_argument("--timeout", type=int, default=None,
                          help="Timeout in seconds (overrides contract)")
    review_p.add_argument("--model", type=str, default=None,
                          help="Codex model override")
    review_p.add_argument("--reasoning-effort", type=str, default=None,
                          help="Codex reasoning effort")

    sub.add_parser("status", help="Show current review status")

    args = parser.parse_args()

    if args.command == "init":
        return cmd_init()
    elif args.command == "review":
        return cmd_review(
            use_resume=args.resume,
            timeout_override=args.timeout,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
        )
    elif args.command == "status":
        return cmd_status()
    else:
        parser.print_help()
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
