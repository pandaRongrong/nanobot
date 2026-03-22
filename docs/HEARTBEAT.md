# Heartbeat 模块

## 概述

Heartbeat 模块实现了一个周期性 agent 唤醒服务，让 nanobot 可以在后台主动检查并执行任务。

## 核心功能

HeartbeatService 会定期（默认 30 分钟）检查 `HEARTBEAT.md` 文件，判断是否有待处理的任务。如果有，则触发完整的 agent 循环来执行任务。

## 工作流程

```
┌─────────────────────────────────────────────────────────────┐
│  阶段 1: 决策 (Phase 1 - Decision)                          │
│  • 读取 workspace/HEARTBEAT.md                              │
│  • 调用 LLM 通过 virtual tool 判断 skip/run                │
│  • 如果是 "skip" → 本轮结束                                  │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  阶段 2: 执行 (Phase 2 - Execution)                         │
│  • 仅当 Phase 1 返回 "run" 时触发                           │
│  • 执行 on_execute 回调运行完整 agent 循环                  │
│  • 使用 evaluate_response 评估是否需要通知用户              │
└─────────────────────────────────────────────────────────────┘
```

## 关键设计

| 特性 | 说明 |
|------|------|
| **Virtual Tool Call** | 使用 `_HEARTBEAT_TOOL` 让 LLM 返回结构化的 `skip`/`run` 决策，避免自由文本解析 |
| **两阶段设计** | 决策和执行分离，避免不必要的 LLM 调用 |
| **后评估机制** | 执行完成后用 `evaluate_response` 判断是否需要通知用户 |

---

## 代码实现详解

### 1. 整体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                      HeartbeatService                            │
├──────────────────────────────────────────────────────────────────┤
│  init() → start() → _run_loop() → _tick() → _decide() + execute │
└──────────────────────────────────────────────────────────────────┘
```

### 2. 核心组件

#### 2.1 Virtual Tool Definition

位置: `nanobot/heartbeat/service.py` 第 14-37 行

```python
_HEARTBEAT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat",
            "description": "Report heartbeat decision after reviewing tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "run"],  # 约束返回值
                    },
                    "tasks": {
                        "type": "string",
                        "description": "Natural-language summary of active tasks",
                    },
                },
                "required": ["action"],
            },
        },
    }
]
```

**设计意图**：使用严格的 `enum` 约束，强制 LLM 返回结构化决策，避免自由文本解析的不可靠性。

#### 2.2 HeartbeatService 类

**构造函数参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `workspace` | `Path` | 工作目录，用于定位 HEARTBEAT.md |
| `provider` | `LLMProvider` | LLM 提供者实例 |
| `model` | `str` | 使用的模型名称 |
| `on_execute` | `Callable` | 执行任务回调，接收 tasks 字符串 |
| `on_notify` | `Callable` | 通知用户回调 |
| `interval_s` | `int` | 心跳间隔，默认 30 分钟 |
| `enabled` | `bool` | 是否启用 |

### 3. 核心方法详解

#### 3.1 `_read_heartbeat_file()`

位置: `nanobot/heartbeat/service.py` 第 77-83 行

```python
def _read_heartbeat_file(self) -> str | None:
    if self.heartbeat_file.exists():
        try:
            return self.heartbeat_file.read_text(encoding="utf-8")
        except Exception:
            return None
    return None
```

- 读取 `workspace/HEARTBEAT.md` 文件
- 静默处理异常，返回 `None` 表示跳过本次心跳
- 如果文件不存在或为空，直接跳过

#### 3.2 `_decide()` - Phase 1 决策

位置: `nanobot/heartbeat/service.py` 第 85-106 行

```python
async def _decide(self, content: str) -> tuple[str, str]:
    response = await self.provider.chat_with_retry(
        messages=[
            {"role": "system", "content": "You are a heartbeat agent..."},
            {"role": "user", "content": f"Review HEARTBEAT.md...\n\n{content}"},
        ],
        tools=_HEARTBEAT_TOOL,
        model=self.model,
    )

    if not response.has_tool_calls:
        return "skip", ""  # 无 tool call 默认 skip

    args = response.tool_calls[0].arguments
    return args.get("action", "skip"), args.get("tasks", "")
```

**关键点**：
- 调用 LLM 时传入 `tools` 参数，强制结构化输出
- 使用 `chat_with_retry` 内置重试机制
- 无 tool call 时默认返回 `"skip"`（保守策略）

#### 3.3 `_tick()` - 完整心跳流程

位置: `nanobot/heartbeat/service.py` 第 140-172 行

```python
async def _tick(self) -> None:
    content = self._read_heartbeat_file()
    if not content:
        logger.debug("Heartbeat: HEARTBEAT.md missing or empty")
        return

    # Phase 1: 决策
    action, tasks = await self._decide(content)

    if action != "run":
        logger.info("Heartbeat: OK (nothing to report)")
        return

    # Phase 2: 执行
    if self.on_execute:
        response = await self.on_execute(tasks)

        # Phase 3: 后评估
        if response:
            should_notify = await evaluate_response(
                response, tasks, self.provider, self.model,
            )
            if should_notify and self.on_notify:
                await self.on_notify(response)
```

**四阶段设计**：
1. **读取** → 检查 HEARTBEAT.md
2. **决策** → LLM 判断 skip/run
3. **执行** → 仅当 action=="run"
4. **评估** → 决定是否通知用户

#### 3.4 `_run_loop()` - 后台循环

位置: `nanobot/heartbeat/service.py` 第 128-138 行

```python
async def _run_loop(self) -> None:
    while self._running:
        try:
            await asyncio.sleep(self.interval_s)
            if self._running:  # 检查状态防止过期任务
                await self._tick()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Heartbeat error: {}", e)
