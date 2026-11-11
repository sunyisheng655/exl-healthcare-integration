"""Run one stdlib-only candidate in a deny-by-default macOS sandbox.

This local prototype deliberately uses Apple's Python 3.9 runtime. No generated
code is executed if sandbox-exec or its enforced policy is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import selectors
import signal
import subprocess
import tempfile
import time
from urllib.parse import urlsplit


class SandboxUnavailable(RuntimeError):
    """The required execution boundary cannot be established."""


def _reject_nonfinite(value):
    raise ValueError("Non-finite JSON number: " + value)


def _parse_float(value):
    number = float(value)
    if not math.isfinite(number):
        _reject_nonfinite(value)
    return number


class CandidateRunner:
    def __init__(self, mock_root: Path, api_base_url: str = "http://127.0.0.1:8765",
                 timeout_seconds: float = 8, max_output_bytes: int = 1048576):
        self.mock_root = Path(mock_root).resolve()
        self.api_base_url = api_base_url
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = int(max_output_bytes)
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if self.max_output_bytes < 128:
            raise ValueError("max_output_bytes must be at least 128")
        self._doctor = None
        self._runtime = None

    @property
    def sources(self) -> dict:
        """The only source locations supplied to candidate code."""
        return {
            "api_base_url": self._api_address()[0],
            "database_path": str(self._source_file("appointments-db/appointments.sqlite3")),
            "csv_path": str(self._source_file("observations-csv/observations.csv")),
        }

    def _source_file(self, relative: str) -> Path:
        path = self.mock_root / relative
        if path.resolve() != path:
            raise SandboxUnavailable("A registered mock file cannot be a symlink")
        return path

    def _api_address(self):
        try:
            parsed = urlsplit(self.api_base_url)
            port = 80 if parsed.port is None else parsed.port
        except ValueError as exc:
            raise SandboxUnavailable("Invalid registered API port") from exc
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"} or not 1 <= port <= 65535):
            raise SandboxUnavailable("Only an http://127.0.0.1:PORT registered API is supported")
        return f"http://127.0.0.1:{port}", port

    @staticmethod
    def _clean_env(output: Path):
        # Never inherit a provider key, PYTHONPATH, proxy, HOME, or other parent state.
        return {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
                "HOME": str(output), "TMPDIR": str(output)}

    def _runtime_info(self):
        if self._runtime is not None:
            return self._runtime
        if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            raise SandboxUnavailable("This runner requires macOS /usr/bin/sandbox-exec")
        # Resolve Apple's launcher outside the candidate sandbox, then invoke the
        # actual toolchain interpreter directly (no xcrun subprocess in the sandbox).
        probe = (
            "import json,sys; print(json.dumps({'executable':sys.executable,"
            "'version':sys.version.split()[0],'prefix':sys.base_prefix}))"
        )
        result = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", "-c", probe],
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode:
            raise SandboxUnavailable("Apple's stdlib Python runtime is unavailable")
        runtime = json.loads(result.stdout)
        runtime["prefix"] = str(Path(runtime["prefix"]).resolve())
        if not runtime["version"].startswith("3.9."):
            raise SandboxUnavailable("The reviewed candidate runtime requires Apple Python 3.9")
        # The framework's bin/python3.9 is itself a launcher. Enter the actual
        # interpreter so the profile need not grant another executable at boot.
        app_executable = Path(runtime["prefix"]) / "Resources/Python.app/Contents/MacOS/Python"
        runtime["executable"] = str(app_executable.resolve(strict=True))
        self._runtime = runtime
        return runtime

    def _profile(self, candidate: Path, output: Path):
        runtime = self._runtime_info()
        _, port = self._api_address()
        literal = lambda value: json.dumps(str(value), ensure_ascii=False)
        read_files = [runtime["executable"], str(candidate), str(Path(__file__).with_name("_candidate_main.py").resolve()),
                      self.sources["database_path"], self.sources["csv_path"],
                      "/dev/null", "/dev/random", "/dev/urandom", "/private/etc/localtime"]
        # These are Apple runtime resources, not user/project/home directories.
        runtime_roots = ["/System/Library", "/usr/lib", "/usr/share/locale", "/usr/share/zoneinfo",
                         str(Path(runtime["prefix"]).parents[1])]
        return "\n".join([
            "(version 1)", "(deny default)",
            # macOS dyld checks the root directory during startup. Literal /
            # permits this directory itself, never a file or subdirectory in it.
            '(allow file-read-data (literal "/"))',
            "(allow file-read* " + " ".join(f"(subpath {literal(p)})" for p in runtime_roots) + ")",
            "(allow file-read* " + " ".join(f"(literal {literal(p)})" for p in read_files) + ")",
            f"(allow file-read* file-write* (subpath {literal(output)}))",
            f"(allow process-exec (literal {literal(runtime['executable'])}))",
            # The entrypoint tightens process limits before loading the candidate.
            # Deliberately no process-fork, arbitrary exec, process-info or Mach lookup.
            '(allow sysctl-read (sysctl-name-regex #"^hw\\.") (sysctl-name "kern.osrelease") (sysctl-name "kern.ostype"))',
            f'(allow network-outbound (remote ip "localhost:{port}"))',
        ])

    def doctor(self) -> dict:
        if self._doctor is not None:
            return dict(self._doctor)
        runtime = {}
        try:
            runtime = self._runtime_info()
            self._api_address()
            for key in ("database_path", "csv_path"):
                if not Path(self.sources[key]).is_file():
                    raise SandboxUnavailable(f"Missing registered source: {key}")
            with tempfile.TemporaryDirectory(prefix="healthcare-sandbox-probe-") as directory:
                root = Path(directory).resolve()
                output = root / "output"
                output.mkdir()
                private = root / "private-marker.txt"
                private.write_text("sandbox-boundary-probe", encoding="utf-8")
                probe = root / "probe.py"
                probe.write_text(
                    "import csv,json,sqlite3,urllib.request\n"
                    "try:\n open(" + repr(str(private)) + ").read()\n"
                    "except PermissionError:\n print(json.dumps({'isolated':True}))\n"
                    "else:\n raise RuntimeError('Private file was readable')\n",
                    encoding="utf-8",
                )
                profile = root / "profile.sb"
                profile.write_text(self._profile(probe, output), encoding="utf-8")
                result = subprocess.run(
                    ["/usr/bin/sandbox-exec", "-f", str(profile), runtime["executable"], "-I", "-S", "-B", str(probe)],
                    env=self._clean_env(output), cwd=output, capture_output=True, text=True, timeout=5,
                )
                if result.returncode or json.loads(result.stdout).get("isolated") is not True:
                    detail = (result.stderr or result.stdout).strip()[:600]
                    raise SandboxUnavailable(f"Sandbox runtime probe failed (exit {result.returncode}): {detail}")
            self._doctor = {"available": True, "isolation_available": True,
                            "reason": "macOS deny-default sandbox probe passed; private-file access denied",
                            "interpreter": runtime["executable"], "interpreter_version": runtime["version"],
                            "stdlib_only": True, "backend": "macos-sandbox-exec"}
        except (SandboxUnavailable, OSError, ValueError, subprocess.SubprocessError) as exc:
            self._doctor = {"available": False, "isolation_available": False, "reason": str(exc),
                            "interpreter": runtime.get("executable"),
                            "interpreter_version": runtime.get("version"), "backend": "macos-sandbox-exec"}
        return dict(self._doctor)

    @staticmethod
    def _kill_group(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)

    def _execute(self, command, payload, output):
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=output, env=self._clean_env(output),
                                   start_new_session=True)
        streams = selectors.DefaultSelector()
        chunks = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            process.stdin.write(json.dumps(payload).encode("utf-8"))
            process.stdin.close()
            for name in chunks:
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                streams.register(stream, selectors.EVENT_READ, name)
            deadline = time.monotonic() + self.timeout_seconds
            count = 0
            while streams.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill_group(process)
                    return None, {"code": "TIMEOUT", "message": "Candidate exceeded its wall-clock limit"}
                for key, _ in streams.select(min(remaining, 0.1)):
                    data = os.read(key.fd, min(65536, self.max_output_bytes + 1))
                    if not data:
                        streams.unregister(key.fileobj)
                        continue
                    count += len(data)
                    if count > self.max_output_bytes:
                        self._kill_group(process)
                        return None, {"code": "OUTPUT_LIMIT", "message": "Candidate exceeded the combined stdout/stderr limit"}
                    chunks[key.data].extend(data)
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                self._kill_group(process)
                return None, {"code": "TIMEOUT", "message": "Candidate exceeded its wall-clock limit"}
            if process.returncode:
                return None, {"code": "EXECUTION_FAILED", "message": "Candidate process failed",
                              "detail": chunks["stderr"].decode("utf-8", errors="replace")[-1500:]}
            try:
                envelope = json.loads(chunks["stdout"].decode("utf-8"),
                                      parse_constant=_reject_nonfinite, parse_float=_parse_float)
            except (ValueError, RecursionError):
                return None, {"code": "INVALID_RESULT", "message": "Candidate did not produce one valid JSON result"}
            if not isinstance(envelope, dict) or type(envelope.get("ok")) is not bool:
                return None, {"code": "INVALID_RESULT", "message": "Invalid candidate execution envelope"}
            if not envelope["ok"]:
                error = envelope.get("error")
                if not isinstance(error, dict) or error.get("code") not in {"EXECUTION_FAILED", "INVALID_RESULT", "OUTPUT_LIMIT"}:
                    error = {"code": "EXECUTION_FAILED", "message": "Candidate raised an error"}
                return None, error
            if not isinstance(envelope.get("result"), dict):
                return None, {"code": "INVALID_RESULT", "message": "integrate() must return a JSON-compatible dict"}
            return envelope["result"], None
        except (OSError, subprocess.SubprocessError) as exc:
            self._kill_group(process)
            return None, {"code": "EXECUTION_FAILED", "message": str(exc)[:500]}
        finally:
            if process.poll() is None:
                self._kill_group(process)
            streams.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()

    def run(self, candidate_path: Path, patient_id: str, *, expected_sha256: str | None = None) -> dict:
        started = time.monotonic()
        def finish(result=None, error=None):
            return {"ok": error is None, "result": result, "error": error,
                    "elapsed_seconds": round(time.monotonic() - started, 6)}
        status = self.doctor()
        if not status["available"]:
            return finish(error={"code": "BLOCKED", "message": status["reason"]})
        try:
            if not isinstance(patient_id, str) or len(patient_id) > 128:
                return finish(error={"code": "INVALID_RESULT", "message": "patient_id must be a short string"})
            with Path(candidate_path).open("rb") as handle:
                code = handle.read(self.max_output_bytes + 1)
            if len(code) > self.max_output_bytes:
                return finish(error={"code": "OUTPUT_LIMIT", "message": "Candidate source exceeds the size limit"})
            if expected_sha256 is not None and hashlib.sha256(code).hexdigest() != expected_sha256:
                return finish(error={"code": "BLOCKED", "message": "Candidate bytes do not match the pinned hash"})
            with tempfile.TemporaryDirectory(prefix="healthcare-candidate-") as directory:
                root = Path(directory).resolve()
                candidate = root / "candidate.py"
                candidate.write_bytes(code)
                output = root / "output"
                output.mkdir()
                profile = root / "profile.sb"
                profile.write_text(self._profile(candidate, output), encoding="utf-8")
                payload = {"candidate_path": str(candidate), "patient_id": patient_id, "sources": self.sources,
                           "cpu_seconds": max(1, math.ceil(self.timeout_seconds)),
                           "max_output_bytes": self.max_output_bytes}
                command = ["/usr/bin/sandbox-exec", "-f", str(profile), status["interpreter"], "-I", "-S", "-B",
                           str(Path(__file__).with_name("_candidate_main.py").resolve())]
                result, error = self._execute(command, payload, output)
                return finish(result, error)
        except (OSError, ValueError, SandboxUnavailable) as exc:
            return finish(error={"code": "BLOCKED", "message": str(exc)[:600]})
