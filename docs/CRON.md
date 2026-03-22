# 定时任务（Cron）实现详解

## 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                         Gateway 入口                             │
│                   (commands.py: gateway 命令)                    │
└─────────────────────────────┬───────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌───────────────┐   ┌─────────────────┐   ┌─────────────────┐
│ CronService   │   │   AgentLoop     │   │  ChannelManager │
│ (定时调度)     │   │   (Agent核心)   │   │   (频道管理)    │
└───────┬───────┘   └────────┬────────┘   └─────────────────┘
        │                    │
        │          ┌─────────┴─────────┐
        │          ▼                   ▼
        │   ┌─────────────┐    ┌─────────────┐
        │   │ CronTool    │    │ 其他 Tools   │
        │   │ (工具接口)   │    │             │
        │   └─────────────┘    └─────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│      jobs.json (持久化存储)           │
│   ~/.nanobot/cron/jobs.json          │
└─────────────────────────────────────┘
```

---

## 1. 类型定义

**文件**: `nanobot/cron/types.py`

| 类型 | 说明 |
|------|------|
| `CronSchedule` | 调度参数：`kind` (at/every/cron)、`at_ms`、`every_ms`、`expr`、`tz` |
| `CronPayload` | 任务载荷：`message`、`deliver`、`channel`、`to` |
| `CronJobState` | 运行状态：`next_run_at_ms`、`last_run_at_ms`、`last_status`、`last_error` |
| `CronJob` | 完整任务：id、name、enabled、schedule、payload、state |
| `CronStore` | 任务仓库：version、jobs 列表 |

---

## 2. 调度核心服务

**文件**: `nanobot/cron/service.py`

### 2.1 初始化

```python
# commands.py:424-426
cron_store_path = get_cron_dir() / "jobs.json"
cron = CronService(cron_store_path)
```

### 2.2 启动服务

```python
# commands.py:559
await cron.start()
```

启动时执行 (`service.py:175-182`)：
1. 加载持久化的 jobs.json
2. 计算所有任务的下次执行时间
3. 启动定时器等待

### 2.3 定时器机制

**文件**: `nanobot/cron/service.py:208-225`

```python
def _arm_timer(self) -> None:
    """调度下一次定时器触发"""
    # 获取最近的下次执行时间
    next_wake = self._get_next_wake_ms()  # service.py:200-206

    # 计算延迟（毫秒转秒）
    delay_ms = max(0, next_wake - _now_ms())
    delay_s = delay_ms / 1000

    # 创建异步任务
    async def tick():
        await asyncio.sleep(delay_s)
        if self._running:
            await self._on_timer()

    self._timer_task = asyncio.create_task(tick())
```

### 2.4 触发执行

**文件**: `nanobot/cron/service.py:227-243`

```python
async def _on_timer(self) -> None:
    """定时器触发 - 执行到期的任务"""
    self._load_store()  # 重新加载（支持外部修改）

    now = _now_ms()
    # 找出所有到期的任务
    due_jobs = [
        j for j in self._store.jobs
        if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms
    ]

    for job in due_jobs:
        await self._execute_job(job)  # 执行每个任务

    self._save_store()
    self._arm_timer()  # 重新调度
```

### 2.5 任务执行

**文件**: `nanobot/cron/service.py:245-276`

```python
async def _execute_job(self, job: CronJob) -> None:
    """执行单个任务"""
    start_ms = _now_ms()

    try:
        # 调用回调函数（由 gateway 设置）
        if self.on_job:
            response = await self.on_job(job)
        job.state.last_status = "ok"
    except Exception as e:
        job.state.last_status = "error"
        job.state.last_error = str(e)

    # 处理一次性任务
    if job.schedule.kind == "at":
        if job.delete_after_run:
            # 删除任务
            self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
        else:
            # 禁用任务
            job.enabled = False
    else:
        # 计算下次执行时间
        job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
```

### 2.6 下次执行时间计算

**文件**: `nanobot/cron/service.py:20-46`

```python
def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    """计算下次运行时间"""
    if schedule.kind == "at":
        # 指定时间点
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None

    if schedule.kind == "every":
        # 间隔重复
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        return now_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        # Cron 表达式（使用 croniter 库）
        from croniter import croniter
        base_time = now_ms / 1000
        tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
        base_dt = datetime.fromtimestamp(base_time, tz=tz)
        cron = croniter(schedule.expr, base_dt)
        next_dt = cron.get_next(datetime)
        return int(next_dt.timestamp() * 1000)
```

---

## 3. Agent 工具接口

**文件**: `nanobot/agent/tools/cron.py`

### 3.1 工具定义

```python
# nanobot/agent/tools/cron.py:11-71
class CronTool(Tool):
    """定时任务工具 - 供 Agent 调用"""

    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {"enum": ["add", "list", "remove"]},
                "message": {...},
                "every_seconds": {...},
                "cron_expr": {...},
                "tz": {...},
                "at": {...},
                "job_id": {...},
            }
        }
