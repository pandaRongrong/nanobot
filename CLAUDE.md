# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**nanobot** is an ultra-lightweight personal AI assistant that connects to multiple chat platforms (Telegram, Discord, Feishu, Slack, QQ, DingTalk, WhatsApp, Email, WeCom, Matrix, Mochat) and LLM providers (OpenRouter, OpenAI, Claude, Azure, Ollama, vLLM, etc.).

## Common Commands

```bash
# Development setup
pip install -e .                    # Install in editable mode
pip install -e ".[dev]"              # Install with dev dependencies

# Run tests
pytest                               # Run all tests
pytest tests/test_name.py            # Run specific test file
pytest -k "test_function_name"       # Run specific test function
pytest --tb=short                    # Short traceback format

# Lint
ruff check nanobot/                  # Check for linting issues
ruff format nanobot/                 # Format code

# CLI commands
nanobot onboard                      # Initialize config & workspace
nanobot agent -m "Hello"             # Chat with the agent (CLI mode)
nanobot gateway                      # Start the gateway (connects to chat channels)
nanobot status                       # Show status
nanobot channels login               # Link WhatsApp (scan QR)
nanobot channels status              # Show channel status
nanobot provider login openai-codex  # OAuth login for providers
```

## Architecture

### Core Components

- **`agent/`** — Core agent logic: loop (LLM ↔ tool execution), context builder, memory, skills, subagent manager
- **`channels/`** — Chat platform integrations (auto-discovered). Implement `BaseChannel` to add new platforms
- **`providers/`** — LLM provider abstraction via LiteLLM. Registry pattern in `registry.py` — add new providers by adding `ProviderSpec` entries
- **`bus/`** — Message routing: `MessageBus` connects channels to the agent
- **`cron/`** — Scheduled task execution
- **`heartbeat/`** — Periodic wake-up to check `HEARTBEAT.md` for proactive tasks
- **`session/`** — Conversation session management with history
- **`config/`** — Configuration loading and schema validation
- **`cli/`** — Typer-based CLI commands

### Key Patterns

**Provider Registry** (`nanobot/providers/registry.py`): Adding a new LLM provider requires two steps:
1. Add a `ProviderSpec` to `PROVIDERS` tuple with metadata (keywords, env vars, prefixing, etc.)
2. Add a field to `ProvidersConfig` in `config/schema.py`

**Channel Auto-Discovery** (`nanobot/channels/registry.py`): Channels are auto-discovered by scanning the package. Each channel module should contain a `BaseChannel` subclass.

**Message Bus** (`nanobot/bus/`): `InboundMessage` and `OutboundMessage` events flow through `MessageBus` — channels receive `OutboundMessage` to send, emit `InboundMessage` to the agent.

### Configuration

- Config file: `~/.nanobot/config.json` (or custom via `--config`)
- Workspace: `~/.nanobot/workspace/` (or custom via `--workspace`)
- Multiple instances: Use `--config` to run separate bots with different configs/ports

## Adding New Providers

See the extensive docstring in `nanobot/providers/registry.py` — it documents every `ProviderSpec` field with examples. Key fields:
- `litellm_prefix`: Auto-prefix model names (`"dashscope"` → `"dashscope/qwen-max"`)
- `skip_prefixes`: Don't prefix if model already starts with these
- `is_gateway`: Can route any model (like OpenRouter)
- `is_direct`: Bypasses LiteLLM entirely (for custom endpoints)

## Adding New Channels

See `.docs/CHANNEL_PLUGIN_GUIDE.md`. Implement `BaseChannel` abstract methods: `start()`, `stop()`, `send()`, `_handle_message()`.

## Testing Notes

- Tests are in `tests/` using pytest with `asyncio_mode = "auto"`
- Many tests use mocked responses — check fixtures and mocks before modifying
- Run single tests frequently during development

## Claude Agent SDK Integration

nanobot can optionally use Claude Agent SDK instead of LiteLLM for agent execution. This provides access to Claude's built-in tools and agent capabilities.

### Enabling Agent SDK

Add to `~/.nanobot/config.json`:
```json
{
  "gateway": {
    "useAgentSdk": true
  },
  "agents": {
    "defaults": {
      "model": "claude-sonnet-4-6"
    }
  }
}
```

### Installation

```bash
pip install claude-agent-sdk
```

### Tool Migration

| Tool | Status |
|------|--------|
| File tools (read_file, write_file, etc.) | Keep nanobot's |
| Shell execution (exec) | Keep nanobot's |
| Web search/fetch | Keep nanobot's |
| Message sending | Keep nanobot's |
| Cron scheduling | Keep nanobot's |
| MCP tools | Keep nanobot's |

The Agent SDK mode reuses nanobot's existing tool implementations rather than replacing them with SDK tools.