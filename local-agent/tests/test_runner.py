"""Integration tests for the actual macOS policy, not a mocked subprocess."""

import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

from healthcare_agent.runner import CandidateRunner


class MockApi(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/patients/P001":
            self.send_error(404)
            return
        body = json.dumps({"patient_id": "P001", "display_name": "Sandbox test patient"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name).resolve()
        cls.database = cls.root / "appointments-db/appointments.sqlite3"
        cls.database.parent.mkdir()
        with sqlite3.connect(cls.database) as connection:
            connection.execute("CREATE TABLE appointments (appt_id TEXT, person_no TEXT)")
            connection.executemany("INSERT INTO appointments VALUES (?, ?)", [("AP001", "P001"), ("AP002", "P002")])
        connection.close()
        cls.csv = cls.root / "observations-csv/observations.csv"
        cls.csv.parent.mkdir()
        with cls.csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerows([["obs_id", "PatientID"], ["OB001", "P001"], ["OB002", "P002"]])
        cls.private = cls.root / "evaluator-oracle.json"
        cls.private.write_text('{"private_marker":"sandbox sibling"}', encoding="utf-8")
        cls.api = cls.start_server()
        cls.other_service = cls.start_server()
        cls.api_url = f"http://127.0.0.1:{cls.api.server_port}"
        cls.runner = CandidateRunner(cls.root, cls.api_url)
        status = cls.runner.doctor()
        if not status["available"]:
            if os.uname().sysname == "Darwin":
                raise RuntimeError("macOS sandbox must work for integration tests: " + status["reason"])
            raise unittest.SkipTest(status["reason"])

    @classmethod
    def start_server(cls):
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockApi)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        worker.start()
        cls.addClassCleanup(worker.join, 5)
        cls.addClassCleanup(server.server_close)
        cls.addClassCleanup(server.shutdown)
        return server

    def run_candidate(self, code, runner=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adapter.py"
            path.write_text(textwrap.dedent(code), encoding="utf-8")
            return (runner or self.runner).run(path, "P001")

    def test_reads_all_three_sources_and_prints_do_not_corrupt_protocol(self):
        reply = self.run_candidate("""
            import csv
            import json
            import sqlite3
            from pathlib import Path
            from urllib.request import urlopen

            def integrate(patient_id, sources):
                print("Candidate diagnostic")
                with urlopen(sources["api_base_url"] + "/patients/" + patient_id) as response:
                    patient = json.load(response)
                connection = sqlite3.connect(Path(sources["database_path"]).as_uri() + "?mode=ro", uri=True)
                try:
                    appts = connection.execute("SELECT appt_id FROM appointments WHERE person_no = ?", (patient_id,)).fetchall()
                finally:
                    connection.close()
                with open(sources["csv_path"], newline="", encoding="utf-8") as data:
                    obs = [row["obs_id"] for row in csv.DictReader(data) if row["PatientID"] == patient_id]
                return {"patient": patient, "appointments": appts, "observations": obs}
        """)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"], {
            "patient": {"patient_id": "P001", "display_name": "Sandbox test patient"},
            "appointments": [["AP001"]], "observations": ["OB001"],
        })

    def test_private_sibling_and_original_candidate_cannot_be_written(self):
        reply = self.run_candidate(f"""
            from pathlib import Path
            def integrate(patient_id, sources):
                results = {{}}
                for key, path, mode in (
                    ("oracle_read", {str(self.private)!r}, "r"),
                    ("oracle_write", {str(self.private)!r}, "w"),
                    ("candidate_write", __file__, "w"),
                ):
                    try:
                        with open(path, mode):
                            pass
                    except PermissionError:
                        results[key] = "denied"
                    else:
                        results[key] = "allowed"
                return results
        """)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"], {"oracle_read": "denied", "oracle_write": "denied", "candidate_write": "denied"})
        self.assertEqual(self.private.read_text(encoding="utf-8"), '{"private_marker":"sandbox sibling"}')

    def test_no_provider_environment_and_no_source_mutation(self):
        expected = {p: p.read_bytes() for p in (self.database, self.csv)}
        with patch.dict(os.environ, {"EXL_TEST_SECRET": "private-parent-marker"}):
            reply = self.run_candidate("""
                import os
                import sqlite3
                def integrate(patient_id, sources):
                    result = {"secret": os.environ.get("EXL_TEST_SECRET")}
                    try:
                        with open(sources["csv_path"], "a", encoding="utf-8") as f:
                            f.write("invalid")
                    except PermissionError:
                        result["csv_write"] = "denied"
                    try:
                        connection = sqlite3.connect(sources["database_path"])
                        try:
                            connection.execute("DELETE FROM appointments")
                            connection.commit()
                        finally:
                            connection.close()
                    except sqlite3.OperationalError:
                        result["db_write"] = "denied"
                    return result
            """)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"], {"secret": None, "csv_write": "denied", "db_write": "denied"})
        self.assertEqual({p: p.read_bytes() for p in expected}, expected)

    def test_network_only_allows_registered_local_api_port(self):
        reply = self.run_candidate(f"""
            import socket
            from urllib.request import urlopen
            def integrate(patient_id, sources):
                with urlopen(sources["api_base_url"] + "/patients/P001", timeout=1) as response:
                    allowed = response.status
                try:
                    socket.create_connection(("127.0.0.1", {self.other_service.server_port}), timeout=1).close()
                except OSError as error:
                    blocked = error.errno
                else:
                    blocked = None
                return {{"allowed": allowed, "blocked": blocked}}
        """)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["allowed"], 200)
        self.assertIn(reply["result"]["blocked"], {1, 13}, reply)

    def test_subprocess_creation_is_denied(self):
        reply = self.run_candidate("""
            import subprocess
            import sys
            def integrate(patient_id, sources):
                try:
                    subprocess.run([sys.executable, "-I", "-c", "print('child')"], check=True)
                except OSError:
                    return {"subprocess_denied": True}
                return {"subprocess_denied": False}
        """)
        self.assertTrue(reply["ok"], reply)
        self.assertIs(reply["result"]["subprocess_denied"], True)

    def test_wall_timeout_even_after_child_closes_its_pipes(self):
        runner = CandidateRunner(self.root, self.api_url, timeout_seconds=0.35)
        self.assertTrue(runner.doctor()["available"])
        started = time.monotonic()
        reply = self.run_candidate("""
            import os
            import time
            def integrate(patient_id, sources):
                os.close(1)
                os.close(2)
                time.sleep(10)
                return {}
        """, runner)
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error"]["code"], "TIMEOUT")
        self.assertLess(time.monotonic() - started, 3)

    def test_excessive_stdout_stderr_are_bounded(self):
        runner = CandidateRunner(self.root, self.api_url, max_output_bytes=4096)
        reply = self.run_candidate("""
            import os
            def integrate(patient_id, sources):
                os.write(1, b"x" * 32768)
                return {"unexpected": True}
        """, runner)
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error"]["code"], "OUTPUT_LIMIT")

    def test_non_dict_and_forged_envelope_are_rejected(self):
        not_dict = self.run_candidate("""
            def integrate(patient_id, sources):
                return []
        """)
        self.assertEqual(not_dict["error"]["code"], "INVALID_RESULT")
        forged = self.run_candidate("""
            import os
            def integrate(patient_id, sources):
                os.write(1, b'{"status":"PASSED"}')
                os._exit(0)
        """)
        self.assertEqual(forged["error"]["code"], "INVALID_RESULT")

    def test_invalid_registered_url_and_unavailable_platform_block_execution(self):
        invalid = CandidateRunner(self.root, "http://192.0.2.1:8765")
        self.assertFalse(invalid.doctor()["available"])
        with patch("healthcare_agent.runner.platform.system", return_value="Linux"):
            runner = CandidateRunner(self.root, self.api_url)
            reply = self.run_candidate("raise RuntimeError('must not execute')", runner)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
