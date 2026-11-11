"""Public output contract. This module contains no evaluator answers or fixtures."""

from __future__ import annotations

import json
from pathlib import Path


CONTRACT_VERSION = "patient-summary-v1"


def contract_document() -> dict:
    """Return metadata safe to include in the coding model's context."""
    schema = json.loads(Path(__file__).with_name("patient-summary.schema.json").read_text(encoding="utf-8"))
    return {
        "contract_version": CONTRACT_VERSION,
        "entrypoint": {
            "module": "candidate.py",
            "callable": "integrate",
            "signature": "integrate(patient_id: str, sources: dict) -> dict",
            "input": "An exact stable synthetic patient ID. Do not join patients by display name.",
        },
        "sources_schema": {
            "type": "object",
            "required": ["api_base_url", "database_path", "csv_path"],
            "additionalProperties": False,
            "properties": {
                "api_base_url": {"type": "string", "description": "Registered local registration API base URL."},
                "database_path": {"type": "string", "description": "Absolute path to the registered SQLite source; open via URI mode=ro."},
                "csv_path": {"type": "string", "description": "Absolute path to the registered UTF-8 CSV source."},
            },
        },
        "output_schema": schema,
        "mapping_rules": {
            "patient": {
                "source": "registration-api",
                "lookup": "GET /patients/{patient_id}; a documented PATIENT_NOT_FOUND 404 is authoritative absence. Other HTTP/source errors must raise an exception.",
                "copy_fields": ["patient_id", "display_name", "middle_name", "birth_date", "active", "updated_at"],
                "gender_code_to_gender": {"F": "female", "M": "male", "O": "other", "U": "unknown"},
                "keep_inactive": True,
            },
            "appointments": {
                "source": "appointments-db",
                "table": "appointments",
                "filter": "person_no equals the input patient_id, with a parameterized SQL query.",
                "rename": {"appt_id": "appointment_id", "person_no": "patient_id", "status_code": "status"},
                "visit_type": {"1": "in_person", "2": "video"},
                "status_code": {"B": "booked", "C": "cancelled", "D": "done"},
                "copy_fields": ["scheduled_at", "department_code", "updated_at"],
                "keep_all_statuses": True,
            },
            "observations": {
                "source": "observations-csv",
                "filter": "PatientID equals the input patient_id.",
                "rename": {"obs_id": "observation_id", "PatientID": "patient_id", "result_value": "value", "result_unit": "unit"},
                "value": "Empty string maps to null. Otherwise use Decimal(raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN), then format with exactly two decimal places. Reject non-finite values; never convert through float.",
                "unit": "Empty string maps to null; otherwise preserve the registered unit.",
                "copy_fields": ["test_code", "observed_at", "status"],
                "keep_pending": True,
            },
            "provenance": {"patient": "registration-api", "appointments": "appointments-db", "observations": "observations-csv"},
            "not_found": "Return found=false, patient=null, appointments=[], observations=[], retain input patient_id and the same provenance.",
            "integrity": "Preserve every matching record by its source primary ID, including equal values and cancelled/pending records. Do not cross-multiply the two child lists or silently discard malformed rows.",
        },
        "ordering": {
            "appointments": ["scheduled_at", "appointment_id"],
            "observations": ["observed_at", "observation_id"],
        },
        "error_semantics": "Source unavailability, malformed source data, duplicate primary IDs, unknown enum meanings, or inconsistent links are errors, not patient absence or empty child lists. This is custom synthetic-data JSON, not a complete FHIR resource.",
    }
