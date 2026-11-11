# 三个固定模拟系统

本目录提供第一题原型使用的三个真实可运行、数据完全虚构的来源：**REST API + SQLite 数据库 + CSV 文件**。它们分别模拟挂号、预约和检查记录系统，后续 Agent 根据各自的说明生成连接代码。

只需要 Python 3.10 或更新版本，使用 Python 标准库。SQLite 是一个本地文件，不需要安装 MySQL、PostgreSQL、Docker，不需要数据库账号或云服务。

## 已准备的数据

| 来源 | 内容 | 入口 |
| --- | --- | --- |
| A：挂号 API | 20 位模拟患者 P001–P020 | `registration-api/patients.json`；启动后通过 HTTP 读取 |
| B：预约数据库 | 50 条预约 | `appointments-db/appointments.sqlite3` 中的 `appointments` 表 |
| C：检查记录文件 | 80 条模拟观察记录 | `observations-csv/observations.csv` |

三个来源的患者编号值相同，但字段名不同：**API 的 `patient_id` = 数据库的 `person_no` = CSV 的 `PatientID`**。姓名不能作为连接键。

这些是自行生成的合成测试资料，不来自真实患者、医院、Synthea 或企业系统。`DEMO_A/B/C` 和 `arb.units` 只是演示字段，没有临床参考值或诊疗含义。它们也不是完整的 FHIR 资源。

## 1. 启动 A 系统：本地 API

在本目录打开终端并运行：

```bash
python3 serve_registration_api.py
```

默认地址是 `http://127.0.0.1:8765`。终端打印启动 JSON 后保持运行，按 `Ctrl+C` 停止。

API 在启动时载入并检查患者数据快照与接口说明。若之后编辑了 `patients.json` 或 `openapi.json`，需要停止并重新启动 API 才会读到新内容。

| 请求 | 作用 |
| --- | --- |
| `GET /health` | 检查 API 是否运行 |
| `GET /patients` | 获取全部 20 位患者 |
| `GET /patients/P001` | 获取 P001 的挂号信息 |
| `GET /patients/P999` | 返回 404，演示患者不存在 |
| `GET /openapi.json` | 获取接口的机器可读说明 |

浏览器可以直接打开上述地址。也可以使用：

```bash
curl http://127.0.0.1:8765/patients/P001
```

如果端口被已有程序使用，保留那个程序，给本 API 换一个端口：

```bash
python3 serve_registration_api.py --port 8766
```

自定义端口后，应将客户端或来源注册表的 API 地址改成相应端口。默认只监听本机；这是本地模拟来源，不是生产医疗服务。

## 2. 使用 B 系统：SQLite 数据库

数据库文件已经生成，无需再初始化。以下代码在本目录运行，使用只读连接查询 P001 的预约：

```python
from pathlib import Path
import sqlite3

db_uri = Path("appointments-db/appointments.sqlite3").resolve().as_uri() + "?mode=ro"
with sqlite3.connect(db_uri, uri=True) as connection:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT * FROM appointments WHERE person_no = ? ORDER BY scheduled_at, appt_id",
        ("P001",),
    ).fetchall()
    print([dict(row) for row in rows])
```

完整建表和初始数据还保存在 `appointments-db/schema.sql` 与 `appointments-db/seed.sql`，方便检查和复现。

## 3. 使用 C 系统：CSV 文件

`observations-csv/observations.csv` 是 UTF-8 CSV，可直接查看。Python 读取示例：

```python
import csv

with open("observations-csv/observations.csv", encoding="utf-8", newline="") as stream:
    rows = [row for row in csv.DictReader(stream) if row["PatientID"] == "P001"]
print(rows)
```

CSV 中所有值起初都是字符串。`result_value` 的空值代表尚无结果，不应自动转换成数值 0。

## 给 Agent 看哪些资料

- [来源注册表](sources.json)：三个来源的位置、入口、编号字段和版本。
- [字段与业务规则](contracts.md)：枚举、时间、空值、关联和一对多关系。
- [API 说明](registration-api/openapi.json)。
- [SQL 表结构](appointments-db/schema.sql)。
- CSV 的列名与少量样例。

开发目标示例：“连接注册表中的三个来源，按 `patient_id` 输出患者、预约列表和检查记录列表，并保留来源标记。”

当前目录只实现模拟来源，没有实现接入 Agent、统一查询程序、聊天前端、代码批准登记或候选代码沙箱。后续生成的连接代码应实际调用 API、查询 SQLite、读取 CSV，不能把预先合并的答案当作这三个来源。

## 数据中特意保留的场景

- 一位患者有多条预约和多条检查记录，验证是否漏行或产生笛卡尔积。
- P002 与 P003 同名，验证是否按患者编号关联。
- 有患者没有预约、没有检查记录，或两者都没有；空列表仍然是有效情况。
- 有可选字段为空、停用患者、未知性别编码。
- 不同记录可以恰好有相同的观察值；按记录编号识别记录，不能按数值去重。
- `pending` 观察记录允许没有数值，不能冒充一个已完成的检查结果。
- P999 等不存在编号用于未找到场景。

## 检查和重新生成

检查数据关联、源服务行为和可复现性：

```bash
python3 -m unittest discover -s tests -v
```

这些检查验证的是**模拟来源本身**，不是尚未生成的接入代码，也不是 Agent 的效率或准确率评估。

数据由 `generate_data.py` 用固定种子 `20261111` 生成。现有数据已可直接使用。生成器默认拒绝覆盖已有输出；需要实验时先用 `--output-dir` 指定一个新的目录，保留现有版本。只有明确决定重建目标数据时才使用 `--overwrite`。

## 参考

- [Python HTTP server](https://docs.python.org/3/library/http.server.html)：本地模拟 API 使用标准库 HTTP 服务。
- [Python SQLite](https://docs.python.org/3/library/sqlite3.html)：文件数据库和只读连接，无需单独的数据库服务进程。
