"""Keep one MCP client session open on a background event loop, so the synchronous agent loop can call
MCP tools quickly (one server process per run, not per call)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading

from mcp import ClientSession, StdioServerParameters, stdio_client

from . import config


class MCPToolError(RuntimeError):
    pass


class MCPBridge:
    def __init__(self):
        self.params = StdioServerParameters(
            command=sys.executable, args=["-m", "sprint_planner.gitlab_mcp"], cwd=str(config.ROOT),
            env={**os.environ, "GITLAB_MOCK_DB": str(config.MOCK_DB)})
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.session: ClientSession | None = None
        self._stop: asyncio.Event | None = None
        self._done = None

    async def _main(self, ready: threading.Event):
        self._stop = asyncio.Event()
        async with stdio_client(self.params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                self.session = s
                ready.set()
                await self._stop.wait()

    def __enter__(self) -> "MCPBridge":
        self.thread.start()
        ready = threading.Event()
        self._done = asyncio.run_coroutine_threadsafe(self._main(ready), self.loop)
        if not ready.wait(timeout=30):
            raise RuntimeError(f"MCP server did not start: {self._done.exception() if self._done.done() else 'timeout'}")
        return self

    def __exit__(self, *exc):
        if self._stop:
            self.loop.call_soon_threadsafe(self._stop.set)
            try:
                self._done.result(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)

    def call(self, name: str, args: dict | None = None):
        """Call an MCP tool; returns parsed JSON (or text). Raises MCPToolError when the server refuses."""
        fut = asyncio.run_coroutine_threadsafe(self.session.call_tool(name, args or {}), self.loop)
        res = fut.result(timeout=60)
        text = "\n".join(getattr(c, "text", "") for c in res.content)
        if res.is_error:
            raise MCPToolError(text)
        structured = getattr(res, "structured_content", None)
        if isinstance(structured, dict) and "result" in structured and len(structured) == 1:
            return structured["result"]
        if structured:
            return structured
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def tool_names(self) -> list[str]:
        fut = asyncio.run_coroutine_threadsafe(self.session.list_tools(), self.loop)
        return [t.name for t in fut.result(timeout=30).tools]
