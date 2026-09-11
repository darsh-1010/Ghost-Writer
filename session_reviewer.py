#!/usr/bin/env python3
"""Ghost Writer — finds repeated mistakes across your AI coding sessions
for one project and suggests CLAUDE.md/AGENTS.md additions. By default it only
prints a report; pass --apply to review suggestions one by one and optionally
write accepted ones to a file. See README.md for usage and harness support.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import anthropic

MAX_CHARS_PER_SESSION = 20_000  # hard cap so one huge session can't blow the prompt
DEFAULT_MODEL = "claude-sonnet-5"          # synthesis: clusters candidates, writes suggestions, web-searches
DEFAULT_FAST_MODEL = "claude-haiku-4-5-20251001"  # extraction: reads raw transcripts, ~1/2 the price of DEFAULT_MODEL
DEFAULT_SESSION_COUNT = 10
DEFAULT_HARNESSES = "claude-code,codex,antigravity"

log = logging.getLogger("session_reviewer")

# --- secret redaction --------------------------------------------------------
# ponytail: pattern list, not a scanning service — extend if a new key format bites you.
SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),           # Anthropic key
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                   # OpenAI-style key
    re.compile(r"AKIA[0-9A-Z]{16}"),                      # AWS access key id
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),            # GitHub token
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['\"]?[\w\-./+]{8,}"),
]


def redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text, n = pattern.subn("[REDACTED]", text)
        if n:
            log.debug("redacted %d match(es) of %s", n, pattern.pattern[:40])
    return text


def load_dotenv(path: Path = Path(__file__).with_name(".env")) -> None:
    """Minimal .env loader — real env vars always win, .env only fills in gaps."""
    if not path.is_file():
        log.debug("no .env file at %s", path)
        return
    loaded = skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key in os.environ:
            skipped += 1
        else:
            os.environ[key] = value
            loaded += 1
    log.debug(".env at %s: %d var(s) loaded, %d already set (env wins)", path, loaded, skipped)


def save_env_var(key: str, value: str, path: Path = Path(__file__).with_name(".env")) -> None:
    """Persist one KEY=VALUE into .env, replacing any existing line for that key.
    Used only when the app's user explicitly opts in to remembering an API key
    on this device — the default is the key lives in memory for the process's
    lifetime only. Never called with a key value that wasn't typed into the
    app's own password-masked field."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    lines = [l for l in lines if not l.strip().startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("saved %s to %s (remember-on-this-device was checked)", key, path)


def _parse_github_username(remote_url: str) -> str | None:
    m = re.search(r"github\.com[:/]([^/]+)/", remote_url)
    return m.group(1) if m else None


def git_remote_username(path: Path = Path(__file__).parent) -> str | None:
    """Best-effort GitHub username, parsed from this repo's own git remote —
    used only to personalize the app's greeting. Never raises: no git, no
    remote, a non-GitHub remote, or any other failure just returns None."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return _parse_github_username(result.stdout.strip())


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- shared helpers used by every harness's trimmer --------------------------

def _truncate(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _text_from(value) -> str:
    """A tool result (or its raw wrapper) can be a str, a dict, or a list of
    content blocks depending on which tool/harness produced it — pull whatever
    text is actually in there."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [c.get("text", "") for c in value if isinstance(c, dict) and c.get("type") == "text"]
        return " ".join(p for p in parts if p)
    if isinstance(value, dict):
        return value.get("stderr") or value.get("stdout") or ""
    return ""


def _finish_trim(path: Path, lines_out: list[str]) -> str:
    """Redact, cap, and log the distillation ratio — shared by every harness's trimmer."""
    text = redact("\n".join(lines_out))
    truncated = len(text) > MAX_CHARS_PER_SESSION
    if truncated:
        text = text[:MAX_CHARS_PER_SESSION] + "\n[... truncated, session too long ...]"
    raw_bytes = path.stat().st_size if path.is_file() else 0
    reduction = (1 - len(text) / raw_bytes) * 100 if raw_bytes else 0.0
    log.info(
        "trimmed %s: %d line(s) kept, %d char(s) from %d raw bytes (%.1f%% reduction)%s",
        path.name, len(lines_out), len(text), raw_bytes, reduction, " [truncated]" if truncated else "",
    )
    return text


# --- harness registry ---------------------------------------------------------

@dataclass
class SessionRef:
    harness: str
    path: Path
    mtime: float
    name: str  # display label, e.g. the filename


# Harnesses we've seen documented but have NOT implemented a parser for, because
# either the format is explicitly undocumented (Cursor CLI) or no independently
# verifiable schema exists (OpenCode, Hermes, Grok CLI, Pi) — see README. Rather
# than guess a schema and risk silently-wrong "verbatim quotes", we only report
# that data was found, and skip parsing it.
UNSUPPORTED_HARNESS_PATHS = {
    "cursor": Path.home() / ".cursor" / "chats",
    "opencode": Path.home() / ".local" / "share" / "opencode" / "opencode.db",
    "hermes": Path.home() / ".hermes" / "state.db",
    "grok": Path.home() / ".grok" / "sessions",
    "pi": Path.home() / ".pi" / "agent" / "sessions",
    # The Antigravity *IDE* (not the CLI — see the antigravity adapter below)
    # stores conversation history as .pb (protobuf) binary files with no public
    # schema — same "undocumented format" bar as the others in this dict.
    "antigravity-ide": Path.home() / ".gemini" / "antigravity" / "conversations",
}


def _warn_unsupported_harnesses(selected: set[str]) -> None:
    for name, path in UNSUPPORTED_HARNESS_PATHS.items():
        if name not in selected:
            continue
        if path.exists():
            log.warning(
                "%s data found at %s, but parsing isn't implemented (format is undocumented/unverified "
                "— see README). Skipping.", name, path,
            )
        else:
            log.info("%s not found on this machine (checked %s) — skipping.", name, path)


# --- Claude Code adapter (proven against 73 real session files) --------------

def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def _cwd_of_claude_code(session_file: Path) -> str | None:
    try:
        with session_file.open("r", encoding="utf-8", errors="ignore") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd")
                if cwd:
                    return cwd
    except OSError:
        return None
    return None


def _find_claude_code(project_path: Path) -> list[SessionRef]:
    """Matches on the cwd field recorded inside each file rather than reverse-
    engineering Claude Code's project-folder naming scheme (which Anthropic's own
    docs say changes between releases) — robust across Windows/macOS/Linux."""
    home = claude_home()
    projects_dir = home / "projects"
    if not projects_dir.is_dir():
        log.info("claude-code: projects dir does not exist: %s", projects_dir)
        return []

    target = _norm(project_path)
    dirs_scanned = files_scanned = 0
    refs: list[SessionRef] = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        dirs_scanned += 1
        for session_file in project_dir.glob("*.jsonl"):  # non-recursive: skips subagent transcripts
            files_scanned += 1
            cwd = _cwd_of_claude_code(session_file)
            if cwd and _norm(Path(cwd)) == target:
                refs.append(SessionRef("claude-code", session_file, session_file.stat().st_mtime, session_file.name))
                log.debug("claude-code match: %s (cwd=%s)", session_file, cwd)

    log.info(
        "claude-code: scanned %d project dir(s) / %d session file(s); %d match(es)",
        dirs_scanned, files_scanned, len(refs),
    )
    return refs


def _summarize_tool_result(block: dict, tool_use_result) -> str | None:
    text = _text_from(block.get("content")) or _text_from(tool_use_result)
    if not text:
        return None
    is_error = block.get("is_error") or (
        isinstance(tool_use_result, dict) and tool_use_result.get("exitCode") not in (None, 0)
    )
    return ("ERROR: " if is_error else "") + _truncate(text, 150)


