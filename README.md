# 回拨承诺管理系统（Callback Promise）

客服当场无法处理咨询时，向客户**承诺回拨时刻**并登记：问题摘要、可接听时段、
负责坐席、原始会话。系统用 SQLite 保存追加式时间线；电话/短信均为**模拟适配器**，
录音只是本地虚构文件。纯 Python 标准库实现，无任何第三方依赖。

## 一条命令启动

```bash
python3 run.py
# → 重置演示库、灌入含“逾期样例”的种子数据、启动 Web
#   http://127.0.0.1:8000/queue      坐席视图
#   http://127.0.0.1:8000/supervisor 主管视图（右上角切换身份）
```

可选参数与环境变量：

```bash
python3 run.py --port 8123        # 指定端口
python3 run.py --keep-db          # 保留现有数据库，仅启动
python3 run.py --no-serve         # 只生成样例库
CALLBACK_PORT=9000 python3 run.py # 也可用环境变量（HOST/PORT/DATA_DIR）
```

测试：

```bash
python3 -m unittest discover -s tests -v
```

## 演示账号（种子数据）

| 身份 | 姓名 | 说明 |
|---|---|---|
| 坐席 | 林晓 | 名下有即将到期、逾期未回拨、拨错号争议等承诺 |
| 坐席 | 陈默 | 已请假，名下承诺已转交值班组 |
| 值班组 | 周岚 | 承接请假坐席的未完成承诺 |
| 主管 | 高远 | 可裁决争议、更正号码、重开承诺 |

种子数据预置的场景：即将到期（实时倒计时）、**逾期未回拨 #2**、号码拨错争议 #3、
客户拒接、逾期后才接通并登记结果、未接通后有依据改约、其他渠道解决、重复创建、
坐席请假→值班组责任交接。

## 业务规则与对应实现

| 需求 | 规则落点 |
|---|---|
| 承诺登记四要素 | `services.create_promise`：摘要/原始会话/可接听时段均必填 |
| 承诺不得落入禁扰时段 | `timeutil.in_quiet_hours`，支持跨午夜（如 22:00–08:00），区间左闭右开；创建与改约都校验 |
| 回拨完成填结果与下一步 | `services.record_outcome`：必须先有一次“已接通”呼叫，结果/下一步必填 |
| 不接通→有依据再约 | `services.reschedule`：必须先有 `no_answer` 呼叫 + 写明依据（短信改约/时段调整/值班安排），新时刻同样校验禁扰，旧承诺置 `rescheduled` 并由新承诺承接 |
| 号码拨错 | `dial_promise(result="wrong_number")` → 升级 `dispute`，不闭环、不丢弃，等主管核实号码 |
| 客户拒接 | `customer_refused` → `closed_refused`，区别于正常完成 |
| 其他渠道已解决 | `close_resolved_elsewhere`：必须填渠道凭据/工单号 → `closed_resolved` |
| 重复创建 | 部分唯一索引 `ux_active_promise` 保证每客户至多一条 open/dispute；误入的重复单用 `close_as_duplicate` 并入有效承诺 |
| 超过承诺时限 | `services.mark_overdue` 幂等扫描，置 `overdue` 并追加“判定逾期”事件；逾期记录**不能删除**，仍须接通补登记或由主管裁决 |
| 坐席请假转交 | `agent_go_on_leave`：名下 open/dispute 承诺一次性转交值班组并逐条留痕 |
| 并发转派唯一负责人 | `transfer_promise` 用**乐观锁 CAS**（`version` + `BEGIN IMMEDIATE`）；竞争失败抛 `TransferConflict` 并记录失败尝试，不会产生两个有效负责人 |
| 主管权限 | 关闭/重开争议、更正号码均要求 `role='supervisor'`；坐席操作返回 409。系统**没有删除承诺的接口** |

## 报表（顶部导航）

- **承诺队列** `/queue`：按承诺时刻排序、逾期置顶，卡片显示每秒刷新的倒计时
- **客户时段** `/customers`：可接听时段 + 每日禁扰时段
- **拨打记录** `/calls`：模拟通话结果与本地虚构录音路径
- **转派记录** `/transfers`：成功交接与竞争失败尝试（请假交接有专门原因）
- **主管视图** `/supervisor`：失约 KPI、**失约原因**（拨错/拒接/逾期）、**责任交接链**、争议裁决入口与裁决留痕
- 客户页 `/customer?id=`：**客户历史与当前承诺同屏**，下方为完整追加式时间线

## 模拟适配器（不接入真实通讯）

- `adapters.MockPhone.dial`：只往 `call_log` 写轨迹；“已接通”时在 `data/recordings/`
  生成一个占位文本文件（非真实音频）。
- `adapters.MockSMS.send`：预约/改约通知只写入 `sms_outbox` 表。

## 数据模型要点

- `promises`：承诺主表，`owner_agent_id` 为唯一有效负责人，`version` 为乐观锁版本，
  `overdue` 为逾期标记（独立于状态）。
- 状态机：`open → fulfilled / rescheduled / closed_resolved / closed_duplicate /
  closed_refused / dispute → closed_dispute`（主管也可重开回 `open`）。
- `timeline_events`：所有状态变化只追加事件，责任交接与逾期判定全程可追溯。
- 表：`agents, customers, promises, calls, outcomes, reschedules, transfers,
  dispute_reviews, timeline_events`（另加模拟用 `call_log, sms_outbox`）。

## 目录结构

```
callback/
  config.py     路径/端口配置（环境变量可覆盖）
  db.py         SQLite 连接、建表、事务、时间线
  timeutil.py   时间解析、跨午夜禁扰判定、倒计时文案
  adapters.py   模拟电话/短信/本地虚构录音
  services.py   全部业务规则与报表查询
  seed.py       含逾期样例的演示数据
  web.py        标准库 http.server 界面（白/墨灰/琥珀）
  errors.py     业务错误类型
tests/
  test_rules.py 禁扰、逾期、转派竞争（含多线程）、主管权限等 29 例
  test_web.py   HTTP 权限与状态码冒烟 6 例
run.py          单命令入口
```
