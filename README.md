# rival-review

A Claude Code skill that orchestrates cross-model plan review: **Claude Code writes plans, Codex CLI reviews them**, looping automatically until both models reach consensus.

## Why

Single-model planning has blind spots. This skill adds an independent reviewer (OpenAI Codex) that reads your actual codebase — not just the plan text — to catch issues before you execute.

## How It Works

```
You (user)          Claude Code (planner)         Codex CLI (reviewer)
    |                      |                            |
    |--- "build X" -----→ |                            |
    |                      |--- goal.md + plan.md ---→  |
    |                      |                            |--- reads code
    |                      |                            |--- reviews plan
    |                      | ←--- review.json ---------|
    |                      |                            |
    |                      |--- revise plan ----------→ |
    |                      | ←--- approved! ------------|
    |                      |                            |
    | ←-- "approved. go?"  |                            |
    |--- "yes" ---------→  |                            |
    |                      |--- executes plan           |
```

All intermediate state lives in `.plan-review/` — file-based, not memory-based.

## Requirements

| Dependency | Version | Required |
|------------|---------|----------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | latest | yes |
| [Codex CLI](https://github.com/openai/codex) | >= 0.97.0 | yes |
| [jq](https://jqlang.github.io/jq/) | >= 1.6 | yes |

## Install

```bash
claude plugin add /path/to/rival-review
```

Or clone and add:

```bash
git clone https://github.com/yourname/rival-review.git
claude plugin add ./rival-review
```

## Usage

In Claude Code, invoke the skill when you want a reviewed plan:

```
/rival-review
```

Or describe your task and ask for a reviewed plan — the skill triggers when
Claude Code recognizes the need for cross-model review.

### Example Session

```
You:    I need to refactor the auth middleware. Use rival-review.
Claude: [Phase 0] What's the objective? Any constraints or off-limits areas?
You:    Goal: replace session tokens with JWTs. Don't touch the user model.
Claude: [Phase 1] Plan written to .plan-review/current-plan.md
Claude: [Phase 2] Sending to Codex for review...
Claude: Codex found 2 major issues — missing token rotation, no logout invalidation.
Claude: [Phase 3] Revising plan...
Claude: Codex approved (confidence: 0.92, 2 rounds). Execute?
You:    yes
Claude: [Phase 5] Executing...
```

## Architecture

```
.plan-review/
├── goal.md               # Shared contract (objectives, constraints, stop conditions)
├── current-plan.md       # Latest plan version
├── latest-review.json    # Most recent Codex review
├── revision-summary.md   # What changed in latest revision
├── review-schema.json    # JSON schema for structured review output
├── codex-session.json    # Session tracking (thread_id, round, status)
└── history/
    ├── round-1-plan.md
    ├── round-1-review.json
    └── ...
```

### Key Design Decisions

- **Files are truth** — all state persisted to disk, not model memory
- **Codex reviews real code** — prompts require reading source files, not just plan text
- **Stateful with fallback** — prefers `resume` for context continuity, falls back to fresh `exec --sandbox read-only` on failure
- **Workspace guard** — `git status` checked before/after every Codex call
- **User decides conflicts** — 2+ rounds on the same issue escalates to user

## Compatibility

Tested with:
- Codex CLI 0.97.0
- Claude Code (latest)
- macOS (no `tac` dependency, uses `jq -s` for portability)

JSONL event format is based on Codex CLI 0.97.0. If Codex changes its
event schema in future versions, the parsing logic in SKILL.md may need
updating.

## License

MIT