def _trim_claude_code(path: Path) -> str:
    lines_out: list[str] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            entry_type = obj.get("type")
            message = obj.get("message") or {}
            content = message.get("content")

            if entry_type == "user":
                if isinstance(content, str):
                    if content.strip():
                        lines_out.append(f"USER: {content.strip()}")
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text" and block.get("text", "").strip():
                            lines_out.append(f"USER: {block['text'].strip()}")
                        elif block.get("type") == "tool_result":
                            summary = _summarize_tool_result(block, obj.get("toolUseResult"))
                            if summary:
                                lines_out.append(f"TOOL_RESULT: {summary}")

            elif entry_type == "assistant" and isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text", "").strip():
                        lines_out.append(f"ASSISTANT: {_truncate(block['text'].strip(), 300)}")
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        input_str = _truncate(json.dumps(block.get("input", {}), default=str), 120)
                        lines_out.append(f"TOOL_CALL: {name}({input_str})")

    return _finish_trim(path, lines_out)


# --- Codex adapter (format confirmed via community docs; cwd field present) --
# Best-effort: I have not personally tested this against a real Codex install
# (none is available in the environment this was built in). If it doesn't find
# your sessions or produces garbage, tell me and I'll fix it against real files
# the same way the Claude Code adapter was fixed.

def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _codex_meta_cwd(rollout_file: Path) -> str | None:
    try:
        with rollout_file.open("r", encoding="utf-8", errors="ignore") as f:
            for _ in range(5):
                line = f.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for candidate in (obj, obj.get("item") or {}, obj.get("payload") or {}):
                    cwd = candidate.get("cwd") if isinstance(candidate, dict) else None
                    if cwd:
                        return cwd
    except OSError:
        return None
    return None


def _find_codex(project_path: Path) -> list[SessionRef]:
    home = _codex_home()
    sessions_dir = home / "sessions"
    if not sessions_dir.is_dir():
        log.info("codex: sessions dir not found at %s — skipping", sessions_dir)
        return []

    target = _norm(project_path)
    refs: list[SessionRef] = []
    for rollout in sessions_dir.glob("**/rollout-*.jsonl"):
        cwd = _codex_meta_cwd(rollout)
        if cwd and _norm(Path(cwd)) == target:
            refs.append(SessionRef("codex", rollout, rollout.stat().st_mtime, rollout.name))
    log.info("codex: scanned %s; %d match(es)", sessions_dir, len(refs))
    return refs


def _trim_codex(path: Path) -> str:
    lines_out: list[str] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            entry_type = obj.get("type")
            item = obj.get("item") or obj.get("payload") or {}

            if entry_type == "user_message":
                text = _text_from(item.get("content"))
                if text.strip():
                    lines_out.append(f"USER: {text.strip()}")
            elif entry_type == "assistant_message":
                text = _text_from(item.get("content"))
                if text.strip():
                    lines_out.append(f"ASSISTANT: {_truncate(text.strip(), 300)}")
            elif entry_type == "tool_call":
                name = item.get("tool_name", "?")
                args_str = _truncate(json.dumps(item.get("arguments", {}), default=str), 120)
                lines_out.append(f"TOOL_CALL: {name}({args_str})")
            elif entry_type == "tool_result":
                result_text = _text_from(item.get("result"))
                if result_text:
                    prefix = "ERROR: " if item.get("exit_code") not in (None, 0) else "TOOL_RESULT: "
                    lines_out.append(prefix + _truncate(result_text, 150))

    return _finish_trim(path, lines_out)


# --- Antigravity CLI adapter (format confirmed via Google's own CLI docs and --
# an independent reverse-engineering writeup; not yet tested against a real
# install — same best-effort status as the Codex adapter above). Note: unlike
# Claude Code/Codex, Antigravity's own workspace cache only remembers the ONE
# most-recently-active conversation per project directory (no history of older
# conversation ids) — so this can surface at most one session per project,
# regardless of -n/--sessions. That's a real limitation of Antigravity's own
# on-disk format, not a bug here.

def _antigravity_home() -> Path:
    return Path(os.environ.get("ANTIGRAVITY_HOME") or (Path.home() / ".gemini" / "antigravity-cli"))


def _find_antigravity(project_path: Path) -> list[SessionRef]:
    home = _antigravity_home()
    cache_file = home / "cache" / "last_conversations.json"
    if not cache_file.is_file():
        log.info("antigravity: cache file not found at %s — skipping", cache_file)
        return []
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("antigravity: cache file at %s is unreadable — skipping", cache_file)
        return []

    target = _norm(project_path)
    conv_id = next((cid for path_str, cid in cache.items() if _norm(Path(path_str)) == target), None)
    if not conv_id:
        log.info("antigravity: no cached conversation for %s", project_path)
        return []

    logs_dir = home / "brain" / conv_id / ".system_generated" / "logs"
    transcript = logs_dir / "transcript_full.jsonl"
    if not transcript.is_file():
        transcript = logs_dir / "transcript.jsonl"
    if not transcript.is_file():
        log.info("antigravity: no transcript file for conversation %s", conv_id)
        return []

    log.info("antigravity: matched workspace to conversation %s", conv_id)
    return [SessionRef("antigravity", transcript, transcript.stat().st_mtime, transcript.name)]


_ANTIGRAVITY_TOOL_TYPES = {"RUN_COMMAND", "VIEW_FILE", "LIST_DIRECTORY", "GREP_SEARCH", "SEARCH_WEB", "CODE_ACTION"}


def _trim_antigravity(path: Path) -> str:
    lines_out: list[str] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            rec_type = obj.get("type")
            content = re.sub(r"</?USER_REQUEST>", "", obj.get("content") or "").strip()

            if rec_type == "USER_INPUT" and content:
                lines_out.append(f"USER: {content}")
            elif rec_type == "PLANNER_RESPONSE":
                if content:
                    lines_out.append(f"ASSISTANT: {_truncate(content, 300)}")
                for call in obj.get("tool_calls") or []:
                    args_str = _truncate(json.dumps(call.get("args", {}), default=str), 120)
                    lines_out.append(f"TOOL_CALL: {call.get('name', '?')}({args_str})")
            elif rec_type in _ANTIGRAVITY_TOOL_TYPES and content:
                lines_out.append(f"TOOL_RESULT: {_truncate(content, 150)}")

    return _finish_trim(path, lines_out)


# --- dispatch across all enabled harnesses -----------------------------------

_FINDERS = {"claude-code": _find_claude_code, "codex": _find_codex, "antigravity": _find_antigravity}
_TRIMMERS = {"claude-code": _trim_claude_code, "codex": _trim_codex, "antigravity": _trim_antigravity}


def find_all_sessions(project_path: Path, harnesses: list[str]) -> list[SessionRef]:
    refs: list[SessionRef] = []
    for h in harnesses:
        finder = _FINDERS.get(h)
        if finder is None:
            continue
        try:
            refs.extend(finder(project_path))
        except Exception:
            log.exception("harness %r failed to scan; skipping it", h)
    _warn_unsupported_harnesses(set(harnesses))
    refs.sort(key=lambda r: r.mtime, reverse=True)
    return refs


def trim_session(ref: SessionRef) -> str:
    return _TRIMMERS[ref.harness](ref.path)


# --- discovering every project with session history, not just one ------------
# Same three harness stores as above, just grouped by cwd found instead of
# filtered against one target path — no new file-format guessing, this reuses
# the already-verified per-harness readers (_cwd_of_claude_code,
# _codex_meta_cwd, the antigravity workspace cache) to answer "what repos does
# this machine even have session history for?" instead of "does this one repo
# have history?". Powers the app's repo picker (see app.py).

