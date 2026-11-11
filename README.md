# EXL Healthcare Integration

An EXL and Trinity hackathon prototype that combines synthetic patient records from a registration REST API, an appointments SQLite database, and an observations CSV file.

The agent generates a Python adapter. An independent validator runs development and holdout tests. When automatic repair is enabled, development failures can go back to the agent for repair. After manual approval, the workbench uses the exact approved version for patient lookups.

## Run locally

On macOS, use Python 3.10 or later for the workbench. The separate candidate runner uses Apple's `/usr/bin/python3` 3.9 and `sandbox-exec`. Install Git LFS before cloning so Git can retrieve the mock database and architecture PDF. Run `doctor` to check that the candidate sandbox works on your Mac. [Git LFS installation](https://git-lfs.com/)

```bash
git lfs install
git clone https://github.com/sunyisheng655/exl-healthcare-integration.git
cd exl-healthcare-integration/local-agent
python3 --version
python3 agent.py doctor
python3 agent.py configure
python3 agent.py serve
```

Continue when `doctor` reports `sandbox.available: true`. During `configure`, press Enter to select `z-ai/glm-4.7` or enter a GLM or Grok model ID with tool calling, such as `x-ai/grok-4.3`. Paste your API key at the hidden prompt. The command stores the key in macOS Keychain. [Create an OpenRouter key](https://openrouter.ai/keys)

Keep the server running and open the workbench. If port `8790` is busy, run `python3 agent.py serve --port 8791` and use the URL printed in the terminal. [Open the local workbench](http://127.0.0.1:8790/)

1. Select **Use example**. To let the agent repair a candidate that fails development tests, select **Allow automatic repair**.
2. Select **Generate integration code**. A job can take several minutes. Review the candidate and its validation results.
3. For a passing candidate, select **Approve this version**. Enter `P001` in **Patient Lookup** and select **Look up patient**.

The mock sources start locally as needed. Job history stays on the computer where the jobs run, so a fresh clone starts with an empty history.

After setup, start the workbench from `local-agent/` with `python3 agent.py serve`. To change the model for one web session, stop the running workbench with Ctrl+C and restart it with `OPENROUTER_MODEL=x-ai/grok-4.3 python3 agent.py serve`. To select a model for a single CLI job, use `python3 agent.py run --model x-ai/grok-4.3 --auto-repair`. [GLM 4.7](https://openrouter.ai/z-ai/glm-4.7) · [Grok 4.3](https://openrouter.ai/x-ai/grok-4.3)

## Project guide

- [Local agent setup and commands](local-agent/README.md)
- [Startup skill for coding assistants](.agents/skills/exl-healthcare-startup/SKILL.md)
- [Mock sources and test data](mock-systems/README.md)
- [Architecture and editable diagrams](architecture.md)
- [English demo script](demo-script.md)
