"""Agent SDK integration for nanobot.

This module provides integration with Claude Agent SDK while keeping
nanobot's existing infrastructure (channels, tools, session management).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Awaitable

from loguru import logger

# Use Anaconda Python where the SDK is installed
if sys.platform == "win32":
    _CONDA_PYTHON = Path("D:/software/anaconda3/python.exe")
    if _CONDA_PYTHON.exists():
        import inspect
        _original_import = __builtins__.__import__

        def _custom_import(name, *args, **kwargs):
            if name == "claude_agent_sdk":
                # Use subprocess to run with conda python
                import subprocess
                result = subprocess.run(
                    [_CONDA_PYTHON, "-c", f"import {name}; print({name}.__file__)"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    sdk_path = result.stdout.strip()
                    # Add path to sys.path temporarily
                    sdk_dir = str(Path(sdk_path).parent)
                    if sdk_dir not in sys.path:
                        sys.path.insert(0, sdk_dir)
            return _original_import(name, *args, **kwargs)

        __builtins__.__import__ = _custom_import

try:
    from claude_agent_sdk import (
        ClaudeSDKClient,
        ClaudeAgentOptions,
        tool as sdk_tool,
        UserMessage,
        ToolUseBlock,
        ToolResultBlock,
        Message,
    )
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    ClaudeSDKClient = None
    ClaudeAgentOptions = None
    sdk_tool = None
    Message = None
    ToolUseBlock = None
    ToolResultBlock = None
    UserMessage = None

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig, WebSearchConfig
    from nanobot.cron.service import CronService


class AgentSDKLoop:
    """
    Agent loop using Claude Agent SDK.

    This replaces the original AgentLoop but maintains compatibility
    with nanobot's channel integrations, tool system, and session management.
    """

    def __init__(
        self,
        bus: MessageBus,
        workspace: Path,
        model: str | None = None,
        max_turns: int = 40,
        context_window_tokens: int = 65_536,
        web_search_config: "WebSearchConfig | None" = None,
        web_proxy: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: "ChannelsConfig | None" = None,
        api_key: str | None = None,
        cli_path: str | None = None,
    ):
        if not _SDK_AVAILABLE:
            raise ImportError("claude_agent_sdk is not installed. Run: pip install claude-agent-sdk")

        self.bus = bus
        self.workspace = workspace
        self.model = model or "claude-sonnet-4-6"
        self.max_turns = max_turns
        self.context_window_tokens = context_window_tokens
        self.web_search_config = web_search_config
        self.web_proxy = web_proxy
        self.exec_config = exec_config or _default_exec_config()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace

        self.sessions = session_manager or SessionManager(workspace)
        self._running = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        self._processing_lock = asyncio.Lock()

        # Agent SDK specific
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._cli_path = cli_path
        self._sdk_client: ClaudeSDKClient | None = None
        self._mcp_servers = mcp_servers or {}

        # Tool converters
        self._nanobot_tools: dict[str, Tool] = {}
        self._sdk_tools: list[Any] = []

    async def start(self) -> None:
        """Initialize the agent and start processing messages."""
        if not self._api_key:
            logger.warning("No ANTHROPIC_API_KEY found, using CLI auth")

        await self._setup_tools()
        self._running = True
        logger.info("Agent SDK loop started")

    async def _setup_tools(self) -> None:
        """Set up tools for the Agent SDK."""
        # Import here to avoid circular imports
        from nanobot.agent.tools.registry import ToolRegistry
        from nanobot.agent.tools.filesystem import (
            EditFileTool, ListDirTool, ReadFileTool, WriteFileTool,
        )
        from nanobot.agent.tools.shell import ExecTool
        from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
        from nanobot.agent.tools.message import MessageTool
        from nanobot.agent.tools.spawn import SpawnTool
        from nanobot.agent.tools.cron import CronTool
        from nanobot.agent.tools.mcp import connect_mcp_servers

        # Create tool registry
        registry = ToolRegistry()
        allowed_dir = self.workspace if self.restrict_to_workspace else None

        # Register nanobot tools
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            tool = cls(workspace=self.workspace, allowed_dir=allowed_dir)
            registry.register(tool)
            self._nanobot_tools[tool.name] = tool

        registry.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            path_append=self.exec_config.path_append,
        ))
        self._nanobot_tools["exec"] = registry.get("exec")

        registry.register(WebSearchTool(config=self.web_search_config, proxy=self.web_proxy))
        registry.register(WebFetchTool(proxy=self.web_proxy))
        self._nanobot_tools["web_search"] = registry.get("web_search")
        self._nanobot_tools["web_fetch"] = registry.get("web_fetch")

        registry.register(MessageTool(send_callback=self.bus.publish_outbound))
        self._nanobot_tools["message"] = registry.get("message")

        registry.register(SpawnTool(manager=None))  # TODO: handle subagents
        self._nanobot_tools["spawn"] = registry.get("spawn")

        if self.cron_service:
            registry.register(CronTool(self.cron_service))
            self._nanobot_tools["cron"] = registry.get("cron")

        # Convert nanobot tools to SDK format
        self._sdk_tools = self._convert_tools_to_sdk(registry)

        # Connect MCP servers if configured
        if self._mcp_servers:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, registry, self._mcp_stack)

    def _convert_tools_to_sdk(self, registry: "ToolRegistry") -> list[Any]:
        """Convert nanobot tools to Claude Agent SDK format."""
        sdk_tools = []

        for tool in registry._tools.values():
            # Create SDK tool wrapper
            @sdk_tool(
                name=tool.name,
                description=tool.description,
                input_schema=tool.parameters,
            )
            async def tool_wrapper(args: dict[str, Any], _tool_name: str = tool.name) -> dict[str, Any]:
                """Wrapper for nanobot tool."""
                nanobot_tool = self._nanobot_tools.get(_tool_name)
                if not nanobot_tool:
                    return {"content": [{"type": "text", "text": f"Tool {_tool_name} not found"}], "is_error": True}

                try:
                    result = await nanobot_tool.execute(**args)
                    return {"content": [{"type": "text", "text": result}]}
                except Exception as e:
                    return {"content": [{"type": "text", "text": f"Error: {str(e)}"}], "is_error": True}

            sdk_tools.append(tool_wrapper)

        return sdk_tools

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks."""
        self._running = True
        await self.start()
        logger.info("Agent SDK loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            cmd = msg.content.strip().lower()
            if cmd == "/stop":
                await self._handle_stop(msg)
            elif cmd == "/restart":
                await self._handle_restart(msg)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(
                    lambda t, k=msg.session_key: (
                        self._active_tasks.get(k, []) and
                        self._active_tasks[k].remove(t)
                        if t in self._active_tasks.get(k, []) else None
                    )
                )

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        content = f"Stopped {cancelled} task(s)." if cancelled else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _handle_restart(self, msg: InboundMessage) -> None:
        """Restart the process."""
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
        ))

        async def _do_restart():
            await asyncio.sleep(1)
            os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

        asyncio.create_task(_do_restart())

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under the global lock."""
        async with self._processing_lock:
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message using Agent SDK."""
        # Handle system messages
        if msg.channel == "system":
            channel, chat_id = (
                msg.chat_id.split(":", 1) if ":" in msg.chat_id
                else ("cli", msg.chat_id)
            )
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            return OutboundMessage(
                channel=channel, chat_id=chat_id,
                content="Background task completed."
            )

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # Handle slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="New session started."
            )
        if cmd == "/help":
            lines = [
                "🐈 nanobot commands:",
                "/new — Start a new conversation",
                "/stop — Stop the current task",
                "/restart — Restart the bot",
                "/help — Show available commands",
            ]
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="\n".join(lines),
            )

        # Build context and run agent
        try:
            result = await self._run_agent_sdk(
                message=msg.content,
                session=session,
                on_progress=on_progress,
            )
        except Exception as e:
            logger.exception("Error running agent SDK")
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=f"Error: {str(e)}",
            )

        # Save session
        self.sessions.save(session)

        if on_progress and result:
            return None

        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=result or "I've completed processing but have no response to give.",
            metadata=msg.metadata or {},
        )

    async def _run_agent_sdk(
        self,
        message: str,
        session: Session,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Run the Agent SDK for a single turn."""
        # Build options
        options = ClaudeAgentOptions(
            tools=self._sdk_tools,
            model=self.model,
            max_turns=self.max_turns,
            cwd=str(self.workspace),
            # TODO: Add system prompt from context builder
            system_prompt=self._build_system_prompt(),
            include_partial_messages=True,
        )

        # Create client and run
        async with ClaudeSDKClient(options=options) as client:
            # Send message and collect response
            final_content = ""

            async for event in client.connect(prompt=message):
                if isinstance(event, Message):
                    # Handle different message types
                    for block in event.content:
                        if hasattr(block, "type"):
                            if block.type == "text" and hasattr(block, "text"):
                                final_content = block.text
                            elif block.type == "tool_use":
                                # Tool is being used - notify progress
                                if on_progress:
                                    await on_progress(f"Using tool: {block.name}")
                            elif block.type == "tool_result":
                                # Tool result received
                                pass

                # Handle progress notifications
                if hasattr(event, "type"):
                    if event.type == "progress":
                        if on_progress and hasattr(event, "message"):
                            await on_progress(event.message)

            return final_content

    def _build_system_prompt(self) -> str:
        """Build system prompt for the agent."""
        from nanobot.agent.context import ContextBuilder
        ctx = ContextBuilder(self.workspace)
        return ctx.build_system_prompt()

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent SDK loop stopping")

    @property
    def tools(self) -> dict:
        """Return tools as a dict-like object for compatibility with cron."""
        return self._nanobot_tools

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(msg, session_key=session_key, on_progress=on_progress)
        return response.content if response else ""


def _default_exec_config() -> "ExecToolConfig":
    """Get default exec tool config."""
    from dataclasses import dataclass

    @dataclass
    class DefaultExecConfig:
        timeout: int = 120
        path_append: str = ""

    return DefaultExecConfig()