def discover_projects(harnesses: list[str]) -> list[dict]:
    found: dict[str, dict] = {}

    def note(path_str: str, harness: str, mtime: float) -> None:
        key = _norm(Path(path_str))
        entry = found.setdefault(key, {"path": path_str, "harnesses": set(), "sessions": 0, "last_active": 0.0})
        entry["harnesses"].add(harness)
        entry["sessions"] += 1
        entry["last_active"] = max(entry["last_active"], mtime)

    if "claude-code" in harnesses:
        projects_dir = claude_home() / "projects"
        if projects_dir.is_dir():
            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                for session_file in project_dir.glob("*.jsonl"):
                    cwd = _cwd_of_claude_code(session_file)
                    if cwd:
                        note(cwd, "claude-code", session_file.stat().st_mtime)

    if "codex" in harnesses:
        sessions_dir = _codex_home() / "sessions"
        if sessions_dir.is_dir():
            for rollout in sessions_dir.glob("**/rollout-*.jsonl"):
                cwd = _codex_meta_cwd(rollout)
                if cwd:
                    note(cwd, "codex", rollout.stat().st_mtime)

    if "antigravity" in harnesses:
        cache_file = _antigravity_home() / "cache" / "last_conversations.json"
        if cache_file.is_file():
            try:
                cache = json.loads(cache_file.read_text(encoding="utf-8"))
                for path_str, conv_id in cache.items():
                    logs_dir = _antigravity_home() / "brain" / conv_id / ".system_generated" / "logs"
                    transcript = logs_dir / "transcript_full.jsonl"
                    if not transcript.is_file():
                        transcript = logs_dir / "transcript.jsonl"
                    if transcript.is_file():
                        note(path_str, "antigravity", transcript.stat().st_mtime)
            except (json.JSONDecodeError, OSError):
                log.warning("antigravity cache at %s is unreadable — skipping in discovery", cache_file)

    results = [
        {"path": v["path"], "harnesses": sorted(v["harnesses"]), "sessions": v["sessions"], "last_active": v["last_active"]}
        for v in found.values()
    ]
    results.sort(key=lambda r: r["last_active"], reverse=True)
    log.info("discovered %d project(s) with session history across %s", len(results), ", ".join(harnesses))
    return results


# --- multi-provider model support (raw HTTP, no SDK/litellm dependency) -----
# Anthropic is the default and the only one with web search wired up (see
# synthesize() below). OpenAI, Gemini, and Ollama are each one small function
# using stdlib urllib — no SDK, no litellm. Deliberately not using litellm:
# it's a translation layer with a documented history of silently dropping
# tool calls/citations when swapping providers, plus a real 2026 supply-chain
# compromise of the package itself. Four small, readable functions cost less
# than that dependency. Trade-off, stated plainly: none of the three
# non-Anthropic providers get web-search-backed suggestions here — OpenAI's
# and Gemini's equivalents live in different, provider-specific API shapes,
# and faking equivalence is exactly the hidden divergence litellm would paper
# over. See README.
#
# Provider is normally inferred from the model name alone (gpt-4o -> openai,
# gemini-2.0-flash -> gemini) so no extra flag is needed for the common case.
# Ollama model names have no distinguishing prefix (llama3.1, qwen2.5, ...),
# so it's the one provider that needs an explicit --provider ollama / a UI
# selection rather than name-sniffing.

def _is_openai_model(model: str) -> bool:
    return model.startswith(("gpt-", "o1", "o3", "o4", "chatgpt"))


def _is_gemini_model(model: str) -> bool:
    return model.startswith("gemini")


def detect_provider(model: str) -> str:
    if _is_openai_model(model):
        return "openai"
    if _is_gemini_model(model):
        return "gemini"
    return "anthropic"


def resolve_provider(explicit: str, model: str) -> str:
    return detect_provider(model) if explicit in (None, "auto") else explicit


def _openai_style_complete(
    model: str, prompt: str, max_tokens: int, url: str, api_key: str, extra_body: dict | None = None,
) -> tuple[str, int, int]:
    """Shared by OpenAI and Ollama — Ollama's /v1/chat/completions is byte-for-byte
    OpenAI-compatible (Ollama's own docs), so one function serves both.
    extra_body is Ollama-only (its "options" block, e.g. num_ctx) — never sent to OpenAI."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        **(extra_body or {}),
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    text = data["choices"][0]["message"]["content"].strip()
    usage = data.get("usage") or {}
    return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


def _openai_complete(model: str, prompt: str, max_tokens: int) -> tuple[str, int, int]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set (checked environment and .env) but an OpenAI model was requested")
    return _openai_style_complete(model, prompt, max_tokens, "https://api.openai.com/v1/chat/completions", api_key)


# Ollama defaults to a 2048 or 4096-token context window per model and SILENTLY
# TRUNCATES whatever doesn't fit — unlike every cloud provider here, which sizes
# the context to the model automatically. A single trimmed session can already be
# ~5k tokens (MAX_CHARS_PER_SESSION=20_000 chars) and synthesis concatenates
# candidates from up to 10 sessions at once, so without this, Ollama runs were
# silently losing most of the transcript before the model ever saw it.
# ponytail: a chars/4 estimate, not a real tokenizer — errs high on purpose
# (padding below), overridable via OLLAMA_NUM_CTX if a model needs more/less
# than this guesses, or if the machine can't afford the RAM a big num_ctx costs.
_OLLAMA_MIN_NUM_CTX = 4096
_OLLAMA_MAX_NUM_CTX = 65536


def _ollama_num_ctx(prompt: str, max_tokens: int) -> int:
    override = os.environ.get("OLLAMA_NUM_CTX")
    if override:
        return int(override)
    estimated_prompt_tokens = len(prompt) // 4
    return max(_OLLAMA_MIN_NUM_CTX, min(_OLLAMA_MAX_NUM_CTX, estimated_prompt_tokens + max_tokens + 512))


def _openrouter_complete(model: str, prompt: str, max_tokens: int) -> tuple[str, int, int]:
    """OpenRouter is OpenAI-compatible (same /chat/completions shape), so it's a thin
    wrapper around _openai_style_complete with a different base URL — the practical way
    to point this tool at the current best open-weight coding models (GLM, DeepSeek,
    Qwen, Kimi K2, ...) without hosting them yourself. See README for the tradeoffs vs
    Anthropic/Ollama."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set (checked environment and .env) but an OpenRouter model was requested")
    return _openai_style_complete(model, prompt, max_tokens, "https://openrouter.ai/api/v1/chat/completions", api_key)


