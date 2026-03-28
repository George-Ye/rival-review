---
name: rival-review
description: Use when implementation plans need cross-model review — orchestrates Claude Code (planner) and Codex CLI (reviewer) in automated review cycles with file-based state until consensus
---

# Rival Review

You (Claude Code) = planner + orchestrator.
Codex CLI = independent reviewer grounded in real code.
`.plan-review/goal.md` = the shared contract both models work from.

State lives in `.plan-review/`. Files are truth, not memory.

---

## Prerequisites

```bash
command -v codex >/dev/null && echo "codex: OK" || echo "codex: MISSING"
command -v jq >/dev/null && echo "jq: OK" || echo "jq: MISSING"
```

Both must be present. Stop if either is missing.

**Timeout**: all Codex calls use the Bash tool's `timeout` parameter
(set to 300000ms). This is the only timeout mechanism the skill relies on.

---

## Setup (first invocation only)

1. Create directories:
   ```bash
   mkdir -p .plan-review/history
   ```

2. Exclude from git tracking (local-only, does not modify repo files):
   ```bash
   grep -qxF '.plan-review/' .git/info/exclude 2>/dev/null \
     || echo '.plan-review/' >> .git/info/exclude
   ```

3. Write output schema to `.plan-review/review-schema.json`:
   ```json
   {
     "type": "object",
     "additionalProperties": false,
     "properties": {
       "approved": { "type": "boolean" },
       "round": { "type": "integer" },
       "issues": {
         "type": "array",
         "items": {
           "type": "object",
           "additionalProperties": false,
           "properties": {
             "severity": { "type": "string", "enum": ["major", "minor", "nit"] },
             "category": { "type": "string", "enum": ["safety", "correctness", "completeness", "efficiency", "clarity"] },
             "description": { "type": "string" },
             "suggestion": { "type": "string" }
           },
           "required": ["severity", "category", "description", "suggestion"]
         }
       },
       "summary": { "type": "string" },
       "confidence": { "type": "number", "minimum": 0, "maximum": 1 }
     },
     "required": ["approved", "round", "issues", "summary", "confidence"]
   }
   ```

4. Initialize `.plan-review/codex-session.json`:
   ```json
   {
     "thread_id": null,
     "model": null,
     "reasoning_effort": null,
     "created_at": "<ISO 8601>",
     "current_round": 0,
     "max_rounds": 5,
     "status": "in_progress"
   }
   ```

---

## Shared Parsing Logic

Used in Phase 2 and Phase 3 whenever extracting a review from Codex JSONL
output. Defined once here, referenced by phase.

```bash
# Extract thread_id (only meaningful on fresh exec, not resume)
THREAD_ID=$(jq -r 'select(.type == "thread.started") | .thread_id' \
  < "$RR_TMP/output.jsonl" | head -1)

# Extract review — scan agent_messages from last to first,
# take the first whose .item.text parses as a JSON object
REVIEW=$(jq -s '
  [.[] | select(.type == "item.completed" and .item.type == "agent_message")]
  | reverse
  | [.[] | .item.text
     | if type == "string" then fromjson? else . end
     | select(type == "object")]
  | .[0]
' < "$RR_TMP/output.jsonl" 2>/dev/null)

# Validate: must be a non-null JSON object
if ! echo "$REVIEW" | jq -e 'type == "object"' >/dev/null 2>&1; then
  REVIEW=""  # signals parse failure to caller
fi
```

---

## Shared Guard Logic

Used before and after every Codex call.

```bash
# Pre-review guard — snapshot workspace state (excluding .plan-review/)
git status --porcelain | grep -v '\.plan-review/' > "$RR_TMP/pre.txt" || true

# Post-review guard — compare against snapshot
git status --porcelain | grep -v '\.plan-review/' > "$RR_TMP/post.txt" || true
if ! diff -q "$RR_TMP/pre.txt" "$RR_TMP/post.txt" >/dev/null 2>&1; then
  echo "ABORT: workspace modified during review"
  # Stop the review loop and warn user
fi
```

---

## Workflow

### Phase 0 — Intent Capture

Before generating any plan, ask the user:

1. **Objective** — What is the end goal of this task?
2. **After approval** — Present the final plan only, or also execute it?
3. **Definition of done** — What counts as "finished"?
4. **Constraints** — Off-limits directories, must-preserve behaviors, hard rules?
5. **Non-goals** — What is explicitly out of scope?

Write answers to `.plan-review/goal.md`:

