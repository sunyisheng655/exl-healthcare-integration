#!/usr/bin/env python3
"""Serve the synthetic registration snapshot as a local read-only REST source."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


HERE = Path(__file__).resolve().parent
FIELDS = {"patient_id", "display_name", "middle_name", "birth_date", "gender_code", "active", "updated_at"}


def load_patients(path: Path) -> list[dict]:
    patients = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(patients, list):
        raise ValueError("patient data must be a JSON array")
    seen = set()
    for index, patient in enumerate(patients):
        where = f"patient row {index + 1}"
        if not isinstance(patient, dict) or set(patient) != FIELDS:
            raise ValueError(f"{where}: fields must be exactly {sorted(FIELDS)}")
        identifier = patient["patient_id"]
        if not isinstance(identifier, str) or not re.fullmatch(r"P[0-9]{3}", identifier):
            raise ValueError(f"{where}: patient_id must match P followed by three digits")
        if identifier in seen:
            raise ValueError(f"{where}: duplicate patient_id {identifier}")
        seen.add(identifier)
        if not isinstance(patient["display_name"], str) or not patient["display_name"]:
            raise ValueError(f"{where}: display_name must be a nonempty string")
        if patient["middle_name"] is not None and not isinstance(patient["middle_name"], str):
            raise ValueError(f"{where}: middle_name must be string or null")
        birth_date = patient["birth_date"]
        if birth_date is not None:
            if not isinstance(birth_date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", birth_date):
                raise ValueError(f"{where}: birth_date must be YYYY-MM-DD or null")
            date.fromisoformat(birth_date)
        if not isinstance(patient["gender_code"], str) or patient["gender_code"] not in {"F", "M", "O", "U"}:
            raise ValueError(f"{where}: invalid gender_code")
        if not isinstance(patient["active"], bool):
            raise ValueError(f"{where}: active must be a JSON boolean")
        timestamp = patient["updated_at"]
        if not isinstance(timestamp, str):
            raise ValueError(f"{where}: updated_at must be an ISO8601 UTC string")
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    return patients


def handler_for(patients: list[dict], spec: dict) -> type[BaseHTTPRequestHandler]:
    by_id = {patient["patient_id"]: patient for patient in patients}

    class Handler(BaseHTTPRequestHandler):
        server_version = "SyntheticRegistrationAPI/1.0"
        sys_version = ""

        def send_json(self, status: int, payload: dict, *, include_body: bool = True, allow: str | None = None) -> None:
            data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Synthetic-Data", "true")
            if allow:
                self.send_header("Allow", allow)
            self.end_headers()
            if include_body:
                self.wfile.write(data)

        def read_request(self, *, include_body: bool) -> None:
            route = unquote(urlsplit(self.path).path)
            if route == "/health":
                self.send_json(200, {"status": "ok", "system": "registration-api", "synthetic": True}, include_body=include_body)
            elif route == "/patients":
                self.send_json(200, {"items": patients, "total": len(patients)}, include_body=include_body)
            elif route.startswith("/patients/") and route.count("/") == 2 and route[len("/patients/"):]:
                identifier = route[len("/patients/"):]
                patient = by_id.get(identifier)
                if patient is None:
                    self.send_json(404, {"error": "PATIENT_NOT_FOUND", "patient_id": identifier}, include_body=include_body)
                else:
                    self.send_json(200, patient, include_body=include_body)
            elif route == "/openapi.json":
                current_spec = deepcopy(spec)
                address, port = self.server.server_address[:2]
                current_spec["servers"] = [{"url": f"http://{address}:{port}"}]
                self.send_json(200, current_spec, include_body=include_body)
            else:
                self.send_json(404, {"error": "NOT_FOUND"}, include_body=include_body)

        def do_GET(self) -> None:
            self.read_request(include_body=True)

        def do_HEAD(self) -> None:
            self.read_request(include_body=False)

        def method_not_allowed(self) -> None:
            self.send_json(405, {"error": "METHOD_NOT_ALLOWED"}, allow="GET, HEAD")

        do_POST = method_not_allowed
        do_PUT = method_not_allowed
        do_PATCH = method_not_allowed
        do_DELETE = method_not_allowed
        do_OPTIONS = method_not_allowed
        do_TRACE = method_not_allowed
        do_CONNECT = method_not_allowed

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; defaults to local access only")
    parser.add_argument("--port", type=int, default=8765, help="TCP port; use 0 for an automatically allocated test port")
    parser.add_argument("--data-file", type=Path, default=HERE / "registration-api/patients.json")
    parser.add_argument("--openapi-file", type=Path, default=HERE / "registration-api/openapi.json")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        patients = load_patients(args.data_file.resolve())
        spec = json.loads(args.openapi_file.read_text(encoding="utf-8"))
        if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
            raise ValueError("OpenAPI file must contain an object with paths")
        server = ThreadingHTTPServer((args.host, args.port), handler_for(patients, spec))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"API startup failed: {exc}\n")
    address, port = server.server_address[:2]
    print(json.dumps({"host": address, "port": port, "url": f"http://{address}:{port}", "synthetic": True, "patients": len(patients)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
