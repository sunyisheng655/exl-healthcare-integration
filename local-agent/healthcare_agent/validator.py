"""Trusted evaluator. Never expose this module or its baseline to candidate code."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
from http.client import HTTPException
import io
import ipaddress
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .contract import CONTRACT_VERSION


PATIENT_FIELDS = {"patient_id", "display_name", "middle_name", "birth_date", "gender_code", "active", "updated_at"}
APPOINTMENT_FIELDS = {"appt_id", "person_no", "scheduled_at", "department_code", "visit_type", "status_code", "updated_at"}
OBSERVATION_FIELDS = {"obs_id", "PatientID", "test_code", "result_value", "result_unit", "observed_at", "status"}
GENDERS = {"F": "female", "M": "male", "O": "other", "U": "unknown"}
VISITS = {1: "in_person", 2: "video"}
STATUSES = {"B": "booked", "C": "cancelled", "D": "done"}
PROVENANCE = {"patient": "registration-api", "appointments": "appointments-db", "observations": "observations-csv"}
MAX_SOURCE_BYTES = 5 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _unique_json_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError(f"Non-finite JSON number {value!r}")


def _text(value, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not value and not allow_empty):
        raise ValueError(f"{label} must be {'a string' if allow_empty else 'a nonempty string'}")
    return value


def _object(value, fields: set[str], label: str) -> dict:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} must have exactly these fields: {', '.join(sorted(fields))}")
    return value


def _timestamp(value, label: str) -> str:
    _text(value, label)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
        raise ValueError(f"{label} must be an ISO8601 UTC timestamp ending in Z")
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return value


def _enum(value, allowed: set, label: str) -> None:
    if type(value) is not str or value not in allowed:
        raise ValueError(f"{label} has an unknown source enum value")


def _decimal_text(value: str) -> str | None:
    if value == "":
        return None
    try:
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError("Observation result_value must be finite")
        return format(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN), ".2f")
    except InvalidOperation as exc:
        raise ValueError("Observation result_value is not a supported finite decimal") from exc


def _read_patients(api_base_url: str) -> list[dict]:
    _text(api_base_url, "api_base_url")
    parsed = urlsplit(api_base_url)
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if parsed.scheme != "http" or not loopback or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("The registered API base must be an HTTP loopback origin without credentials, path, query, or fragment")
    # Ignore ambient proxies and refuse redirects beyond the registered source.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(Request(api_base_url.rstrip("/") + "/patients", headers={"Accept": "application/json"}), timeout=5) as response:
            raw = response.read(MAX_SOURCE_BYTES + 1)
    except HTTPError as exc:
        exc.close()
        raise ValueError(f"registration-api source is unavailable: HTTP {exc.code}") from exc
    except (URLError, HTTPException, OSError) as exc:
        raise ValueError(f"registration-api source is unavailable: {exc}") from exc
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("registration-api response exceeds the mock snapshot limit")
    document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object, parse_constant=_reject_constant)
    _object(document, {"items", "total"}, "registration-api list response")
    patients = document["items"]
    if type(patients) is not list or type(document["total"]) is not int or document["total"] != len(patients):
        raise ValueError("registration-api items/total are inconsistent")
    seen = set()
    for index, record in enumerate(patients):
        label = f"registration-api row {index + 1}"
        _object(record, PATIENT_FIELDS, label)
        identifier = _text(record["patient_id"], label + ".patient_id")
        if identifier in seen:
            raise ValueError(f"Duplicate patient primary ID {identifier}")
        seen.add(identifier)
        _text(record["display_name"], label + ".display_name")
        if record["middle_name"] is not None:
            _text(record["middle_name"], label + ".middle_name", allow_empty=True)
        birth = record["birth_date"]
        if birth is not None:
            _text(birth, label + ".birth_date")
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", birth):
                raise ValueError(label + ".birth_date is not YYYY-MM-DD")
            date.fromisoformat(birth)
        _enum(record["gender_code"], set(GENDERS), label + ".gender_code")
        if type(record["active"]) is not bool:
            raise ValueError(label + ".active must be a JSON boolean")
        _timestamp(record["updated_at"], label + ".updated_at")
    return patients


def _read_appointments(database: Path, patient_ids: set[str]) -> list[dict]:
    if not database.is_file():
        raise ValueError("appointments-db source file is missing")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(appointments)")}
        if columns != APPOINTMENT_FIELDS:
            raise ValueError("appointments-db schema does not match the accepted source contract")
        records = [dict(row) for row in connection.execute(
            "SELECT appt_id, person_no, scheduled_at, department_code, visit_type, status_code, updated_at FROM appointments"
        )]
    seen = set()
    for index, record in enumerate(records):
        label = f"appointments-db row {index + 1}"
        identifier = _text(record["appt_id"], label + ".appt_id")
        if identifier in seen:
            raise ValueError(f"Duplicate appointment primary ID {identifier}")
        seen.add(identifier)
        person = _text(record["person_no"], label + ".person_no")
        if person not in patient_ids:
            raise ValueError(f"Appointment {identifier} refers to an unregistered patient")
        _timestamp(record["scheduled_at"], label + ".scheduled_at")
        _timestamp(record["updated_at"], label + ".updated_at")
        _enum(record["department_code"], {"GP", "RAD", "LAB"}, label + ".department_code")
        if type(record["visit_type"]) is not int or record["visit_type"] not in VISITS:
            raise ValueError(label + ".visit_type has an unknown source integer enum")
        _enum(record["status_code"], set(STATUSES), label + ".status_code")
    return records


def _read_observations(csv_path: Path, patient_ids: set[str]) -> tuple[list[dict], bytes]:
    raw = csv_path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("observations-csv exceeds the mock snapshot limit")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True)
    columns = reader.fieldnames
    if columns is None or len(columns) != len(set(columns)) or set(columns) != OBSERVATION_FIELDS:
        raise ValueError("observations-csv header is missing, duplicated, or does not match the source contract")
    records = list(reader)
    seen = set()
    for index, record in enumerate(records):
        label = f"observations-csv row {index + 1}"
        _object(record, OBSERVATION_FIELDS, label)
        for field in OBSERVATION_FIELDS:
            _text(record[field], label + "." + field, allow_empty=field in {"result_value", "result_unit"})
        identifier = record["obs_id"]
        if identifier in seen:
            raise ValueError(f"Duplicate observation primary ID {identifier}")
        seen.add(identifier)
        if record["PatientID"] not in patient_ids:
            raise ValueError(f"Observation {identifier} refers to an unregistered patient")
        _enum(record["test_code"], {"DEMO_A", "DEMO_B", "DEMO_C"}, label + ".test_code")
        _enum(record["status"], {"final", "pending"}, label + ".status")
        _enum(record["result_unit"], {"", "arb.units"}, label + ".result_unit")
        _timestamp(record["observed_at"], label + ".observed_at")
        _decimal_text(record["result_value"])
    return records, raw


def make_baseline(mock_root: Path, api_base_url: str) -> dict:
    """Freeze a trusted oracle for the current three-source snapshot.

    This entire return value is evaluator-private. The held-out split uses other
    patient IDs from this same snapshot; it does not prove new-data generalization.
    Source or schema failures raise ValueError rather than inventing empty data.
    """
    root = Path(mock_root)
    try:
        patients = _read_patients(api_base_url)
        patient_ids = {record["patient_id"] for record in patients}
        appointments = _read_appointments(root / "appointments-db/appointments.sqlite3", patient_ids)
        observations, csv_bytes = _read_observations(root / "observations-csv/observations.csv", patient_ids)
    except (OSError, ValueError, sqlite3.Error, csv.Error) as exc:
        raise ValueError(f"Cannot freeze valid source baseline: {exc}") from exc

    by_id = {record["patient_id"]: record for record in patients}
    appts_by_id = defaultdict(list)
    observations_by_id = defaultdict(list)
    for record in appointments:
        appts_by_id[record["person_no"]].append({
            "appointment_id": record["appt_id"], "patient_id": record["person_no"],
            "scheduled_at": record["scheduled_at"], "department_code": record["department_code"],
            "visit_type": VISITS[record["visit_type"]], "status": STATUSES[record["status_code"]],
            "updated_at": record["updated_at"],
        })
    for record in observations:
        observations_by_id[record["PatientID"]].append({
            "observation_id": record["obs_id"], "patient_id": record["PatientID"],
            "test_code": record["test_code"], "value": _decimal_text(record["result_value"]),
            "unit": record["result_unit"] or None, "observed_at": record["observed_at"], "status": record["status"],
        })

    def expected(identifier: str) -> dict:
        source = by_id.get(identifier)
        result = {"patient_id": identifier, "found": source is not None, "patient": None,
                  "appointments": [], "observations": [], "provenance": dict(PROVENANCE)}
        if source is not None:
            result["patient"] = {"patient_id": source["patient_id"], "display_name": source["display_name"],
                                 "middle_name": source["middle_name"], "birth_date": source["birth_date"],
                                 "gender": GENDERS[source["gender_code"]], "active": source["active"],
                                 "updated_at": source["updated_at"]}
            result["appointments"] = sorted(appts_by_id[identifier], key=lambda r: (r["scheduled_at"], r["appointment_id"]))
            result["observations"] = sorted(observations_by_id[identifier], key=lambda r: (r["observed_at"], r["observation_id"]))
        return result

    def case(identifier: str) -> dict:
        return {"case_id": "patient-" + identifier, "input": {"patient_id": identifier}, "expected": expected(identifier)}

    snapshot = {
        "patients": sorted(patients, key=lambda p: p["patient_id"]),
        "appointments": sorted(appointments, key=lambda p: p["appt_id"]),
        "csv_sha256": hashlib.sha256(csv_bytes).hexdigest(),
    }
    fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    return {
        "contract_version": CONTRACT_VERSION,
        "source_fingerprint": fingerprint,
        "development_cases": [case(f"P{i:03d}") for i in range(1, 11)] + [case("P999")],
        "heldout_cases": [case(f"P{i:03d}") for i in range(11, 21)],
        "metadata": {
            "synthetic": True,
            "source_counts": {"patients": len(patients), "appointments": len(appointments), "observations": len(observations)},
            "heldout_scope": "Other patient IDs in the same frozen snapshot; no claim of unknown-data generalization.",
            "fingerprint_scope": "Canonical API patient snapshot, canonical SQL rows, and exact CSV content SHA256.",
        },
    }


def _json_type(value) -> str:
    return {dict: "object", list: "array", str: "string", bool: "boolean", int: "integer", float: "number", type(None): "null"}.get(type(value), type(value).__name__)


def _safe(value):
    if type(value) is float and not math.isfinite(value):
        return f"<non-finite {value}>"
    if type(value) is dict:
        return {str(key): _safe(item) for key, item in value.items()}
    if type(value) is list:
        return [_safe(item) for item in value]
    if type(value) in {str, bool, int, float, type(None)}:
        return value
    return repr(value)


def compare_output(actual, expected) -> list[dict]:
    """Compare exact JSON shape, types, values, array order and multiplicity."""
    differences = []

    def add(path: str, code: str, want, got, **extra):
        differences.append({"path": path, "code": code, "expected": _safe(want), "actual": _safe(got), **extra})

    def visit(got, want, path: str):
        if type(got) is not type(want):
            add(path, "TYPE_MISMATCH", want, got, expected_type=_json_type(want), actual_type=_json_type(got))
        elif type(want) is dict:
            for key in sorted(set(want) - set(got), key=str):
                add(path + "." + str(key), "MISSING_KEY", want[key], "<missing>")
            for key in sorted(set(got) - set(want), key=str):
                add(path + "." + str(key), "EXTRA_KEY", "<absent>", got[key])
            for key in sorted(set(want) & set(got), key=str):
                visit(got[key], want[key], path + "." + str(key))
        elif type(want) is list:
            if len(got) != len(want):
                add(path, "LENGTH_MISMATCH", len(want), len(got))
            for index in range(min(len(got), len(want))):
                visit(got[index], want[index], f"{path}[{index}]")
        elif got != want:
            add(path, "VALUE_MISMATCH", want, got)

    visit(actual, expected, "$")
    return differences


def evaluate_cases(outputs: dict[str, dict], cases: list) -> dict:
    """Outputs are keyed by case_id; missing execution blocks a complete pass.

    Provided but malformed results fail. BLOCKED takes precedence when any case
    is missing. Extra output entries are ignored when evaluating a suite subset.
    Empty suites are BLOCKED, never a successful zero-test approval.
    """
    if type(cases) is not list:
        raise ValueError("cases must be a list")
    seen = set()
    for case in cases:
        if type(case) is not dict or type(case.get("case_id")) is not str or not case["case_id"] or "expected" not in case:
            raise ValueError("Each case needs a nonempty case_id and a trusted expected output")
        if case["case_id"] in seen:
            raise ValueError("Duplicate test case_id")
        seen.add(case["case_id"])
    result = {"status": "BLOCKED", "cases": [], "total": len(cases), "executed": 0, "passed": 0, "failed": 0, "blocked": 0}
    if not cases:
        result["reason"] = "NO_CASES"
        return result
    for case in cases:
        identifier = case["case_id"]
        if type(outputs) is not dict or identifier not in outputs:
            result["blocked"] += 1
            result["cases"].append({"case_id": identifier, "status": "BLOCKED", "differences": [{"path": "$", "code": "NOT_EXECUTED", "expected": "an executed candidate result", "actual": "<missing>"}]})
            continue
        differences = compare_output(outputs[identifier], case["expected"])
        status = "FAILED" if differences else "PASSED"
        result["executed"] += 1
        result["failed" if differences else "passed"] += 1
        result["cases"].append({"case_id": identifier, "status": status, "differences": differences})
    result["status"] = "BLOCKED" if result["blocked"] else "FAILED" if result["failed"] else "PASSED"
    return result
