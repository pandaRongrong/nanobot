# 会话管理系统

## 概述

会话管理模块负责追踪对话历史、持久化存储、管理会话生命周期。

## 核心文件

- `nanobot/session/manager.py` - 主实现

## Session 类

位置: `nanobot/session/manager.py:16-71`

```python
@dataclass
class Session:
    key: str                    # 16行 - 会话标识符 (channel:chat_id)
    messages: list[...]         # 29行 - 消息列表(内存)
    created_at: datetime        # 30行 - 创建时间
    updated_at: datetime        # 31行 - 更新时间
    metadata: dict              # 32行 - 任意元数据
    last_consolidated: int      # 33行 - 已整合的message数量
```

### 关键方法

| 方法 | 行号 | 作用 |
|------|------|------|
| `add_message(role, content, **kwargs)` | 35-44 | 添加消息到会话 |
| `get_history(max_messages=500)` | 46-64 | 获取未整合消息供LLM使用 |
| `clear()` | 66-70 | 清空会话，重置状态 |

## SessionManager 类

位置: `nanobot/session/manager.py:73-214`

```python
class SessionManager:
    def __init__(self, workspace: Path):  # 80-84行
    def _get_session_path(self, key: str) -> Path:  # 86-89行
    def get_or_create(self, key: str) -> Session:   # 96-114行
    def _load(self, key: str) -> Session | None:    # 116-161行
    def save(self, session: Session) -> None:       # 163-180行
    def invalidate(self, key: str) -> None:         # 182-184行
    def list_sessions(self) -> list[dict]:          # 186-213行
```

## 存储格式

JSONL (每行一条JSON):

```jsonl
{"_type": "metadata", "key": "telegram:123", "created_at": "2024-01-01T00:00:00", "last_consolidated": 5}
{"role": "user", "content": "Hello", "timestamp": "2024-01-01T00:00:01"}
{"role": "assistant", "content": "Hi!", "timestamp": "2024-01-01T00:00:02"}
```

- **第一行**: 元数据 (metadata)
- **后续行**: 消息 (messages)

## 路径

| 类型 | 路径 |
|------|------|
| 新位置 | `~/.nanobot/workspace/sessions/{key}.jsonl` |
| 旧位置(迁移) | `~/.nanobot/sessions/{key}.jsonl` |

## 关键设计

### 1. Append-Only 模式

位置: `manager.py:22-25`

消息只追加不修改，保证 LLM 缓存效率。consolidation 过程会将摘要写入 MEMORY.md/HISTORY.md，但不会修改 messages 列表。

### 2. last_consolidated 追踪

位置: `manager.py:33`

标记已写入历史文件的 message 数量，用于增量同步：

```python
unconsolidated = self.messages[self.last_consolidated:]
```

### 3. 用户对齐

位置: `manager.py:51-55`

`get_history()` 丢弃开头非 user 消息，避免孤立的 tool_result：

```python
for i, m in enumerate(sliced):
    if m.get("role") == "user":
        sliced = sliced[i:]
        break
```

### 4. 自动迁移

位置: `manager.py:120-126`

从旧路径自动迁移会话文件到新位置。