def _openrouter_search_complete(model: str, prompt: str, max_tokens: int) -> tuple[str, int, int]:
    """OpenRouter's web-search plugin is enabled per-request by appending ':online' to
    the model slug — no separate endpoint, no client-side tool loop needed, unlike
    Ollama's search below. Used by synthesize() only; extraction doesn't need search."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set (checked environment and .env) but an OpenRouter model was requested")
    return _openai_style_complete(f"{model}:online", prompt, max_tokens, "https://openrouter.ai/api/v1/chat/completions", api_key)


def _ollama_complete(model: str, prompt: str, max_tokens: int) -> tuple[str, int, int]:
    host = (os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
    num_ctx = _ollama_num_ctx(prompt, max_tokens)
    log.debug("ollama: requesting num_ctx=%d for a %d-char prompt", num_ctx, len(prompt))
    # no real key: Ollama's OpenAI-compatible endpoint accepts (and ignores) any bearer value
    return _openai_style_complete(
        model, prompt, max_tokens, f"{host}/v1/chat/completions", "ollama",
        extra_body={"options": {"num_ctx": num_ctx}},
    )


# Ollama has no built-in search of its own to call during generation — but ollama.com
# (the company) runs a separate hosted search service any Ollama account can query,
# and local models with tool support (llama3.1+) can emit an OpenAI-style tool_call
# asking for one. So unlike Anthropic/OpenAI/Gemini above, where the provider runs the
# whole search loop server-side, here WE run the loop: the local model decides when to
# search, we make the actual HTTP call to ollama.com/api/web_search ourselves, and feed
# the results back as a "tool" message. Opt-in via OLLAMA_API_KEY (a *different* key
# from OLLAMA_HOST — that one just points at your local server, this one authenticates
# to ollama.com's cloud). Get one free at https://ollama.com/settings/keys.
_OLLAMA_SEARCH_TOOL = [{
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for a credible best-practice source backing up one suggestion.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "the search query"}},
            "required": ["query"],
        },
    },
}]


def _ollama_web_search(query: str, api_key: str, max_results: int = 5) -> list[dict]:
    body = json.dumps({"query": query, "max_results": max_results}).encode("utf-8")
    req = urllib.request.Request(
        "https://ollama.com/api/web_search", data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data.get("results", [])


def _ollama_search_complete(
    model: str, prompt: str, max_tokens: int, search_api_key: str, max_uses: int = 5,
) -> tuple[str, int, int]:
    host = (os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
    num_ctx = _ollama_num_ctx(prompt, max_tokens)
    messages = [{"role": "user", "content": prompt}]
    total_in = total_out = searches_used = 0

    for turn in range(1, 7):  # ponytail: hard cap on tool-call turns, mirrors the Anthropic loop's cap
        body = json.dumps({
            "model": model, "messages": messages, "max_tokens": max_tokens,
            "tools": _OLLAMA_SEARCH_TOOL, "options": {"num_ctx": num_ctx},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{host}/v1/chat/completions", data=body,
            headers={"Authorization": "Bearer ollama", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        usage = data.get("usage") or {}
        total_in += usage.get("prompt_tokens", 0)
        total_out += usage.get("completion_tokens", 0)
        message = data["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        log.info(
            "ollama search turn %d: %d tool call(s) requested, %d/%d searches used so far",
            turn, len(tool_calls), searches_used, max_uses,
        )
        if not tool_calls or searches_used >= max_uses:
            return (message.get("content") or "").strip(), total_in, total_out

        messages.append(message)
        for call in tool_calls:
            try:
                args = json.loads(call.get("function", {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            results = _ollama_web_search(args.get("query", ""), search_api_key)
            searches_used += 1
            messages.append({
                "role": "tool", "tool_call_id": call.get("id", ""),
                "content": json.dumps(results)[:4000],  # keep the local model's small context sane
            })

    log.warning("ollama search synthesis hit the 6-turn tool-call cap; response may be incomplete")
    return "", total_in, total_out


def _gemini_complete(model: str, prompt: str, max_tokens: int, search: bool = False) -> tuple[str, int, int]:
    """search=True turns on Gemini's native "Grounding with Google Search" — same
    generateContent endpoint, just one extra tools entry. Used by synthesize()."""
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set (checked environment and .env) but a Gemini model was requested")
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if search:
        body["tools"] = [{"google_search": {}}]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180 if search else 120) as resp:
        data = json.loads(resp.read())
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()
    usage = data.get("usageMetadata") or {}
    return text, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)


def _openai_search_complete(model: str, prompt: str, max_tokens: int) -> tuple[str, int, int]:
    """OpenAI's native web_search tool only exists on the Responses API
    (/v1/responses), a different endpoint and request/response shape from Chat
    Completions — not OpenAI-compatible the way Ollama's endpoint is, so this
    can't reuse _openai_style_complete(). Used by synthesize() only; extraction
    (extract_candidates/revise_suggestion) has no need for search and keeps
    using the cheaper, simpler Chat Completions path via _openai_complete()."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set (checked environment and .env) but an OpenAI model was requested")
    body = json.dumps({
        "model": model,
        "input": prompt,
        "tools": [{"type": "web_search"}],
        "max_output_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses", data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
    text = "".join(
        part.get("text", "")
        for item in data.get("output", []) if item.get("type") == "message"
        for part in item.get("content", []) if part.get("type") == "output_text"
    ).strip()
    usage = data.get("usage") or {}
    return text, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


def _complete(model: str, prompt: str, max_tokens: int, provider: str = "auto") -> tuple[str, int, int]:
    """Single dispatch point for a non-Anthropic model call — extract_candidates()
    and revise_suggestion() both go through this; synthesize() special-cases
    Anthropic itself below because only Anthropic gets the web-search tool loop."""
    resolved = resolve_provider(provider, model)
    if resolved == "openai":
        return _openai_complete(model, prompt, max_tokens)
    if resolved == "gemini":
        return _gemini_complete(model, prompt, max_tokens)
    if resolved == "ollama":
        return _ollama_complete(model, prompt, max_tokens)
    if resolved == "openrouter":
        return _openrouter_complete(model, prompt, max_tokens)
    client = anthropic.Anthropic()
    response = client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text, response.usage.input_tokens, response.usage.output_tokens


# --- prompting Claude: two tiers, cheap extraction then synthesis ------------

EXTRACT_PROMPT = """Read this transcript from one AI coding session. Find every mistake, piece of \
friction, or inefficiency visible in it — wrong flags, forgotten steps, repeated failures, anything \
that cost time.

For each one, output exactly one line in this form, and NOTHING else on that line — no label, no \
description, no colon-separated prefix, just a single quoted string:
- "<verbatim quote from the transcript below, copied exactly, character for character>"

Pick a quote short enough to stand alone (under ~200 characters) and specific enough to identify the \
mistake on its own. Do not add commentary before or after the quote. If a quote would itself contain a \
double-quote character, choose a different, shorter quote from the same transcript that doesn't need one.
If nothing stands out, output nothing at all. No preamble, no summary line.

Transcript:
{text}
"""

SYNTHESIS_PROMPT = """Below are candidate quotes independently extracted from {n} separate AI coding \
sessions in one project (most recent first). Each line under a session header is one verbatim quote \
from that session's transcript, already isolated — copy it exactly as given, do not alter it or wrap it \
in extra description.

Find quotes from 3 OR MORE distinct session files that describe THE SAME underlying mistake (fewer than \
3 does not count — skip it). Group those into one suggestion.

For each suggestion, output a block in exactly this form:

## Suggestion: <short title you write yourself, describing the pattern>
**Add to CLAUDE.md:** <one short instruction>
**Evidence:**
- Session <exact filename>: "<the quote, copied exactly and only, from that session's candidate list — no label, no extra text>"
- Session <exact filename>: "<a quote from a different session, same rule>"
- (one line per distinct session it appears in — need 3+ distinct filenames total)
**Web source:** <one-sentence paraphrase in your own words of a credible best-practice source, then its URL> \
(omit this whole line if web search found nothing useful — do not force it, never paste quoted text from the page)

Hard rules:
- Each evidence line must contain ONLY a filename and ONE quote copied character-for-character from that \
session's candidate list below. Never combine a quote with your own description on the same line.
- Do not invent a session filename — use only filenames that appear in a "=== SESSION: ... ===" header below.
- If you use web search, paraphrase what you find in your own words; never quote the source text directly.
- Output only Suggestion blocks, nothing else. If nothing repeats 3+ times, output exactly: \
No repeated mistakes found across 3+ sessions.

Candidates:
"""


SECURITY_EXTRACT_PROMPT = """Read this transcript from one AI coding session. Find every SECURITY-relevant \
mistake visible in it — a hardcoded secret, API key, password, or credential that got written into a file \
or committed; a SQL, shell, or query string built by concatenating untrusted input instead of using \
parameterization/escaping; an authentication, authorization, or input-validation check that was skipped, \
disabled, or worked around; or any other OWASP-class issue. Ignore plain workflow friction (wrong flags, \
forgotten steps) unless it also has a security angle.

For each one, output exactly one line in this form, and NOTHING else on that line — no label, no \
description, no colon-separated prefix, just a single quoted string:
- "<verbatim quote from the transcript below, copied exactly, character for character>"

Pick a quote short enough to stand alone (under ~200 characters) and specific enough to identify the \
mistake on its own. Do not add commentary before or after the quote. If a quote would itself contain a \
double-quote character, choose a different, shorter quote from the same transcript that doesn't need one.
If nothing stands out, output nothing at all. No preamble, no summary line.

Transcript:
{text}
"""

SECURITY_SYNTHESIS_PROMPT = """Below are candidate quotes independently extracted from {n} separate AI \
coding sessions in one project (most recent first), each already flagged as a SECURITY-relevant mistake — \
a hardcoded secret, SQL/command built by string concatenation, a skipped auth/validation check, or similar \
OWASP-class issue. Each line under a session header is one verbatim quote from that session's transcript, \
already isolated — copy it exactly as given, do not alter it or wrap it in extra description.

Find quotes from 3 OR MORE distinct session files that describe THE SAME underlying security mistake \
(fewer than 3 does not count — skip it). Group those into one suggestion.

For each suggestion, output a block in exactly this form:

## Suggestion: <short title you write yourself, describing the security pattern>
**Add to CLAUDE.md:** <one short instruction fixing the pattern>
**Evidence:**
- Session <exact filename>: "<the quote, copied exactly and only, from that session's candidate list — no label, no extra text>"
- Session <exact filename>: "<a quote from a different session, same pattern>"
- (one line per distinct session it appears in — need 3+ distinct filenames total)
**Web source:** <one-sentence paraphrase in your own words of a credible best-practice source (e.g. OWASP), \
then its URL> (omit this whole line if web search found nothing useful — do not force it, never paste \
quoted text from the page)

Hard rules:
- Each evidence line must contain ONLY a filename and ONE quote copied character-for-character from that \
session's candidate list below. Never combine a quote with your own description on the same line.
- Do not invent a session filename — use only filenames that appear in a "=== SESSION: ... ===" header below.
- If you use web search, paraphrase what you find in your own words; never quote the source text directly.
- Output only Suggestion blocks, nothing else. If nothing repeats 3+ times, output exactly: \
No repeated security mistakes found across 3+ sessions.

Candidates:
"""


def extract_candidates(session_text: str, model: str, provider: str = "auto", prompt_template: str = EXTRACT_PROMPT) -> str:
    """Cheap first pass: one small model call per session, no tools needed.
    prompt_template swaps in SECURITY_EXTRACT_PROMPT for the security-pattern lens —
    same pipeline, no new architecture."""
    if not session_text.strip():
        return ""
    prompt = prompt_template.format(text=session_text)
    t0 = time.monotonic()
    text, in_tok, out_tok = _complete(model, prompt, max_tokens=1024, provider=provider)
    elapsed = time.monotonic() - t0
    log.info(
        "extract (%s): %.1fs, input_tokens=%d, output_tokens=%d, %d candidate quote(s)",
        model, elapsed, in_tok, out_tok, len(QUOTE_RE.findall(text)),
    )
    return text


def build_synthesis_prompt(candidates: dict[str, str], n: int, prompt_template: str = SYNTHESIS_PROMPT) -> str:
    parts = [prompt_template.format(n=n)]
    for filename, text in candidates.items():
        parts.append(f"\n=== SESSION: {filename} ===\n{text or '(nothing flagged)'}\n")
    return "".join(parts)


def synthesize(prompt: str, model: str, max_uses: int = 10, provider: str = "auto") -> str:
    """Expensive pass: clusters candidates across sessions, writes suggestions, web-searches for sources.
    Each provider's real web-search shape is different enough (Anthropic: multi-turn tool loop on the
    Messages API; OpenAI: single call but a *different endpoint*, /v1/responses; Gemini: single call,
    same endpoint as everything else, one extra tools field; Ollama: no built-in search of its own, so
    WE run the tool loop against ollama.com's hosted search; OpenRouter: single call, one suffix on the
    model slug) that this branches per-provider rather than faking one unified interface — see CLAUDE.md
    on why litellm-style cross-provider equivalence was rejected here."""
    resolved = resolve_provider(provider, model)

    if resolved == "openai":
        text, in_tok, out_tok = _openai_search_complete(model, prompt, max_tokens=8000)
        log.info("synthesis done (%s, openai web_search): input_tokens=%d, output_tokens=%d", model, in_tok, out_tok)
        return text

    if resolved == "gemini":
        text, in_tok, out_tok = _gemini_complete(model, prompt, max_tokens=8000, search=True)
        log.info("synthesis done (%s, gemini google_search grounding): input_tokens=%d, output_tokens=%d", model, in_tok, out_tok)
        return text

    if resolved == "openrouter":
        text, in_tok, out_tok = _openrouter_search_complete(model, prompt, max_tokens=8000)
        log.info("synthesis done (%s, openrouter :online web search): input_tokens=%d, output_tokens=%d", model, in_tok, out_tok)
        return text

    if resolved == "ollama":
        search_key = os.environ.get("OLLAMA_API_KEY")
        if search_key:
            text, in_tok, out_tok = _ollama_search_complete(model, prompt, max_tokens=8000, search_api_key=search_key, max_uses=max_uses)
            log.info("synthesis done (%s, ollama web_search): input_tokens=%d, output_tokens=%d", model, in_tok, out_tok)
            return text
        log.warning(
            "ollama has no web search without OLLAMA_API_KEY (free at https://ollama.com/settings/keys) — "
            "synthesis will run without one, so suggestions won't have a **Web source** line. See README.",
        )
        text, in_tok, out_tok = _complete(model, prompt, max_tokens=8000, provider=provider)
        log.info("synthesis done (%s, no web search): input_tokens=%d, output_tokens=%d", model, in_tok, out_tok)
        return text

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}]

    log.info("synthesizing with %s (prompt=%d chars, max_uses=%d)", model, len(prompt), max_uses)
    text_parts: list[str] = []
    total_in = total_out = total_searches = 0
    for turn in range(1, 7):  # ponytail: hard cap on pause_turn continuations, not an infinite loop
        t0 = time.monotonic()
        response = client.messages.create(
            model=model, max_tokens=8000, messages=messages, tools=tools
        )
        elapsed = time.monotonic() - t0
        usage = response.usage
        searches = getattr(usage.server_tool_use, "web_search_requests", 0) if usage.server_tool_use else 0
        total_in += usage.input_tokens
        total_out += usage.output_tokens
        total_searches += searches
        log.info(
            "synthesis turn %d: stop_reason=%s, %.1fs, input_tokens=%d, output_tokens=%d, web_searches=%d",
            turn, response.stop_reason, elapsed, usage.input_tokens, usage.output_tokens, searches,
        )
        text_parts.extend(b.text for b in response.content if b.type == "text")
        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
    else:
        log.warning("hit the 6-turn pause_turn cap; response may be incomplete")

    log.info(
        "synthesis done: %d turn(s), %d total input tokens, %d total output tokens, %d web search(es)",
        turn, total_in, total_out, total_searches,
    )
    return "".join(text_parts).strip()


