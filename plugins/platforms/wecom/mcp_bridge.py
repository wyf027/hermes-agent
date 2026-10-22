"""Read-only WeCom MCP bridge owned by the authenticated gateway connection.

Bot secrets and signed MCP URLs stay in the gateway. Each Codex subprocess
receives a short-lived capability bound to the verified inbound sender, never
an editable userid. The loopback listener is torn down with the adapter.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sys
from pathlib import Path
from urllib.parse import urlparse

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
_BRIDGES: dict[str, "WeComMCPBridge"] = {}
READ_TOOLS = {
    "msg": {"get_msg_chat_list", "get_message"},
    "todo": {"get_todo_list", "get_todo_detail"},
    "contact": {"get_userlist", "get_user_info", "get_current_userid", "get_userid"},
    "schedule": {"get_schedule_list_by_range", "get_schedule_detail", "check_availability"},
}


class WeComMCPBridge:
    def __init__(self, adapter):
        self.adapter = adapter
        self.profile = str(get_hermes_home())
        self.runner = None
        self.url = ""
        self.senders: set[str] = set()
        self.capabilities: dict[str, str] = {}
        self._tokens: dict[str, str] = {}
        self.cli = None
        extra = getattr(getattr(adapter, "config", None), "extra", {}) or {}
        if extra.get("cli_path"):
            from .cli_queries import WeComCLIQueries
            self.cli = WeComCLIQueries(
                str(extra["cli_path"]),
                Path(extra.get("cli_config_dir") or Path(self.profile) / "wecom-cli-config"),
                adapter._bot_id,
            )

    def observe_sender(self, userid: str) -> None:
        if userid:
            self.senders.add(userid)

    def bind(self, userid: str) -> str:
        if userid not in self.senders:
            raise ValueError("No verified WeCom sender for this session")
        token = self._tokens.get(userid)
        if token is None:
            token = secrets.token_urlsafe(32)
            self._tokens[userid] = token
            self.capabilities[token] = userid
        return token

    async def start(self):
        from aiohttp import web
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post("/query", self._handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{port}/query"
        _BRIDGES[self.profile] = self
        logger.info("WeCom read-only MCP bridge ready")

    async def close(self):
        if _BRIDGES.get(self.profile) is self:
            _BRIDGES.pop(self.profile, None)
        self.capabilities.clear()
        self._tokens.clear()
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    async def _handle(self, request):
        from aiohttp import web
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        userid = self.capabilities.get(token)
        if not userid:
            raise web.HTTPUnauthorized()
        try:
            data = await request.json()
            result = await asyncio.wait_for(self.query(userid, data), timeout=60)
            return web.json_response(result)
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid or unsupported read-only WeCom query"}, status=400)
        except Exception as exc:
            # HTTP/MCP exceptions can contain signed URLs. Never send/log them.
            logger.warning("WeCom MCP query failed (%s)", type(exc).__name__)
            return web.json_response({"error": f"WeCom MCP query failed ({type(exc).__name__})"}, status=502)

    async def query(self, userid: str, data: dict) -> dict:
        if not isinstance(data, dict):
            raise ValueError("query must be an object")
        action = data.get("action")
        if action == "user":
            return {"userid": userid, "source": "authenticated_wecom_message"}
        category = data.get("category")
        if category not in READ_TOOLS or action not in {"list", "call"}:
            raise ValueError("unsupported category or action")
        method = data.get("method")
        arguments = data.get("arguments", {})
        if self.cli and category in {"msg", "schedule"}:
            return await self.cli.query(userid, action, category, method, arguments)
        if action == "call" and (method not in READ_TOOLS[category] or not isinstance(arguments, dict)):
            raise ValueError("only known read operations are enabled")
        response = await self.adapter._send_request(
            "aibot_get_mcp_config", {"biz_type": category, "plugin_version": "2026.7.2"}
        )
        if response.get("errcode", 0) not in (0, None):
            return {"errcode": response["errcode"], "category": category,
                    "error": "WeCom MCP configuration request failed"}
        url = (response.get("body") or {}).get("url", "")
        parsed = urlparse(url)
        # Only official, authenticated MCP endpoints may receive this identity.
        if parsed.scheme != "https" or not (
            parsed.hostname == "qyapi.weixin.qq.com"
            or (parsed.hostname or "").endswith(".work.weixin.qq.com")
        ):
            raise ValueError("unexpected WeCom MCP endpoint")
        return await self._remote(url, userid, action, category, method, arguments)

    async def _remote(self, url, userid, action, category, method, arguments):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from tools.url_safety import create_ssrf_safe_async_client
        # A fresh MCP session per operation prevents cross-user session reuse.
        async with create_ssrf_safe_async_client(
            headers={"x-openclaw-wecom-userid": userid, "User-Agent": "HermesWeCom/1.0"},
            timeout=45, follow_redirects=False,
        ) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if action == "call":
                        return (await session.call_tool(method, arguments)).model_dump(mode="json", exclude_none=True)
                    tools = []
                    cursor = None
                    for _ in range(50):
                        result = await session.list_tools(cursor=cursor)
                        tools.extend(t.model_dump(mode="json", exclude_none=True) for t in result.tools
                                     if t.name in READ_TOOLS[category])
                        cursor = result.nextCursor
                        if not cursor:
                            return {"tools": tools}
                    raise ValueError("MCP pagination limit exceeded")


def codex_mcp_settings(env: dict) -> list[str]:
    """Install the WeCom bridge only for a verified WeCom gateway session."""
    if env.get("HERMES_SESSION_PLATFORM") != "wecom":
        return []
    bridge = _BRIDGES.get(str(get_hermes_home()))
    if bridge is None:
        return []
    userid = env.get("HERMES_SESSION_USER_ID", "")
    env["WECOM_MCP_CAPABILITY"] = bridge.bind(userid)
    env["WECOM_MCP_BRIDGE_URL"] = bridge.url
    script = str(Path(__file__).with_name("mcp_server.py"))
    config = {
        "command": sys.executable,
        "args": [script],
        "env_vars": ["WECOM_MCP_CAPABILITY", "WECOM_MCP_BRIDGE_URL"],
        "startup_timeout_sec": 20,
        "tool_timeout_sec": 70,
    }
    # JSON scalar/list values are valid TOML values in -c overrides.
    args = []
    for key, value in config.items():
        args.extend(["-c", f"mcp_servers.wecom.{key}={json.dumps(value)}"])
    return args
