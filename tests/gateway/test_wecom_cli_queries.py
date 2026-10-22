"""Exercise the gateway -> CLI subprocess boundary with isolated credentials."""
import json
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from plugins.platforms.wecom.cli_queries import WeComCLIQueries
from plugins.platforms.wecom.mcp_bridge import WeComMCPBridge


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WECOM_CLI_ACCESS_TOKEN", "foreign-token")
    monkeypatch.setenv("WECOM_CLI_ADDITIONAL_HEADERS_TEST", "foreign-header")
    monkeypatch.setenv("WECOM_CLI_BASE_URL", "https://wrong.invalid")
    program = tmp_path / "official-cli-double"
    program.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
assert not any(k.startswith("WECOM_CLI_") and k != "WECOM_CLI_CONFIG_DIR" for k in os.environ)
root = Path(os.environ["WECOM_CLI_CONFIG_DIR"])
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as f:
    f.write(json.dumps(args) + "\\n")
if args == ["identity", "whoami"]:
    print(json.dumps({"extra_identity_context": "机器人身份：\\nID：bot\\n授权真人用户身份：\\nID：alice\\n"}))
elif args[:4] == ["message", "aibot", "sessions", "list"]:
    print(json.dumps({"sessions": [{"chat_name": "Alice"}], "sessions_count": 1, "extra_identity_context": "private"}))
elif args[:3] == ["calendar", "schedules", "list"]:
    print(json.dumps({"schedule_list_count": 0}))
elif args[:3] == ["calendar", "schedules", "get"]:
    print(json.dumps({"error": {"code": 403, "message": "Not permitted"}}))
    sys.exit(1)
else:
    raise RuntimeError("Unexpected command")
''')
    program.chmod(0o700)
    return program


@pytest.mark.asyncio
async def test_live_broker_cli_sender_and_command_boundaries(cli, tmp_path):
    adapter = SimpleNamespace(_bot_id="bot", _send_request=AsyncMock(),
                              config=SimpleNamespace(extra={"cli_path": str(cli), "cli_config_dir": str(tmp_path)}))
    bridge = WeComMCPBridge(adapter)
    await bridge.start()
    try:
        for sender in ("alice", "bob"):
            bridge.observe_sender(sender)
        async with httpx.AsyncClient(trust_env=False) as client:
            async def query(sender, **data):
                return await client.post(bridge.url, headers={"Authorization": f"Bearer {bridge.bind(sender)}"}, json=data)
            response = await query("alice", action="list", category="msg")
            assert response.json()["tools"][0]["name"] == "message.aibot.sessions.list"
            response = await query("alice", action="call", category="msg", method="message.aibot.sessions.list", arguments={})
            result = response.json()
            assert result["isError"] is False
            data = json.loads(result["content"][0]["text"])
            assert data["sessions_count"] == 1
            assert data["transport"] == "wecom-cli"
            assert "extra_identity_context" not in data
            response = await query("bob", action="call", category="msg", method="message.aibot.sessions.list", arguments={}, userid="alice")
            assert response.json()["isError"] is True
            assert "identity_mismatch" in response.text
            calls = (tmp_path / "calls.jsonl").read_text()
            assert calls.count('"sessions"') == 1  # Bob never reaches the business API.
            for method, arguments in (("message.aibot.send", {}), ("message.aibot.sessions.list", {"userid": "alice"})):
                response = await query("alice", action="call", category="msg", method=method, arguments=arguments)
                assert response.status_code == 400
            assert (tmp_path / "calls.jsonl").read_text() == calls
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            response = await query("alice", action="call", category="schedule", method="calendar.schedules.list", arguments={
                "begin_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": (now + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
            })
            assert response.json()["isError"] is False
            assert "schedule_list_count" in response.text
            response = await query("alice", action="call", category="schedule", method="calendar.schedules.get", arguments={"schedule_ids": ["denied"]})
            assert response.json()["isError"] is True
            assert "403" in response.json()["content"][0]["text"]
        adapter._send_request.assert_not_called()
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_wrong_bot_fails_closed(cli, tmp_path):
    reader = WeComCLIQueries(str(cli), tmp_path, "different-bot")
    result = await reader.query("alice", "list", "msg", None, {})
    assert result["isError"] is True
    assert "identity_mismatch" in result["content"][0]["text"]


@pytest.mark.parametrize("method,arguments", [
    ("calendar.schedules.list", {"begin_time": "2020-01-01 00:00:00", "end_time": "2020-02-01 00:00:00"}),
    ("calendar.schedules.list", {"begin_time": 5, "end_time": 10}),
    ("calendar.schedules.list", {"begin_time": "2026-10-22 00:00:00"}),
    ("calendar.schedules.get", {"schedule_ids": "not-an-array"}),
    ("calendar.schedules.get", {"schedule_ids": []}),
])
def test_invalid_calendar_requests_never_spawn(method, arguments):
    with pytest.raises(ValueError):
        WeComCLIQueries._validate(method, arguments)
