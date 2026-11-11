# EXL Healthcare Integration

An EXL and Trinity hackathon prototype that combines synthetic patient records from a registration REST API, an appointments SQLite database, and an observations CSV file.

The agent generates a Python adapter. An independent validator runs development and holdout tests. When automatic repair is enabled, development failures can go back to the agent for repair. After manual approval, the workbench uses the exact approved version for patient lookups.

## Run locally

The prototype requires macOS, Python 3.10 or later, and Apple's Python 3.9 with `sandbox-exec` for isolated candidate execution. Install Git LFS before cloning so Git can retrieve the included mock database and architecture PDF.

From the repository root, run these commands. The configuration step accepts an OpenRouter key through a hidden terminal prompt.

```bash
cd local-agent
python3 agent.py doctor
python3 agent.py configure
python3 agent.py serve
```

Open the [local workbench](http://127.0.0.1:8790/). Use the example goal to generate and test an adapter. Review the results, approve a passing version, then look up `P001`. The mock sources start locally as needed. A fresh clone has an empty job history.

## Project guide

- [Local agent setup and commands](local-agent/README.md)
- [Mock sources and test data](mock-systems/README.md)
- [Architecture and editable diagrams](architecture.md)
- [English demo script](demo-script.md)
