# 本地医疗系统接入 Agent

这个本地原型用一个 Agent 连接三个现有模拟系统：挂号 REST API、预约 SQLite、检查记录 CSV。**模型通过 OpenRouter 接入，默认 GLM，也可选 Grok。** 网页和命令行共用同一套运行器、验证器和版本批准记录，无需部署 AWS、安装数据库服务或安装 Python 第三方库。

程序需要 Python 3.10+。当前候选代码执行器使用 macOS 的 sandbox-exec 和 Apple Python 3.9 标准库，在本机隔离检查失败时不会直接执行生成代码。

## 本地聊天界面

已经配置过 OpenRouter Key 时，在本目录直接运行：

```bash
python3 agent.py serve
```

打开 [本地聊天界面](http://127.0.0.1:8790/)。如果端口已被占用，可以用 `python3 agent.py serve --port 8791`，并打开终端打印的地址。服务只监听本机，按 `Ctrl+C` 停止。

1. **生成并验证**：输入集成需求，提交后查看任务进度。想让 Agent 在失败时修复，提交前勾选自动修复；默认关闭，启用时最多修复 3 次。
2. **查看结果**：在验证区检查每轮的开发测试、失败差异、留出测试汇总及候选代码。此前通过 CLI 生成的报告也会出现在任务历史中；受控故障验证会有单独标记。
3. **批准与查询**：检查通过的最终代码及版本哈希，显式批准。随后在查询区输入 `P001`，直接运行该批准版本，查看患者、预约和检查记录以及来源。

页面不接触 API Key，模型调用只在后端发生。查看历史、批准和普通查询都不会调用模型。

## 首次配置与命令行运行

在本目录打开本机终端。先检查执行环境：

```bash
python3 agent.py doctor
```

在 [OpenRouter Keys](https://openrouter.ai/keys) 创建或选用你的 API Key，然后运行：

```bash
python3 agent.py configure
```

模型栏直接按回车会选 `z-ai/glm-4.7`。也可以填支持工具调用的 Grok 模型 ID，如 `x-ai/grok-4.3`。接下来在**隐藏输入**处粘贴 Key。不要把 Key 放在命令参数、Git 或聊天里。配置只把模型 ID 写进被 Git 忽略的 `local-config.json`，Key 存在 macOS 钥匙串。若钥匙串要求授权，可在本机确认；如果不想保存 Key，使用 `run --prompt-key`，或由自己的安全终端配置 `OPENROUTER_API_KEY`。

启动一次真实生成和测试任务：

```bash
python3 agent.py run --auto-repair
```

可在单次运行中切换模型或指定受支持的目标：

```bash
python3 agent.py run --model x-ai/grok-4.3 --auto-repair --max-repairs 2 \
  --goal 'Connect the three mock systems and return the complete patient summary by patient ID.'
```

`--auto-repair` 明确授权同一任务中根据失败反馈改候选代码。省略它会在开发测试失败后停止并报告。默认最多修复 3 次、总时限 360 秒，模型调用另有次数和输出长度限制。OpenRouter 真实调用消耗账户用量；程序不会自动换模型、重复提交网络失败的请求或充值。

本地命令会启动并清理它自己使用的临时挂号 API。也可以用 `--api-url http://127.0.0.1:8765` 复用手动启动的模拟 API。SQLite 和 CSV 都来自同一项目的 [mock-systems](../mock-systems/README.md)。

## 闭环如何工作

1. 后端载入 [现有 Skill](../.agents/skills/agentic-healthcare-integration/SKILL.md) 和公开的目标输出契约。模型通过 `inspect_source` 读取三个来源说明，再通过 `submit_candidate` 提交 Python 连接代码和方案摘要。
2. 候选代码定义 `integrate(patient_id, sources)`，在隔离进程中实际调用 API、查询 SQLite、读取 CSV。生成代码只能使用注册的来源，不继承 OpenRouter 凭证。
3. 独立验证器直接读取三个来源，建立本次快照的期望答案。按具体值、数据类型、记录数量、顺序及关联核对。11 个开发用例覆盖 P001 多条记录、同名患者、缺少子记录及不存在的患者。
4. 开发测试失败时，若启用修复，则把有限的差异发回同一个 Agent，保存新候选并重跑整组开发测试。重复代码或重复失败会提前停止。
5. 开发通过后，运行另外 10 个同快照留出患者用例。候选代码无法读取验证器和答案，留出差异也不会交给模型。两组都通过才返回 `PASSED`。这些结果只说明覆盖的合成场景通过，不代表临床可用或已经验证未知数据。

适配器和报告保存在 Git 忽略的 `.runtime/<job-id>/`。报告记录模型、尝试、代码哈希、开发测试差异、留出汇总、来源及契约版本。不会保存 API Key 或模型的原始推理消息。目标 JSON 是本项目的演示格式，不宣称完整兼容 FHIR。

## 人工批准后查询

如果运行返回 `PASSED`，先查看它输出的 `candidate_path` 和 `report_path`。确认后，使用输出中的准确 Job ID 和 SHA-256：

```bash
python3 agent.py approve JOB_ID --code-sha256 CODE_SHA256
python3 agent.py query JOB_ID P001
```

批准绑定当前候选、报告、来源快照、契约和测试版本。修改候选文件后需要重新验证、重新批准。普通查询直接运行已批准的适配器，不调用模型，也不修复代码。

## 离线检查

```bash
python3 -B -m unittest discover -s tests -v
```

离线集成测试用人工准备的故障与正确候选替代云模型响应，执行真实的本地三源读取、隔离、验证、修复分支和批准后查询。它不需要 Key，也不代表 GLM 或 Grok 已经成功生成代码。真实生成用上面的 `run` 验证。

- [OpenRouter 工具调用协议](https://openrouter.ai/blog/tutorials/tool-calling/)
- [GLM 4.7](https://openrouter.ai/z-ai/glm-4.7) · [Grok 4.3](https://openrouter.ai/x-ai/grok-4.3)