```markdown
# Review Goal

## Objective
<what the user wants to achieve>

## Definition of Done
<measurable criteria for completion>

## After Approval
present_plan_only | execute_plan

## Constraints
- <hard rules, forbidden areas, must-preserve behaviors>

## Non-goals
- <explicitly out of scope>

## Stop Conditions
- Codex returns approved=true with zero major issues
- Repeated disagreement (2+ rounds on same issue) → ask user
- max_rounds reached → ask user
```

This file is the **shared contract**. Every Codex prompt must reference it.

---

### Phase 1 — Plan

1. Read `.plan-review/goal.md` to ground your work
2. Generate (or adopt existing) implementation plan
3. Write to `.plan-review/current-plan.md`
4. **The plan MUST list specific files and modules it touches** — Codex needs
   these for code-grounded review
5. Tell user: "Plan ready. Starting Codex review..."

---

### Phase 2 — First Review (Fresh Exec)

**2.1 Create temp + pre-guard:**
```bash
RR_TMP=$(mktemp -d)
```
Run pre-review guard (see Shared Guard Logic).

**2.2 Call Codex (stdin mode, flags before positional args):**

```bash
printf '%s' 'You are a senior software architect reviewing an implementation
plan against a real codebase. This is round 1.

Step 1 — Read the shared contract:
  Read .plan-review/goal.md to understand objectives, constraints, and
  definition of done.

Step 2 — Read the plan:
  Read .plan-review/current-plan.md

Step 3 — Read the actual code:
  For every file, module, and test mentioned in the plan, read the source.
  If the plan does not list specific files, search the repository yourself
  to find relevant implementations before reviewing.

Step 4 — Review (grounded in real code, not just plan text):
  - safety: Will this break existing functionality or data?
  - correctness: Is the approach sound given the actual code structure?
  - completeness: Missing steps, edge cases, or dependencies?
  - efficiency: Unnecessary complexity?
  - clarity: Can a developer follow this unambiguously?

Every issue must include a concrete suggestion.
Set approved=true ONLY if there are zero major issues.
Set round to 1.' \
| codex exec \
  --sandbox read-only \
  --json \
  --output-schema .plan-review/review-schema.json \
  - \
  2>/dev/null > "$RR_TMP/output.jsonl"
```

Use Bash tool `timeout: 300000` for this call.
If timed out: tell user, ask retry or abort.

**2.3 Parse + validate:**
Run Shared Parsing Logic. If `REVIEW` is empty, tell user and abort.

**2.4 Save state:**
- `codex-session.json` → update thread_id, model, reasoning_effort, round=1
- `latest-review.json` → write `$REVIEW`
- `history/round-1-plan.md` → snapshot of current-plan.md
- `history/round-1-review.json` → snapshot of review

**2.5 Post-guard + cleanup:**
Run post-review guard. Abort if workspace changed.
```bash
rm -rf "$RR_TMP"
```

**2.6 Branch:**
- `approved: true` → Phase 4
- `approved: false` → Phase 3

---

### Phase 3 — Revision Loop

#### 3a. Revise

1. Read `latest-review.json`
2. Fix all `major` (must), address `minor` (should), judgment on `nit`
3. Update `.plan-review/current-plan.md`
4. Write `.plan-review/revision-summary.md`:
   ```markdown
   # Round N Revision
   ## Addressed
   - [major] <issue>: <what changed>
   - [minor] <issue>: <what changed>
   ## Deferred
   - [nit] <issue>: <why>
   ```

#### 3b. Re-review

**Create temp + pre-guard** (same as 2.1).

Read `current_round` from `codex-session.json` and increment.
Substitute the real integer into all prompts before sending.

**Primary path — Resume session:**

Only if `thread_id` exists in `codex-session.json`:

```bash
printf '%s' "The plan has been revised based on your review.

Read these files now:
1. .plan-review/goal.md (shared contract — review against this)
2. .plan-review/current-plan.md (updated plan)
3. .plan-review/revision-summary.md (what changed and why)
4. .plan-review/latest-review.json (your previous review)

Also re-read any source files relevant to the changes.

Focus on:
- Were your previous major/minor issues resolved?
- Did the revision introduce NEW issues?
- Does the plan still meet the objectives in goal.md?

Output ONLY valid JSON, no markdown fences, no explanation.
The JSON must have these fields:
  approved (boolean), round (integer, set to ${CURRENT_ROUND}),
  issues (array of objects), summary (string), confidence (number 0-1).
Each issue object has: severity, category, description, suggestion." \
| codex exec resume \
  --json \
  "$THREAD_ID" \
  - \
  2>/dev/null > "$RR_TMP/output.jsonl"
```

Use Bash tool `timeout: 300000`.

**Validate resume output:**

Run Shared Parsing Logic. If `REVIEW` is empty (parse failed), send ONE
retry in same session. Use natural language, no pseudo-JSON templates:

