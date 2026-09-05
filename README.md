# Session Reviewer

Reads your past AI coding sessions for one project, asks Claude to find mistakes
that repeat across 3+ sessions, backs each one up with a web search, and prints
a Markdown report. Default behavior never edits CLAUDE.md/AGENTS.md — pass
`--apply` if you want to review and accept suggestions yourself.

See [features.md](features.md) for a plain-language, detailed list of
everything this tool does — including exactly which coding tools (Claude
Code, Codex, Cursor, etc.) it can and can't read.

Open [dashboard/index.html](dashboard/index.html) in a browser (no server,
no build step) for a visual view of a report + ledger — drop in the files
from `-o report.md` and `~/.session_reviewer/ledger.json` to replace the
sample data shown on load. Everything runs client-side; nothing is uploaded.
Three tabs in the sidebar: **Overview** (the run + suggestion cards),
**Analytics** (quote-verification and ledger-status donut charts, evidence
and per-harness bar charts — plain CSS/SVG, no chart library), and
**Ledger** (the full suggestion history table).

## The app: `python app.py`

A local, interactive GUI wrapper around the same pipeline, for anyone who'd
rather click through this than run a CLI flag by flag:

```bash
python app.py            # opens http://127.0.0.1:8765 in your browser
```

1. **Pick a provider** — Anthropic (default), OpenAI, Gemini, or Ollama for
   a local open-source model — and paste in a key (Ollama needs none). The
   key lives in the app's memory for that run only, unless you tick
   "remember this key on this device", which writes it to `.env`.
2. **Pick a repo** — the app scans the same known session-store locations
   `session_reviewer.py` already knows about (Claude Code, Codex, Antigravity
   CLI — see the support matrix below) across your whole machine and lists
   every project it found history for, with session counts. This is *not* a
   disk crawl; it's the same handful of already-documented folders, just
   grouped by project instead of filtered to one.
3. **Review each suggestion** — every card shows the instruction, the
   verified/unverified evidence quotes, and any safety-guardrail or
   under-sourced warning, exactly like the CLI report. Three actions per
   suggestion:
   - **Approve** / **Reject** — stages a decision; nothing is written yet.
   - **Comment & revise** — type feedback ("this isn't really a mistake",
     "be more specific about which flag"), and the model rewrites the title
     and instruction. The evidence quotes are never handed back through the
     model for rewriting — the revised card always carries the *original*,
     already-verified evidence forward untouched, so a revision can't quietly
     replace a real quote with a paraphrased one.
4. **Apply** — one button, one confirm, writes every approved suggestion to
   `AGENTS.md`/`CLAUDE.md` in that repo (same write path, same redaction
   pass, as `--apply`) and updates the ledger.

**Security, plainly:** the app binds to `127.0.0.1` only — it is never
reachable from another machine — and has no login of its own, because it's
not meant to be reachable by anyone but you. Don't run it on a shared or
multi-user machine and expose the port. Your key is never written to disk
unless you explicitly check "remember", and even then only into a local
`.env` file next to the script, following the exact same convention the CLI
already used.

## Setup

```bash
pip install -r requirements.txt
```

Set your API key (Anthropic Console key, billed against API credits) — either:

```bash
setx ANTHROPIC_API_KEY "sk-ant-..."     # Windows, new shells
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # Windows, current PowerShell session
```

or open the [`.env`](.env) file next to this script and put your key on the
`ANTHROPIC_API_KEY=` line. `.env` is already in `.gitignore` so it won't get
committed. An env var you set yourself always wins over `.env` if both are
present.

## Run

```bash
python session_reviewer.py "C:\Users\you\code\myproject"
```

Options:

