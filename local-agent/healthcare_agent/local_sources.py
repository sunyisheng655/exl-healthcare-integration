"""Start only this project's mock API for the duration of a local command."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile


@contextmanager
def local_api(mock_root: Path, existing_url: str | None = None):
    if existing_url is not None:
        yield existing_url
        return
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(
            [sys.executable, "-B", str(mock_root / "serve_registration_api.py"),
             "--host", "127.0.0.1", "--port", "0"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr,
            env={"PATH": os.defpath, "LANG": "en_US.UTF-8"}, cwd=mock_root,
        )
        try:
            if not select.select([proc.stdout], [], [], 5)[0]:
                raise ValueError("The mock registration API did not start in time.")
            line = proc.stdout.readline(4096)
            if not line:
                stderr.seek(0)
                raise ValueError("The mock registration API failed to start: " + stderr.read(1000).decode(errors="replace"))
            startup = json.loads(line)
            port = startup.get("port")
            if startup.get("host") != "127.0.0.1" or type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("The mock API returned invalid startup information.")
            yield f"http://127.0.0.1:{port}"
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            proc.stdout.close()