```

- 使用 `asyncio.create_task` 在后台运行
- 支持 graceful shutdown (CancelledError)
- 循环中再次检查 `_running` 状态

#### 3.5 `trigger_now()` - 手动触发

位置: `nanobot/heartbeat/service.py` 第 174-182 行

```python
async def trigger_now(self) -> str | None:
    """Manually trigger a heartbeat."""
    content = self._read_heartbeat_file()
    if not content:
        return None
    action, tasks = await self._decide(content)
    if action != "run" or not self.on_execute:
        return None
    return await self.on_execute(tasks)
```

- 用于 CLI 命令手动触发心跳
- 返回执行结果，不做通知决策

### 4. 后评估机制 (evaluator.py)

位置: `nanobot/utils/evaluator.py`

```python
async def evaluate_response(response, task_context, provider, model) -> bool:
    """
    决策逻辑：
    - Notify: 包含可操作信息、错误、完成交付物、用户明确要求提醒的内容
    - Suppress: 常规状态检查、无新内容的确认、几乎为空
    """
```

**fallback 策略**：任何异常都返回 `True`（通知），确保重要消息不丢失。

### 5. 配置文件结构

位置: `nanobot/config/schema.py`

```python
class HeartbeatConfig(Base):
    """Heartbeat service configuration."""
    enabled: bool = True
    interval_s: int = 30 * 60  # 30 分钟

class GatewayConfig(Base):
    host: str = "0.0.0.0"
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
```

---

## 使用指南

### 配置

在 `config.json` 中启用：

```json
{
  "gateway": {
    "heartbeat": {
      "enabled": true,
      "interval_s": 1800
    }
  }
}
```

### HEARTBEAT.md 模板

位置: `nanobot/templates/HEARTBEAT.md`

```markdown
# Heartbeat Tasks

This file is checked every 30 minutes by your nanobot agent.
Add tasks below that you want the agent to work on periodically.

If this file has no tasks (only headers and comments), the agent will skip the heartbeat.

## Active Tasks

<!-- Add your periodic tasks below this line -->


## Completed

<!-- Move completed tasks here or delete them -->
```

---

---

## 心跳机制 vs 定时任务 (Cron)

### 核心区别

| 特性 | 心跳 (Heartbeat) | 定时任务 (Cron) |
|------|----------------|----------------|
| **触发条件** | **事件驱动** - 由 LLM 判断 HEARTBEAT.md 是否有任务 | **时间驱动** - 固定时间/周期执行 |
| **任务定义** | 动态写在 HEARTBEAT.md 文件中 | 存储在 `jobs.json` 中 |
| **任务内容** | 自由格式的 Markdown，LLM 理解后决定是否执行 | 结构化的 job 定义（message + schedule） |
| **执行频率** | 固定间隔（默认 30 分钟） | 任意 cron 表达式、every 间隔、指定时间 |
| **典型场景** | "检查是否有新任务" | "每天 9 点叫我起床" |

### 为什么有 Cron 还需要 Heartbeat？

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    什么时候需要执行？                                    │
├─────────────────────────────────────────────────────────────────────────┤
│                                                  │
│  Cron: "我知道什么时候要执行"  →  精确时间触发                            │
│                                                  │
│  Heartbeat: "不知道什么时候有任务，但需要周期性检查"  →  事件触发                  │
│                                                  │
└─────────────────────────────────────────────────────────────────────────┘
```

**本质区别**：

- **Cron** = **确定性执行**。你明确知道任务内容、触发时间，到点就执行。如「每天 9:00 检查股票行情」「每 5 分钟检查某个 API」。

- **Heartbeat** = **不确定性执行**。你不知道什么时候有任务，需要 agent 周期性主动检查。如「有新需求时处理」「有 bug 时处理」「有新的 GitHub issue 时处理」。

### 典型对比示例

```
# Cron 任务：每天早上 9 点叫我起床
{
  "name": "morning_call",
  "schedule": { "kind": "cron", "expr": "0 9 * * *" },
  "message": "叫我起床"
}

# Heartbeat：检查是否有待处理的 bug
# 在 HEARTBEAT.md 中写入：
## Active Tasks

- 检查 GitHub 仓库是否有新的 bug report，如果有则分析并分类
```

### 互补关系

```
┌─────────────────────────────────────────────────────────────┐
│              需要明确时间/周期？                        │
│           ┌───────────┴───────────┐                    │
│           Yes                No                  │
│           ▼                                     ▼
│    ┌──────────┐      ┌─────────────┐
│    │   Cron   │      │  Heartbeat │
│    └──────────┘      └─────────────┘
│                                   │
│    定时/周期性的    │    事件驱动型的
│    确定性任务    │    不确定性任务
```

### 实际使用场景

| 场景 | 方案 | 原因 |
|------|------|------|
| 每天早上 9 点发送天气 | Cron | 固定时间，确定性执行 |
| 每小时检查服务器状态 | Cron | 固定周期，确定性执行 |
| 有新 GitHub issue 时处理 | Heartbeat | 不知道什么时候有新 issue，需要周期性检查 |
| 有新的用户反馈时处理 | Heartbeat | 事件驱动，不知道什么时候会发生 |
| 每周总结项目进度 | Cron | 固定周期，周复盘 |

---

## 文件清单

| 文件路径 | 说明 |
|----------|------|
| `nanobot/heartbeat/__init__.py` | 模块导出 |
| `nanobot/heartbeat/service.py` | 核心实现 |
| `nanobot/utils/evaluator.py` | 后评估机制 |
| `nanobot/templates/HEARTBEAT.md` | 任务模板 |
| `nanobot/cron/service.py` | 定时任务服务（对比参考） |
| `tests/test_heartbeat_service.py` | 测试用例 |