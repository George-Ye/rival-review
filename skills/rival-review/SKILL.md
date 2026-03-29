---
name: rival-review
description: Use when any document, plan, or draft needs cross-model review — orchestrates Claude Code (author) and Codex CLI (reviewer) via the rival_review.py runner until consensus
---

# Rival Review

You (Claude Code) = author + orchestrator.
Codex CLI = independent reviewer.
`rival_review.py` = runner that handles transport, parsing, validation, archiving.

- `.rival-review/goal.md` = human-readable shared contract
- `.rival-review/contract.json` = machine-readable canonical contract
- `.rival-review/state.json` = runner state (round, verdict, thread_id)

Files are truth, not memory.

---

## Prerequisites

```bash
command -v codex >/dev/null && echo "codex: OK" || echo "codex: MISSING"
command -v python3 >/dev/null && echo "python3: OK" || echo "python3: MISSING"
```

Both must be present. No other dependencies (no jq, no pip packages).

---

## Setup

```bash
python3 rival_review.py init
```

This scaffolds `.rival-review/` with empty templates. You then fill them in.

---

## Workflow

### Phase 0 — Intent Capture

Ask the user:

1. **Document type** — code_plan, product_spec, novel, marketing, or other?
2. **Objective** — What is the end goal?
3. **After approval** — Present the final draft only, or also execute it?
4. **Definition of done** — What counts as "finished"?
5. **Constraints** — Off-limits areas, must-preserve behaviors?
6. **Non-goals** — What is explicitly out of scope?
7. **Source materials** — What files must the reviewer read?

Write human-readable answers to `.rival-review/goal.md`.

Write machine-readable contract to `.rival-review/contract.json`:

```json
{
  "document_type": "code_plan",
  "reviewer_role": "Senior software architect",
  "review_criteria": [
    {
      "id": "safety",
      "label": "Safety",
      "description": "Will this break existing functionality or data?"
    }
  ],
  "source_manifest": [
    "src/auth/middleware.ts",
    "tests/auth/"
  ],
  "transport": {
    "allow_resume": true,
    "timeout_sec": 1800,
    "sandbox": "read-only"
  },
  "max_rounds": 5,
  "after_approval": "execute_plan"
}
```

**Default templates by document type** (user can override):

| Type | Default Reviewer | Default Criteria |
|------|-----------------|------------------|
| code_plan | Senior software architect | safety, correctness, completeness, efficiency, clarity |
| product_spec | Senior product manager | feasibility, completeness, clarity, consistency, scope |
| novel | Senior developmental editor | plot, character, pacing, worldbuilding, voice |
| marketing | Senior marketing strategist | messaging, audience, brand, action, differentiation |
| other | (user specifies) | (user specifies) |

**Source Manifest rules:**
- Only workspace-local relative paths allowed
- If user provides URLs or external links, pre-fetch to `.rival-review/sources/`
  and list the local path in source_manifest
- Runner validates all paths before calling Codex (preflight check)

---

### Phase 1 — Draft

1. Read `.rival-review/goal.md` and `contract.json`
2. Generate (or adopt existing) draft
3. Write to `.rival-review/current-draft.md`
4. The draft SHOULD list specific sources or sections it references
5. Tell user: "Draft ready. Starting review..."

---

### Phase 2 — Review

Run the runner. **Use `run_in_background: true`** because Codex review
can take 10+ minutes with high-reasoning models, exceeding the Bash tool's
timeout limit:

```bash
python3 rival_review.py review
```

The runner handles everything:
- Builds the Codex prompt dynamically from `contract.json`
- Calls `codex exec --sandbox read-only --json --output-schema ...`
- Parses JSONL, extracts thread_id and review
- Validates review against strict schema (all errors block)
- Saves raw output, review, transport metadata, and snapshots to history
- Updates `state.json` and `latest-review.json`

**Exit codes tell you what happened:**

| Code | Meaning | Your action |
|------|---------|-------------|
| 0 | approved | → Phase 4 |
| 1 | needs_revision | → Phase 3 (revise and re-review) |
| 2 | error | Check output, fix issue, retry |
| 3 | blocked | Max rounds reached — ask user |
| 4 | insufficient_context | Sources missing — fix manifest |

Check status anytime:

```bash
python3 rival_review.py status
```

---

### Phase 3 — Revision Loop

#### 3a. Revise (with independent judgment)

