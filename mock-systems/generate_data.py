#!/usr/bin/env python3
"""Generate three deterministic synthetic source systems using the standard library."""

from __future__ import annotations

import argparse
from contextlib import closing
import csv
import io
import json
import os
from pathlib import Path
import random
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone


DEFAULT_SEED = 20261111
HERE = Path(__file__).resolve().parent
PATIENT_FIELDS = (
    "patient_id", "display_name", "middle_name", "birth_date", "gender_code",
    "active", "updated_at",
)
APPOINTMENT_FIELDS = (
    "appt_id", "person_no", "scheduled_at", "department_code", "visit_type",
    "status_code", "updated_at",
)
OBSERVATION_FIELDS = (
    "obs_id", "PatientID", "test_code", "result_value", "result_unit",
    "observed_at", "status",
)

SCHEMA_SQL = """-- Synthetic appointments source; no real healthcare records.
-- person_no references the external registration API's patient_id by contract.
-- It is intentionally not a foreign key to a table in this SQLite database.
PRAGMA user_version = 1;
CREATE TABLE appointments (
    appt_id TEXT PRIMARY KEY NOT NULL,
    person_no TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    department_code TEXT NOT NULL CHECK (department_code IN ('GP', 'RAD', 'LAB')),
    visit_type INTEGER NOT NULL CHECK (visit_type IN (1, 2)),
    status_code TEXT NOT NULL CHECK (status_code IN ('B', 'C', 'D')),
    updated_at TEXT NOT NULL,
    CHECK (appt_id GLOB 'AP[0-9][0-9][0-9]'),
    CHECK (person_no GLOB 'P[0-9][0-9][0-9]'),
    CHECK (scheduled_at GLOB '????-??-??T??:??:??Z'),
    CHECK (updated_at GLOB '????-??-??T??:??:??Z')
);
CREATE INDEX idx_appointments_person_no ON appointments(person_no);
"""


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_records(seed: int) -> tuple[list[dict], list[dict], list[dict]]:
    rng = random.Random(seed)
    snapshot = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)
    patients = []
    for number in range(1, 21):
        patients.append({
            "patient_id": f"P{number:03d}",
            "display_name": "Demo Alex" if number in (2, 3) else f"Synthetic Patient {number:03d}",
            "middle_name": None if number % 2 else f"Synthetic-M{number:02d}",
            "birth_date": None if number == 10 else date(
                1950 + (number * 3) % 55, number % 12 + 1, number * 3 % 27 + 1
            ).isoformat(),
            "gender_code": "U" if number == 9 else ("F", "M", "O", "U")[(number - 1) % 4],
            "active": number != 8,
            "updated_at": utc_text(snapshot + timedelta(minutes=number)),
        })

    appointment_patients = [p["patient_id"] for p in patients if p["patient_id"] not in {"P004", "P006"}]
    appointments = []
    for number in range(1, 51):
        round_number = (number - 1) // len(appointment_patients)
        status = ("B", "C", "D")[(number - 1 + round_number) % 3]
        base = datetime(2026, 10, 20, 8, tzinfo=timezone.utc) if status == "D" else datetime(
            2026, 11, 15 if status == "B" else 17, 8, tzinfo=timezone.utc
        )
        appointments.append({
            "appt_id": f"AP{number:03d}",
            "person_no": appointment_patients[(number - 1) % len(appointment_patients)],
            "scheduled_at": utc_text(base + timedelta(hours=number * 3)),
            "department_code": ("GP", "RAD", "LAB")[(number - 1 + round_number) % 3],
            "visit_type": 1 + number % 2,
            "status_code": status,
            "updated_at": utc_text(snapshot),
        })

    observation_patients = [p["patient_id"] for p in patients if p["patient_id"] not in {"P005", "P006"}]
    observations = []
    observation_start = datetime(2026, 10, 25, 8, tzinfo=timezone.utc)
    for number in range(1, 81):
        pending = number % 11 == 0
        value = "" if pending else f"{rng.randint(100, 9000) / 100:.2f}"
        if number in (1, 19):
            value = "12.50"
        observations.append({
            "obs_id": f"OB{number:03d}",
            "PatientID": observation_patients[(number - 1) % len(observation_patients)],
            "test_code": ("DEMO_A", "DEMO_B", "DEMO_C")[(number - 1 + (number - 1) // len(observation_patients)) % 3],
            "result_value": value,
            "result_unit": "" if pending else "arb.units",
            "observed_at": utc_text(observation_start + timedelta(hours=number * 3)),
            "status": "pending" if pending else "final",
        })
    return patients, appointments, observations


def validate_records(patients: list[dict], appointments: list[dict], observations: list[dict]) -> None:
    if (len(patients), len(appointments), len(observations)) != (20, 50, 80):
        raise ValueError("Expected exactly 20 patients, 50 appointments, and 80 observations")
    ids = {p["patient_id"] for p in patients}
    if len(ids) != 20 or len({a["appt_id"] for a in appointments}) != 50 or len({o["obs_id"] for o in observations}) != 80:
        raise ValueError("Source primary IDs must be unique")
    if not all(a["person_no"] in ids for a in appointments) or not all(o["PatientID"] in ids for o in observations):
        raise ValueError("All source references must identify registered synthetic patients")
    for patient in patients:
        if tuple(patient) != PATIENT_FIELDS:
            raise ValueError("Patient field contract mismatch")
    for item in observations:
        if tuple(item) != OBSERVATION_FIELDS:
            raise ValueError("Observation field contract mismatch")
        if item["status"] == "pending" and (item["result_value"] or item["result_unit"]):
            raise ValueError("Pending observations must have empty result fields in this fixture")


def openapi_spec() -> dict:
    patient = {
        "type": "object",
        "additionalProperties": False,
        "required": list(PATIENT_FIELDS),
        "properties": {
            "patient_id": {"type": "string", "pattern": "^P[0-9]{3}$", "example": "P001"},
            "display_name": {"type": "string", "description": "Synthetic display name; not a join key."},
            "middle_name": {"type": ["string", "null"]},
            "birth_date": {"type": ["string", "null"], "format": "date"},
            "gender_code": {
                "type": "string", "enum": ["F", "M", "O", "U"],
                "description": "Local administrative code: F=female, M=male, O=other, U=unknown. Not a clinical interpretation.",
            },
            "active": {"type": "boolean"},
            "updated_at": {"type": "string", "format": "date-time", "description": "UTC timestamp ending in Z."},
        },
    }

    def content(schema: dict) -> dict:
        return {"application/json": {"schema": schema}}

    def response(description: str, schema: dict) -> dict:
        return {"description": description, "content": content(schema)}

    error = {
        "type": "object", "required": ["error"],
        "properties": {"error": {"type": "string"}, "patient_id": {"type": "string"}},
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Synthetic Registration API", "version": "1.0.0",
            "description": "Local read-only mock source for the EXL integration prototype. All records are fictional. This is custom JSON, not a FHIR endpoint.",
        },
        "servers": [{"url": "http://127.0.0.1:8765"}],
        "x-synthetic-data": True,
        "paths": {
            "/health": {"get": {
                "operationId": "health", "summary": "Check this mock API",
                "responses": {"200": response("Mock server is available", {
                    "type": "object", "required": ["status", "system", "synthetic"],
                    "properties": {"status": {"const": "ok"}, "system": {"const": "registration-api"}, "synthetic": {"const": True}},
                })},
            }},
            "/patients": {"get": {
                "operationId": "listPatients", "summary": "Return the complete loaded patient snapshot; no pagination or name matching",
                "responses": {"200": response("All loaded synthetic patients", {
                    "type": "object", "required": ["items", "total"],
                    "properties": {"items": {"type": "array", "items": {"$ref": "#/components/schemas/Patient"}}, "total": {"type": "integer", "minimum": 0}},
                })},
            }},
            "/patients/{patient_id}": {"get": {
                "operationId": "getPatient", "summary": "Find a patient by exact stable synthetic ID",
                "parameters": [{"name": "patient_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": response("Patient found", {"$ref": "#/components/schemas/Patient"}),
                    "404": response("Patient not found; error is PATIENT_NOT_FOUND", {"$ref": "#/components/schemas/Error"}),
                },
            }},
            "/openapi.json": {"get": {
                "operationId": "getOpenApi", "summary": "Return this mock API specification",
                "responses": {"200": response("OpenAPI specification", {"type": "object"})},
            }},
        },
        "components": {"schemas": {"Patient": patient, "Error": error}},
        "x-http-behavior": {
            "head": "Supported on each GET route with identical status/headers and no response body.",
            "write-methods": "POST, PUT, PATCH, DELETE, OPTIONS, TRACE, CONNECT return 405 JSON and Allow: GET, HEAD.",
            "unknown-route": "404 JSON with error NOT_FOUND.",
            "query-parameters": "Not used; no filters are applied.",
            "snapshot": "The data file is loaded and validated at startup. Restart the server after changing it.",
        },
    }


def quote_sql(value: str | int) -> str:
    return str(value) if isinstance(value, int) else "'" + value.replace("'", "''") + "'"


def make_seed_sql(appointments: list[dict]) -> str:
    lines = ["-- Synthetic appointments only. Run schema.sql before this seed.", "BEGIN TRANSACTION;"]
    for item in appointments:
        values = ", ".join(quote_sql(item[field]) for field in APPOINTMENT_FIELDS)
        lines.append(f"INSERT INTO appointments ({', '.join(APPOINTMENT_FIELDS)}) VALUES ({values});")
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def atomic_text(destination: Path, value: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(value)
        Path(temp_name).replace(destination)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def generate(output_dir: Path, seed: int, overwrite: bool) -> dict:
    paths = {
        "patients": output_dir / "registration-api/patients.json",
        "openapi": output_dir / "registration-api/openapi.json",
        "schema": output_dir / "appointments-db/schema.sql",
        "seed": output_dir / "appointments-db/seed.sql",
        "database": output_dir / "appointments-db/appointments.sqlite3",
        "observations": output_dir / "observations-csv/observations.csv",
    }
    existing = [str(p) for p in paths.values() if p.exists()]
    if existing and not overwrite:
        raise FileExistsError("Refusing to overwrite existing data. Use a new --output-dir, or explicitly pass --overwrite: " + ", ".join(existing))
    patients, appointments, observations = build_records(seed)
    validate_records(patients, appointments, observations)
    seed_sql = make_seed_sql(appointments)
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=OBSERVATION_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(observations)

    database = paths["database"]
    database.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".appointments.", suffix=".sqlite3", dir=database.parent)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(temp_name)) as connection:
            connection.executescript(SCHEMA_SQL)
            connection.executescript(seed_sql)
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Generated SQLite database failed integrity_check")
            if connection.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] != 50:
                raise RuntimeError("Generated appointment count is incorrect")
        atomic_text(paths["patients"], json.dumps(patients, ensure_ascii=False, indent=2) + "\n")
        atomic_text(paths["openapi"], json.dumps(openapi_spec(), ensure_ascii=False, indent=2) + "\n")
        atomic_text(paths["schema"], SCHEMA_SQL)
        atomic_text(paths["seed"], seed_sql)
        atomic_text(paths["observations"], csv_buffer.getvalue())
        Path(temp_name).replace(database)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return {"synthetic": True, "seed": seed, "output_dir": str(output_dir), "patients": 20, "appointments": 50, "observations": 80}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=HERE, help="Destination for the three source directories")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace the six generated source files")
    args = parser.parse_args()
    try:
        summary = generate(args.output_dir.resolve(), args.seed, args.overwrite)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Generation failed: {exc}\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
