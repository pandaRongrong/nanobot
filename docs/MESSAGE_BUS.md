# Message Bus (消息总线)

消息总线位于 `nanobot/bus/`，用于**解耦**频道与 Agent 核心之间的通信。

## 核心结构

```
┌─────────────┐     inbound      ┌──────────┐     outbound     ┌─────────────┐
│   Channel   │ ──────────────▶  │ MessageBus│  ──────────────▶ │   Channel   │
│  (Telegram, │                  │           │                  │ (发送响应)   │
│  Discord...)│                  │           │                  │             │
└─────────────┘                  └───────────┘                  └─────────────┘
```

## 核心代码位置

### Gateway 启动流程

| 文件 | 行号 | 说明 |
|------|------|------|
| `nanobot/cli/commands.py` | 420 | `bus = MessageBus()` 创建实例 |
| `nanobot/cli/commands.py` | 429-444 | `AgentLoop(bus=bus, ...)` 注入 bus |
| `nanobot/cli/commands.py` | 493 | `ChannelManager(config, bus)` 注入 bus |

### 消息流转

```
Telegram/Discord/WhatsApp/...  ──▶  base.py:129 publish_inbound()
                                              │
                                              ▼
                                      inbound Queue (asyncio.Queue)
                                              │
                                              ▼
                                      loop.py:260 consume_inbound()
                                              │
                                              ▼
                                      AgentLoop._process_message()
                                              │
                                              ▼
                                      loop.py:313 publish_outbound()
                                              │
                                              ▼
                                      outbound Queue
                                              │
                                              ▼
                                      manager.py:120 consume_outbound()
                                              │
                                              ▼
                                      channel.send() ──▶ 推送到聊天平台
```

## 核心类

### events.py - 事件模型

#### InboundMessage
- `channel`: 来源频道 (telegram, discord, etc.)
- `sender_id`: 用户标识
- `chat_id`: 会话标识
- `content`: 消息内容
- `media`: 媒体 URL 列表
- `session_key`: 会话 key (自动生成: `channel:chat_id`)

#### OutboundMessage
- `channel`: 目标频道
- `chat_id`: 目标会话
- `content`: 响应内容
- `reply_to`: 回复目标消息 ID
- `media`: 媒体 URL 列表

### queue.py - MessageBus

```python
class MessageBus:
    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage]   # 频道 → Agent
        self.outbound: asyncio.Queue[OutboundMessage] # Agent → 频道

    # 生产者接口
    async def publish_inbound(msg)   # 频道调用
    async def publish_outbound(msg)  # Agent调用

    # 消费者接口
    async def consume_inbound()      # Agent调用
    async def consume_outbound()    # ChannelManager调用
```

## 关键文件

| 文件 | 说明 |
|------|------|
| `nanobot/bus/__init__.py` | 模块导出 |
| `nanobot/bus/events.py` | InboundMessage / OutboundMessage 定义 |
| `nanobot/bus/queue.py` | MessageBus 核心实现 |
| `nanobot/cli/commands.py` | Gateway 启动入口，创建并连接各组件 |
| `nanobot/channels/manager.py` | 频道管理器，消费 outbound 队列 |
| `nanobot/channels/base.py` | 频道基类，publish_inbound 入口 |
| `nanobot/agent/loop.py` | Agent 主循环，消费 inbound，发布 outbound |

## 设计模式

典型的 **生产者-消费者** 模式，通过 `asyncio.Queue` 实现异步解耦。