# --- verifying quotes against the real transcripts -------------------------------

QUOTE_RE = re.compile(r'"([^"\n]{8,})"')


def verify_quotes(report: str, sessions: dict[str, str]) -> str:
    """Tag every quoted line with whether it's a real substring of the session it's
    attributed to, and flag suggestions backed by fewer than 3 sessions. Runs against
    the ORIGINAL raw session transcripts regardless of which model(s) produced the
    quote, so a slip during the cheap extraction pass is still caught here."""
    out_lines: list[str] = []
    block_files: set[str] = set()
    stats = {"suggestions": 0, "quotes_verified": 0, "quotes_failed": 0, "undersourced": 0}

    def flush_block_warning():
        if block_files and len(block_files) < 3:
            stats["undersourced"] += 1
            out_lines.append(
                f"> ⚠️ WARNING: only {len(block_files)} distinct session(s) cited above — "
                "should be 3 or more. Treat this suggestion with suspicion."
            )

    for line in report.split("\n"):
        if line.startswith("## Suggestion"):
            flush_block_warning()
            block_files = set()
            stats["suggestions"] += 1

        matched_file = next((fn for fn in sessions if fn in line), None)
        quotes = QUOTE_RE.findall(line)
        if matched_file and quotes:
            block_files.add(matched_file)
            for q in quotes:
                ok = q in sessions[matched_file]
                stats["quotes_verified" if ok else "quotes_failed"] += 1
                tag = " [✓ verified]" if ok else " [⚠️ QUOTE NOT FOUND VERBATIM]"
                line += tag
        out_lines.append(line)

    flush_block_warning()
    log.info(
        "verification: %d suggestion(s), %d quote(s) verified, %d quote(s) failed, "
        "%d suggestion(s) under-sourced (<3 sessions)",
        stats["suggestions"], stats["quotes_verified"], stats["quotes_failed"], stats["undersourced"],
    )
    return "\n".join(out_lines)