- `-n / --sessions` — how many recent sessions to review (default 10)
- `--provider {auto,anthropic,openai,gemini,ollama}` — force a provider instead of guessing from the model name (default `auto`; required for `ollama`)
- `--harnesses` — comma list of tools to scan (default `claude-code,codex,antigravity`) — see support matrix below
- `--model` — synthesis model: clusters findings, writes suggestions, web-searches (default `claude-sonnet-5`)
- `--fast-model` — extraction model: reads each raw transcript (default `claude-haiku-4-5-20251001`, ~half the price of the synthesis model)
- `--apply` — after the report, review new suggestions one at a time (`y`/`N`/`q`) and optionally write accepted ones to a file
- `-o / --out report.md` — also save the report to a file
- `-v / --verbose` — debug-level logging: every pipeline stage, `.env` loads, redaction hits, per-call token counts, ledger reads/writes, per-suggestion accept/reject
- `--log-file FILE` — also write those logs to a file

## Using OpenAI, Gemini, or a local Ollama model instead of Claude

`--model`/`--fast-model` accept model names from any of four providers — no
[litellm](https://github.com/BerriAI/litellm) dependency, each is one small
function using stdlib `urllib`:

| Provider | Model name examples | Key needed |
|---|---|---|
| Anthropic (default) | `claude-sonnet-5`, `claude-haiku-4-5-20251001` | `ANTHROPIC_API_KEY` |
| OpenAI | `gpt-4o`, `o3-mini` | `OPENAI_API_KEY` |
| Gemini | `gemini-2.0-flash` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) |
| Ollama (local, open-source) | `llama3.1`, `qwen2.5`, whatever you've pulled | none — needs `ollama serve` running, `OLLAMA_HOST` if not the default `http://localhost:11434` |

The provider is guessed from the model name for the first three (`gpt-*`
→ openai, `gemini*` → gemini, everything else → anthropic). Ollama model
names have no distinguishing prefix, so pass `--provider ollama` explicitly
(the app's provider dropdown does this for you).

Two things change when you point either flag at a non-Anthropic model:

- **No web search.** Anthropic's synthesis pass uses a server-side web-search
  tool; the others' equivalents live in different, provider-specific shapes
  this script doesn't wire up (and Ollama-hosted local models generally have
  none at all). Suggestions simply won't have a `**Web source**` line —
  logged as a warning, not hidden.
- **Quality varies by model, same as it would with Anthropic models** — this
  tool's quote-verification step (§4 below) stays honest regardless of which
  model produced a quote, but the judgment calls (what counts as a repeated
  pattern, how well a suggestion is worded) are only as good as the model you
  point it at. A small local Ollama model is the cheapest way to try this
  tool with zero API cost, at some quality cost on the synthesis step.

We deliberately didn't add litellm for this: it's a translation layer with a
documented history of dropping tool calls/citations across providers, plus a
2026 supply-chain compromise of the package itself — not something to add as
a dependency to a tool whose whole pitch is keeping your data safe. Four
small, readable stdlib functions do the job without it.

## Security: what this tool will never write into CLAUDE.md/AGENTS.md

CLAUDE.md/AGENTS.md are read as *trusted instructions* by every future AI
agent session in a project — so a suggestion that smuggled in something like
"ignore previous instructions" or a shell one-liner would be a persistent
attack, not just a bad suggestion. Before `--apply` ever shows you a
suggestion to accept, it's checked against a pattern list (prompt-injection
phrasing, `rm -rf`, `curl | bash`, exfiltration-style wording) and flagged
with a loud warning if matched — you still decide, but you decide informed.
Separately, every accepted instruction is redacted (see §5 in
[features.md](features.md)) a second time, right before the bytes are
written to disk, regardless of whether it was redacted earlier.

## How it finds your sessions

