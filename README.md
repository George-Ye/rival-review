# rival-review

Cross-model review for any document: **Claude Code writes, Codex CLI reviews**, automated loop until consensus.

Works for code plans, novel outlines, product specs, marketing copy — anything that benefits from an independent reviewer.

## How It Works

```
You (user)          Claude Code (author)          rival_review.py          Codex CLI (reviewer)
    |                      |                            |                        |
    |--- "review X" ----→  |                            |                        |
    |                      |--- fills goal.md -------→  |                        |
    |                      |--- fills contract.json --→ |                        |
    |                      |--- writes draft ---------→ |                        |
    |                      |                            |                        |
    |                      |--- review ---------------→ |--- codex exec ------→  |
    |                      |                            |                        |--- reads sources
    |                      |                            |                        |--- reviews draft
    |                      |                            | ←--- JSONL ------------|
    |                      |                            |--- parse + validate    |
    |                      |                            |--- archive to history  |
    |                      | ←--- exit code + review ---|                        |
    |                      |                            |                        |
    |                      |--- revise draft --------→  |                        |
    |                      |--- review ---------------→ |--- codex exec ------→  |
    |                      | ←--- approved! ------------|                        |
    |                      |                            |                        |
    | ←-- "approved. go?"  |                            |                        |
```

- **Claude Code** handles user interaction, drafting, and decision-making
- **rival_review.py** handles Codex transport, JSONL parsing, validation, and archiving
- **Codex CLI** reviews the draft against real source materials

## Requirements

| Dependency | Version | Required |
|------------|---------|----------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | latest | yes |
| [Codex CLI](https://github.com/openai/codex) | >= 0.97.0 | yes |
| Python 3 | >= 3.9 | yes |

No pip packages needed. Zero external Python dependencies.

## Install

```bash
git clone https://github.com/George-Ye/rival-review.git
claude plugin add ./rival-review
```

## Quick Start

```bash
# 1. Initialize (scaffolds .rival-review/ with empty templates)
python3 rival_review.py init

# 2. Claude fills in goal.md, contract.json, and current-draft.md
#    (this happens through the skill, not manually)

# 3. Run review
python3 rival_review.py review

# 4. Check status
python3 rival_review.py status
# → Round 1 | needs_revision | confidence: 0.95 | issues: 3 (2 major)

# 5. Claude revises draft, then reviews again
python3 rival_review.py review

# 6. Repeat until approved (exit code 0)
```

## Example Session

```
You:    I need to refactor the auth middleware. Use rival-review.
Claude: [Phase 0] What's the objective? Any constraints?
You:    Replace session tokens with JWTs. Don't touch the user model.
Claude: [Phase 1] Draft written. Starting review...
Claude: $ python3 rival_review.py review
        Round 1 | needs_revision | confidence: 0.90 | issues: 3 (2 major)
Claude: Codex found 2 major issues — missing token rotation, no logout invalidation.
Claude: [Phase 3] Revising draft...
Claude: $ python3 rival_review.py review
        Round 2 | approved | confidence: 0.95 | issues: 1 (0 major)
Claude: Codex approved (confidence: 0.95, 2 rounds). Execute?
You:    yes
```

## Directory Structure

```
.rival-review/
├── goal.md                    # Human-readable shared contract
├── contract.json              # Machine contract (criteria, sources, transport)
├── review-schema.json         # JSON schema for Codex output
├── state.json                 # Runner state (round, verdict, thread_id)
├── current-draft.md           # The document being reviewed
├── revision-summary.md        # What changed in latest revision
├── latest-review.json         # Most recent review
├── sources/                   # Pre-fetched external materials
└── history/
    └── round-001/
        ├── draft.md           # Draft snapshot
        ├── prompt.md          # Full prompt sent to Codex
        ├── raw.jsonl          # Raw Codex JSONL output
        ├── raw-resume.jsonl   # (if --resume was attempted)
        ├── raw-retry.jsonl    # (if retry was needed)
        ├── raw-fresh.jsonl    # (if fallback to fresh)
        ├── review.json        # Extracted structured review
        ├── transport.json     # Transport metadata
        ├── contract.json      # Contract snapshot
        └── goal.md            # Goal snapshot
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `rival-review init` | Scaffold `.rival-review/` with empty templates |
| `rival-review review` | Run one review round (fresh exec, default) |
| `rival-review review --resume` | Attempt to resume previous Codex session |
| `rival-review review --timeout 600` | Override timeout (seconds) |
| `rival-review review --model o3` | Override Codex model |
| `rival-review status` | Show current round, verdict, transport info |

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | approved — no major issues |
| 1 | needs_revision — has issues to fix |
| 2 | error — transport or parse failure |
| 3 | blocked — max rounds reached |
| 4 | insufficient_context — missing source materials |

## Key Design Decisions

- **Fresh exec is default** — `--sandbox read-only` + `--output-schema` guaranteed. `--resume` is opt-in.
- **Files are truth** — all state on disk, not model memory
- **Strict validation** — all schema errors block, no warn-and-continue
- **Failed rounds don't count** — transport failures don't consume round quota
- **Workspace guard** — `git status` checked before/after every Codex call
- **Separate raw files** — resume, retry, and fresh each get their own JSONL for debugging
- **Domain-agnostic** — reviewer role and criteria configured per document type

## Compatibility

Tested with:
- Codex CLI 0.97.0
- Claude Code (latest)
- Python 3.9+ on macOS

JSONL event format is based on Codex CLI 0.97.0. If the event schema
changes in future versions, `parse_codex_output()` in `rival_review.py`
may need updating.

## License

MIT
