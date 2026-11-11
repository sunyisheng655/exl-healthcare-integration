"""Loopback-only HTTP interface to the existing local Agent workflow."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import stat
from threading import Lock, Thread
import uuid

from .artifacts import approve, query
from .controller import DEFAULT_GOAL, MAX_CODE_BYTES, run_job
from .credentials import CredentialError
from .local_sources import local_api
from .provider import ProviderError
from .runner import CandidateRunner, SandboxUnavailable


STATIC_ROOT = Path(__file__).resolve().parents[1] / "web"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
JOB_ID = re.compile(r"[0-9a-f]{16}")
SHA256 = re.compile(r"[0-9a-f]{64}")
CANDIDATE = re.compile(r"candidate-0[1-4]\.py")
JOB_ROUTE = re.compile(r"/api/jobs/([0-9a-f]{16})(?:/(approve|query)|/code/(candidate-0[1-4]\.py))?")
MAX_REQUEST_BYTES = 16_384
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_REPAIRS = 3
JOB_TIMEOUT = 360
SECRET = re.compile(r"(?i)\b(?:sk-(?:or-)?[a-z0-9_-]{4,}|Bearer\s+[a-z0-9_.~-]+)")
LOCAL_PATH = re.compile(
    r"(?i)(?:/(?:Users|home|var|private|tmp|Volumes|opt|etc|dev|usr|Library|System)/"
    r"|(?<![A-Z0-9_])[A-Z]:[\\/])[^\r\n\t'\"<>]*"
)
STATUS = {"PASSED", "FAILED", "BLOCKED", "NEEDS_CLARIFICATION", "AWAITING_REPAIR_AUTHORIZATION", "NOT_PERFORMED"}


class WebError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _redact(text: str) -> str:
    env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_key:
        text = text.replace(env_key, "[redacted]")
    return LOCAL_PATH.sub("[local path]", SECRET.sub("[redacted]", text))


def _text(value, limit: int = 2000) -> str:
    return _redact(value)[:limit] if isinstance(value, str) else ""


def _status(value) -> str:
    return value if isinstance(value, str) and value in STATUS else "BLOCKED"


def _public_value(value, depth: int = 0):
    if type(value) is str:
        return _text(value)
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if depth >= 7:
        return "[omitted]"
    if type(value) is list:
        return [_public_value(item, depth + 1) for item in value[:128]]
    if type(value) is dict:
        return {_text(key, 128): _public_value(item, depth + 1) for key, item in list(value.items())[:64]}
    return None


def _public_suite(raw, *, include_cases: bool) -> dict:
    if type(raw) is not dict:
        return {}
    suite = {key: raw[key] for key in ("total", "executed", "passed", "failed", "blocked", "execution_errors")
             if type(raw.get(key)) is int and raw[key] >= 0}
    suite["status"] = _status(raw.get("status"))
    if include_cases:
        suite["cases"] = []
        cases = raw.get("cases")
        for case in cases[:32] if type(cases) is list else []:
            if type(case) is not dict:
                continue
            public = {"case_id": _text(case.get("case_id"), 100), "status": _status(case.get("status")),
                      "differences": []}
            differences = case.get("differences")
            for difference in differences[:8] if type(differences) is list else []:
                if type(difference) is dict:
                    public["differences"].append(_public_value({k: difference[k] for k in
                        ("path", "code", "expected", "actual", "expected_type", "actual_type") if k in difference}))
            error = case.get("execution_error")
            if type(error) is dict:
                public["execution_error"] = {"code": _text(error.get("code"), 80),
                                              "message": "The candidate did not return a usable result."}
            suite["cases"].append(public)
    return suite


def _attempts(report: dict) -> list[dict]:
    attempts = report.get("attempts")
    return [a for a in attempts[:4] if type(a) is dict] if type(attempts) is list else []


def _public_report(report: dict) -> dict:
    result = {"job_id": report["job_id"], "goal": _text(report.get("goal")), "status": _status(report.get("status")),
              "reason": _text(report.get("reason")), "heldout_status": _status(report.get("heldout_status", "NOT_PERFORMED")),
              "attempts": [], "limits": {}, "source_counts": {}}
    for field, keys in (("limits", ("allow_auto_repair", "max_repairs", "timeout_seconds")),
                        ("source_counts", ("patients", "appointments", "observations"))):
        section = report.get(field)
        if type(section) is dict:
            result[field] = {key: section[key] for key in keys if type(section.get(key)) in {bool, int}}
    elapsed = report.get("elapsed_seconds")
    if type(elapsed) in {int, float} and math.isfinite(elapsed):
        result["elapsed_seconds"] = elapsed
    if "heldout" in report:
        result["heldout"] = _public_suite(report["heldout"], include_cases=False)
    for attempt in _attempts(report):
        candidate, sha = attempt.get("candidate"), attempt.get("code_sha256")
        if not (type(candidate) is str and CANDIDATE.fullmatch(candidate)
                and type(sha) is str and SHA256.fullmatch(sha)):
            continue
        result["attempts"].append({"attempt": int(candidate.removeprefix("candidate-").removesuffix(".py")),
                                   "candidate": candidate,
                                   "code_sha256": sha, "plan": _text(attempt.get("plan")),
                                   "development": _public_suite(attempt.get("development"), include_cases=True)})
    return result


def _kind(report: dict) -> str:
    calls = report.get("model_calls")
    first = calls[0] if isinstance(calls, list) and calls else None
    return "controlled_repair" if isinstance(first, dict) and first.get("model") == "controlled-fixture" else "live_generation"


def _regular_bytes(path: Path, limit: int) -> bytes:
    if path.resolve() != path:
        raise ValueError("A local artifact cannot be a symbolic link.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("A local artifact must be a regular file.")
        result = stream.read(limit + 1)
    if len(result) > limit:
        raise ValueError("A local artifact exceeds the display size limit.")
    return result


class JobService:
    def __init__(self, mock_root, runtime, skill_path, model, client_factory):
        self.mock_root = Path(mock_root).resolve()
        self.skill_path = Path(skill_path).resolve()
        runtime = Path(runtime)
        if runtime.is_symlink():
            raise ValueError("The local runtime directory cannot be a symbolic link.")
        self.runtime = runtime.resolve()
        self.runtime.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.model = model
        self.client_factory = client_factory
        self.lock = Lock()
        self.running = None
        self.jobs = {}
        self.api_contexts = set()
        self.closing = False

    def _directory(self, identifier: str) -> Path:
        if not JOB_ID.fullmatch(identifier):
            raise WebError(404, "Local job not found.")
        location = self.runtime / identifier
        if location.is_symlink() or not location.is_dir():
            raise WebError(404, "Local job not found.")
        return location

    def _report(self, identifier: str) -> tuple[Path, bytes, dict]:
        location = self._directory(identifier)
        try:
            raw = _regular_bytes(location / "report.json", MAX_REPORT_BYTES)
            report = json.loads(raw)
        except (OSError, ValueError, RuntimeError):
            raise WebError(404, "Completed local report not found.") from None
        if type(report) is not dict or report.get("job_id") != identifier:
            raise WebError(404, "Completed local report not found.")
        return location, raw, report

    def _code(self, location: Path, report: dict, filename: str) -> tuple[bytes, str]:
        if not CANDIDATE.fullmatch(filename):
            raise WebError(404, "Candidate not found.")
        listed = next((a for a in _attempts(report) if a.get("candidate") == filename), None)
        if listed is None:
            raise WebError(404, "Candidate not listed in this report.")
        try:
            code = _regular_bytes(location / filename, MAX_CODE_BYTES)
        except (OSError, ValueError, RuntimeError):
            raise WebError(404, "Candidate file not found.") from None
        checksum = hashlib.sha256(code).hexdigest()
        if listed.get("code_sha256") != checksum:
            raise WebError(409, "Candidate changed after validation.")
        return code, checksum

    def _passing_candidate(self, location: Path, report: dict) -> tuple[str, str]:
        attempts = _attempts(report)
        final = attempts[-1] if attempts else {}
        development = final.get("development")
        filename = final.get("candidate")
        if (report.get("status") != "PASSED" or report.get("heldout_status") != "PASSED"
                or type(development) is not dict or development.get("status") != "PASSED"
                or type(filename) is not str):
            raise WebError(409, "This job does not have a passing final candidate.")
        _, checksum = self._code(location, report, filename)
        return filename, checksum

    def _approved(self, location: Path, raw: bytes, report: dict) -> bool:
        try:
            approval = json.loads(_regular_bytes(location / "approval.json", 65_536))
            if type(approval) is not dict or type(report.get("bindings")) is not dict:
                return False
            filename, checksum = self._passing_candidate(location, report)
            return (approval.get("job_id") == location.name and approval.get("candidate") == filename
                    and approval.get("code_sha256") == checksum and approval.get("bindings") == report["bindings"]
                    and approval.get("report_sha256") == hashlib.sha256(raw).hexdigest())
        except (OSError, ValueError, RuntimeError, WebError):
            return False

    @staticmethod
    def _summary(location: Path, report: dict, approved: bool) -> dict:
        info = location.stat()
        timestamp = datetime.fromtimestamp(getattr(info, "st_birthtime", info.st_mtime), timezone.utc)
        return {"job_id": location.name, "goal": _text(report.get("goal")), "status": _status(report.get("status")),
                "approved": approved, "created_at": timestamp.isoformat(), "kind": _kind(report)}

    def detail(self, identifier: str) -> dict:
        with self.lock:
            record = self.jobs.get(identifier)
            live = dict(record, events=list(record["events"])) if record is not None else None
        if live is not None and (live["status"] == "RUNNING" or live["error"]):
            return live
        location, raw, report = self._report(identifier)
        public = _public_report(report)
        events = live["events"] if live is not None else self._replay_events(public)
        return {**self._summary(location, report, self._approved(location, raw, report)),
                "events": events, "report": public, "error": None}

    @staticmethod
    def _replay_events(report: dict) -> list[str]:
        events = []
        for attempt in report["attempts"]:
            dev = attempt["development"]
            events.append(f"Attempt {attempt['attempt']}: development checks {dev.get('passed', 0)}/{dev.get('total', 0)} passed.")
        held = report.get("heldout")
        if held:
            events.append(f"Holdout checks: {held.get('passed', 0)}/{held.get('total', 0)} passed.")
        return events

    def summaries(self) -> list[dict]:
        summaries = {}
        for location in self.runtime.iterdir():
            if not JOB_ID.fullmatch(location.name):
                continue
            try:
                directory, raw, report = self._report(location.name)
                summaries[location.name] = self._summary(directory, report, self._approved(directory, raw, report))
            except (OSError, WebError):
                continue
        fields = ("job_id", "goal", "status", "approved", "created_at", "kind")
        with self.lock:
            for identifier, entry in self.jobs.items():
                if identifier not in summaries or entry["status"] == "RUNNING":
                    summaries[identifier] = {key: entry[key] for key in fields}
        return sorted(summaries.values(), key=lambda item: item["created_at"], reverse=True)

    @contextmanager
    def _api(self):
        # Track only temporary APIs we start, so Ctrl+C can stop them even while
        # a daemon worker is waiting on its bounded provider call.
        with self.lock:
            if self.closing:
                raise WebError(409, "The local server is stopping.")
            context = local_api(self.mock_root)
            url = context.__enter__()
            self.api_contexts.add(context)
        try:
            yield url
        finally:
            with self.lock:
                owned = context in self.api_contexts
                self.api_contexts.discard(context)
            if owned:
                context.__exit__(None, None, None)

    def close(self):
        with self.lock:
            self.closing = True
            contexts = tuple(self.api_contexts)
            self.api_contexts.clear()
        for context in contexts:
            context.__exit__(None, None, None)

    def start(self, goal: str, auto_repair: bool) -> str:
        with self.lock:
            if self.closing or self.running is not None:
                raise WebError(409, "A local generation is already running or the server is stopping.")
            identifier = uuid.uuid4().hex[:16]
            self.running = identifier
            self.jobs[identifier] = {"job_id": identifier, "goal": goal, "status": "RUNNING", "approved": False,
                                     "created_at": datetime.now(timezone.utc).isoformat(), "kind": "live_generation",
                                     "events": [], "report": None, "error": None}
        Thread(target=self._generate, args=(identifier, goal, auto_repair), daemon=True,
               name="local-agent-generation").start()
        return identifier

    def _generate(self, identifier: str, goal: str, auto_repair: bool):
        def event(line):
            with self.lock:
                events = self.jobs[identifier]["events"]
                if len(events) < 64:
                    events.append(_text(line, 300))

        status, error = "BLOCKED", None
        try:
            client = self.client_factory()
            with self._api() as url:
                report = run_job(client, CandidateRunner(self.mock_root, url), self.runtime / identifier,
                                 self.skill_path, goal=goal, allow_auto_repair=auto_repair,
                                 max_repairs=MAX_REPAIRS, timeout_seconds=JOB_TIMEOUT, event=event)
            status = _status(report.get("status"))
        except CredentialError:
            error = "OpenRouter key unavailable. Run python3 agent.py configure and try again."
        except (ProviderError, SandboxUnavailable, ValueError, WebError) as exc:
            error = _text(str(exc), 350)
        except Exception:
            error = "The local job could not finish. Check the local Agent setup and try again."
        finally:
            with self.lock:
                self.jobs[identifier].update(status=status, error=error)
                self.running = None

    def code(self, identifier: str, filename: str) -> dict:
        location, _, report = self._report(identifier)
        code, checksum = self._code(location, report, filename)
        try:
            source = code.decode("utf-8")
        except UnicodeError:
            raise WebError(409, "Candidate is not readable UTF-8 source.") from None
        # A portable interpreter directive is not a host-specific path.
        first_line, _, rest = source.partition("\n")
        review_text = rest if first_line == "#!/usr/bin/env python3" else source
        if review_text != _redact(review_text):
            raise WebError(409, "Candidate contains a local path or credential and cannot be displayed.")
        return {"code": source, "sha256": checksum}

    def approve(self, identifier: str, confirmed_hash: str) -> dict:
        location, raw, report = self._report(identifier)
        filename, checksum = self._passing_candidate(location, report)
        if confirmed_hash != checksum:
            raise WebError(409, "Confirm the exact SHA-256 of the passing candidate.")
        if self._approved(location, raw, report):
            return {"approved": True}
        if (location / "approval.json").exists() or (location / "approval.json").is_symlink():
            raise WebError(409, "This job has a stale approval. Generate and validate a new candidate.")
        # Do not approve a candidate whose exact code cannot be reviewed here.
        self.code(identifier, filename)
        with self._api() as url:
            approve(location, CandidateRunner(self.mock_root, url), confirmed_hash)
        return {"approved": True}

    def query(self, identifier: str, patient_id: str) -> dict:
        location, raw, report = self._report(identifier)
        if not self._approved(location, raw, report):
            raise WebError(409, "Approve the passing candidate before querying it.")
        with self._api() as url:
            result = query(location, CandidateRunner(self.mock_root, url), patient_id)
        return _public_value(result)


class _LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        try:
            super().server_close()
        finally:
            self.service.close()


class _Handler(BaseHTTPRequestHandler):
    server_version = "LocalIntegrationAgent"
    sys_version = ""
    timeout = 15

    def log_message(self, format, *args):
        # Request paths and user input should not enter terminal logs.
        pass

    def _send(self, status: int, content_type: str, payload: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: int, payload: dict):
        self._send(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8"))

    def _host(self) -> str:
        host = self.headers.get_all("Host", [])
        port = self.server.server_port
        if len(host) != 1 or host[0] not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            raise WebError(403, "Use this server's local address.")
        return host[0]

    def _body(self, host: str) -> dict:
        origins = self.headers.get_all("Origin", [])
        if origins and origins != ["http://" + host]:
            raise WebError(403, "Cross-origin requests are not allowed.")
        if self.headers.get_all("X-Local-Agent", []) != ["1"]:
            raise WebError(403, "The local application request header is required.")
        if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
            raise WebError(415, "Send a JSON request body.")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,9}", lengths[0]):
            raise WebError(400, "Send a valid JSON request length.")
        size = int(lengths[0])
        if size > MAX_REQUEST_BYTES:
            raise WebError(413, "Request body exceeds the local size limit.")
        try:
            raw = self.rfile.read(size)
            body = json.loads(raw.decode("utf-8")) if len(raw) == size else None
        except (OSError, ValueError, RecursionError):
            body = None
        if type(body) is not dict:
            raise WebError(400, "Send a JSON object.")
        return body

    def _route(self, write: bool):
        host = self._host()
        path = self.path
        service = self.server.service
        match = JOB_ROUTE.fullmatch(path)
        if not write:
            if path in STATIC_FILES:
                filename, content_type = STATIC_FILES[path]
                try:
                    content = _regular_bytes(STATIC_ROOT / filename, 1_048_576)
                except (OSError, ValueError, RuntimeError):
                    raise WebError(404, "Local page file not found.") from None
                self._send(200, content_type, content)
                return
            if path == "/api/config":
                self._json(200, {"model": _text(service.model, 256), "default_goal": DEFAULT_GOAL})
                return
            if path == "/api/jobs":
                self._json(200, {"jobs": service.summaries()})
                return
            if match and match[2] is None:
                self._json(200, service.code(match[1], match[3]) if match[3] else service.detail(match[1]))
                return
        else:
            body = self._body(host)
            if path == "/api/jobs":
                goal, auto = body.get("goal"), body.get("allow_auto_repair", False)
                if (set(body) - {"goal", "allow_auto_repair"} or type(goal) is not str
                        or not 1 <= len(goal.strip()) <= 2000 or type(auto) is not bool):
                    raise WebError(400, "Provide a 1–2000 character goal and a true/false auto-repair choice.")
                if "sk-or-" in goal.lower() or goal != _redact(goal):
                    raise WebError(400, "Use a synthetic integration goal without credentials or local paths.")
                self._json(202, {"job_id": service.start(goal.strip(), auto)})
                return
            if match and match[2] == "approve":
                checksum = body.get("code_sha256")
                if set(body) != {"code_sha256"} or type(checksum) is not str or not SHA256.fullmatch(checksum):
                    raise WebError(400, "Provide the exact passing candidate SHA-256.")
                self._json(200, service.approve(match[1], checksum))
                return
            if match and match[2] == "query":
                patient = body.get("patient_id")
                if set(body) != {"patient_id"} or type(patient) is not str or not re.fullmatch(r"P[0-9]{3}", patient):
                    raise WebError(400, "Provide a synthetic patient ID, for example P001.")
                self._json(200, service.query(match[1], patient))
                return
        raise WebError(404, "Local route not found.")

    def _handle(self, write: bool):
        try:
            self._route(write)
        except (BrokenPipeError, ConnectionResetError):
            return
        except WebError as exc:
            self._json(exc.status, {"error": str(exc)})
        except ValueError as exc:
            message = str(exc)
            if message.startswith("Approved query failed:"):
                message = "The approved code could not complete the mock-source query."
            self._json(409, {"error": _text(message, 350)})
        except (FileNotFoundError, FileExistsError):
            self._json(409, {"error": "The local artifact changed; reload the job and try again."})
        except Exception:
            self._json(500, {"error": "The local operation could not complete."})

    def do_GET(self):
        self._handle(False)

    do_HEAD = do_GET

    def do_POST(self):
        self._handle(True)


def create_server(*, port=8790, mock_root, runtime, skill_path, model, client_factory) -> ThreadingHTTPServer:
    """Create but do not start the loopback UI server. Model access stays lazy."""
    if type(port) is not int or not 0 <= port <= 65_535:
        raise ValueError("Use a local web port between 0 and 65535.")
    service = JobService(mock_root, runtime, skill_path, model, client_factory)
    server = _LocalServer(("127.0.0.1", port), _Handler, bind_and_activate=False)
    server.service = service
    try:
        server.server_bind()
        server.server_activate()
    except OSError:
        server.server_close()
        raise
    return server