Claude Code stores each session as a `.jsonl` file under
`%USERPROFILE%\.claude\projects\<some-folder>\`. Rather than guessing how
Claude Code encodes your project path into that folder name (undocumented,
and it has changed between versions), this script reads the `cwd` field that
Claude Code already records inside every session file and matches it against
the project path you gave it. Works the same on Windows, macOS, and Linux.

## Multi-harness support

| Harness | Status |
|---|---|
| Claude Code | **Fully supported**, tested against 77 real session files |
| Codex | **Supported**, format confirmed via community documentation and a synthetic test — not yet tested against a real Codex install. Tell me if it breaks and I'll fix it against real files, same as Claude Code was |
| Antigravity CLI | **Supported**, format confirmed via [Google's own CLI docs](https://antigravity.google/docs/cli/commands/resume) and an independent reverse-engineering writeup — not yet tested against a real install. **Known limitation:** Antigravity's own workspace cache only remembers the *one* most-recently-active conversation per project — there's no on-disk history of older conversation ids — so at most 1 session can be found per project, regardless of `-n` |
| Cursor CLI | **Detected, not parsed.** Format is explicitly undocumented (its own community docs warn against building on it) — mixed SQLite/JSONL, no stable schema |
| OpenCode | **Detected, not parsed.** SQLite database exists, but no independently verifiable column schema is published anywhere |
| Hermes, Grok CLI, Pi Coding Agent | **Detected, not parsed.** No independent documentation exists for any of these formats |
| Antigravity **IDE** (not the CLI) | **Detected, not parsed** (as `antigravity-ide`). Stores conversations as `.pb` protobuf binary files — no public schema to parse against |

For the three "detected, not parsed" categories: if the tool's data directory exists on your
machine, the log says so and moves on — it never guesses at a schema it can't verify, because a
wrong guess would silently produce fake "verbatim quotes," which breaks this tool's entire premise.
If you actually use one of these and can share how its session files look, I can add real support
the same way Claude Code and Codex were built — against real files, not guesses.

## What gets sent to the API

For each session: your typed messages, plus a one-line summary of each tool
call and result (not raw tool output). Anything that looks like an API key,
password, token, or private key is redacted (`[REDACTED]`) before it's ever
sent. The log reports the exact size reduction per session (raw bytes → chars sent).

## Two-tier model pipeline (cost optimization)

Every session is read once by a **cheap model** (`--fast-model`, default Haiku 4.5 —
$1/$5 per million tokens in/out) which extracts candidate mistake quotes from that
one session alone. Only the much smaller candidate list — not the full raw
transcripts — goes to the **expensive model** (`--model`, default Sonnet 5 — $2/$10
per million tokens) for cross-session clustering, final write-up, and web search.
On a real 7-session test this cut input tokens into the expensive model from
~150,000 to ~27,000 (roughly a 50% total cost reduction) with no loss in quote
accuracy — verification still runs against the *original* raw transcripts regardless
of which model touched the quote along the way, so a slip in the cheap pass is still
caught.

## Trust, but verify

The prompt tells the models to quote transcripts verbatim and only surface
patterns seen in 3+ sessions — but LLMs occasionally paraphrase a "close
enough" quote. After the API calls, the script independently checks every
quoted line against the actual (redacted) transcript text it sent, and tags
each as `[✓ verified]` or `[⚠️ QUOTE NOT FOUND VERBATIM]`, and flags any
suggestion citing fewer than 3 distinct sessions. Treat unflagged, verified
suggestions as trustworthy; double-check anything flagged.

## The "already suggested" ledger

Every suggestion the script has ever shown you for a given project is recorded at
`~/.session_reviewer/ledger.json` (override with `SESSION_REVIEWER_HOME`). A future
run for the same project won't repeat a suggestion it already surfaced — the report
just notes how many were suppressed and points at the ledger file.

**Known limitation:** the ledger matches on the *exact wording* of the "Add to
CLAUDE.md" instruction. Since the model doesn't phrase things identically every
run, a semantically-identical suggestion worded slightly differently on a later
run won't be recognized as a repeat. You'll notice and skip it yourself; making
this fuzzy is real complexity not worth adding for a single-user tool.

## `--apply`: reviewing and writing suggestions

By default the script only prints — nothing is ever written. With `--apply`,
after the report it walks through each *new* suggestion (already-seen ones are
skipped) and asks `[y/N/q]`. Accepted ones are collected, you're shown the exact
text block that would be appended, and asked to confirm once more before anything
touches disk. It writes to `AGENTS.md` if that file exists in the project, otherwise
`CLAUDE.md` — always appending a new dated section, never overwriting existing
content. `--apply` needs an interactive terminal; it won't work piped or in a script.

## Tests

```bash
python test_session_reviewer.py
```
