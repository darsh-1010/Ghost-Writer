# session_reviewer.py vs. backpass — cold hard facts

Sources: [backpass README](https://github.com/kunchenguid/backpass), [backpass blog post](https://blog.kunchenguid.com/p/your-agentsmd-is-a-neural-net), [claude-doctor](https://github.com/millionco/claude-doctor) (a third, simpler tool in the same space, used here as a second reference point).

## 1. What each tool actually is

| | **session_reviewer.py (ours)** | **backpass** | **claude-doctor** |
|---|---|---|---|
| Scope | Claude Code only | 7 harnesses: Claude Code, Codex, Pi, OpenCode, Grok, Cursor CLI, Hermes | Claude Code only |
| Language | Python | TypeScript/Node (needs Node ≥22.5) | JavaScript/TypeScript (npm) |
| Needs an LLM call at all | Yes | Yes | **No** — pure statistics (thresholds + AFINN-165 sentiment scoring), no model call |
| Auth | Anthropic Console API key (`ANTHROPIC_API_KEY`) | None of its own — routes through `acpx` to whatever agent CLI you're already logged into | None — reads local files only |
| Web search enrichment | **Yes** — cites a paraphrased best-practice source + link per suggestion | Not mentioned anywhere in docs — **no evidence it does this** | No |
| Writes to CLAUDE.md | **Never, under any circumstance** | Can write — `backpass apply` writes after a human accepts each edit in a review UI | Never — prints suggestions only |
| Evidence required per suggestion | ≥3 distinct sessions + verbatim quote | ≥2 distinct sessions (`minGapEvidence`, configurable) + verbatim quote | 0 — aggregate stats only ("based on analysis of 838 sessions"), **no verbatim quotes at all** |
| Independent quote re-verification | **Yes** — after the model responds, we re-check every quote is an exact substring of the actual redacted transcript we sent, and flag failures | Model is told to discard quoteless claims; no documented step that re-checks the quote against the source file *after* generation | N/A (no quotes generated) |

## 2. What backpass has that we deliberately don't

Every item below is something your original spec explicitly told me to skip. Listed as fact, not a suggestion to add:

| backpass feature | What it does | Why we don't have it |
|---|---|---|
| Caching (`.backpass/scan-cache.json`, `evidence/*.json`) | Skips re-analyzing a session if it hasn't changed since last run — real cost/time savings on repeat runs | Your spec: *"No caching system"* |
| Token/budget math | Tracks a fixed byte budget for CLAUDE.md + skill descriptions, forces "zero-sum" edits (every addition must name a removal) once over budget | Your spec: *"no budget/token math, no 'gradient descent' terminology"* |
| Apply/review UI (`apply.html`) | Per-edit accept/reject cards, live budget gauge, remembers rejections so they aren't re-proposed | Your spec: *"No web UI for reviewing"*, *"no auto-editing of CLAUDE.md"* |
| Multi-harness support | Claude, Codex, Pi, OpenCode, Grok, Cursor, Hermes | Your spec: *"Claude Code only"* |
| Two-tier model ladder | Cheap model does first-pass analysis, expensive model does synthesis, with auto-fallback across providers | Your spec asked for one model, one API key, no multi-provider logic |

**Bottom line:** most of what makes backpass "bigger" is scope you already told me to cut. This isn't backpass being better engineered — it's backpass solving a harder, more general problem (N tools, N users, repeat runs over months) that you explicitly said you didn't need.

## 3. What backpass does better that ISN'T on your non-goals list

These are real gaps, not scope decisions:

| Gap | backpass's approach | Our exposure |
|---|---|---|
| Session-to-project matching robustness | 4-tier fallback: exact cwd → git-worktree sibling → git remote match → dead-path glob match. Survives a renamed or moved repo folder. | We match on exact recorded `cwd` only. If you rename or move the project folder after a session ran, that old session silently stops being found. |
| Interactive vs. non-interactive session tagging | Flags CI/automated/SDK runs separately so they don't skew the "mistake" pattern with non-human runs | We treat every session identically. If you ever run Claude Code in a script or CI, its transcript counts the same as a session you typed yourself. |
| Repeat-run noise | `rejections.json` remembers what you declined so it isn't proposed again | Every run is stateless — if you run it again next week, you may see the exact same suggestion again even if you already dismissed it. |

## 4. What we do that backpass does not (confirmed absence in its docs)

- **Web search enrichment with a cited, paraphrased source per suggestion.** Not mentioned anywhere in backpass's README, blog post, or config schema.
- **Post-hoc programmatic quote verification** — checking the model's cited quote against the actual redacted transcript text *after* the response, independent of the prompt. backpass relies on prompting the model to discard quoteless claims; no documented step re-verifies the quote in code afterward.
- **Categorical no-write guarantee.** backpass's `apply` command is a real, working write path (gated by human approval, but a write path nonetheless). Ours has no write path in the code at all — there is no function anywhere in `session_reviewer.py` capable of touching `CLAUDE.md`.
- **Simpler auth story for a single Console API key user.** backpass needs Node + `acpx` + an already-authenticated CLI harness on your machine. We need one env var.

## 5. Worth adding vs. not, given your own non-goals

| Idea | Effort | Recommendation |
|---|---|---|
| A small "already suggested" ledger (list of suggestion titles/hashes from past runs, skip repeats) | ~15 lines, one JSON file | **Worth adding** if you'll run this more than once per project — doesn't require the caching/budget machinery you excluded, just a seen-list |
| Log the distillation ratio (raw `.jsonl` bytes vs. trimmed chars sent to the model) | ~3 lines, data we already have | **Worth adding** — pure transparency, zero new logic |
| Git-remote-based project matching fallback | Needs a `git remote -v` shell-out and remote-URL comparison | **Skip unless it actually bites you** — only matters if you rename/move project folders; add it the day it does |
| Interactive/CI session tagging | Needs a heuristic for "was this run by a human" | **Skip** — you're not running Claude Code in CI for this project today |
| Multi-harness, caching, budget math, apply UI | Large | **Skip** — explicitly excluded in your spec, and backpass already exists if you ever want that tool instead of this one |
