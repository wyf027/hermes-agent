"""Exercise actual loopback + stdio MCP I/O with isolated profile state."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from plugins.platforms.wecom.mcp_bridge import WeComMCPBridge, codex_mcp_settings


@pytest.mark.asyncio
async def test_sender_capabilities_and_read_only_enforcement(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = SimpleNamespace(_send_request=AsyncMock(return_value={
        "body": {"url": "https://mcp.work.weixin.qq.com/test"}, "errcode": 0,
    }))
    bridge = WeComMCPBridge(adapter)
    bridge._remote = AsyncMock(return_value={"content": [], "isError": False})
    await bridge.start()
    try:
        bridge.observe_sender("alice")
        bridge.observe_sender("bob")
        alice, bob = bridge.bind("alice"), bridge.bind("bob")
        assert alice != bob
        with pytest.raises(ValueError):
            bridge.bind("unknown")
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(bridge.url, json={"action": "user"})
            assert response.status_code == 401
            for token, expected in ((alice, "alice"), (bob, "bob")):
                headers = {"Authorization": f"Bearer {token}"}
                response = await client.post(bridge.url, headers=headers, json={"action": "user", "userid": "forged"})
                assert response.json()["userid"] == expected
                response = await client.post(bridge.url, headers=headers, json={
                    "action": "call", "category": "todo", "method": "get_todo_list", "arguments": {},
                })
                assert response.status_code == 200
                assert bridge._remote.call_args.args[1] == expected
                response = await client.post(bridge.url, headers=headers, json={
                    "action": "call", "category": "todo", "method": "delete_todo", "arguments": {},
                })
                assert response.status_code == 400
        assert adapter._send_request.await_count == 2
        env = {"HERMES_SESSION_PLATFORM": "wecom", "HERMES_SESSION_USER_ID": "alice"}
        args = codex_mcp_settings(env)
        assert env["WECOM_MCP_CAPABILITY"] == alice
        assert alice not in " ".join(args)
        assert codex_mcp_settings({"HERMES_SESSION_PLATFORM": "telegram"}) == []
    finally:
        await bridge.close()
    assert not bridge.capabilities


@pytest.mark.asyncio
async def test_stdio_facade_preserves_remote_error_and_schema(tmp_path, monkeypatch):
    from pathlib import Path
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    bridge = WeComMCPBridge(SimpleNamespace())
    bridge.query = AsyncMock(side_effect=lambda userid, data: (
        {"userid": userid} if data["action"] == "user" else
        ({"errcode": 846610, "error": "WeCom MCP configuration request failed"}
         if data["category"] == "msg" else {"tools": []}) if data["action"] == "list" else
        {"content": [{"type": "text", "text": '{"errcode":403}'}], "isError": True}
    ))
    await bridge.start()
    try:
        bridge.observe_sender("alice")
        token = bridge.bind("alice")
        script = Path(__file__).parents[2] / "plugins/platforms/wecom/mcp_server.py"
        params = StdioServerParameters(command=sys.executable, args=[str(script)], env={
            "WECOM_MCP_BRIDGE_URL": bridge.url, "WECOM_MCP_CAPABILITY": token,
        })
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name: tool for tool in listed.tools}
                assert "wecom_user_info" in names
                assert "userid" not in names["wecom_query"].inputSchema["properties"]
                result = await session.call_tool("wecom_user_info", {})
                assert "alice" in result.content[0].text
                result = await session.call_tool("wecom_list_tools", {"category": "msg"})
                assert result.isError is True
                assert "846610" in result.content[0].text
                result = await session.call_tool("wecom_list_tools", {"category": "todo"})
                assert result.isError is False
                result = await session.call_tool("wecom_query", {"category": "todo", "method": "get_todo_list", "arguments": {}})
                assert result.isError is True
                assert "403" in result.content[0].text
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_endpoint_rejects_non_wecom_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = SimpleNamespace(_send_request=AsyncMock(return_value={"body": {"url": "https://attacker.invalid/mcp"}}))
    bridge = WeComMCPBridge(adapter)
    with pytest.raises(ValueError):
        await bridge.query("alice", {"action": "list", "category": "todo"})
