#!/usr/bin/env python3
"""Ghost Writer — local web app.

A GUI wrapper around session_reviewer.py's pipeline: pick a provider, scan
this machine for projects with AI-coding session history, run the review,
then approve/reject/comment on each suggestion before anything is written
to CLAUDE.md/AGENTS.md. Runs entirely on localhost — see README.md.

Usage: python app.py [--port 8765]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anthropic

import session_reviewer as sr

log = logging.getLogger("session_reviewer.app")

WEBAPP_DIR = Path(__file__).with_name("webapp")

# --- in-memory state -----------------------------------------------------
# ponytail: a single global STATE/JOBS pair, not a session/user table — this
# is a local single-user desktop tool with one browser tab talking to one
# process. Add real multi-session state the day this needs to serve more
# than one person at once (it shouldn't, ever — see README's security notes).
STATE_LOCK = threading.Lock()
STATE: dict = {
    "provider": "anthropic",
    "model": sr.DEFAULT_MODEL,
    "fast_model": sr.DEFAULT_FAST_MODEL,
    "ollama_host": "http://localhost:11434",
    "configured": False,
}
JOBS_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}

KEY_ENV_VAR = {
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _run_scan_job(
    job_id: str, project_path: Path, sessions_n: int, provider: str, model: str, fast_model: str, security: bool = False,
) -> None:
    job = JOBS[job_id]
    try:
        job["status"] = "scanning"
        all_matches = sr.find_all_sessions(project_path, ["claude-code", "codex", "antigravity"])
        if not all_matches:
            job["status"] = "error"
            job["error"] = "No sessions found for this project."
            return

        chosen = all_matches[:sessions_n]
        sessions = {ref.name: sr.trim_session(ref) for ref in chosen}

        job["status"] = "extracting"
        candidates = {name: sr.extract_candidates(text, fast_model, provider) for name, text in sessions.items()}
        prompt = sr.build_synthesis_prompt(candidates, len(chosen))

        job["status"] = "synthesizing"
        raw_report = sr.synthesize(prompt, model, provider=provider)

        report = sr.verify_quotes(raw_report, sessions)
        parsed = sr.parse_suggestions(report)

        if security:
            job["status"] = "extracting_security"
            sec_candidates = {
                name: sr.extract_candidates(text, fast_model, provider, sr.SECURITY_EXTRACT_PROMPT)
                for name, text in sessions.items()
            }
            sec_prompt = sr.build_synthesis_prompt(sec_candidates, len(chosen), sr.SECURITY_SYNTHESIS_PROMPT)
            job["status"] = "synthesizing_security"
            sec_raw_report = sr.synthesize(sec_prompt, model, provider=provider)
            sec_report = sr.verify_quotes(sec_raw_report, sessions)
            parsed += sr.parse_suggestions(sec_report, category="security")

        project_key = sr._norm(project_path)
        with STATE_LOCK:
            ledger = sr.load_ledger()
            new, seen = sr.filter_new_suggestions(parsed, project_key, ledger)
            effectiveness = sr.check_rule_effectiveness(ledger, project_key, sessions, {r.name: r.mtime for r in chosen})
            sr.save_ledger(ledger)

        job["header"] = {
            "project": str(project_path),
            "sessions_reviewed": len(chosen),
            "sessions_found": len(all_matches),
            "sessions": [{"harness": r.harness, "name": r.name} for r in chosen],
        }
        job["effectiveness"] = effectiveness
        job["suggestions"] = [
            {
                "id": i,
                "title": s.title,
                "add_line": s.add_line,
                "block": s.block,
                "key": s.key,
                "category": s.category,
                "status": "pending",
                "unsafe_reasons": sr.unsafe_reasons(s.add_line),
                "already_seen": False,
            }
            for i, s in enumerate(new)
        ] + [
            {
                "id": len(new) + i,
                "title": s.title,
                "add_line": s.add_line,
                "block": s.block,
                "key": s.key,
                "category": s.category,
                "status": "pending",
                "unsafe_reasons": sr.unsafe_reasons(s.add_line),
                "already_seen": True,
            }
            for i, s in enumerate(seen)
        ]
        job["status"] = "done"
    except (anthropic.APIError, RuntimeError, KeyError) as e:
        log.exception("scan job %s failed", job_id)
        job["status"] = "error"
        job["error"] = str(e)
    except Exception as e:  # noqa: BLE001 — a background thread has no other way to surface this
        log.exception("scan job %s failed unexpectedly", job_id)
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 — quiet the default stderr access log; we log via `log` instead
        log.debug("%s - %s", self.address_string(), fmt % args)

    # --- helpers ---
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _error(self, status, message):
        log.warning("%s %s -> %d: %s", self.command, self.path, status, message)
        self._send_json({"error": message}, status)

    def _serve_static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        file_path = (WEBAPP_DIR / rel).resolve()
        if WEBAPP_DIR.resolve() not in file_path.parents and file_path != WEBAPP_DIR.resolve():
            return self._error(404, "not found")
        if not file_path.is_file():
            return self._error(404, "not found")
        content_type = "text/html" if file_path.suffix == ".html" else "application/octet-stream"
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- routing ---
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/projects":
                projects = sr.discover_projects(["claude-code", "codex", "antigravity"])
                return self._send_json({"projects": projects})
            if path.startswith("/api/scan/"):
                job_id = path.rsplit("/", 1)[-1]
                job = JOBS.get(job_id)
                if not job:
                    return self._error(404, "unknown job id")
                return self._send_json(job)
            if path == "/api/whoami":
                return self._send_json({"username": sr.git_remote_username()})
            if path == "/api/state":
                with STATE_LOCK:
                    return self._send_json(dict(STATE))  # never holds the key itself, safe to return whole
            if path == "/api/analytics":
                projects = sr.discover_projects(["claude-code", "codex", "antigravity"])
                by_harness: dict[str, int] = {}
                for p in projects:
                    for h in p["harnesses"]:
                        by_harness[h] = by_harness.get(h, 0) + 1
                with STATE_LOCK:
                    ledger = sr.load_ledger()
                stats = sr.ledger_stats(ledger)
                stats["repos_detected"] = len(projects)
                stats["sessions_detected"] = sum(p["sessions"] for p in projects)
                stats["by_harness"] = by_harness
                return self._send_json(stats)
            if path == "/api/effectiveness":
                qs = parse_qs(urlparse(self.path).query)
                project_path_str = (qs.get("project_path") or [None])[0]
                if not project_path_str:
                    return self._error(400, "project_path is required")
                project_path = Path(project_path_str).expanduser()
                if not project_path.exists():
                    return self._error(400, f"project folder does not exist: {project_path_str}")
                all_matches = sr.find_all_sessions(project_path, ["claude-code", "codex", "antigravity"])
                sessions = {ref.name: sr.trim_session(ref) for ref in all_matches}
                session_mtimes = {ref.name: ref.mtime for ref in all_matches}
                project_key = sr._norm(project_path)
                with STATE_LOCK:
                    ledger = sr.load_ledger()
                results = sr.check_rule_effectiveness(ledger, project_key, sessions, session_mtimes)
                return self._send_json({"project": str(project_path), "results": results})
            return self._serve_static(path)
        except Exception as e:  # noqa: BLE001
            log.exception("GET %s failed", path)
            self._error(500, str(e))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/session":
                return self._handle_session()
            if path == "/api/scan":
                return self._handle_scan()
            if path == "/api/suggestion/decide":
                return self._handle_decide()
            if path == "/api/suggestion/revise":
                return self._handle_revise()
            if path == "/api/apply":
                return self._handle_apply()
            return self._error(404, "unknown endpoint")
        except Exception as e:  # noqa: BLE001
            log.exception("POST %s failed", path)
            self._error(500, str(e))

    # --- handlers ---
    def _handle_session(self):
        body = self._read_json()
        provider = body.get("provider", "anthropic")
        if provider not in ("anthropic", "openai", "gemini", "ollama", "openrouter"):
            return self._error(400, f"unknown provider: {provider}")

        with STATE_LOCK:
            STATE["provider"] = provider
            STATE["model"] = body.get("model") or STATE["model"]
            STATE["fast_model"] = body.get("fast_model") or STATE["fast_model"]
            STATE["ollama_host"] = body.get("ollama_host") or STATE["ollama_host"]
            STATE["configured"] = True
            os.environ["OLLAMA_HOST"] = STATE["ollama_host"]
            api_key = (body.get("api_key") or "").strip()
            if provider != "ollama":
                if not api_key:
                    return self._error(400, f"{provider} needs an API key")
                os.environ[KEY_ENV_VAR[provider]] = api_key
                if body.get("remember"):
                    sr.save_env_var(KEY_ENV_VAR[provider], api_key)
            # Ollama's own key is separate and optional — it's not needed to talk to
            # your local server, only to enable synthesis web search via ollama.com's
            # hosted search API (see _ollama_search_complete). Free key, so no
            # provider-is-ollama gate on requiring it like the others above.
            search_key = (body.get("ollama_search_api_key") or "").strip()
            if provider == "ollama" and search_key:
                os.environ["OLLAMA_API_KEY"] = search_key
                if body.get("remember"):
                    sr.save_env_var("OLLAMA_API_KEY", search_key)

        self._send_json({"ok": True, "provider": provider})

    def _handle_scan(self):
        body = self._read_json()
        project_path_str = body.get("project_path")
        if not project_path_str:
            return self._error(400, "project_path is required")
        if not Path(project_path_str).expanduser().exists():
            return self._error(400, f"project folder does not exist: {project_path_str}")
        with STATE_LOCK:
            if not STATE["configured"]:
                return self._error(400, "call /api/session first")
            provider, model, fast_model = STATE["provider"], STATE["model"], STATE["fast_model"]

        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {
            "status": "queued", "project_path": project_path_str, "suggestions": [],
            "header": None, "error": None, "effectiveness": [],
        }
        thread = threading.Thread(
            target=_run_scan_job,
            args=(job_id, Path(project_path_str).expanduser(), int(body.get("sessions", 10)), provider, model, fast_model),
            kwargs={"security": bool(body.get("security"))},
            daemon=True,
        )
        thread.start()
        self._send_json({"job_id": job_id})

    def _handle_decide(self):
        body = self._read_json()
        job = JOBS.get(body.get("job_id"))
        if not job:
            return self._error(404, "unknown job id")
        status = body.get("status")
        if status not in ("accepted", "rejected", "pending"):
            return self._error(400, f"invalid status: {status}")
        for s in job["suggestions"]:
            if s["id"] == body.get("id"):
                s["status"] = status
                return self._send_json({"ok": True, "suggestion": s})
        self._error(404, "unknown suggestion id")

    def _handle_revise(self):
        body = self._read_json()
        job = JOBS.get(body.get("job_id"))
        if not job:
            return self._error(404, "unknown job id")
        comment = (body.get("comment") or "").strip()
        if not comment:
            return self._error(400, "comment is required")

        for s in job["suggestions"]:
            if s["id"] == body.get("id"):
                original = sr.Suggestion(title=s["title"], add_line=s["add_line"], block=s["block"], key=s["key"])
                with STATE_LOCK:
                    provider, model = STATE["provider"], STATE["model"]
                try:
                    revised = sr.revise_suggestion(original, comment, model, provider)
                except (anthropic.APIError, RuntimeError) as e:
                    return self._error(502, f"revision failed: {e}")
                s.update(
                    title=revised.title, add_line=revised.add_line, block=revised.block,
                    key=revised.key, status="pending", unsafe_reasons=sr.unsafe_reasons(revised.add_line),
                )
                return self._send_json({"ok": True, "suggestion": s})
        self._error(404, "unknown suggestion id")

    def _handle_apply(self):
        body = self._read_json()
        job = JOBS.get(body.get("job_id"))
        if not job:
            return self._error(404, "unknown job id")
        accepted_dicts = [s for s in job["suggestions"] if s["status"] == "accepted"]
        if not accepted_dicts:
            return self._error(400, "no accepted suggestions to apply")

        accepted = [sr.Suggestion(title=s["title"], add_line=s["add_line"], block=s["block"], key=s["key"]) for s in accepted_dicts]
        project_path = Path(job["project_path"]).expanduser()
        target, block = sr.write_accepted_suggestions(project_path, accepted)

        project_key = sr._norm(project_path)
        with STATE_LOCK:
            ledger = sr.load_ledger()
            proj = ledger.setdefault(project_key, {})
            now = sr._now_iso()
            for s in job["suggestions"]:
                if s["status"] in ("accepted", "rejected"):
                    entry = proj.setdefault(s["key"], {"title": s["title"]})
                    entry["status"] = s["status"]
                    entry["last_seen"] = now
                    if s["status"] == "accepted":
                        entry["accepted_at"] = entry.get("accepted_at") or now
                        entry["add_line"] = s["add_line"]
                        entry["evidence_quotes"] = sr.QUOTE_RE.findall(s["block"])
            sr.save_ledger(ledger)

        for s in accepted_dicts:
            s["status"] = "applied"
        self._send_json({"ok": True, "written_to": str(target), "block": block})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="Don't auto-open a browser tab")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    sr.load_dotenv()

    # 127.0.0.1 only, never 0.0.0.0 — this process ends up holding a live API
    # key in memory, and it has no auth of its own. It must never be reachable
    # from outside this machine.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    log.info("Ghost Writer app running at %s (Ctrl+C to stop)", url)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
