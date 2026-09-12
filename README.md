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
Per-session extraction runs concurrently (`MAX_CONCURRENT_EXTRACTIONS`,
default 5) instead of one session at a time — that serial loop, not
synthesis, was the main reason a single-repo scan felt slow. Ollama is the
one exception worth knowing: it defaults to handling 1 request at a time
*server-side* regardless of this setting, so raise `OLLAMA_NUM_PARALLEL` on
the machine running `ollama serve` too if you want local scans to actually
speed up rather than just queue.

Supports Anthropic, OpenAI, Gemini, local Ollama, and OpenRouter models
(`--provider`). Synthesis is web-search-backed on every one of them — each
with its own real search mechanism, not a faked common interface:
Anthropic's `web_search` tool (multi-turn), OpenAI's `web_search` tool on
the Responses API, Gemini's Google Search grounding, OpenRouter's `:online`
model-slug suffix, and Ollama's own hosted search
(`ollama.com/api/web_search`) — opt-in via a free `OLLAMA_API_KEY`
(separate from `OLLAMA_HOST`, which just points at your local server);
without it Ollama synthesis still runs, just without a **Web source** line.
Ollama's extraction/synthesis prompt window is also auto-sized to fit
(`OLLAMA_NUM_CTX` to override) since Ollama's default context is small
enough to silently truncate a real transcript otherwise. A small local
model's hit rate on the security-pattern pass in particular will also be
lower than Claude's — it's a more specialized judgment call than spotting
workflow friction. OpenRouter is the practical way to point this at the
current best open-weight coding models (GLM, DeepSeek, Qwen, Kimi K2, ...)
without self-hosting a multi-GPU cluster — one `OPENROUTER_API_KEY`, model
names like `deepseek/deepseek-chat` or `z-ai/glm-4.6`.
Full breakdown of what's supported, the safety guardrails, and every flag →
**[features.md](features.md)**.

## The app

```bash
python app.py
```

Sidebar tabs — **Home** (pick a repo, scan, review suggestions as cards with
**Approve / Reject / Comment & revise**), **Provider** (switch between
Anthropic/OpenAI/Gemini/Ollama/OpenRouter any time, not just on first run),
**Analytics** (repos detected, repos actually worked up, suggestions
approved/rejected/pending, changes actually written — all real numbers
already on your machine, no separate tracking), and **Effectiveness** (did a
previously-accepted suggestion actually stick, or is the same mistake still
showing up). A scan that's stuck or was started with the wrong
provider/key can be stopped from its own screen — **Stop this scan** cancels
it (cooperatively; it can't yank back a request already in flight, but it
stops queueing further work at the next checkpoint), and **Change provider
/ API key** does that and jumps straight to the Provider tab so you don't
have to hunt for it. Runs on `127.0.0.1` only. Details in [features.md](features.md).

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
