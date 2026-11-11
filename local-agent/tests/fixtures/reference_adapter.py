"""Handwritten adapter used only in offline orchestration tests."""
from contextlib import closing
import csv
from decimal import Decimal, ROUND_HALF_EVEN
import json
from pathlib import Path
import sqlite3
from urllib.error import HTTPError
from urllib.request import urlopen


def integrate(patient_id, sources):
    summary = {
        "patient_id": patient_id, "found": False, "patient": None, "appointments": [], "observations": [],
        "provenance": {"patient": "registration-api", "appointments": "appointments-db", "observations": "observations-csv"},
    }
    try:
        with urlopen(sources["api_base_url"] + "/patients/" + patient_id, timeout=2) as response:
            registration = json.load(response)
    except HTTPError as error:
        try:
            if error.code == 404 and json.load(error).get("error") == "PATIENT_NOT_FOUND":
                return summary
            raise
        finally:
            error.close()
    if registration["patient_id"] != patient_id:
        raise ValueError("Patient ID mismatch.")
    summary["found"] = True
    summary["patient"] = {k: v for k, v in registration.items() if k != "gender_code"}
    summary["patient"]["gender"] = {"F": "female", "M": "male", "O": "other", "U": "unknown"}[registration["gender_code"]]
    database = Path(sources["database_path"]).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM appointments WHERE person_no=? ORDER BY scheduled_at, appt_id", (patient_id,)
        )
        for row in rows:
            summary["appointments"].append({
                "appointment_id": row["appt_id"], "patient_id": row["person_no"],
                "scheduled_at": row["scheduled_at"], "department_code": row["department_code"],
                "visit_type": {1: "in_person", 2: "video"}[row["visit_type"]],
                "status": {"B": "booked", "C": "cancelled", "D": "done"}[row["status_code"]],
                "updated_at": row["updated_at"],
            })
    with open(sources["csv_path"], encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            if row["PatientID"] == patient_id:
                value = row["result_value"]
                summary["observations"].append({
                    "observation_id": row["obs_id"], "patient_id": row["PatientID"],
                    "test_code": row["test_code"], "value": None if not value else
                    format(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN), ".2f"),
                    "unit": row["result_unit"] or None, "observed_at": row["observed_at"], "status": row["status"],
                })
    summary["observations"].sort(key=lambda r: (r["observed_at"], r["observation_id"]))
    return summary
