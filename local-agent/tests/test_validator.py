"""Local tests for trusted source snapshots and meaningful output mistakes."""

from contextlib import closing
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from healthcare_agent.contract import CONTRACT_VERSION, contract_document
from healthcare_agent.validator import compare_output, evaluate_cases, make_baseline


MOCK_ROOT = Path(__file__).resolve().parents[2] / "mock-systems"


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.csv_path = self.root / "observations-csv/observations.csv"
        self.db_path = self.root / "appointments-db/appointments.sqlite3"
        for target in (self.csv_path, self.db_path):
            target.parent.mkdir()
            shutil.copyfile(MOCK_ROOT / target.relative_to(self.root), target)
        self.patients = json.loads((MOCK_ROOT / "registration-api/patients.json").read_text(encoding="utf-8"))
        self.http_status = 200
        self.raw_response = None
        test = self

        class SourceHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/patients":
                    self.send_error(404)
                    return
                body = test.raw_response
                if body is None:
                    body = json.dumps({"items": test.patients, "total": len(test.patients)}).encode("utf-8")
                self.send_response(test.http_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.api = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
        thread = Thread(target=self.api.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def cleanup_api():
            self.api.shutdown()
            self.api.server_close()
            thread.join(timeout=2)

        self.addCleanup(cleanup_api)
        self.url = f"http://127.0.0.1:{self.api.server_port}"

    def baseline(self):
        return make_baseline(self.root, self.url)

    def test_source_join_preserves_synthetic_data_and_absence(self):
        frozen = self.baseline()
        dev = {case["input"]["patient_id"]: case["expected"] for case in frozen["development_cases"]}
        held = {case["input"]["patient_id"] for case in frozen["heldout_cases"]}
        self.assertEqual(frozen["contract_version"], CONTRACT_VERSION)
        self.assertEqual(set(dev), {f"P{i:03d}" for i in range(1, 11)} | {"P999"})
        self.assertEqual(held, {f"P{i:03d}" for i in range(11, 21)})
        self.assertEqual(frozen["metadata"]["source_counts"], {"patients": 20, "appointments": 50, "observations": 80})
        self.assertEqual({p["appointment_id"] for p in dev["P001"]["appointments"]}, {"AP001", "AP019", "AP037"})
        self.assertEqual({p["status"] for p in dev["P001"]["appointments"]}, {"booked", "cancelled", "done"})
        self.assertEqual([p["value"] for p in dev["P001"]["observations"]].count("12.50"), 2)
        self.assertEqual(dev["P002"]["patient"]["display_name"], dev["P003"]["patient"]["display_name"])
        self.assertEqual({a["patient_id"] for a in dev["P002"]["appointments"]}, {"P002"})
        self.assertEqual({a["patient_id"] for a in dev["P003"]["appointments"]}, {"P003"})
        self.assertEqual(dev["P004"]["appointments"], [])
        self.assertEqual(dev["P005"]["observations"], [])
        self.assertTrue(dev["P006"]["found"])
        self.assertEqual((dev["P006"]["appointments"], dev["P006"]["observations"]), ([], []))
        self.assertIsNone(dev["P007"]["patient"]["middle_name"])
        self.assertIs(dev["P008"]["patient"]["active"], False)
        self.assertEqual(dev["P009"]["patient"]["gender"], "unknown")
        self.assertIsNone(dev["P010"]["patient"]["birth_date"])
        self.assertIs(dev["P999"]["found"], False)
        self.assertIsNone(dev["P999"]["patient"])

    def test_validator_rejects_omissions_duplicates_and_wrong_order(self):
        expected = self.baseline()["development_cases"][0]["expected"]
        self.assertEqual(compare_output(deepcopy(expected), expected), [])
        for mutation in ("omit", "duplicate", "cross_join", "reverse"):
            with self.subTest(mutation=mutation):
                actual = deepcopy(expected)
                if mutation == "omit":
                    actual["appointments"].pop()
                elif mutation == "duplicate":
                    actual["observations"][1] = actual["observations"][0]
                elif mutation == "cross_join":
                    actual["appointments"][0]["patient_id"] = "P002"
                else:
                    actual["appointments"].reverse()
                self.assertTrue(compare_output(actual, expected))

    def test_validator_rejects_wrong_types_meanings_and_extra_fields(self):
        cases = self.baseline()["development_cases"]
        expected = next(case["expected"] for case in cases if case["input"]["patient_id"] == "P008")
        for field, wrong in {"active": 0, "gender": "F", "middle_name": "null"}.items():
            with self.subTest(field=field):
                actual = deepcopy(expected)
                actual["patient"][field] = wrong
                differences = compare_output(actual, expected)
                self.assertTrue(any(difference["path"] == f"$.patient.{field}" for difference in differences))
        actual = deepcopy(expected)
        actual["patient"]["uncontracted"] = True
        self.assertEqual(compare_output(actual, expected)[0]["code"], "EXTRA_KEY")
        actual = deepcopy(expected)
        del actual["patient"]["middle_name"]
        self.assertEqual(compare_output(actual, expected)[0]["code"], "MISSING_KEY")

    def test_pending_null_does_not_compare_equal_to_numeric_zero(self):
        cases = self.baseline()["development_cases"]
        expected = next(case["expected"] for case in cases if any(row["status"] == "pending" for row in case["expected"]["observations"]))
        index = next(index for index, row in enumerate(expected["observations"]) if row["status"] == "pending")
        self.assertIsNone(expected["observations"][index]["value"])
        self.assertIsNone(expected["observations"][index]["unit"])
        actual = deepcopy(expected)
        actual["observations"][index]["value"] = 0
        self.assertEqual(compare_output(actual, expected)[0]["code"], "TYPE_MISMATCH")

    def test_complete_failed_partial_and_empty_suite(self):
        cases = self.baseline()["development_cases"][:2]
        outputs = {case["case_id"]: deepcopy(case["expected"]) for case in cases}
        passed = evaluate_cases(outputs, cases)
        self.assertEqual((passed["status"], passed["executed"], passed["passed"]), ("PASSED", 2, 2))
        outputs[cases[0]["case_id"]]["patient"]["gender"] = "incorrect"
        failed = evaluate_cases(outputs, cases)
        self.assertEqual((failed["status"], failed["passed"], failed["failed"]), ("FAILED", 1, 1))
        del outputs[cases[1]["case_id"]]
        blocked = evaluate_cases(outputs, cases)
        self.assertEqual((blocked["status"], blocked["executed"], blocked["failed"], blocked["blocked"]), ("BLOCKED", 1, 1, 1))
        self.assertEqual(evaluate_cases({}, [])["status"], "BLOCKED")

    def test_fingerprint_is_deterministic_and_sensitive_to_live_api_and_db(self):
        first = self.baseline()["source_fingerprint"]
        self.patients.reverse()
        self.assertEqual(first, self.baseline()["source_fingerprint"])
        self.patients[0]["display_name"] += " changed"
        second = self.baseline()["source_fingerprint"]
        self.assertNotEqual(first, second)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("UPDATE appointments SET status_code='C' WHERE appt_id='AP001'")
            connection.commit()
        self.assertNotEqual(second, self.baseline()["source_fingerprint"])

    def test_unavailable_or_inconsistent_api_blocks_baseline(self):
        self.http_status = 503
        with self.assertRaisesRegex(ValueError, "registration-api source is unavailable"):
            self.baseline()
        self.http_status = 200
        self.raw_response = b'{"items": [], "total": 1}'
        with self.assertRaisesRegex(ValueError, "items/total"):
            self.baseline()
        self.raw_response = b'{"items": [], "total": 0, "total": 0}'
        with self.assertRaisesRegex(ValueError, "Duplicate JSON field"):
            self.baseline()
        self.raw_response = None
        self.patients.append(self.patients[0])
        with self.assertRaisesRegex(ValueError, "Duplicate patient primary ID"):
            self.baseline()

    def test_invalid_sql_link_and_missing_source_block_baseline(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("UPDATE appointments SET person_no='P999' WHERE appt_id='AP001'")
            connection.commit()
        with self.assertRaisesRegex(ValueError, "unregistered patient"):
            self.baseline()
        self.db_path.unlink()
        with self.assertRaisesRegex(ValueError, "source file is missing"):
            self.baseline()

    def test_invalid_csv_cannot_be_silently_truncated(self):
        original = self.csv_path.read_text(encoding="utf-8")
        mutations = (
            (original + original.splitlines()[1] + "\n", "Duplicate observation primary ID"),
            (original.replace("12.50", "NaN", 1), "must be finite"),
            (original.replace("obs_id,PatientID", "obs_id,obs_id", 1), "header"),
        )
        for content, error in mutations:
            with self.subTest(error=error):
                self.csv_path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, error):
                    self.baseline()

    def test_public_contract_has_no_evaluator_data(self):
        document = contract_document()
        self.assertEqual(document["contract_version"], CONTRACT_VERSION)
        self.assertEqual(document["entrypoint"]["callable"], "integrate")
        self.assertEqual(set(document["sources_schema"]["required"]), {"api_base_url", "database_path", "csv_path"})
        encoded = json.dumps(document)
        for private in ("heldout_cases", "development_cases", "source_fingerprint", "AP001", "OB001"):
            self.assertNotIn(private, encoded)


if __name__ == "__main__":
    unittest.main()
