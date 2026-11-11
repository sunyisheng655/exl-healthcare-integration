"""Exercise the loopback HTTP boundary and a real offline web workflow."""

from contextlib import ExitStack
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from healthcare_agent.runner import CandidateRunner
from healthcare_agent.web import create_server
from test_workflow import BAD, HOLDOUT_BAD, MOCK_ROOT, REFERENCE, SKILL, ScriptedModel


class GatedModel(ScriptedModel):
    def __init__(self, *candidates):
        super().__init__(*candidates)
        self.started = threading.Event()
        self.unblock = threading.Event()

    def complete(self, messages, tools, timeout_seconds):
        if not self.requests:
            self.started.set()
            if not self.unblock.wait(timeout=10):
                raise RuntimeError("The HTTP test did not release its offline model")
        reply = super().complete(messages, tools, timeout_seconds)
        reply["model"] = "controlled-fixture"
        return reply


class WebServerTestCase(unittest.TestCase):
    def setUp(self):
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.runtime = Path(resources.enter_context(tempfile.TemporaryDirectory()))
        self.factory_calls = 0
        self.client = None

        def factory(*_args, **_kwargs):
            self.factory_calls += 1
            if self.client is None:
                raise AssertionError("This test must not construct any model client")
            return self.client

        self.server = create_server(
            port=0, mock_root=MOCK_ROOT, runtime=self.runtime, skill_path=SKILL,
            model="offline-test-fixture", client_factory=factory,
        )
        self.worker = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
        )
        self.worker.start()
        resources.callback(self.worker.join, 5)
        resources.callback(self.server.server_close)
        resources.callback(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.host = f"127.0.0.1:{self.port}"

    def request(self, method, path, payload=None, *, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            body = json.dumps(payload) if payload is not None else None
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def post(self, path, payload, *, headers=None):
        return self.request("POST", path, payload, headers={
            "Content-Type": "application/json", "X-Local-Agent": "1",
            "Origin": f"http://{self.host}", **(headers or {}),
        })

    def get_json(self, path):
        status, headers, body = self.request("GET", path)
        self.assertIn("application/json", headers.get("Content-Type", ""), (status, body))
        return status, json.loads(body)

    def assert_rejected(self, response):
        status, headers, body = response
        self.assertGreaterEqual(status, 400, body)
        self.assertLess(status, 500, body)
        self.assertIn("application/json", headers.get("Content-Type", ""), body)
        self.assertTrue(json.loads(body).get("error"), body)

    def create_job(self, *, repair=False):
        status, _, body = self.post("/api/jobs", {
            "goal": "Build the registered synthetic patient-summary adapter",
            "allow_auto_repair": repair,
        })
        self.assertEqual(status, 202, body)
        job_id = json.loads(body)["job_id"]
        self.assertRegex(job_id, r"^[0-9a-f]{16}$")
        return f"/api/jobs/{job_id}"

    def await_report(self, path):
        deadline = time.monotonic() + 35
        detail = None
        while time.monotonic() < deadline:
            status, detail = self.get_json(path)
            self.assertEqual(status, 200, detail)
            if detail.get("report") is not None:
                return detail
            if detail.get("error"):
                self.fail(f"Background job failed: {detail['error']}")
            time.sleep(0.1)
        self.fail(f"Background job produced no report: {detail}")


class WebAccessTests(WebServerTestCase):
    def test_only_loopback_host_and_intentional_same_origin_mutations(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for host in ("unrelated.invalid", f"127.0.0.1:{self.port + 1}"):
            with self.subTest(host=host):
                self.assert_rejected(self.request("GET", "/api/config", headers={"Host": host}))
        status, _, body = self.request("GET", "/api/config", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["model"], "offline-test-fixture")
        for headers in (
            {"X-Local-Agent": "0"},
            {"Origin": "https://unrelated.invalid"},
            {"Origin": f"http://127.0.0.1:{self.port + 1}"},
            {"Origin": f"http://localhost:{self.port}"},
            {"Content-Type": "text/plain"},
        ):
            with self.subTest(headers=headers):
                self.assert_rejected(self.post("/api/jobs", {"goal": "New job"}, headers=headers))
        self.assert_rejected(self.request("POST", "/api/jobs", {"goal": "New job"}, headers={
            "Content-Type": "application/json",
        }))
        self.assert_rejected(self.post("/api/jobs", {"goal": "New job", "allow_auto_repair": "yes"}))
        status, history = self.get_json("/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(history["jobs"], [])
        self.assertEqual(self.factory_calls, 0)

    def test_static_assets_and_code_routes_do_not_serve_secrets_or_arbitrary_files(self):
        marker = "test-private-marker-never-sent-to-browser"
        (self.runtime / ".env").write_text(marker, encoding="utf-8")
        fake_job = self.runtime / ("b" * 16)
        fake_job.mkdir()
        source = f"# {marker}\n"
        (fake_job / "candidate-01.py").write_text(source, encoding="utf-8")
        (fake_job / "candidate-02.py").write_text("# exists but is unlisted\n", encoding="utf-8")
        windows_path = "C:/workspace/private-config.json"
        windows_source = f'path = "{windows_path}"\n'
        (fake_job / "candidate-03.py").write_text(windows_source, encoding="utf-8")
        (fake_job / "report.json").write_text(json.dumps({
            "job_id": fake_job.name, "status": "FAILED", "goal": "Fixture job",
            "reason": marker, "attempts": [{
                "candidate": "candidate-01.py", "code_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "development": {"status": "FAILED"},
            }, {
                "candidate": "candidate-03.py", "code_sha256": hashlib.sha256(windows_source.encode()).hexdigest(),
                "development": {"status": "FAILED"},
            }],
        }), encoding="utf-8")
        job_url = f"/api/jobs/{fake_job.name}"
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": marker}):
            for path in ("/", "/app.js", "/styles.css", "/api/config", job_url):
                with self.subTest(path=path):
                    status, _, body = self.request("GET", path)
                    self.assertEqual(status, 200)
                    self.assertNotIn(marker.encode(), body)
            listed = self.request("GET", f"{job_url}/code/candidate-01.py")
            self.assert_rejected(listed)
            self.assertNotIn(marker.encode(), listed[-1])
        private_path = self.request("GET", f"{job_url}/code/candidate-03.py")
        self.assert_rejected(private_path)
        self.assertNotIn(windows_path.encode(), private_path[-1])
        for path in (
            "/.env", f"{job_url}/code/candidate-02.py",
            f"{job_url}/code/report.json", f"{job_url}/code/../.env",
            f"{job_url}/code/%2e%2e%2f.env", "/api/jobs/%2e%2e/code/candidate-01.py",
        ):
            with self.subTest(path=path):
                reply = self.request("GET", path)
                self.assert_rejected(reply)
                self.assertNotIn(marker.encode(), reply[-1])
        self.assertEqual(self.factory_calls, 0)


class WebWorkflowTests(WebServerTestCase):
    @classmethod
    def setUpClass(cls):
        if not CandidateRunner(MOCK_ROOT).doctor()["available"]:
            raise unittest.SkipTest("Requires the real macOS candidate sandbox")

    def test_asynchronous_repair_report_approval_query_and_tamper_rejection(self):
        candidate = REFERENCE + "\n# Example mock API: http://127.0.0.1:8765\n"
        client = self.client = GatedModel(BAD, candidate)
        try:
            job_url = self.create_job(repair=True)
            self.assertTrue(client.started.wait(timeout=4))
            status, pending = self.get_json(job_url)
            self.assertEqual(status, 200)
            self.assertIsNone(pending["report"])
        finally:
            client.unblock.set()

        detail = self.await_report(job_url)
        report = detail["report"]
        self.assertEqual(report["status"], "PASSED", detail)
        self.assertEqual([a["development"]["status"] for a in report["attempts"]], ["FAILED", "PASSED"])
        self.assertEqual(report["heldout_status"], "PASSED")
        self.assertNotIn("cases", report["heldout"])
        self.assertTrue(any("repair" in event.lower() for event in detail["events"]), detail["events"])
        final = report["attempts"][-1]
        self.assert_rejected(self.post(f"{job_url}/query", {"patient_id": "P001"}))
        self.assert_rejected(self.post(f"{job_url}/approve", {"code_sha256": report["attempts"][0]["code_sha256"]}))

        status, version = self.get_json(f"{job_url}/code/{final['candidate']}")
        self.assertEqual(status, 200, version)
        self.assertEqual(version["code"], candidate)
        self.assertEqual(version["sha256"], hashlib.sha256(version["code"].encode()).hexdigest())
        self.assertEqual(version["sha256"], final["code_sha256"])
        status, _, body = self.post(f"{job_url}/approve", {"code_sha256": version["sha256"]})
        self.assertEqual(status, 200, body)
        self.assertIs(json.loads(body)["approved"], True)

        calls_before = len(client.requests)
        status, _, body = self.post(f"{job_url}/query", {"patient_id": "P001"})
        self.assertEqual(status, 200, body)
        query = json.loads(body)
        self.assertEqual(query["code_sha256"], final["code_sha256"])
        self.assertEqual((len(query["result"]["appointments"]), len(query["result"]["observations"])), (3, 5))
        self.assertEqual(len(client.requests), calls_before)
        self.assertEqual(self.factory_calls, 1)
        status, history = self.get_json("/api/jobs")
        self.assertEqual(status, 200)
        row = next(item for item in history["jobs"] if item["job_id"] == report["job_id"])
        self.assertEqual(row["status"], "PASSED")
        self.assertEqual(row["kind"], "controlled_repair")
        self.assertIs(row["approved"], True)

        candidate_path = self.runtime / report["job_id"] / final["candidate"]
        with candidate_path.open("a", encoding="utf-8") as handle:
            handle.write("\n# changed after approval\n")
        self.assert_rejected(self.request("GET", f"{job_url}/code/{final['candidate']}"))
        self.assert_rejected(self.post(f"{job_url}/query", {"patient_id": "P001"}))
        status, detail = self.get_json(job_url)
        self.assertEqual(status, 200)
        self.assertIs(detail["approved"], False)
        self.assertEqual(len(client.requests), calls_before)

    def test_failed_holdout_cannot_be_approved(self):
        self.client = ScriptedModel(HOLDOUT_BAD)
        job_url = self.create_job()
        detail = self.await_report(job_url)
        report = detail["report"]
        self.assertEqual(report["attempts"][0]["development"]["status"], "PASSED")
        self.assertEqual(report["status"], "FAILED")
        self.assertEqual(report["heldout_status"], "FAILED")
        self.assert_rejected(self.post(f"{job_url}/approve", {
            "code_sha256": report["attempts"][0]["code_sha256"],
        }))
        self.assert_rejected(self.post(f"{job_url}/query", {"patient_id": "P001"}))


if __name__ == "__main__":
    unittest.main()
