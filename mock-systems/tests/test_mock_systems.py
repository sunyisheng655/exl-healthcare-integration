"""Independent checks for the three synthetic sources; Python stdlib only.

Run with: python3 -m unittest discover -s tests -v
All write and HTTP checks use temporary copies, never the checked-in sources.
"""

import csv
import http.client
import json
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


ROOT = Path(__file__).resolve().parents[1]
PATIENTS = Path("registration-api/patients.json")
DATABASE = Path("appointments-db/appointments.sqlite3")
SCHEMA = Path("appointments-db/schema.sql")
SEED_SQL = Path("appointments-db/seed.sql")
OBSERVATIONS = Path("observations-csv/observations.csv")
TEXT_OUTPUTS = (PATIENTS, SCHEMA, SEED_SQL, OBSERVATIONS)


def read_patients(root):
    return json.loads((root / PATIENTS).read_text(encoding="utf-8"))


def read_observations(root):
    with (root / OBSERVATIONS).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def open_database(root):
    connection = sqlite3.connect((root / DATABASE).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def database_dump(root):
    connection = open_database(root)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()


def file_snapshot(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def generate(output_dir, *extra):
    return subprocess.run(
        [sys.executable, str(ROOT / "generate_data.py"), "--output-dir", str(output_dir), *extra],
        cwd=output_dir.parent,
        text=True,
        capture_output=True,
        timeout=15,
    )


class SourceDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patients = read_patients(ROOT)
        cls.observations = read_observations(ROOT)
        connection = open_database(ROOT)
        try:
            cls.appointments = [dict(row) for row in connection.execute("SELECT * FROM appointments")]
        finally:
            connection.close()
        cls.patient_by_id = {row["patient_id"]: row for row in cls.patients}

    def assert_utc_timestamp(self, value):
        self.assertIsInstance(value, str)
        self.assertTrue(value.endswith("Z"), value)
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_patients_have_unique_expected_ids_and_typed_fields(self):
        self.assertIsInstance(self.patients, list)
        self.assertEqual(len(self.patients), 20)
        self.assertEqual(set(self.patient_by_id), {f"P{number:03d}" for number in range(1, 21)})
        required = {"patient_id", "display_name", "middle_name", "birth_date", "gender_code", "active", "updated_at"}
        for patient in self.patients:
            with self.subTest(patient=patient["patient_id"]):
                self.assertTrue(required <= patient.keys())
                self.assertIsInstance(patient["display_name"], str)
                self.assertTrue(patient["display_name"].strip())
                self.assertTrue(patient["middle_name"] is None or isinstance(patient["middle_name"], str))
                if patient["birth_date"] is not None:
                    date.fromisoformat(patient["birth_date"])
                self.assertIn(patient["gender_code"], {"F", "M", "O", "U"})
                self.assertIs(type(patient["active"]), bool)
                self.assert_utc_timestamp(patient["updated_at"])

    def test_duplicate_names_do_not_replace_patient_identity(self):
        first, second = self.patient_by_id["P002"], self.patient_by_id["P003"]
        self.assertEqual(first["display_name"], "Demo Alex")
        self.assertEqual(second["display_name"], "Demo Alex")
        self.assertNotEqual(first["patient_id"], second["patient_id"])
        # Grouping by display name must retain both stable IDs, not collapse one.
        self.assertEqual(
            {row["patient_id"] for row in self.patients if row["display_name"] == "Demo Alex"},
            {"P002", "P003"},
        )

    def test_nullable_unknown_and_inactive_patient_cases(self):
        self.assertIsNone(self.patient_by_id["P007"]["middle_name"])
        self.assertIs(self.patient_by_id["P008"]["active"], False)
        self.assertEqual(self.patient_by_id["P009"]["gender_code"], "U")
        self.assertIsNone(self.patient_by_id["P010"]["birth_date"])

    def test_appointments_have_unique_ids_valid_codes_and_no_orphans(self):
        self.assertEqual(len(self.appointments), 50)
        ids = [row["appt_id"] for row in self.appointments]
        self.assertEqual(len(set(ids)), 50)
        self.assertEqual(set(ids), {f"AP{number:03d}" for number in range(1, 51)})
        for appointment in self.appointments:
            with self.subTest(appointment=appointment["appt_id"]):
                self.assertIn(appointment["person_no"], self.patient_by_id)
                self.assertIn(appointment["department_code"], {"GP", "RAD", "LAB"})
                self.assertIn(appointment["visit_type"], {1, 2})
                self.assertIn(appointment["status_code"], {"B", "C", "D"})
                self.assert_utc_timestamp(appointment["scheduled_at"])
                self.assert_utc_timestamp(appointment["updated_at"])

    def test_observations_have_unique_ids_valid_values_and_no_orphans(self):
        self.assertEqual(len(self.observations), 80)
        ids = [row["obs_id"] for row in self.observations]
        self.assertEqual(len(set(ids)), 80)
        self.assertEqual(set(ids), {f"OB{number:03d}" for number in range(1, 81)})
        pending_blank = 0
        for observation in self.observations:
            with self.subTest(observation=observation["obs_id"]):
                self.assertIn(observation["PatientID"], self.patient_by_id)
                self.assertIn(observation["test_code"], {"DEMO_A", "DEMO_B", "DEMO_C"})
                self.assertIn(observation["status"], {"final", "pending"})
                self.assert_utc_timestamp(observation["observed_at"])
                if observation["result_value"] == "":
                    self.assertEqual(observation["status"], "pending")
                    self.assertEqual(observation["result_unit"], "")
                    pending_blank += 1
                else:
                    try:
                        value = Decimal(observation["result_value"])
                    except InvalidOperation:
                        self.fail(f"Invalid decimal for {observation['obs_id']}")
                    self.assertTrue(value.is_finite())
                    self.assertEqual(observation["result_unit"], "arb.units")
        self.assertGreater(pending_blank, 0, "The fixture needs a missing-value case to exercise parsing")

    def test_one_to_many_and_missing_collection_cases(self):
        appointment_counts = Counter(row["person_no"] for row in self.appointments)
        observation_counts = Counter(row["PatientID"] for row in self.observations)
        self.assertGreaterEqual(appointment_counts["P001"], 2)
        self.assertGreaterEqual(observation_counts["P001"], 2)
        self.assertEqual(appointment_counts["P004"], 0)
        self.assertGreaterEqual(observation_counts["P004"], 1)
        self.assertGreaterEqual(appointment_counts["P005"], 1)
        self.assertEqual(observation_counts["P005"], 0)
        self.assertEqual(appointment_counts["P006"], 0)
        self.assertEqual(observation_counts["P006"], 0)

    def test_sql_sources_rebuild_the_same_rows(self):
        rebuilt = sqlite3.connect(":memory:")
        rebuilt.row_factory = sqlite3.Row
        try:
            rebuilt.executescript((ROOT / SCHEMA).read_text(encoding="utf-8"))
            rebuilt.executescript((ROOT / SEED_SQL).read_text(encoding="utf-8"))
            actual = [dict(row) for row in rebuilt.execute("SELECT * FROM appointments ORDER BY appt_id")]
            self.assertEqual(actual, sorted(self.appointments, key=lambda row: row["appt_id"]))
        finally:
            rebuilt.close()

    def test_read_only_sql_connection_rejects_writes_without_changing_source(self):
        # Test the documented reader mode using a copy, even if a regression makes it writable.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / DATABASE).parent.mkdir(parents=True)
            shutil.copy2(ROOT / DATABASE, root / DATABASE)
            before = (root / DATABASE).read_bytes()
            connection = open_database(root)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("UPDATE appointments SET person_no = 'P999' WHERE appt_id = 'AP001'")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 50)
            finally:
                connection.close()
            self.assertEqual((root / DATABASE).read_bytes(), before)


class GeneratorTests(unittest.TestCase):
    def assert_generated(self, result):
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_default_seed_and_explicit_seed_are_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "first", Path(directory) / "second"
            self.assert_generated(generate(first))
            self.assert_generated(generate(second, "--seed", "20261111"))
            for path in TEXT_OUTPUTS:
                with self.subTest(path=str(path)):
                    self.assertEqual((first / path).read_bytes(), (second / path).read_bytes())
                    self.assertEqual((first / path).read_bytes(), (ROOT / path).read_bytes())
            self.assertEqual(database_dump(first), database_dump(second))
            self.assertEqual(database_dump(first), database_dump(ROOT))

    def test_existing_complete_output_is_preserved_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "generated"
            self.assert_generated(generate(root))
            before = file_snapshot(root)
            result = generate(root)
            self.assertNotEqual(result.returncode, 0, "Existing generated files must require --overwrite")
            self.assertEqual(file_snapshot(root), before)

    def test_partial_collision_is_detected_before_any_output_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "partial"
            (root / OBSERVATIONS).parent.mkdir(parents=True)
            (root / OBSERVATIONS).write_bytes(b"keep this user content\n")
            before = file_snapshot(root)
            result = generate(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(file_snapshot(root), before, "A late collision must not leave partial replacement files")

    def test_explicit_overwrite_restores_generated_data_and_preserves_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "generated"
            self.assert_generated(generate(root))
            (root / PATIENTS).write_bytes(b"temporary altered generated content\n")
            note = root / "my-existing-note.txt"
            note.write_bytes(b"not a generated artifact\n")
            self.assert_generated(generate(root, "--overwrite"))
            self.assertEqual((root / PATIENTS).read_bytes(), (ROOT / PATIENTS).read_bytes())
            self.assertEqual(note.read_bytes(), b"not a generated artifact\n")


class RegistrationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.data_file = Path(cls.temp.name) / "patients.json"
        shutil.copy2(ROOT / PATIENTS, cls.data_file)
        shutil.copy2(ROOT / "registration-api/openapi.json", cls.data_file.parent / "openapi.json")
        cls.original_bytes = cls.data_file.read_bytes()
        cls.expected_patients = json.loads(cls.original_bytes)
        cls.process = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "serve_registration_api.py"), "--port", "0", "--data-file", str(cls.data_file)],
            cwd=cls.temp.name,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        cls.addClassCleanup(cls.stop_server)
        ready, _, _ = select.select([cls.process.stdout], [], [], 8)
        if not ready:
            raise RuntimeError("API did not emit its startup JSON within 8 seconds")
        line = cls.process.stdout.readline()
        if not line:
            raise RuntimeError(f"API exited before startup: {cls.process.stderr.read()}")
        startup = json.loads(line)
        cls.host, cls.port = startup["host"], startup["port"]
        if cls.host != "127.0.0.1" or not isinstance(cls.port, int) or cls.port <= 0:
            raise AssertionError(f"Default bind must be loopback with an allocated port: {startup}")

    @classmethod
    def stop_server(cls):
        if cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=5)
        cls.process.stdout.close()
        cls.process.stderr.close()

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def get_json(self, path):
        status, headers, body = self.request("GET", path)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        return status, json.loads(body)

    def test_health_marks_source_synthetic(self):
        status, health = self.get_json("/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["system"], "registration-api")
        self.assertIs(health["synthetic"], True)

    def test_patient_list_returns_all_source_records_without_name_deduplication(self):
        status, payload = self.get_json("/patients")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 20)
        self.assertEqual(payload["items"], self.expected_patients)

    def test_patient_detail_is_raw_record_and_unknown_id_is_explicit(self):
        status, patient = self.get_json("/patients/P001")
        self.assertEqual(status, 200)
        self.assertEqual(patient, next(row for row in self.expected_patients if row["patient_id"] == "P001"))
        status, error = self.get_json("/patients/P999")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"], "PATIENT_NOT_FOUND")
        self.assertEqual(error["patient_id"], "P999")

    def test_openapi_exposes_the_read_endpoints(self):
        status, spec = self.get_json("/openapi.json")
        self.assertEqual(status, 200)
        self.assertTrue(spec["openapi"].startswith("3."))
        self.assertIn("get", spec["paths"]["/health"])
        self.assertIn("get", spec["paths"]["/patients"])
        self.assertTrue(any(path.startswith("/patients/{") for path in spec["paths"]))

    def test_unknown_routes_are_json_404(self):
        status, error = self.get_json("/not-a-route")
        self.assertEqual(status, 404)
        self.assertIn("error", error)

    def test_writes_are_rejected_and_data_remains_unchanged(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, headers, body = self.request(method, "/patients/P001", '{"display_name":"CHANGED"}')
                self.assertEqual(status, 405)
                self.assertEqual({item.strip() for item in headers.get("Allow", "").split(",")}, {"GET", "HEAD"})
                self.assertIn("error", json.loads(body))
        self.assertEqual(self.data_file.read_bytes(), self.original_bytes)
        status, payload = self.get_json("/patients")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], self.expected_patients)

    def test_head_has_get_status_without_a_response_body(self):
        for path, expected_status in (("/health", 200), ("/patients", 200), ("/patients/P001", 200), ("/patients/P999", 404)):
            with self.subTest(path=path):
                status, _, body = self.request("HEAD", path)
                self.assertEqual(status, expected_status)
                self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
