# 👻 Ghost Writer

**Your vibe coding has a ghostwriter now.** It reads your own past AI coding
sessions, finds the mistakes you keep making — the same wrong flag, the same
forgotten step, the same "the fix should work now" that didn't — and turns
them into rules your agent actually follows next time. Less repeated
friction, more shipped code, same vibe.

It never touches `CLAUDE.md`/`AGENTS.md` on its own. It shows you the
evidence, you decide.

## Why

Vibe coding throughput isn't bottlenecked by typing speed — it's bottlenecked
by the same five mistakes your agent makes across ten different sessions
because nothing ever told it not to. Ghost Writer finds those patterns in
your own transcripts (not generic best practices — *your* actual history)
and turns them into `CLAUDE.md` rules, with the receipts to prove each one
is real.

- **Evidence-based, not vibes-based.** Every suggestion cites 3+ sessions and
  a verbatim quote, independently re-checked against your real transcripts —
  not the model's word for it.
- **You're always the gate.** Nothing is written until you approve it —
  per-suggestion, with a final confirm before anything touches disk.
- **Two ways in:** a CLI for scripting, or a local point-and-click app.

## Quick start

```bash
pip install -r requirements.txt
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

python session_reviewer.py "/path/to/your/project"     # CLI: prints a report
python app.py                                           # or: click through it in a browser
```

`--apply` on the CLI walks you through each suggestion (`y`/`N`/`q`) before
writing anything. The app gives you **Approve / Reject / Comment & revise**
per suggestion instead.

## What it actually checks

Scans your Claude Code, Codex, and Antigravity CLI session history for one
project, redacts anything that looks like a secret, then a cheap model
extracts candidate mistakes per-session before a stronger model clusters
them across sessions and writes suggestions — cutting what the expensive
model reads by ~50% with no loss in accuracy. Every quote gets tagged
`✓ verified` or flagged if it isn't found verbatim in your real transcript.

Supports Anthropic, OpenAI, Gemini, and local Ollama models (`--provider`).
Full breakdown of what's supported, the safety guardrails, and every flag →
**[features.md](features.md)**.

## The app

```bash
python app.py
```

Three tabs in the sidebar — **Home** (pick a repo, scan, review suggestions
as cards with **Approve / Reject / Comment & revise**), **Provider** (switch
between Anthropic/OpenAI/Gemini/Ollama any time, not just on first run), and
**Analytics** (repos detected, repos actually worked up, suggestions
approved/rejected/pending — all real numbers already on your machine, no
separate tracking). Runs on `127.0.0.1` only. Details in [features.md](features.md).

## Not another backpass

If you've seen [backpass](https://github.com/kunchenguid/backpass) — same
space, different bet: no caching, no multi-harness apply UI, no budget/token
math, one Anthropic-first pipeline with independent quote re-verification
and web-search-backed sources it doesn't have. Full point-by-point diff in
[COMPARISON.md](COMPARISON.md).

## Tests

```bash
python test_session_reviewer.py
```