# --- parsing suggestions out of the final report, for the ledger + --apply ---

@dataclass
class Suggestion:
    title: str
    add_line: str  # the exact instruction text to append to CLAUDE.md/AGENTS.md
    block: str      # full markdown block, for display
    key: str        # stable id for the seen/rejected ledger
    category: str = "workflow"  # "workflow" or "security" — which extraction pass produced it


def parse_suggestions(report: str, category: str = "workflow") -> list[Suggestion]:
    blocks = re.split(r"(?=^## Suggestion)", report, flags=re.M)
    out: list[Suggestion] = []
    for b in blocks:
        b = b.strip()
        if not b.startswith("## Suggestion"):
            continue
        title_m = re.match(r"## Suggestion:\s*(.+)", b)
        add_m = re.search(r"\*\*Add to CLAUDE\.md:\*\*\s*(.+)", b)
        if not title_m or not add_m:
            continue
        title = title_m.group(1).strip()
        add_line = add_m.group(1).strip()
        key = hashlib.sha256(add_line.lower().encode()).hexdigest()[:16]
        out.append(Suggestion(title=title, add_line=add_line, block=b, key=key, category=category))
    return out


# --- revising a suggestion from user feedback (the app's "comment" action) --
# The model sees the evidence quotes as read-only context (it needs them to
# make a sensible revision) but the OUTPUT never takes evidence from the
# model's response — new_block below is built by string-replacing only the
# title heading and the "Add to CLAUDE.md" line on top of the ORIGINAL block,
# so the evidence text that ships is always byte-for-byte what verify_quotes()
# already checked against the real transcripts, regardless of what the model
# did or didn't do with it. That's what actually matters: not whether the
# model was tempted, but whether a paraphrased quote could ever reach disk.
# It can't — nothing here ever reads a quote back out of the model's reply.

REVISE_PROMPT = """A user reviewing this suggested CLAUDE.md/AGENTS.md addition left feedback on it. \
Rewrite ONLY the title and the instruction line based on their feedback. Do not invent, rephrase, or \
comment on the evidence quotes below — they're shown for context only.

If their feedback says the suggestion is wrong, not worth adding, or should be dropped, make the \
instruction line say so plainly (start it with "DISMISSED:") rather than forcing a rewrite.

Current title: {title}
Current instruction: {add_line}
Evidence (context only — do not rewrite this):
{evidence_text}

User feedback: {comment}

Output exactly two lines, nothing else:
Title: <new title>
Instruction: <new instruction>
"""


def revise_suggestion(s: Suggestion, comment: str, model: str, provider: str = "auto") -> Suggestion:
    evidence_text = "\n".join(re.findall(r"^- Session .*$", s.block, re.M)) or "(none)"
    prompt = REVISE_PROMPT.format(title=s.title, add_line=s.add_line, evidence_text=evidence_text, comment=comment)
    text, in_tok, out_tok = _complete(model, prompt, max_tokens=300, provider=provider)
    log.info("revise (%s) for %r: input_tokens=%d, output_tokens=%d", model, s.title, in_tok, out_tok)

    title_m = re.search(r"Title:\s*(.+)", text)
    add_m = re.search(r"Instruction:\s*(.+)", text)
    new_title = title_m.group(1).strip() if title_m else s.title
    new_add_line = add_m.group(1).strip() if add_m else s.add_line

    new_block = s.block.replace(f"## Suggestion: {s.title}", f"## Suggestion: {new_title}", 1)
    new_block = re.sub(r"(\*\*Add to CLAUDE\.md:\*\*\s*).+", lambda m: m.group(1) + new_add_line, new_block, count=1)
    key = hashlib.sha256(new_add_line.lower().encode()).hexdigest()[:16]
    return Suggestion(title=new_title, add_line=new_add_line, block=new_block, key=key)


# --- the "already suggested" ledger ------------------------------------------

def ledger_path() -> Path:
    return Path(os.environ.get("SESSION_REVIEWER_HOME") or (Path.home() / ".session_reviewer")) / "ledger.json"


def load_ledger() -> dict:
    p = ledger_path()
    if not p.is_file():
        log.debug("no ledger file yet at %s", p)
        return {}
    try:
        ledger = json.loads(p.read_text(encoding="utf-8"))
        log.debug("loaded ledger from %s: %d project(s)", p, len(ledger))
        return ledger
    except (json.JSONDecodeError, OSError):
        log.warning("ledger at %s is unreadable; starting fresh", p)
        return {}


def save_ledger(ledger: dict) -> None:
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ledger, indent=2), encoding="utf-8")


def ledger_stats(ledger: dict) -> dict:
    """All-time counts across every project ever reviewed — powers the app's
    Analytics tab. Pure aggregation over the ledger already on disk, no new
    tracking file: 'repos worked up' is just how many distinct project keys
    the ledger has ever recorded a suggestion for.
    'changes_written' is the subset of 'accepted' that actually landed in a
    CLAUDE.md/AGENTS.md file — accepted_at is only set at the moment
    write_accepted_suggestions() runs (see apply_flow / app.py's
    /api/apply), so it's the real "a change was made" signal, not just
    "the user clicked approve"."""
    stats = {
        "repos_worked_up": len(ledger), "total_suggestions": 0,
        "accepted": 0, "rejected": 0, "pending": 0, "changes_written": 0,
    }
    for project in ledger.values():
        for entry in project.values():
            stats["total_suggestions"] += 1
            status = entry.get("status", "seen")
            if status == "accepted":
                stats["accepted"] += 1
                if entry.get("accepted_at"):
                    stats["changes_written"] += 1
            elif status == "rejected":
                stats["rejected"] += 1
            else:
                stats["pending"] += 1
    return stats


