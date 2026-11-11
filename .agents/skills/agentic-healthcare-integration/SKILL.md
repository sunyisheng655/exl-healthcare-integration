---
name: agentic-healthcare-integration
description: >-
  Build, review, or operate the single-agent healthcare integration prototype for
  EXL Challenge 01: connect fixed mock API, SQL, and file sources; generate and
  repair adapters with bounded execution feedback; batch-validate independently;
  approve a code version; then query that approved version. Use for this prototype's
  chat workflow, adapter generation, validation, version approval, or architecture.
  Does not cover clinical advice or a general-purpose code-generation chatbot.
---

# Agentic Healthcare Integration

Implement the agreed healthcare integration workflow without confusing code
generation with querying already integrated data. This Skill supplies instructions;
it does not itself install an agent runtime, start a backend, or provide a web UI.

For states, frontend/backend payloads, test semantics, and worked examples, read
[workflow-contract.md](references/workflow-contract.md) when implementing or
reviewing those parts. Keep essential decisions below even if the reference is
not loaded.

## Architecture and scope

- Use one logical coding/repair Agent. The runner, deterministic validator,
  loop controller, version registry, and UI are ordinary program components.
  Several LLM calls do not imply several Agents.
- Default mock sources are a fixed REST API, SQL database, and CSV feed. Their
  specifications and samples can be registered in advance. The user supplies an
  integration goal, not a new set of systems on every chat message.
- The Agent reads source metadata, proposes a connection/mapping plan, and
  generates adapters. Review ambiguous semantics with the user instead of
  guessing identity joins, enum meanings, missing-value policy, or conversions.
- Target an explicitly defined, bounded healthcare record contract. Document
  any FHIR-aligned fields and omissions; do not label custom JSON as fully FHIR
  compliant without the required conformance checks.
- The chosen UI has three modes: generate integration, batch validation, and
  approved-version single query. Show plan/mapping, source provenance, actual
  results, test differences, code version, and approval status outside the chat
  transcript so the user can review them.

## Execution invariants

1. Separate readable inputs from private evaluation materials. Candidate code
   may read registered mock data, input fixtures, and accepted source/target
   contracts through read-only access. Expected outputs, validator code, and
   held-out answers stay in a separate trusted evaluator and are not mounted or
   readable by candidate code. Read-only oracle files would still permit answer
   harvesting. A prompt or a different working directory is not an execution
   boundary. Restrict endpoints, process duration, resources, and output size.
2. Use an independent validator with human-confirmed expectations or explicit
   rules. The Agent may inspect failures and propose extra tests, but cannot
   change the accepted oracle, skip assertions, or change source data to obtain
   a pass. Development-case expected/actual differences may be sent to the Agent;
   final held-out answers stay out of its context and repair feedback. A separate
   final acceptance set is recommended when available. If absent, record it as
   not performed and do not claim held-out or generalization validation.
3. A batch of inputs without expected outputs or checkable rules is a preview,
   not a correctness pass. UI and reports must preserve this distinction.
4. Generation or validation permission does not authorize code rewriting. Repair
   only when the user requests it or explicitly enables `allow_auto_repair` for
   that job at its start; missing permission defaults to false. With permission,
   default to at most three repairs after the initial candidate and a configured
   total deadline. Expose permission and both limits before a run. Stop on budget exhaustion,
   repeated failures without progress, missing prerequisites, or semantic
   ambiguity. Report the evidence and hand control back to the user.
5. Approve only an identified candidate with a matching completed `PASSED` report
   and explicit human approval. Bind approval to code hash, requirement revision,
   source/target contract versions, test/validator/fixture versions, and report
   identity. A changed dependency of that approval requires a new report and
   approval. A normal change in mock record values under the same source
   contract does not itself approve different code or rewrite test fixtures.
   Present the required suite's tested scope and held-out status to the human;
   absence of optional held-out cases is not itself a failed required suite.
6. Normal queries execute the explicitly approved artifact through the runner.
   They do not generate code, repair code, or promote a new version. Reject a
   stale approval or contract mismatch and offer the separate development mode.

## Workflow

**Establish inputs.** Inspect the requested project directory and its local rules,
existing source registry, runtime tools, and acceptance suite. Reuse established
frameworks. If mock sources or runtime components are absent, name the missing
prerequisites; create synthetic sources only within an authorized implementation
task. Never report invented endpoints, query results, tests, or benchmarks as
observed.

**Generate and preview.** Confirm the goal, permitted sources, target contract,
identity key, unresolved mappings, and whether repair was explicitly enabled.
The Agent selects API calls, SQL reads,
and file parsing strategies, generates a versioned candidate, and proposes tests.
In the read-only mock environment, run the candidate and return actual output
with provenance. Keep unverified preview visibly distinct from a passed report.

**Validate and repair.** Run the frozen validator against a stable fixture
snapshot. Check executable behavior, target structure, reference values, patient
identity joins, record completeness, duplicates, and specified failures. A failed
development case returns structured differences and execution logs. If repair
is authorized, send them to the same Agent and allow edits only to candidate code;
otherwise show the failed report and await the user's repair decision. Rerun all applicable regression cases after each
repair, recording rounds, time, model usage/cost when available, and interventions.

**Approve and query.** Present the exact candidate and report. Persist approval
only after explicit human confirmation of that revision. In query mode, parse
input into the approved operation and validated parameters, select the approved
artifact, and execute it. For a small demo, a patient identifier field is enough;
natural-language parsing is optional and must not turn into arbitrary SQL/shell
execution. Disambiguate patient identities rather than joining on a guessed name.

**Handle feedback.** Classify feedback as an implementation bug, a changed
requirement, or a proposed correction to an oracle. An authorized bug fix creates a new candidate
and rerun the unchanged suite. Requirement or oracle changes need human-confirmed
revisions, fresh validation, and fresh approval. An observed output or thumbs-up
on one record is not automatically a new expected result for all cases.

## Challenge alignment and delivery

The challenge asks for 3–5 mock systems, Agent participation in discovering each
system/selecting an integration pattern/generating code, and comparison with
hand-built integrations for time and consistency. The three-source layout,
frontend modes, single logical Agent, and repair limit above are this design's
defaults, not additional competition rules. The deck's full lifecycle hints are
optional. Azure/.NET preference does not ban other languages.

For a requested submission pack, follow the supplied current brief: solution deck
up to 10 slides, demo link up to 3 minutes, code repository with setup README,
one-page architecture diagram, and one-page Responsible AI note. State AI tools
and reused code. Use synthetic or permitted public materials; do not import
real patient data or private employer code. Measure manual and Agent-assisted
work on comparable tasks, including review and repair time. Report missing
measurements honestly instead of inventing efficiency gains.

Project-specific diagrams and decisions belong in the project, currently
`Learning-教育学习/Overseas-海外教育/Trinity-三一学院/EXL-Hackathon-2026/Challenge-01-医疗系统集成/`
relative to the workspace root. Do not copy project internals into unrelated
skills or change the user's learning site as a side effect.

Example requests:

- “用 $agentic-healthcare-integration 按架构实现单 Agent 原型，先检查现有数据源和测试。”
- “批量运行这些测试，展示预期和实际差异；没有正确答案的条目标记为预览。”
- “只查询我已批准的版本，查找合成病人 P001，不重新生成代码。”
