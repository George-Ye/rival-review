---
name: rival-review
description: Use when any document, plan, or draft needs cross-model review — orchestrates Claude Code (author) and Codex CLI (reviewer) via the rival_review.py runner until consensus
---

# Rival Review

## Role Model

- **Claude Code** = author + orchestrator. Writes drafts, revises, makes judgment calls.
- **Codex CLI** = independent reviewer. Reviews drafts, flags issues, challenges the author.
- **User** = final arbiter. Resolves disagreements, approves execution.

Claude and Codex are **equals with different roles**, not superior and subordinate.
Codex does not have final say. Claude does not blindly obey.
When they disagree and neither backs down, the user decides.

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
    "sandbox": "read-only",
    "model": "gpt-5.4",
    "reasoning_effort": "xhigh"
  },
  "max_rounds": 0,
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
- Reads model (`gpt-5.4`) and reasoning effort (`xhigh`) from `contract.json`
- Calls `codex exec --sandbox read-only --json --output-schema ...`
- Streams live progress (heartbeat, file reads, commands)
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

#### 3a. Revise (as an equal, not a subordinate)

Read `.rival-review/latest-review.json`.

**You and Codex are equals.** You are the author; Codex is the reviewer.
Neither has authority over the other. The user is the final arbiter.

**Every issue must be accounted for.** For each `issue_id` in the review,
you MUST place it in exactly one of these buckets — no issue may be skipped:

- **Accept** — the issue is valid. Fix it in the draft.
- **Partially accept** — the concern has merit but the suggested fix is wrong
  or excessive. Apply your own solution.
- **Reject** — the issue is incorrect, unnecessary, or misunderstands the goal.
- **Defer** — valid but out of scope for this round.

**Any severity can be rejected.** Including `major`. There is no rule that
major issues must be accepted. The only requirement is that your reasoning
must be substantive.

**Rejections and partial accepts must cite evidence.** You cannot just say
"I disagree." You must reference at least one of:
- `goal.md` — "The goal explicitly says X, so this issue doesn't apply"
- `contract.json` — "The criteria definition for Y means Z"
- Source manifest materials — "The actual code at path X shows Y"
- `current-draft.md` — "Step N already covers this because..."

Update `.rival-review/current-draft.md` with your changes.

Write `.rival-review/revision-summary.md`:
```markdown
# Round N Revision

## Accepted
- [major] <issue_id>: <what changed and why>
- [minor] <issue_id>: <what changed>

## Partially Accepted
- [major] <issue_id>: Codex suggested X, but I did Y instead because
  goal.md says "..." (cite specific text)

## Rejected
- [minor] <issue_id>: This is not an issue because contract.json
  criterion "completeness" means Z, and the draft already covers it
  at Step 3 (cite specific location)

## Deferred
- [nit] <issue_id>: Valid but out of scope per goal.md non-goals
```

Codex reads this next round. If your reasoning is sound, Codex should
drop the issue. If Codex still insists after seeing your evidence,
it becomes a **true disagreement** → escalate to user (see below).

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

**Disagreement detection:** if the same `issue_id` persists across 2+
consecutive rounds after Claude has provided evidence-based rejection,
it is a true disagreement → escalate to user.

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

## User Visibility

The user is the final arbiter — not a passive observer. The system must
keep the user informed of key state and surface disagreements proactively.

### During each review round (runner output)
- Round number
- Transport method: fresh / resume (and whether fallback occurred)
- Model and reasoning effort being used
- Elapsed time
- Live progress: files being read, commands being run

### After each review round (Claude reports to user)
- Verdict and confidence
- Issue counts: major / minor / nit
- Top 1-3 key issues (one sentence each)
- Path to full review file

### After Claude revises (Claude reports to user)
- How many issues accepted / partially accepted / rejected / deferred
- For each rejection or partial accept: one-sentence reason
- Clear statement of what changed in the draft

### Must escalate to user immediately
- Same issue_id unresolved for 2+ consecutive rounds (true disagreement)
- `max_rounds` reached without consensus
- `insufficient_context` (exit 4)
- `blocked` (exit 3)
- Claude and Codex have fundamentally opposing views on the approach

### Do NOT flood the user with
- Full JSONL event streams
- Low-level transport details
- Issues that were accepted without controversy
- Unchanged state between rounds

---

## Runner CLI Reference

```bash
# Scaffold directory and templates
python3 rival_review.py init

# Run one review round (fresh exec, default)
python3 rival_review.py review

# Run with resume optimization
python3 rival_review.py review --resume

# Override model or timeout (overrides contract.json)
python3 rival_review.py review --model o3 --timeout 600

# Check current state
python3 rival_review.py status
```

---

## Defaults & Overrides

| Setting | Default | Source | Override |
|---------|---------|-------|---------|
| model | gpt-5.4 | contract.json | `--model o3` |
| reasoning_effort | xhigh | contract.json | `--reasoning-effort high` |
| max_rounds | 0 (unlimited) | contract.json | set in contract.json |
| timeout | 1800s | contract.json | `--timeout 600` |
| transport | fresh | - | `--resume` to attempt resume |

**Priority**: CLI flag > contract.json > runner default.

---

## Hard Rules

1. **Claude and Codex are equals.** Neither has final authority. User decides ties.
2. **Every issue must be accounted for.** No skipping. Accept / partial / reject / defer.
3. **Rejections require evidence.** Cite goal.md, contract.json, sources, or draft.
4. **Fresh exec is the default**. `--resume` is opt-in optimization only.
5. **All state on disk**. Cross-round continuity = files, not model memory.
6. **Workspace guard**: runner checks `git status` before/after every Codex call.
7. **Strict validation**: all schema errors block. No warn-and-continue.
8. **Failed rounds don't consume round count** toward max_rounds.
9. **User decides** when models disagree for 2+ rounds on same issue.
10. **No execution without explicit user confirmation** (when execute_plan).
11. **Source Manifest: workspace-local paths only**. External content must be
    pre-fetched to `.rival-review/sources/`.
12. **contract.json is the machine contract**. Runner reads model, reasoning_effort,
    criteria, role, sources, and transport config from it.
13. **Default model is gpt-5.4, default reasoning is xhigh.** Explicit in contract,
    not inherited from CLI global defaults.
14. **Each round archived** with: draft, prompt, raw JSONL, review, transport
    metadata, contract snapshot. Resume/retry get separate raw files.
15. **User sees key info by default**: verdict, issue summary, disagreements.
    Not raw JSONL or low-level transport noise.
