---
name: exl-healthcare-startup
description: Start or troubleshoot the EXL Healthcare Integration workbench on a Mac. Use when setting up a fresh clone, launching the local page and agent, configuring or switching OpenRouter models, or checking the first generation, approval, and patient lookup.
---

# EXL Healthcare Startup

Use the [repository startup guide](../../../README.md#run-locally) for the commands to share with a teammate. The app uses the separate [integration Skill](../agentic-healthcare-integration/SKILL.md) when generating adapters.

## Check the Mac and start the workbench

For a fresh checkout, install Git LFS, run `git lfs install`, clone `https://github.com/sunyisheng655/exl-healthcare-integration.git`, and run `git lfs pull` from the repository root to retrieve the SQLite database. Run the rest of the commands from `local-agent/`.

```bash
python3 --version
python3 agent.py doctor
```

The Python on PATH must be 3.10 or newer. The candidate runner separately requires Apple's `/usr/bin/python3` to resolve to Python 3.9.x and a working `/usr/bin/sandbox-exec`. Check that `doctor` reports `sandbox.available: true` before attempting generation. If the Apple interpreter is missing, check the selected Xcode Command Line Tools and rerun `doctor` after installation. If it reports a different Apple Python version, share the reported reason. Installing a different Python on PATH only changes the interpreter that starts the app.

On first use, run `python3 agent.py configure` in a terminal and enter an OpenRouter API key at the hidden prompt. Press Enter at the model prompt to use `z-ai/glm-4.7`. The key is saved in the Mac's Keychain. `doctor` reports whether the environment variable is set, so `environment_key_configured: false` is compatible with a configured Keychain key.

```bash
python3 agent.py serve
```

Keep the process running and open the address printed in the terminal, normally [http://127.0.0.1:8790/](http://127.0.0.1:8790/), in the same Mac's browser. Verify the page loads and `/api/config` returns the expected model. If the port is occupied, use `python3 agent.py serve --port 8791` and the printed address. The local mock sources start as needed.

## Run a model and verify the first result

- In the browser, click **Use example**, then **Generate integration code**. Enable **Allow automatic repair** before submitting if the user wants the model to revise a candidate after development test failures.
- After a **Passed** job, review the candidate and test counts. For an authorized approval, click **Approve this version**. Use **Look up patient** with `P001` to check the result. Patient lookup executes the approved adapter locally.
- For a different OpenRouter model, choose a GLM or Grok chat model with tool calling. To override the model for the web process, start it with `OPENROUTER_MODEL=x-ai/grok-4.3 python3 agent.py serve`. Use `python3 agent.py run --model x-ai/grok-4.3` for one CLI job. Check the web header or the job report for the model that ran. Generation uses the configured OpenRouter account's credits.
- If a job reports **Blocked** and an OpenRouter timeout, inspect the job reason and whether any candidate was submitted. **Allow automatic repair** applies to failed development tests after candidate submission. For an otherwise healthy setup, start a fresh job when the user wants to retry.

Fresh clones start with empty job history. The IDs in the demo script refer to jobs on the original demo machine.