Read `.rival-review/latest-review.json` and **critically evaluate each issue**.
You are not Codex's subordinate — you are an equal participant in a dialogue.

For each issue, independently decide:
- **Agree and fix** — the issue is valid, change the draft
- **Partially agree** — the concern is real but the suggested fix is wrong;
  apply your own solution and explain why
- **Disagree and defend** — the issue is incorrect, unnecessary, or based on
  a misunderstanding; explain your reasoning clearly so Codex can reconsider

Do NOT blindly accept all issues. Ask yourself:
- Is this issue actually a problem, or is Codex being overly cautious?
- Does the suggested fix make the draft better or worse?
- Is Codex applying the right criteria, or misinterpreting the goal?

Update `.rival-review/current-draft.md` with your changes.

Write `.rival-review/revision-summary.md`:
```markdown
# Round N Revision

## Accepted and Fixed
- [major] <issue_id>: <what changed and why you agree>
- [minor] <issue_id>: <what changed>

## Partially Accepted
- [major] <issue_id>: <what you changed instead and why>

## Rejected
- [minor] <issue_id>: <why you disagree — give specific reasoning>
- [nit] <issue_id>: <why this is not an issue>

## Deferred
- [nit] <issue_id>: <why not now>
```

This revision summary is what Codex reads next round. If your reasoning
is sound, Codex should drop the issue. If Codex still disagrees after
seeing your argument, it becomes a true disagreement → escalate to user.

#### 3b. Re-review

```bash
python3 rival_review.py review
```

Or, to attempt resuming the previous Codex session:

```bash
python3 rival_review.py review --resume
```

`--resume` is optional optimization only. Fresh exec is the default and
always has `--sandbox read-only` + `--output-schema`. If resume fails,
the runner automatically falls back to fresh exec.

#### 3c. After each round

Based on exit code:
- **0 (approved)** → Phase 4
- **1 (needs_revision)** → go to 3a
- **2 (error)** → diagnose, fix, retry
- **3 (blocked)** → show remaining issues, ask user: raise limit / accept as-is / abort
- **4 (insufficient_context)** → fix source manifest, retry

**Disagreement detection:** if the same issue persists across 2+ consecutive
rounds (by issue_id), stop and present to user: "We disagree on <issue>. Your call."

---

### Phase 4 — User Confirmation

Check `contract.json` `after_approval`:

If `present_plan_only`:
- Present the final draft, review summary, confidence, rounds taken
- **Stop here. Do not ask about execution.**

If `execute_plan`:
- Present the same information
- Ask: **"Codex approved (confidence: X.XX, N rounds). Execute?"**
- Do NOT proceed without explicit user confirmation

---

### Phase 5 — Execute

Only when `after_approval` is `execute_plan` AND user confirms.

1. Execute the draft using normal Claude Code workflow
2. Keep `.rival-review/` for reference

---

## Runner CLI Reference

```bash
# Scaffold directory and templates
python3 rival_review.py init

# Run one review round (fresh exec, default)
python3 rival_review.py review

# Run with resume optimization
python3 rival_review.py review --resume

# Override timeout or model
python3 rival_review.py review --timeout 600 --model o3

# Check current state
python3 rival_review.py status
```

---

## Defaults & Overrides

| Setting | Default | Override |
|---------|---------|---------|
| max_rounds | 5 | Set in contract.json |
| codex model | codex default | `--model o3` |
| reasoning_effort | codex default | `--reasoning-effort high` |
| timeout | 300s | `--timeout 600` |
| transport | fresh (default) | `--resume` to attempt resume |

---

## Hard Rules

1. **Fresh exec is the default**. `--resume` is opt-in optimization only.
2. **All state on disk**. Cross-round continuity = files, not model memory.
3. **Workspace guard**: runner checks `git status` before/after every Codex call.
4. **Strict validation**: all schema errors block. No warn-and-continue.
5. **Failed rounds don't consume round count** toward max_rounds.
6. **User decides** when models disagree for 2+ rounds on same issue.
7. **No execution without explicit user confirmation** (when execute_plan).
8. **Source Manifest: workspace-local paths only**. External content must be
   pre-fetched to `.rival-review/sources/`.
9. **contract.json is the machine contract**. Runner reads criteria, role,
   sources, and transport config from it. goal.md is for humans.
10. **Each round archived** with: draft, prompt, raw JSONL, review, transport
    metadata, contract snapshot. Resume/retry get separate raw files.
11. **verdict/approved consistency** is enforced by the runner.
