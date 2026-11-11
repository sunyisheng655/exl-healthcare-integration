"""Exercise the real sandbox and sources with scripted, explicitly offline model replies."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from healthcare_agent.artifacts import approve, query
from healthcare_agent.controller import run_job
from healthcare_agent.local_sources import local_api
from healthcare_agent.runner import CandidateRunner

HERE = Path(__file__).resolve().parent
MOCK_ROOT = HERE.parents[1] / "mock-systems"
SKILL = next(p / ".agents/skills/agentic-healthcare-integration/SKILL.md" for p in HERE.parents
             if (p / ".agents/skills/agentic-healthcare-integration/SKILL.md").is_file())
REFERENCE = (HERE / "fixtures/reference_adapter.py").read_text(encoding="utf-8")
BAD = REFERENCE.replace('    summary["observations"].sort(', '    summary["appointments"] = summary["appointments"][:1]\n    summary["observations"].sort(')
HOLDOUT_BAD = REFERENCE.replace('    summary["observations"].sort(', '    if patient_id == "P020":\n        summary["appointments"] = []\n    summary["observations"].sort(')


class ScriptedModel:
    def __init__(self, *candidates):
        self.candidates = iter(candidates)
        self.requests = []

    def complete(self, messages, tools, timeout_seconds):
        self.requests.append(deepcopy(messages))
        if len(self.requests) == 1:
            calls = [
                {"name": "inspect_source", "arguments": json.dumps({"source_id": source})}
                for source in ("registration-api", "appointments-db", "observations-csv")
            ]
        else:
            calls = [{"name": "submit_candidate", "arguments": json.dumps(
                {"code": next(self.candidates), "plan": "Offline test fixture, not model generated."})}]
        return {"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"offline-{len(self.requests)}-{i}", "type": "function", "function": function}
            for i, function in enumerate(calls)]}, "model": "offline-test-fixture", "usage": {}}


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CandidateRunner(MOCK_ROOT).doctor()["available"]:
            raise unittest.SkipTest("Requires the real macOS candidate sandbox")

    def test_failure_repair_holdout_and_approved_query(self):
        with tempfile.TemporaryDirectory() as temp, local_api(MOCK_ROOT) as url:
            job = Path(temp) / "repair"
            runner = CandidateRunner(MOCK_ROOT, url)
            client = ScriptedModel(BAD, REFERENCE)
            report = run_job(client, runner, job, SKILL, allow_auto_repair=True)
            self.assertEqual(report["status"], "PASSED", report.get("reason"))
            self.assertEqual([a["development"]["status"] for a in report["attempts"]], ["FAILED", "PASSED"])
            self.assertEqual(report["attempts"][-1]["development"]["passed"], 11)
            self.assertEqual(report["heldout"]["passed"], 10)
            self.assertIn("patient-P001", client.requests[-1][-1]["content"])
            self.assertNotIn("patient-P020", json.dumps(client.requests))
            self.assertNotIn("cases", report["heldout"])
            final = report["attempts"][-1]
            with self.assertRaises(FileNotFoundError):
                query(job, runner, "P001")
            with self.assertRaisesRegex(ValueError, "exact SHA"):
                approve(job, runner, "0" * 64)
            approve(job, runner, final["code_sha256"])
            summary = query(job, runner, "P001")["result"]
            self.assertEqual((len(summary["appointments"]), len(summary["observations"])), (3, 5))
            self.assertEqual(len(client.requests), 3)
            with (job / final["candidate"]).open("a", encoding="utf-8") as out:
                out.write("\n# changed\n")
            with self.assertRaisesRegex(ValueError, "changed after validation"):
                query(job, runner, "P001")

    def test_repair_requires_explicit_request(self):
        with tempfile.TemporaryDirectory() as temp, local_api(MOCK_ROOT) as url:
            job = Path(temp) / "no-repair"
            client = ScriptedModel(BAD)
            runner = CandidateRunner(MOCK_ROOT, url)
            report = run_job(client, runner, job, SKILL)
            self.assertEqual(report["status"], "AWAITING_REPAIR_AUTHORIZATION")
            self.assertEqual(report["heldout_status"], "NOT_PERFORMED")
            self.assertEqual(len(client.requests), 2)
            with self.assertRaisesRegex(ValueError, "passing"):
                approve(job, runner, report["attempts"][0]["code_sha256"])

    def test_hidden_failure_is_not_fed_to_model(self):
        with tempfile.TemporaryDirectory() as temp, local_api(MOCK_ROOT) as url:
            job = Path(temp) / "holdout"
            client = ScriptedModel(HOLDOUT_BAD)
            runner = CandidateRunner(MOCK_ROOT, url)
            report = run_job(client, runner, job, SKILL, allow_auto_repair=True)
            self.assertEqual(report["attempts"][0]["development"]["status"], "PASSED")
            self.assertEqual(report["heldout_status"], "FAILED")
            self.assertEqual(report["status"], "FAILED")
            self.assertEqual(len(client.requests), 2)
            self.assertNotIn("patient-P020", json.dumps(client.requests))