def filter_new_suggestions(
    suggestions: list[Suggestion], project_key: str, ledger: dict
) -> tuple[list[Suggestion], list[Suggestion]]:
    """Splits suggestions into (new, already-seen-before), and records new ones
    in the ledger as 'seen' so a future run won't repeat them."""
    proj = ledger.setdefault(project_key, {})
    now = _now_iso()
    new, seen = [], []
    for s in suggestions:
        entry = proj.get(s.key)
        if entry:
            entry["last_seen"] = now
            seen.append(s)
        else:
            proj[s.key] = {"title": s.title, "status": "seen", "first_seen": now, "last_seen": now}
            new.append(s)
    return new, seen


def _iso_to_epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def check_rule_effectiveness(
    ledger: dict, project_key: str, sessions: dict[str, str], session_mtimes: dict[str, float]
) -> list[dict]:
    """For every suggestion this project's ledger already marked 'accepted', check whether any
    of its original evidence quotes still shows up verbatim in a session recorded AFTER it was
    accepted. Answers "did this rule actually stick?" using only data Ghost Writer already has —
    ledger timestamps (accepted_at) plus the same substring search verify_quotes() already does.
    No extra LLM call.
    ponytail: literal substring match, not semantic — a rephrased recurrence of the same mistake
    won't be caught. Upgrade to an LLM judge if that blind spot matters.
    """
    results = []
    for key, entry in ledger.get(project_key, {}).items():
        if entry.get("status") != "accepted" or not entry.get("accepted_at") or not entry.get("evidence_quotes"):
            continue
        cutoff = _iso_to_epoch(entry["accepted_at"])
        recurrences = [
            {"session": name, "quote": q}
            for name, text in sessions.items()
            if session_mtimes.get(name, 0) > cutoff
            for q in entry["evidence_quotes"]
            if q in text
        ]
        results.append({
            "key": key,
            "title": entry.get("title"),
            "add_line": entry.get("add_line"),
            "accepted_at": entry["accepted_at"],
            "recurrences": recurrences,
            "sticking": not recurrences,
        })
    log.info(
        "rule effectiveness: %d accepted suggestion(s) checked, %d still recurring",
        len(results), sum(1 for r in results if not r["sticking"]),
    )
    return results


# --- guardrail: never let a suggestion smuggle an instruction into CLAUDE.md/AGENTS.md ---
# CLAUDE.md/AGENTS.md are read as trusted instructions by every future AI agent
# session in this project — so this is the one file this whole tool touches
# that a hidden prompt-injection in a transcript could turn into a persistent
# attack. Suggestions come from a model reading YOUR OWN transcripts, so this
# is a low-probability, high-blast-radius case, not a hypothetical stranger's
# input — worth a check even though the writer is you reviewing your own data.
# ponytail: heuristic pattern list, not a real prompt-injection classifier —
# extend the list if something slips through, don't build a scanning service.
UNSAFE_INSTRUCTION_PATTERNS = [
    re.compile(r"(?i)ignore (all |any )?(previous|prior|above) instructions"),
    re.compile(r"(?i)disregard (the |your )?(system|previous) prompt"),
    re.compile(r"(?i)\byou are now\b"),
    re.compile(r"(?i)\bact as\b.{0,30}\b(root|admin|system)\b"),
    re.compile(r"(?i)curl\s+\S+\s*\|\s*(sh|bash)"),
    re.compile(r"(?i)\brm\s+-rf\b"),
    re.compile(r"(?i)\bexfiltrat\w*\b"),
    re.compile(r"(?i)\bsend\b.{0,40}\bto\s+https?://"),
]


def unsafe_reasons(text: str) -> list[str]:
    """Which UNSAFE_INSTRUCTION_PATTERNS (if any) match this suggestion's instruction text."""
    return [p.pattern for p in UNSAFE_INSTRUCTION_PATTERNS if p.search(text)]


# --- --apply: interactive per-suggestion review, then a single gated write ---

def apply_flow(suggestions: list[Suggestion], ledger: dict, project_key: str, project_path: Path) -> None:
    if not suggestions:
        print("\nNothing new to review.", file=sys.stderr)
        return

    accepted: list[Suggestion] = []
    for s in suggestions:
        print("\n" + s.block)
        reasons = unsafe_reasons(s.add_line)
        if reasons:
            log.warning("suggestion %r flagged as unsafe (%s)", s.title, ", ".join(reasons))
            print(
                f"\n⚠️  SECURITY WARNING: this instruction matches a suspicious pattern ({', '.join(reasons)}).\n"
                "CLAUDE.md/AGENTS.md are read as trusted instructions by every future AI agent session in "
                "this project — make sure this is a genuine coding note, not something injected via a transcript."
            )
        try:
            ans = input("\nAdd this to CLAUDE.md/AGENTS.md? [y/N/q] ").strip().lower()
        except EOFError:
            log.warning("no interactive input available; stopping --apply review")
            break
        if ans == "q":
            break
        status = "accepted" if ans == "y" else "rejected"
        entry = ledger[project_key][s.key]
        entry["status"] = status
        entry["last_seen"] = _now_iso()
        if status == "accepted":
            entry["accepted_at"] = entry.get("accepted_at") or _now_iso()
            entry["add_line"] = s.add_line
            entry["evidence_quotes"] = QUOTE_RE.findall(s.block)
            accepted.append(s)
        log.info("suggestion %r: %s", s.title, status)

    if not accepted:
        print("\nNothing accepted — no file written.", file=sys.stderr)
        return

    target, block = _target_and_block(project_path, accepted)
    print(f"\nAbout to append to {target}:\n{block}")
    try:
        confirm = input(f"Write this to {target}? [y/N] ").strip().lower()
    except EOFError:
        confirm = "n"
    if confirm == "y":
        write_accepted_suggestions(project_path, accepted)
    else:
        log.info("write cancelled by user; nothing written")


def _target_and_block(project_path: Path, accepted: list[Suggestion]) -> tuple[Path, str]:
    agents_md = project_path / "AGENTS.md"
    target = agents_md if agents_md.is_file() else project_path / "CLAUDE.md"
    # redact() again here, defense in depth: this text was already redacted
    # before it ever reached a model, but this is the actual write into a
    # file every future agent session trusts, so a second, free check on the
    # exact bytes about to land on disk costs nothing and catches anything
    # a model paraphrased back in from redacted-looking input.
    block = (
        f"\n## Added by session_reviewer.py ({_now_iso()[:10]})\n"
        + "\n".join(f"- {redact(s.add_line)}" for s in accepted)
        + "\n"
    )
    return target, block


def write_accepted_suggestions(project_path: Path, accepted: list[Suggestion]) -> tuple[Path, str]:
    """The one function in this whole codebase that writes to CLAUDE.md/AGENTS.md.
    Shared by --apply (CLI, after its own interactive confirm) and the app's
    /api/apply endpoint (after its own explicit confirm) — one write path,
    one place to audit."""
    target, block = _target_and_block(project_path, accepted)
    with target.open("a", encoding="utf-8") as f:
        f.write(block)
    log.info("appended %d accepted suggestion(s) to %s", len(accepted), target)
    return target, block


# --- CLI -------------------------------------------------------------------------

@dataclass
class Args:
    project: Path
    sessions: int
    model: str
    fast_model: str
    provider: str
    harnesses: list[str]
    security: bool
    apply: bool
    out: Path | None
    log_file: Path | None
    verbose: bool


