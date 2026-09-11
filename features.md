# Features

Plain-language list of everything this tool (`session_reviewer.py`) actually
does today. If it's not on this list, it doesn't exist yet — this file is
meant to be trustworthy, not aspirational. See [README.md](README.md) for
how to install and run it, and [CLAUDE.md](CLAUDE.md) for contributor notes.

## 1. What it does, in one sentence

It reads your past AI coding chat sessions for one project, finds mistakes
that happened 3+ times, double-checks itself, and shows you a report you can
optionally turn into new instructions for `CLAUDE.md`/`AGENTS.md` — either
from the command line (`session_reviewer.py`) or a local point-and-click app
(`app.py`, see below).

## 1a. The app (`python app.py`) — the whole thing without touching a flag

A local web app (opens in your own browser, nothing hosted anywhere else),
laid out as three tabs behind a small icon rail on the left:

### Home

0. **A greeting that's actually yours** — "Good morning/afternoon/evening,
   `<your GitHub username>`", where the username is parsed straight from this
   repo's own `git remote origin` URL (no login, no API call, no tracking).
   A sun/moon button next to it switches the whole app between dark and a
   frosted light theme — remembered per-browser, not sent anywhere.
1. **Pick a repo** — a live "repos detected / sessions found" count up top,
   then a searchable list of every project the app found session history
   for, pulled from the same known session-store folders the CLI already
   knows about across your whole machine — no path typing.
2. **Review suggestions as cards** — each one explains itself: the
   instruction, the evidence quotes with verified/not-found tags, and any
   safety warning. Three choices per card:
   - **Approve** or **Reject** — just stages the decision.
   - **Comment & revise** — tell it what's wrong ("too vague", "this isn't
     actually a mistake") and it rewrites the suggestion's title and
     instruction. The evidence underneath is never handed back through the
     model to "clean up" — a revision literally cannot alter or fabricate a
     quote, because the written-to-disk evidence always comes from the
     original, already-verified suggestion, not from whatever the model said
     during the rewrite.
3. **Apply** — one button, one confirmation, and every approved suggestion
   is written to `CLAUDE.md`/`AGENTS.md` — same write path, same guardrail,
   same redaction pass as the CLI's `--apply`.

### Provider

Pick Anthropic (default), OpenAI, Gemini, a local Ollama model, or
OpenRouter (access to open-weight coding models like GLM/DeepSeek/Qwen/Kimi
K2 through one key), and enter a key (skip this for Ollama, unless you also
want its optional web-search key) — a settings panel you can come back to
and switch any time, not a one-time gate. A dot at the bottom of the icon
rail lights up once a provider is configured.

### Analytics

Real numbers already sitting on your machine, aggregated on the spot —
nothing new is tracked to produce this:

- **Repos detected** — every project `discover_projects()` found session
  history for, and a breakdown by harness (Claude Code / Codex / Antigravity).
- **Repos worked up** — how many distinct projects have ever had a suggestion
  recorded for them (i.e. appear in `~/.session_reviewer/ledger.json`).
- **Sessions found**, **suggestions surfaced**, and **approved / rejected /
  pending** counts, summed across every project's ledger history.

It binds to your own machine only (`127.0.0.1`) and was never meant to be
reachable by anyone else — there's no login screen because there's no
"other user" this is designed for.

## 2. Which coding tools ("harnesses") it can read

Not every AI coding tool stores its chat history the same way, so support is
checked and added one tool at a time — never guessed. There are three tiers:

### ✅ Fully supported — it reads and understands these

