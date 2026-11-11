"""Local candidate approval and content verification."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .controller import bindings, contract_bindings, save_json, source_context
from .validator import make_baseline


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _verified_report(job_dir: Path):
    report_path = job_dir / "report.json"
    report = _load(report_path)
    if report.get("job_id") != job_dir.name or report.get("status") != "PASSED" or report.get("heldout_status") != "PASSED":
        raise ValueError("Approval or query requires a passing development and holdout report.")
    final = report["attempts"][-1]
    if final["development"]["status"] != "PASSED" or not re.fullmatch(r"candidate-0[1-4]\.py", final["candidate"]):
        raise ValueError("The final candidate or its test report is invalid.")
    candidate = job_dir / final["candidate"]
    if candidate.is_symlink() or _sha(candidate) != final["code_sha256"]:
        raise ValueError("Candidate code changed after validation; rerun the job.")
    return report, report_path, candidate, final["code_sha256"]


def approve(job_dir: Path, runner, confirmed_sha256: str):
    report, report_path, candidate, code_sha = _verified_report(job_dir)
    if confirmed_sha256 != code_sha:
        raise ValueError("Confirm the exact SHA-256 shown in the passing report.")
    current = bindings(source_context(runner.mock_root),
                       make_baseline(runner.mock_root, runner.sources["api_base_url"]))
    if current != report["bindings"]:
        raise ValueError("Source, test, validator, or contract changed after validation; rerun the job.")
    result = {"job_id": job_dir.name, "candidate": candidate.name, "code_sha256": code_sha,
              "report_sha256": _sha(report_path), "bindings": current,
              "approved_at": datetime.now(timezone.utc).isoformat(), "approved_by": "local_cli_operator"}
    save_json(job_dir / "approval.json", result)
    return result


def query(job_dir: Path, runner, patient_id: str):
    if not re.fullmatch(r"P[0-9]{3}", patient_id):
        raise ValueError("Provide a synthetic patient ID, e.g. P001.")
    report, report_path, candidate, code_sha = _verified_report(job_dir)
    approval = _load(job_dir / "approval.json")
    if (approval.get("job_id") != job_dir.name or approval.get("candidate") != candidate.name
            or approval.get("code_sha256") != code_sha or approval.get("report_sha256") != _sha(report_path)
            or approval.get("bindings") != report["bindings"]):
        raise ValueError("The approved artifact no longer matches the code and report.")
    # Query reads the current mock records under the approved schema, not the test answers.
    current = contract_bindings(source_context(runner.mock_root))
    for key in current:
        if current[key] != approval["bindings"][key]:
            raise ValueError("The source/output contract, runner, or validator changed; rerun and reapprove.")
    execution = runner.run(candidate, patient_id, expected_sha256=code_sha)
    if not execution["ok"]:
        raise ValueError("Approved query failed: " + execution["error"]["message"])
    return {"job_id": job_dir.name, "code_sha256": code_sha, "result": execution["result"]}
