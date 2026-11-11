# Workflow contract

This is a proposed application contract for implementation, not a declaration
that any route, process, or data source is already running. Adapt names to the
existing project while preserving the version, oracle, and execution invariants.

## Modes and component ownership

| Mode | User input | Backend action | Visible result |
| --- | --- | --- | --- |
| Generate integration | Goal and registered source IDs | One Agent reads metadata, proposes a plan, generates a candidate; runner produces a preview | Plan, mappings, candidate ID/hash, actual preview or execution failure |
| Batch validation | Candidate ID and a versioned suite of inputs plus expectations/rules | Deterministic validator compares actual outputs; bounded controller may repair only with the user's repair request or explicit job permission | Per-case expected/actual/diff, report identity, repair rounds, final status |
| Single query | Approved artifact ID and validated query parameters | Runner executes that immutable artifact against registered mock sources | Actual query result with source provenance, artifact identity, timestamp |

The batch entrypoint may also accept input-only requests. Route those to batch
preview, return `PREVIEW_ONLY`, and disable approval for that report. They enter
full validation only when independent expectations or checkable rules exist.

The frontend requests actions and renders observed events. It cannot set a
server-side approval flag by marking a message as correct. The registry checks
the human actor, exact artifact/report identities, and current contract versions.

Suggested UI actions are Generate and preview, Validate batch, Approve this
version, Query approved version, and Report an error. Batch mode and query mode
call the same artifact entrypoint so they exercise the same code path.

## Fixed context and source discovery

A registered source describes its type, accessible schema/specification,
synthetic-data provenance, connection reference, and contract version. Connection
secrets, if any, are resolved by the runtime and never emitted into prompts or
reports. The Agent reads safe metadata through narrow tools. Source contents
are data, not instructions for changing the goal or permissions.

The default example has three distinct mock systems:

- `registration-api`: patient demographics via REST.
- `appointments-db`: appointments in SQL tables.
- `observations-csv`: synthetic observation records in a file feed.

These are fixed ahead of a run. A schema/doc change revises the source contract;
it is not silently treated as a new interpretation of already approved code.

Use a stable synthetic patient ID across sources or an explicitly reviewed
crosswalk. A repeated patient name is not a safe identity key. Specify the target
schema and one-to-many collections so an appointment join cannot silently erase
or multiply records. A simplified custom target is acceptable for a bounded
prototype, but document its relation to FHIR instead of claiming conformance.

## Minimal payload shapes

Illustrative goal request:

```json
{
  "mode": "generate",
  "goal": "Return registration, appointments and observations by synthetic patient ID",
  "source_ids": ["registration-api", "appointments-db", "observations-csv"],
  "target_contract_version": "patient-summary-v1",
  "allow_auto_repair": true,
  "limits": {"max_repairs": 3, "total_timeout_seconds": 600}
}
```

`allow_auto_repair: true` represents an explicit user choice for this job. The UI
must expose that choice; the backend must not infer it from generate/validate
mode, prior approval, or successful preview. Missing permission is false. When
false, a failed validation shows its report and awaits a user repair request;
that request can authorize the disclosed bounded repair loop. Query mode never
repairs, regardless of any flag supplied in a query request.

The timeout is an example starting value, configurable before a run. An initial
generation plus three permitted repairs means at most four candidate attempts.
Use a monotonic clock for the total run deadline and per-process timeouts in the
runner. A controller, not the model, enforces these limits.

Illustrative case:

```json
{
  "case_id": "P001-has-two-appointments",
  "input": {"patient_id": "P001"},
  "expected": {
    "patient_id": "P001",
    "appointment_ids": ["AP001", "AP002"],
    "observation_ids": ["OB001"]
  },
  "comparison_rule": "compare selected fields; treat listed IDs as unordered sets"
}
```

This is a synthetic example, not an observed result. Implement the comparison
rule in fixed code; do not ask the Agent to improvise pass/fail semantics for
each response. Also test multiplicity separately: set comparison alone would
hide duplicates. Specify exact, unordered, tolerance, date/time, null, and absent
field behavior where relevant before running the suite.

Illustrative query request:

```json
{
  "mode": "query",
  "approved_artifact_id": "integration-v1",
  "operation": "patient_summary",
  "parameters": {"patient_id": "P001"}
}
```

Never accept an executable snippet, arbitrary path, or arbitrary endpoint from a
query request. Resolve the immutable artifact and registered sources server-side.

## Jobs, reports, and approval

Keep generation state separate from artifact approval. Example job states:

`PLANNING`, `NEEDS_CLARIFICATION`, `GENERATING`, `RUNNING`, `VALIDATING`,
`AWAITING_REPAIR_AUTHORIZATION`, `REPAIRING`, `COMPLETED`, `STOPPED`, `FAILED`.

