# 模拟来源字段与业务规则 v1

本契约由本项目定义，适用于随附的 20 位患者、50 条预约和 80 条观察记录。全部数据虚构，无临床或真实个人含义。后续字段或含义改变时更新来源契约版本；运行器不应隐式套用旧批准。

## 来源间的身份对应

| A：REST API | B：SQLite | C：CSV | 含义 |
| --- | --- | --- | --- |
| `patient_id` | `person_no` | `PatientID` | 相同的合成患者编号，如 P001 |

编号大小写敏感。使用编号精确关联，不根据姓名、性别或生日猜测。P002 与 P003 的显示名相同，但代表两个不同的模拟患者。

API 中存在患者时，即使没有预约或检查记录也应保留该患者；缺少子记录应体现为空列表。患者不存在与数据源请求失败必须区分，失败不能假装为空列表。

## A：registration-api

患者原始数据保存在 `registration-api/patients.json`，格式为 JSON 数组。API 的列表响应将其包装为 `{"items": [...], "total": 20}`，单条接口直接返回患者对象。

| 字段 | 类型／格式 | 规则 |
| --- | --- | --- |
| `patient_id` | 字符串 | 唯一，初始资料为 P001–P020 |
| `display_name` | 非空字符串 | 显示用，不唯一，不作为关联键 |
| `middle_name` | 字符串或 JSON `null` | 可缺省内容；`null` 不等于空字符串或文字 `"null"` |
| `birth_date` | `YYYY-MM-DD` 或 JSON `null` | 可无已知生日；不能用猜测值补全 |
| `gender_code` | `F` / `M` / `O` / `U` | 本项目定义：female / male / other / unknown；不推断性别 |
| `active` | JSON 布尔值 | `false` 表示停用，仍是一个存在的患者；不能自动过滤丢弃 |
| `updated_at` | ISO 8601 UTC，后缀 `Z` | 记录更新时间，不能当成就诊时间 |

API 路径：

- `/health`：来源健康状态，不是患者数据。
- `/patients`：全部患者，返回 `items` 和 `total`。
- `/patients/{patient_id}`：按编号精确查找，成功返回 200，未找到返回 404 与 `PATIENT_NOT_FOUND`。
- `/openapi.json`：接口契约。未实现的路径返回 404；写操作不支持。

## B：appointments-db

文件：`appointments-db/appointments.sqlite3`。表：`appointments`。SQL 建表文件和完整初始 INSERT 资料一并提供。

| 字段 | SQLite 类型 | 规则 |
| --- | --- | --- |
| `appt_id` | TEXT | 预约主键，唯一，不按时间或患者号去重 |
| `person_no` | TEXT | 对应 A 的 `patient_id`；跨系统关联，数据库内部没有跨文件外键 |
| `scheduled_at` | TEXT | ISO 8601 UTC `Z` 时间；这是预约时间 |
| `department_code` | TEXT | `GP`、`RAD`、`LAB`，分别表示演示用全科、放射、检验部门 |
| `visit_type` | INTEGER | `1` 为 in_person，`2` 为 video；不是字符串布尔值 |
| `status_code` | TEXT | `B` 为 booked，`C` 为 cancelled，`D` 为 done |
| `updated_at` | TEXT | 预约记录更新时间 |

一位患者可以有多条预约。查询“所有详情”默认保留三种状态，由输出契约明确是否另行筛选。不要把取消记录误当作缺失，也不要把 appointment 的主键当作患者编号。

推荐读取：

```sql
SELECT appt_id, person_no, scheduled_at, department_code, visit_type, status_code, updated_at
FROM appointments
WHERE person_no = ?
ORDER BY scheduled_at, appt_id;
```

客户端以 URI `mode=ro` 打开 SQLite。候选代码不获得修改来源数据的权限。

## C：observations-csv

文件：`observations-csv/observations.csv`。编码 UTF-8，首行为列名。列名大小写按下表保留。

| 列 | CSV 文本内容 | 规则 |
| --- | --- | --- |
| `obs_id` | 唯一字符串 | 观察记录主键 |
| `PatientID` | 患者编号 | 对应 A 的 `patient_id`，保留精确值 |
| `test_code` | `DEMO_A` / `DEMO_B` / `DEMO_C` | 演示用代码，不属于真实临床术语集 |
| `result_value` | 十进制数字字符串或空 | 用十进制语义读取；空值表示尚无结果，不是数值 0 |
| `result_unit` | `arb.units` 或空 | 演示用任意单位；无临床换算关系 |
| `observed_at` | ISO 8601 UTC `Z` | 观察记录时间 |
| `status` | `final` / `pending` | pending 记录可无数值；不可将其改成 final |

两条不同 `obs_id` 的数值可以相同，仍是两条记录。不要按 `result_value` 去重。读取时保留 pending 条目及其状态；如果输出契约要求过滤，应记录明确规则。

## 读取与归并约定

1. 三个来源按同一合成患者编号对应，预约和观察均为一对多。
2. 先分别取得预约列表和观察列表，再放入同一患者记录。避免把 3 条预约与 5 条观察交叉连接成 15 行。
3. 返回列表建议按“业务时间、记录编号”排序；验证时应事先约定有序还是无序比较。
4. 空字符串、JSON null、空列表、未知枚举和来源执行失败是不同情况，不应相互替代。
5. 示例编号 P999 不在来源数据中。不存在的患者应有明确未找到状态，不能由 Agent 编造患者信息。
6. 源记录可以相同数值、相同显示名，但不同记录主键仍应保留。完整性由独立验证器计算，不由 Agent 自报。

上述规则是公开的来源契约，可以给 Agent 阅读。后续正式验收的期望答案、验证器代码和留出答案应放在独立评估环境，不能因本目录包含 mock 数据就把这些答案暴露给候选程序。
