"""Trusted sandbox entrypoint. Keep compatible with Apple's Python 3.9 stdlib."""

import contextlib
import importlib.util
import json
import resource
import sys


def main():
    protocol_output = sys.stdout
    config = json.loads(sys.stdin.buffer.read(65536))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (config["cpu_seconds"], config["cpu_seconds"] + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (config["max_output_bytes"], config["max_output_bytes"]))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    try:
        # Ordinary candidate print() chatter cannot be mistaken for the result.
        # Both streams remain bounded by the parent. Raw fd output is untrusted
        # data too; malformed envelopes fail instead of becoming a test report.
        with contextlib.redirect_stdout(sys.stderr):
            spec = importlib.util.spec_from_file_location("generated_candidate", config["candidate_path"])
            candidate = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(candidate)
            entrypoint = getattr(candidate, "integrate", None)
            if not callable(entrypoint):
                raise TypeError("Candidate must export integrate(patient_id, sources)")
            result = entrypoint(config["patient_id"], config["sources"])
        if not isinstance(result, dict):
            envelope = {"ok": False, "error": {"code": "INVALID_RESULT", "message": "integrate() must return a dict"}}
        else:
            # This is only a transport envelope, never a correctness/PASSED claim.
            envelope = {"ok": True, "result": result}
        encoded = json.dumps(envelope, ensure_ascii=True, allow_nan=False)
        if len(encoded.encode("utf-8")) > config["max_output_bytes"]:
            encoded = json.dumps({"ok": False, "error": {"code": "OUTPUT_LIMIT", "message": "JSON result exceeds the output limit"}})
    except BaseException as exc:
        encoded = json.dumps({"ok": False, "error": {"code": "EXECUTION_FAILED", "message": type(exc).__name__ + ": " + str(exc)[:600]}})
    protocol_output.write(encoded + "\n")
    protocol_output.flush()


if __name__ == "__main__":
    main()