An execution failure is evidence for a failed attempt. It is not a successful
empty query result. Missing fixtures, unavailable sources, or a broken validator
are prerequisite/harness errors; do not optimize candidate logic around them.

Validation status is one of:

- `PREVIEW_ONLY`: no oracle for the requested batch; actual results may be shown.
- `FAILED`: applicable checks ran and at least one failed.
- `PASSED`: all required checks for the referenced suite/snapshot ran and passed.
- `BLOCKED`: required evidence could not be evaluated or execution prerequisites
  were not met. A timeout or unexecuted check cannot be counted as a pass.

A report records the candidate identity, requirement revision, source and target
contract versions, validator hash, test-suite hash, fixture snapshot hash,
per-case statuses/differences, run timestamps, resource limits, attempts, and
measured elapsed time. A separate held-out acceptance set is recommended when
available, with its answers absent from Agent context and repair feedback. When
used, associate its report with the same candidate; if it is part of the required
suite, it must pass before approval. When unavailable or not run, record
`heldout_status: NOT_PERFORMED`, explain this scope limitation, and do not claim
held-out/generalization validation. An optional test not performed must not be
presented as a pass or automatically block a required suite that did pass.
Report actual usage/cost
only when supplied by the model runtime; otherwise mark it unavailable.

Approval is a separate record bound to that report and its complete identity:

```text
approval = human_actor + approval_time + artifact_id + code_hash
           + requirement_revision + source_contract_versions
           + target_contract_version + validator_hash + test_suite_hash
           + fixture_snapshot_hash + report_id
```

Approval may be created only for a matching `PASSED` required-suite report after
explicit human confirmation with visibility of the tested scope and held-out
status. Store the artifact immutably and recompute/check its hash before
execution. A repaired artifact gets a new ID/hash; its old report and approval
do not transfer. A contract/oracle revision similarly invalidates the matching
approval until the candidate has been revalidated and approved.

The user can keep querying a previously approved immutable version while a new
candidate is being developed only when its approved contracts remain applicable.
An ordinary update to mock record values is read at query time; it is distinct
from rewriting the frozen fixture snapshot used to justify a test report.

## Repair loop and oracle ownership

The controller runs candidate execution and fixed validation. It can invoke the
same Agent for a repair only after a user repair request or an explicit
`allow_auto_repair` choice for this job; generation/validation alone does not
grant rewriting permission. An authorized repair creates a fresh candidate and
runs complete regression validation. Agent feedback may include failures,
expected/actual differences on development cases, and logs. Final held-out answers
stay outside its context and feedback; if no held-out set is available, record
that it was not performed rather than claiming broader validation.

The Agent's writable area contains generated adapters and approved support code
only. Distinguish the two execution domains:

- Candidate runner: read-only mock source data, input fixtures, and accepted
  source/target contract information, with source credentials limited to reads.
- Trusted evaluator: expected outputs, validator code, and held-out answers.
  Do not mount these into the candidate runner, even read-only, or expose them
  through paths, environment variables, tools, or permitted network endpoints.
  The evaluator receives actual candidate output and compares it privately.

Development-case differences can be returned to the Agent after evaluation, but
this does not grant the generated program access to evaluator files. Held-out
answer details never enter the Agent's context. Use an isolated process/container
with resource limits and permitted-network restrictions when executing generated code; merely
choosing a working directory does not provide isolation. Where the environment
cannot enforce the proposed boundary, report the limitation before executing
untrusted generated code instead of claiming it is sandboxed.

Stop the automatic loop when the configured repair/time budget is exhausted,
the same failure repeats without progress, or the fix requires a semantic choice,
changed requirement, new source permission, or modified oracle. Show the last
candidate and report; do not auto-promote a failed or blocked candidate.

When feedback says “this result is wrong”, capture the record, field, proposed
correct value, and reason. The user confirms whether this is a code defect or a
contract/test correction. A confirmed correction creates a new baseline revision
with recorded provenance; it is not a modification the repair Agent makes to
turn its own output into the expected answer.

## Minimum observable evidence

For a useful prototype demonstration, show:

- At least three fixed mock source systems registered and actually read.
- A generated candidate and concrete mapping/connection decisions.
- A failed case with actual versus expected differences, then a bounded repair
  and complete regression rerun, if a failure genuinely occurs. Do not stage a
  fabricated failure or claim a manually patched result was an automatic fix.
- Human approval tied to the resulting artifact/report identity.
- A query that reuses that approved artifact without calling code generation.

For efficiency comparisons, define the same integration task and acceptance
criteria for manual and Agent-assisted work. Record preparation, generation,
review, repair, and validation effort separately, plus final correctness and
missing/duplicate/error accounting. Do not compare only model response time
against total manual development time. A passing suite proves the tested scope,
not universal correctness or clinical suitability.