def parse_args(argv: list[str]) -> Args:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("project", type=Path, help="Path to the project folder to review")
    p.add_argument("-n", "--sessions", type=int, default=DEFAULT_SESSION_COUNT,
                   help=f"How many recent sessions to look at (default {DEFAULT_SESSION_COUNT})")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Synthesis model — clusters candidates, writes suggestions, web-searches (default {DEFAULT_MODEL})")
    p.add_argument("--fast-model", default=DEFAULT_FAST_MODEL,
                   help=f"Extraction model — reads raw transcripts, one call per session (default {DEFAULT_FAST_MODEL})")
    p.add_argument("--provider", default="auto", choices=["auto", "anthropic", "openai", "gemini", "ollama", "openrouter"],
                   help="Force a provider for both --model/--fast-model instead of guessing from the model name "
                        "(default auto — required for ollama/openrouter, whose model names have no distinguishing prefix)")
    p.add_argument("--harnesses", default=DEFAULT_HARNESSES,
                   help=f"Comma-separated list to scan (default {DEFAULT_HARNESSES}). "
                        f"Also accepts cursor/opencode/hermes/grok/pi/antigravity-ide to report detection without parsing.")
    p.add_argument("--security", action="store_true",
                   help="Also run a second extraction+synthesis pass hunting specifically for security-relevant "
                        "mistakes (hardcoded secrets, SQL/command built by string concatenation, skipped auth "
                        "checks) instead of only workflow friction. Same pipeline, a prompt variant.")
    p.add_argument("--apply", action="store_true",
                   help="After the report, review new suggestions one by one and optionally write accepted ones")
    p.add_argument("-o", "--out", type=Path, default=None, help="Also write the report to this .md file")
    p.add_argument("--log-file", type=Path, default=None, help="Also write logs to this file")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug-level logging (shows redaction detail, per-file cwd matches)")
    a = p.parse_args(argv)
    return Args(
        project=a.project, sessions=a.sessions, model=a.model, fast_model=a.fast_model, provider=a.provider,
        harnesses=[h.strip() for h in a.harnesses.split(",") if h.strip()],
        security=a.security, apply=a.apply, out=a.out, log_file=a.log_file, verbose=a.verbose,
    )


def _setup_logging(verbose: bool, log_file: Path | None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S", handlers=handlers)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    _setup_logging(args.verbose, args.log_file)
    log.debug("args: %s", args)
    load_dotenv()

    fast_provider = resolve_provider(args.provider, args.fast_model)
    synth_provider = resolve_provider(args.provider, args.model)
    needed = {fast_provider, synth_provider}
    key_env = {
        "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }
    for provider, env_var in key_env.items():
        if provider in needed and not os.environ.get(env_var):
            log.error("--model/--fast-model/--provider requested %s but %s is not set (checked environment and .env).", provider, env_var)
            return 1
    log.info("models: extraction=%s (%s), synthesis=%s (%s)", args.fast_model, fast_provider, args.model, synth_provider)

    project_path = args.project.expanduser()
    if not project_path.exists():
        log.error("project folder does not exist: %s", project_path)
        return 1

    log.info("scanning for sessions under project=%s harnesses=%s", project_path, ", ".join(args.harnesses))
    all_matches = find_all_sessions(project_path, args.harnesses)
    if not all_matches:
        log.error("no sessions found for %s across harnesses: %s", project_path, ", ".join(args.harnesses))
        return 1

    chosen = all_matches[: args.sessions]
    log.info("reviewing %d of %d found session(s)", len(chosen), len(all_matches))

    sessions = {ref.name: trim_session(ref) for ref in chosen}
    log.debug("trimmed %d session(s): %s", len(sessions), ", ".join(sessions))

    try:
        log.info("extracting candidates from %d session(s)...", len(sessions))
        candidates = {name: extract_candidates(text, args.fast_model, args.provider) for name, text in sessions.items()}
        synthesis_prompt = build_synthesis_prompt(candidates, len(chosen))
        log.info("synthesizing report...")
        raw_report = synthesize(synthesis_prompt, args.model, provider=args.provider)

        sec_suggestions: list[Suggestion] = []
        if args.security:
            log.info("running security-pattern pass on %d session(s)...", len(sessions))
            sec_candidates = {
                name: extract_candidates(text, args.fast_model, args.provider, SECURITY_EXTRACT_PROMPT)
                for name, text in sessions.items()
            }
            sec_prompt = build_synthesis_prompt(sec_candidates, len(chosen), SECURITY_SYNTHESIS_PROMPT)
            sec_raw_report = synthesize(sec_prompt, args.model, provider=args.provider)
            sec_report = verify_quotes(sec_raw_report, sessions)
            sec_suggestions = parse_suggestions(sec_report, category="security")
    except (anthropic.APIError, urllib.error.URLError, RuntimeError, KeyError) as e:
        log.exception("model API call failed: %s", e)
        return 1

    report = verify_quotes(raw_report, sessions)
    suggestions = parse_suggestions(report) + sec_suggestions
    log.info("parsed %d suggestion(s) from the report (%d security)", len(suggestions), len(sec_suggestions))

    project_key = _norm(project_path)
    ledger = load_ledger()
    new, seen = filter_new_suggestions(suggestions, project_key, ledger)
    save_ledger(ledger)
    log.info("ledger: %d new, %d already seen (ledger: %s)", len(new), len(seen), ledger_path())

    if not suggestions:
        body = report + (f"\n\n## Security-pattern findings\n\n{sec_report}" if args.security else "")
    elif not new:
        body = f"All {len(seen)} suggestion(s) were already surfaced in a previous run — nothing new. (ledger: {ledger_path()})"
    else:
        workflow_new = [s for s in new if s.category != "security"]
        security_new = [s for s in new if s.category == "security"]
        body = "\n\n".join(s.block for s in workflow_new)
        if security_new:
            body += "\n\n---\n## 🔒 Security-pattern findings\n\n" + "\n\n".join(s.block for s in security_new)
        if seen:
            body += f"\n\n---\n{len(seen)} suggestion(s) suppressed as already seen in a previous run (see {ledger_path()})."

    effectiveness = check_rule_effectiveness(ledger, project_key, sessions, {ref.name: ref.mtime for ref in chosen})
    if effectiveness:
        eff_lines = ["\n\n---\n## Rule effectiveness (previously accepted suggestions)\n"]
        for r in effectiveness:
            if r["sticking"]:
                eff_lines.append(f"- ✅ **{r['title']}** — no recurrence since accepted {r['accepted_at'][:10]}.")
            else:
                sess_list = ", ".join(sorted({rec['session'] for rec in r['recurrences']}))
                eff_lines.append(
                    f"- ⚠️ **{r['title']}** — still recurring in: {sess_list} (accepted {r['accepted_at'][:10]}). "
                    "This rule doesn't seem to be sticking."
                )
        body += "\n".join(eff_lines)

    header = (
        f"# Ghost Writer report\n\n"
        f"Project: `{project_path}`  \n"
        f"Harnesses: {', '.join(args.harnesses)}  \n"
        f"Sessions reviewed ({len(chosen)} of {len(all_matches)} found, most recent first):\n"
        + "".join(f"- [{ref.harness}] {ref.name}\n" for ref in chosen)
        + "\nNothing here was written to CLAUDE.md/AGENTS.md unless you pass --apply and accept a suggestion.\n\n---\n\n"
    )
    full_report = header + body

    print(full_report)
    if args.out:
        args.out.write_text(full_report, encoding="utf-8")
        log.info("wrote report to %s", args.out)

    if args.apply:
        apply_flow(new, ledger, project_key, project_path)
        save_ledger(ledger)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