```bash
printf '%s' "Your last output could not be parsed as a JSON object. \
Do NOT re-review or change your assessment. Just output your previous \
conclusion again as a single valid JSON object with fields: approved \
(boolean), round (integer), issues (array), summary (string), \
confidence (number between 0 and 1)." \
| codex exec resume \
  --json \
  "$THREAD_ID" \
  - \
  2>/dev/null > "$RR_TMP/output.jsonl"
```

Re-run Shared Parsing Logic. If still empty → trigger fallback.

**Fallback path — Fresh exec:**

Trigger when ANY of:
- No thread_id in session file
- resume exits non-zero or times out
- Output not a valid JSON object after 1 retry
- Post-guard detects unexpected workspace changes

```bash
printf '%s' "You are a plan reviewer continuing a multi-round review.

Read ALL of these:
1. .plan-review/goal.md (shared contract)
2. .plan-review/current-plan.md (current plan)
3. .plan-review/revision-summary.md (latest changes)
4. All files in .plan-review/history/ (full review history)
5. Source files referenced in the plan

This is round ${CURRENT_ROUND}. Evaluate the plan holistically against
the real codebase and the objectives defined in goal.md.
Set round to ${CURRENT_ROUND}." \
| codex exec \
  --sandbox read-only \
  --json \
  --output-schema .plan-review/review-schema.json \
  - \
  2>/dev/null > "$RR_TMP/output.jsonl"
```

Save new thread_id from fresh session.

#### 3c. After each round

1. Save review → `latest-review.json` + `history/round-N-review.json`
2. Save plan → `history/round-N-plan.md`
3. Update `codex-session.json` (increment round)
4. Post-guard + cleanup temp
5. **Disagreement**: same issue unresolved 2+ consecutive rounds →
   stop loop, present to user: "We disagree on <issue>. Your call."
6. **Max rounds**: `current_round >= max_rounds` and not approved →
   show remaining issues, ask user: raise limit / force-approve / abort

---

### Phase 4 — User Confirmation

**First, check `goal.md` After Approval mode:**

If `present_plan_only`:
- Present the final plan, review summary, confidence, rounds taken
- Present any remaining minor/nit issues
- **Stop here. Do not ask about execution.**

If `execute_plan`:
- Present the same information
- Ask: **"Codex reviewer approved (confidence: X.XX, N rounds). Execute?"**
- Do NOT proceed without explicit user confirmation

### Phase 5 — Execute

Only reached when `goal.md` says `execute_plan` AND user confirms.

1. Update `codex-session.json` → status: `"approved"`
2. Execute the plan using normal Claude Code workflow
3. Keep `.plan-review/` for reference

---

## Defaults & Overrides

| Setting | Default | Override example |
|---------|---------|-----------------|
| max_rounds | 5 | "max 3 rounds" |
| codex model | codex default | "use o3" → add `--model o3` |
| reasoning_effort | codex default | "reasoning high" → add `--reasoning-effort high` |
| timeout | 300s | "timeout 5 min" → Bash tool timeout: 300000 |
| stateless | off | "no resume" → always fresh exec |

---

## Hard Rules

1. **Never `resume --last`**. Always explicit `thread_id`.
2. **Save `thread_id`** to `codex-session.json` immediately after extraction.
3. **All state on disk**. Cross-round continuity = files, not model memory.
4. **`git status --porcelain` guard** before and after every Codex call,
   excluding `.plan-review/`. Use `|| true` after `grep -v` and
   `if ! diff -q` for safe exit-code handling. Use `mktemp -d`.
5. **Fresh exec = `--sandbox read-only`**. Resume cannot enforce sandbox,
   so rely on prompt constraint + guard detection.
6. **User decides** when models disagree for 2+ rounds.
7. **No execution without explicit user confirmation** (when execute_plan).
8. **`--output-schema` takes a file path**: `.plan-review/review-schema.json`.
9. **Timeout every Codex call** via Bash tool `timeout` parameter (300000ms).
10. **Every Codex prompt must reference `goal.md` first**.
11. **Do not modify `.gitignore`**. Use `.git/info/exclude`.
12. **JSONL parsing**: thread_id from `type == "thread.started"`;
    review from `type == "item.completed"` + `.item.type == "agent_message"`,
    extract `.item.text`, `fromjson` if string, validate `type == "object"`.
    Scan from last to first, take first parseable object. Use `jq -s`.
13. **No literal placeholders** in prompts sent to Codex. Substitute all
    variables before sending.
14. **Prompts via stdin**: `printf | codex exec --flags... -` pattern.
    Flags always before `-`.