```

### 3.2 添加任务

**文件**: `nanobot/agent/tools/cron.py:94-144`

```python
def _add_job(self, message, every_seconds, cron_expr, tz, at) -> str:
    # 构建调度参数
    if every_seconds:
        schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
    elif at:
        dt = datetime.fromisoformat(at)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
        delete_after = True

    # 调用 CronService 添加任务
    job = self._cron.add_job(
        name=message[:30],
        schedule=schedule,
        message=message,
        deliver=True,
        channel=self._channel,  # 当前频道
        to=self._chat_id,       # 当前会话
        delete_after_run=delete_after,
    )
```

---

## 4. Gateway 集成

**文件**: `nanobot/cli/commands.py:424-490`

### 4.1 创建 CronService

```python
# commands.py:424-426
cron_store_path = get_cron_dir() / "jobs.json"
cron = CronService(cron_store_path)
```

### 4.2 注册回调

```python
# commands.py:447-476
async def on_cron_job(job: CronJob) -> str | None:
    """定时任务触发时的回调"""
    # 构建触发消息
    reminder_note = (
        f"[Scheduled Task] Timer finished.\n"
        f"Task '{job.name}' has been triggered.\n"
        f"Scheduled instruction: {job.payload.message}"
    )

    # 通过 Agent 执行
    response = await agent.process_direct(
        reminder_note,
        session_key=f"cron:{job.id}",
        channel=job.payload.channel or "cli",
        chat_id=job.payload.to or "direct",
    )

    # 如果需要投递结果到频道
    if job.payload.deliver and job.payload.to and response:
        # 投递逻辑...
        pass

# 注册回调
cron.on_job = on_cron_job
```

### 4.3 启动/停止

```python
# commands.py:559, 574
await cron.start()   # 启动调度
# ... 运行其他服务 ...
cron.stop()          # 停止调度
```

---

## 5. 持久化存储

**路径**: `~/.nanobot/cron/jobs.json` (可通过 `--config` 指定不同实例)

**结构**:
```json
{
  "version": 1,
  "jobs": [
    {
      "id": "abc12345",
      "name": "提醒事项",
      "enabled": true,
      "schedule": {
        "kind": "every",
        "everyMs": 3600000,
        "expr": null,
        "tz": null
      },
      "payload": {
        "kind": "agent_turn",
        "message": "喝水提醒",
        "deliver": true,
        "channel": "cli",
        "to": "direct"
      },
      "state": {
        "nextRunAtMs": 1700000000000,
        "lastRunAtMs": 1699999900000,
        "lastStatus": "ok",
        "lastError": null
      },
      "createdAtMs": 1699999900000,
      "updatedAtMs": 1700000000000,
      "deleteAfterRun": false
    }
  ]
}
```

**加载逻辑**: `nanobot/cron/service.py:78-128`
- 启动时从文件加载
- 检测到文件修改自动重载（支持外部编辑）

---

## 6. 使用示例

通过 Agent 调用的命令：

```
# 每小时执行一次
cron(action="add", message="喝水提醒", every_seconds=3600)

# 每天早上9点执行
cron(action="add", message="每日总结", cron_expr="0 9 * * *", tz="Asia/Shanghai")

# 指定时间执行一次
cron(action="add", message="会议提醒", at="2026-03-25T14:00:00")

# 列出所有任务
cron(action="list")

# 删除任务
cron(action="remove", job_id="abc12345")
```

---

## 7. 关键文件索引

| 文件 | 职责 |
|------|------|
| `nanobot/cron/types.py` | 数据类型定义 |
| `nanobot/cron/service.py` | 调度核心服务 |
| `nanobot/agent/tools/cron.py` | Agent 工具接口 |
| `nanobot/cli/commands.py:424-490` | Gateway 集成逻辑 |
| `nanobot/config/paths.py:27-29` | 路径获取 (`get_cron_dir`) |

---

## 8. 调度类型详解

### 8.1 at - 指定时间执行

适用于一次性任务，在指定时间点触发一次后自动删除或禁用。

```python
schedule = CronSchedule(
    kind="at",
    at_ms=1700000000000  # 毫秒时间戳
)
```

### 8.2 every - 间隔执行

适用于周期性任务，按固定间隔重复执行。

```python
schedule = CronSchedule(
    kind="every",
    every_ms=3600000  # 每小时执行一次
)
```

### 8.3 cron - Cron 表达式

支持标准 5 字段 cron 表达式，支持时区。

```python
schedule = CronSchedule(
    kind="cron",
    expr="0 9 * * *",           # 每天早上9点
    tz="Asia/Shanghai"         # 上海时区
)
```

**Cron 表达式格式**: `分 时 日 月 周`

| 字段 | 取值范围 | 示例 |
|------|----------|------|
| 分 | 0-59 | `0`, `*/5`, `0,30` |
| 时 | 0-23 | `9`, `0-6`, `9,18` |
| 日 | 1-31 | `1`, `15`, `1,15` |
| 月 | 1-12 | `*`, `1-6`, `1,4,7` |
| 周 | 0-6 | `0` (周日), `1-5` (工作日) |

**常用表达式**:
- `0 9 * * *` - 每天早上9点
- `0 9 * * 1-5` - 工作日早上9点
- `*/30 * * * *` - 每30分钟
- `0 0 1 * *` - 每月1号午夜