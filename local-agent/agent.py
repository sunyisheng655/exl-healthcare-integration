#!/usr/bin/env python3
"""Run the local synthetic healthcare integration Agent."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import sys
import uuid
import warnings

from healthcare_agent.artifacts import approve, query
from healthcare_agent.controller import DEFAULT_GOAL, run_job
from healthcare_agent.credentials import CredentialError, load_api_key, store_keychain_key
from healthcare_agent.local_sources import local_api
from healthcare_agent.provider import DEFAULT_MODEL, OpenRouterClient, ProviderError
from healthcare_agent.runner import CandidateRunner, SandboxUnavailable

HERE = Path(__file__).resolve().parent
MOCK_ROOT = HERE.parent / "mock-systems"
RUNTIME = HERE / ".runtime"
CONFIG = HERE / "local-config.json"
MODEL_ID = re.compile(r"(?:z-ai/glm|x-ai/grok)[a-zA-Z0-9.:-]*\Z")


def default_skill():
    for ancestor in HERE.parents:
        path = ancestor / ".agents/skills/agentic-healthcare-integration/SKILL.md"
        if path.is_file():
            return path
    raise ValueError("Cannot find the project Skill. Use --skill PATH to supply it.")


def checked_model(value):
    if not isinstance(value, str) or not MODEL_ID.fullmatch(value) or value.endswith(":batch"):
        raise ValueError("Select an OpenRouter GLM or Grok chat model ID with tool calling support.")
    return value


def model_from_config(explicit):
    if explicit:
        return checked_model(explicit)
    if os.environ.get("OPENROUTER_MODEL"):
        return checked_model(os.environ["OPENROUTER_MODEL"])
    if CONFIG.exists():
        return checked_model(json.loads(CONFIG.read_text(encoding="utf-8"))["model"])
    return DEFAULT_MODEL


def serve_web(port: int) -> int:
    from healthcare_agent.web import create_server

    model = model_from_config(None)

    def client_factory():
        key, _ = load_api_key()
        if not key:
            raise CredentialError("OpenRouter key not configured. Run python3 agent.py configure in your terminal.")
        return OpenRouterClient(key, model=model)

    with create_server(port=port, mock_root=MOCK_ROOT, runtime=RUNTIME,
                       skill_path=default_skill(), model=model, client_factory=client_factory) as server:
        print(f"Local Agent: http://127.0.0.1:{server.server_port}/  |  {model}  |  Ctrl+C to stop",
              flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def prompt_key(prompt):
    if not sys.stdin.isatty():
        raise CredentialError("Open a local terminal for hidden key input.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning as error:
            raise CredentialError("Could not disable terminal echo; no key was read.") from error


def configure() -> int:
    if not sys.stdin.isatty():
        raise CredentialError("Run configure in an interactive terminal. For other shells, use OPENROUTER_API_KEY.")
    model = checked_model(input(f"OpenRouter model ID [{DEFAULT_MODEL}]: ").strip() or DEFAULT_MODEL)
    key = prompt_key("OpenRouter API key (hidden; saved to macOS Keychain): ")
    store_keychain_key(key)
    temp = CONFIG.with_suffix(".tmp")
    temp.write_text(json.dumps({"model": model}, indent=2) + "\n", encoding="utf-8")
    temp.replace(CONFIG)
    print("Configured. The project stores only the model ID; the key is in macOS Keychain.")
    return 0


def print_json(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


def job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{16}", job_id):
        raise ValueError("Use the job ID printed by the run command.")
    result = RUNTIME / job_id
    if result.is_symlink() or not result.is_dir():
        raise ValueError("The specified local job does not exist.")
    return result


def main() -> int:
    # A pasted key must never be echoed in argparse's unrecognized-argument error.
    if any("sk-or-" in argument.lower() for argument in sys.argv[1:]):
        print("Do not pass an API key as a command argument. Use configure or --prompt-key.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("configure", help="store an OpenRouter key in macOS Keychain")
    commands.add_parser("doctor", help="check local source files and actual sandbox enforcement")
    web = commands.add_parser("serve", help="start the local chat interface")
    web.add_argument("--port", type=int, default=8790, help="loopback port (default: 8790)")
    run = commands.add_parser("run", help="generate code and independently validate it")
    run.add_argument("--model", help="OpenRouter model ID (default: GLM)")
    run.add_argument("--goal", default=DEFAULT_GOAL, help="integration goal within the fixed patient-summary contract")
    run.add_argument("--skill", type=Path, help="path to an alternate copy of the project Skill")
    run.add_argument("--auto-repair", action="store_true", help="explicitly allow bounded code repair for this run")
    run.add_argument("--max-repairs", type=int, choices=range(4), default=3)
    run.add_argument("--timeout", type=int, default=360, metavar="SECONDS")
    run.add_argument("--prompt-key", action="store_true", help="use a key once without saving it")
    accept = commands.add_parser("approve", help="approve a specific passing code hash after reviewing it")
    accept.add_argument("job_id")
    accept.add_argument("--code-sha256", required=True)
    lookup = commands.add_parser("query", help="run a pinned approved adapter without a model call")
    lookup.add_argument("job_id")
    lookup.add_argument("patient_id")
    for command in (run, accept, lookup):
        command.add_argument("--api-url", help="use an existing registered loopback API instead of a temporary one")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            return configure()
        if args.command == "serve":
            if not 0 <= args.port <= 65535:
                raise ValueError("--port must be between 0 and 65535.")
            return serve_web(args.port)
        if args.command == "doctor":
            runner = CandidateRunner(MOCK_ROOT)
            isolation = runner.doctor()
            print_json({"sandbox": isolation, "mock_source_directory": str(MOCK_ROOT),
                        "environment_key_configured": bool(os.environ.get("OPENROUTER_API_KEY")),
                        "model": model_from_config(None)})
            return 0 if isolation["available"] else 2
        if args.command == "run":
            if not args.goal.strip() or len(args.goal) > 2000:
                raise ValueError("Provide a goal of 1–2000 characters using synthetic data only.")
            skill = args.skill or default_skill()
            model = model_from_config(args.model)
            if not 1 <= args.timeout <= 600:
                raise ValueError("--timeout must be 1–600 seconds.")
            key = prompt_key("OpenRouter API key (not saved): ") if args.prompt_key else load_api_key()[0]
            if not key:
                raise CredentialError("OpenRouter key not configured. Run python3 agent.py configure, or use --prompt-key.")
            client = OpenRouterClient(key, model=model)
            RUNTIME.mkdir(mode=0o700, exist_ok=True)
            location = RUNTIME / uuid.uuid4().hex[:16]
            print(f"Model: {model} | repair: {'enabled' if args.auto_repair else 'off'} | "
                  f"max repairs: {args.max_repairs if args.auto_repair else 0} | timeout: {args.timeout}s",
                  file=sys.stderr, flush=True)
            with local_api(MOCK_ROOT, args.api_url) as url:
                report = run_job(client, CandidateRunner(MOCK_ROOT, url), location, skill,
                                 goal=args.goal, allow_auto_repair=args.auto_repair,
                                 max_repairs=args.max_repairs, timeout_seconds=args.timeout,
                                 event=lambda line: print(line, file=sys.stderr, flush=True))
            output = {k: report[k] for k in ("job_id", "status", "reason", "heldout_status", "elapsed_seconds") if k in report}
            output["report_path"] = str(location / "report.json")
            if report["attempts"]:
                last = report["attempts"][-1]
                output.update(candidate_path=str(location / last["candidate"]), code_sha256=last["code_sha256"])
            print_json(output)
            return 0 if report["status"] == "PASSED" else 2
        with local_api(MOCK_ROOT, args.api_url) as url:
            runner = CandidateRunner(MOCK_ROOT, url)
            directory = job_dir(args.job_id)
            result = approve(directory, runner, args.code_sha256) if args.command == "approve" else query(directory, runner, args.patient_id)
            print_json(result)
        return 0
    except (CredentialError, ProviderError, SandboxUnavailable, OSError, ValueError, KeyError, EOFError) as error:
        print(f"Agent: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Agent cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
