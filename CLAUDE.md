# CLAUDE.md

Guidance for Claude Code when working in this repo.

## What this is

Ghost Writer (`session_reviewer.py`) is a single-file Python CLI. It reads a
user's past AI coding sessions for one project (Claude Code `.jsonl`,
Codex rollout files), asks an LLM to find mistakes that repeat across 3+
sessions, verifies each quoted mistake is an exact substring of the actual
transcript, backs it up with a web search, and prints a Markdown report.
It never edits `CLAUDE.md`/`AGENTS.md` unless the user passes `--apply` and
then confirms each suggestion interactively. Full behavior is documented in
[README.md](README.md); a feature-by-feature diff against a similar tool
("backpass") is in [COMPARISON.md](COMPARISON.md) — read that before
proposing a feature, several of the omissions there are deliberate
non-goals (no caching, no multi-harness apply UI, no budget/token math),
not gaps.

## Layout

- [session_reviewer.py](session_reviewer.py) — all the actual logic, importable:
  session discovery/parsing (`find_all_sessions`, `discover_projects`) →
  redaction → multi-provider LLM pipeline (`_complete` dispatches to
  Anthropic/OpenAI/Gemini/Ollama; cheap model extracts candidates per-session,
  expensive model synthesizes + web-searches Anthropic-only) → quote
  verification → `revise_suggestion` (comment-driven rewrite, evidence never
  round-trips through the model) → ledger → `write_accepted_suggestions` (the
  one function that touches CLAUDE.md/AGENTS.md) → CLI (`main`/`apply_flow`).
- [app.py](app.py) — local web app: a stdlib `http.server` wrapping the same
  pipeline functions above (imports `session_reviewer`, adds no logic of its
  own beyond routing/job-state) behind a browser UI in [webapp/index.html](webapp/index.html).
- [test_session_reviewer.py](test_session_reviewer.py) — stdlib
  `unittest`, no fixtures/mocking framework.
- [scripts/graphify.py](scripts/graphify.py) — see Codebase map below.
- [dashboard/index.html](dashboard/index.html) — static, client-only report/ledger
  viewer (no backend). Separate from `webapp/index.html`, which needs `app.py` running.
- No package layout, no `src/`, no build step for the Python side. One
  importable module (`session_reviewer.py`), one thin CLI, one thin server.

## Commands

```bash
pip install -r requirements.txt
python session_reviewer.py "/path/to/project"      # CLI
python app.py                                       # local web app (http://127.0.0.1:8765)
python test_session_reviewer.py                    # test (stdlib unittest)
python scripts/graphify.py                          # regenerate graphify-out.md
```

Requires `ANTHROPIC_API_KEY` (env var, or in `.env` next to the script —
already gitignored) unless `--provider`/the app's provider dropdown selects
OpenAI/Gemini/Ollama instead. See [README.md](README.md) for all CLI flags.

## Conventions

- Stdlib only beyond `anthropic` (see `requirements.txt`) — don't add a
  dependency for something a few lines of stdlib covers.
- Private helpers are `_prefixed`; anything imported/tested from outside
  the module is not.
- Redact secrets (`redact()`) before any text reaches the API — never
  bypass this when adding a new text path into a prompt.
- Quote verification (`verify_quotes`) must run against the *original*
  redacted transcript text, independent of whichever model produced the
  quote — don't trust a model's own claim that a quote is verbatim.
- Every suggestion needs ≥3 distinct session citations — this is a
  correctness gate, not a style preference; don't lower it.
- Every suggestion's instruction text is screened by `unsafe_reasons()`
  (prompt-injection-style phrasing, shell one-liners) before `--apply` shows
  it, and `redact()` runs a second time right before the write to
  CLAUDE.md/AGENTS.md — both are because that file is read as trusted
  instructions by every future agent session. Don't remove either check to
  simplify `apply_flow`.
- `--model`/`--fast-model` accept OpenAI/Gemini model names directly
  (detected by `detect_provider()`; Ollama needs an explicit `--provider`
  since its model names don't self-identify) using stdlib `urllib`, not an
  SDK or litellm — litellm was evaluated and rejected (see README) for its
  translation-layer failure modes and a 2026 supply-chain compromise. Don't
  add it back without the user asking again. All provider calls funnel
  through `_complete()` — add a fifth provider there, not as a new branch
  scattered across `extract_candidates`/`synthesize`.
- `revise_suggestion()` may show the model the evidence block as read-only
  context, but the returned `Suggestion`'s evidence text always comes from
  the original block via string-replace, never from the model's reply. Don't
  "simplify" this by asking the model to output the whole block — that
  reopens the exact hole `verify_quotes()` exists to close.
- `app.py` holds no logic of its own — job/session state and HTTP routing
  only. Any new pipeline behavior belongs in `session_reviewer.py` so the CLI
  and the app both get it. `app.py` binds `127.0.0.1` only; don't change that
  without the user explicitly asking (it holds a live API key in memory and
  has no auth of its own).
- This project's own non-goals (from COMPARISON.md): no caching layer, no
  multi-harness apply UI, no budget/token-math system, no auto-editing of
  CLAUDE.md without an explicit human `y` per suggestion. Don't reintroduce
  these without the user asking first.

## Codebase map

[scripts/graphify.py](scripts/graphify.py) walks the repo and writes
[graphify-out.md](graphify-out.md) — a single Markdown file listing every
source file and, for each `.py` file, its top-level classes/functions via
the stdlib `ast` module (no parsing dependency). It's a structural map, not
prose — use it to get oriented or to check nothing got missed, not as a
substitute for reading the actual file.

Run `/graphify` (or `python scripts/graphify.py` directly) after adding,
removing, or renaming files, functions, or classes, so the map stays
accurate. It fully overwrites `graphify-out.md` each time — don't hand-edit
that file, edit the script if the output format needs to change.