| Tool | Status |
|---|---|
| **Claude Code** | Fully working, tested against 77 real session files on this machine. |
| **Codex** (OpenAI's CLI) | Working, based on documented format + a synthetic test file — not yet run against a real Codex install. If it misbehaves on your real files, that's a bug to report, not expected behavior. |
| **Antigravity CLI** (Google's `agy`) | Working, based on Google's own docs + an independent write-up of the file format — not yet run against a real install. **One catch:** Antigravity itself only remembers your *most recent* conversation per project (there's no on-disk list of older ones), so this tool can find at most 1 session per project here, never the last 10. |

### 🔍 Detected, but not read

These tools' data folders are checked for — if found, you get a log message
saying so — but the file contents are **not** parsed, on purpose:

| Tool | Why it's not parsed |
|---|---|
| **Cursor CLI** | Cursor's own docs say the storage format isn't stable — building on it would break without warning. |
| **OpenCode** | Uses a SQLite database with no published, verifiable table layout. |
| **Hermes** | No independently documented file format exists. |
| **Grok CLI** | Same — no documented format. |
| **Pi Coding Agent** | Same — no documented format. |

The rule behind this list: guessing a schema wrong would make the tool
silently invent a "verbatim quote" that was never actually said — which
breaks the whole point of the tool (see §4). So it waits for a documented or
user-confirmed format instead of guessing.

Also now covered — but as "detected, not parsed" rather than "not integrated"
— is:

| Tool | Why it's not parsed |
|---|---|
| **Antigravity IDE** (the editor, not the `agy` CLI above) | Stores conversations as `.pb` (protobuf) binary files with no published schema — same reasoning as Cursor/OpenCode. Use `--harnesses antigravity-ide` to get the "found but not read" log line. |

### ❌ Not integrated at all

**Windsurf**, **Aider**, **Continue**, and any other AI coding tool not
named above. There is no code that even looks for these — not "detected",
just entirely unhandled today. Someone would need to add a finder/trimmer
pair the same way Claude Code, Codex, and Antigravity CLI were, ideally
against real exported session files.

You choose which of the supported/detected tools to scan with `--harnesses`
(comma-separated, default `claude-code,codex,antigravity`).

## 3. Finding the right sessions for your project

- Matches sessions to *your* project folder, not by folder-naming tricks
  (those change between tool versions and aren't documented) — instead it
  reads the actual working-directory field each tool already records inside
  every session file, and compares that to the path you gave it.
- Works the same way on Windows, macOS, and Linux.
- You pick how many recent sessions to include with `-n` (default 10).

## 4. Trust, but verify — every quote is fact-checked

- The AI is told to quote your transcripts word-for-word, never paraphrase.
- After the AI responds, the script independently re-checks every quote
  against your actual (redacted) transcript text — not by trusting the AI's
  own claim. Each quote gets tagged `[✓ verified]` or
  `[⚠️ QUOTE NOT FOUND VERBATIM]` right in the report.
- Any suggestion backed by fewer than 3 separate sessions gets a visible
  warning — the rule is 3+ sessions or it doesn't count as a real pattern.

## 5. Keeps your secrets out of the API call

Before anything is sent to the AI, the script scans for and blanks out
things that look like API keys, passwords, tokens, AWS keys, GitHub tokens,
JWTs, and private key blocks — replacing them with `[REDACTED]`. It also
tells you exactly how much smaller your transcript got after this cleanup
and trimming (raw bytes → characters actually sent).

## 6. Two-tier AI pipeline (keeps the cost down)

- Every individual session is first read by a **cheap, fast model**
  (Haiku by default) that just pulls out candidate mistake quotes.
- Only that short candidate list — not your full raw transcripts — goes to
  a **more capable model** (Sonnet by default) that compares sessions,
  writes the final suggestions, and does live web searches for supporting
  best-practice sources.
- On a real 7-session test this cut the tokens sent to the expensive model
  by about half, with no drop in accuracy (verification in §4 still runs
  against your original transcripts either way).
- Both models are configurable (`--model`, `--fast-model`) if you want to
  swap them out.
- **You can point either model at OpenAI, Gemini, a local Ollama model, or
  OpenRouter instead of Claude** — pass a model name from that provider
  (`gpt-4o`, `gemini-2.0-flash`, `llama3.1`, `deepseek/deepseek-chat`, ...),
  set the matching key (none needed for Ollama itself), and for Ollama or
  OpenRouter add `--provider ollama`/`--provider openrouter` since neither
  one's model names self-identify. No litellm dependency — small, plain
  HTTP functions via Python's stdlib do it (Ollama and OpenRouter both reuse
  the same OpenAI-compatible function: Ollama's `/v1/chat/completions` is
  byte-for-byte OpenAI-compatible by Ollama's own design, and OpenRouter is
  natively OpenAI-compatible). We looked at
  [litellm](https://github.com/BerriAI/litellm) specifically and passed —
  it's a translation layer with a documented history of dropping tool
  calls/citations when swapping providers, plus a real 2026 supply-chain
  compromise of the package on PyPI. Not a trade worth making for a handful
  of small functions.
- OpenRouter in particular is the practical way to try the current best
  open-weight coding models (GLM, DeepSeek, Qwen, Kimi K2, ...) without
  self-hosting a multi-GPU cluster — one `OPENROUTER_API_KEY` reaches all
  of them.

## 7. Web-backed suggestions

When it finds a repeated mistake, it can search the web for a credible
best-practice source and include a one-sentence paraphrase plus a link — it
never copy-pastes the source's own wording into your report. Every
provider gets its own *real* search mechanism rather than one faked
interface (see CLAUDE.md on why): Anthropic's `web_search` tool (multi-turn
tool loop), OpenAI's `web_search` tool on the Responses API (a different
endpoint from normal chat), Gemini's Google Search grounding (one extra
field on the same endpoint), OpenRouter's `:online` model-slug suffix, and
Ollama's own hosted search at `ollama.com/api/web_search` — the one case
where *we* run the tool-call loop client-side (opt-in, free `OLLAMA_API_KEY`
from your Ollama account, separate from `OLLAMA_HOST`) since Ollama has no
search of its own for a local model to call. Skip the Ollama key and
synthesis still runs, just without a **Web source** line.

## 8. Remembers what it already told you

Every suggestion it has ever shown you for a project is recorded in a small
local file (`~/.session_reviewer/ledger.json`). Run it again next week and
you won't see the exact same suggestion repeated — the report just says how
many were skipped as "already seen" and where the ledger file lives.

*(Known rough edge: it matches on the exact wording of the suggested
instruction. If the AI phrases the same idea slightly differently next
time, it won't recognize it as a repeat — you'd just notice and skip it
yourself.)*

## 9. Never edits your files by accident

By default, this tool **only prints a report** — it does not touch
`CLAUDE.md` or `AGENTS.md` at all, ever, unless you explicitly pass
`--apply`. Even then:

1. You're shown each *new* suggestion one at a time and asked `y/N/q`.
2. You're shown the exact block of text that would be added.
3. You're asked to confirm *again*, in full, before anything is written.
4. It writes to `AGENTS.md` if that file already exists in your project,
   otherwise `CLAUDE.md` — always adding a new dated section at the end,
   never overwriting or removing anything already there.
5. `--apply` requires you to be at an actual keyboard (interactive
   terminal) — it refuses to run unattended or piped into a script.
6. **Every suggestion is screened before you're even asked.** `CLAUDE.md`/
   `AGENTS.md` get read as trusted instructions by every future AI agent
   session — so if a suggestion's wording looks like it's trying to plant an
   instruction rather than describe a coding convention (things like "ignore
   previous instructions", `rm -rf`, `curl … | bash`, exfiltration-style
   phrasing), you get a loud on-screen warning naming what matched before
   the `y/N/q` prompt. You still decide — this makes sure you decide with
   the warning in front of you, not because the check silently passed.
7. The exact text about to be written gets redacted a second time right
   before the write — belt-and-suspenders on top of the redaction your
   transcripts already went through in §5.

## 10. Everything else, briefly

- `-o report.md` also saves the full report to a file.
- `-v` turns on detailed debug logging — every pipeline stage (scanning,
  extraction, synthesis, verification, ledger), `.env` loads, which files
  matched, what got redacted, per-call token counts, and every accept/reject
  decision during `--apply`.
- `--log-file` additionally writes those logs to a file.
- Ships with its own test suite (`test_session_reviewer.py`, plain Python
  `unittest`, no extra frameworks) covering redaction, session parsing for
  both supported tools, quote verification, the ledger, and suggestion
  parsing.
- [`scripts/graphify.py`](scripts/graphify.py) (run via `/graphify` or
  directly) regenerates [graphify-out.md](graphify-out.md), a one-file map
  of every source file's classes and functions — useful for getting
  oriented in this codebase, not a user-facing feature of the tool itself.

## What this tool deliberately does NOT do

Documented in full in [COMPARISON.md](COMPARISON.md), but the short version:
no caching between runs, no multi-tool "apply" review UI, no token/cost
budget system, and no automatic editing of `CLAUDE.md` without a human
typing `y` first. These are intentional scope cuts for a single-user tool,
not missing features.
