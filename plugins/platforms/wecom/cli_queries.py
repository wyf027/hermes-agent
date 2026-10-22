"""Sender-bound, read-only facade over Tencent's official wecom-cli.

The CLI acts as its authorizing human, not the chat's arbitrary speaker. Verify
both identities before each operation and fail closed if its identity format
changes. Bot credentials remain in the CLI's profile-local encrypted store.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MAX_OUTPUT = 2 * 1024 * 1024
COMMANDS = {
    "msg": {"message.aibot.sessions.list": {}},
    "schedule": {
        "calendar.schedules.list": {"begin_time": {"type": "string"}, "end_time": {"type": "string"}},
        "calendar.schedules.get": {"schedule_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20}},
    },
}
DESCRIPTIONS = {
    "message.aibot.sessions.list": "官方 CLI：最近最多20个机器人会话，按最后消息时间排序。无参数；不支持消息正文/完整历史查询。时间过滤和展示条数可在返回后处理。",
    "calendar.schedules.list": "官方 CLI：查询企微日程。必填开始和结束时间，格式 YYYY-MM-DD HH:MM:SS，Asia/Shanghai，范围在当前前后30天内。返回 schedule_list 和 schedule_list_count。",
    "calendar.schedules.get": "官方 CLI：获取日程详情；schedule_ids 必须来自当前查询结果。",
}


def tool_result(data: dict, failed: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": failed}


class WeComCLIQueries:
    def __init__(self, executable: str, config_dir: Path, bot_id: str):
        if not Path(executable).is_absolute() or not config_dir.is_absolute():
            raise ValueError("WeCom CLI paths must be absolute")
        self.executable = executable
        self.config_dir = config_dir
        self.bot_id = bot_id
        self._lock = asyncio.Lock()

    async def _run(self, args: list[str]) -> dict:
        # Do not inherit another profile's access token, endpoint, custom headers,
        # proxy configuration, or logging settings. Never use a shell.
        env = {k: os.environ[k] for k in ("HOME", "PATH", "SYSTEMROOT", "TMPDIR") if k in os.environ}
        env.update(WECOM_CLI_CONFIG_DIR=str(self.config_dir), TZ="Asia/Shanghai", LANG="en_US.UTF-8")
        proc = await asyncio.create_subprocess_exec(
            self.executable, *args, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )

        async def collect():
            output = bytearray()
            while chunk := await proc.stdout.read(65536):
                output.extend(chunk)
                if len(output) > MAX_OUTPUT:
                    raise ValueError("WeCom CLI output limit exceeded")
            await proc.wait()
            return output

        try:
            output = await asyncio.wait_for(collect(), timeout=25)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        data = json.loads(output)
        if not isinstance(data, dict):
            raise ValueError("Unexpected CLI response")
        if proc.returncode and not data.get("error") and not data.get("errcode"):
            data = {"error": {"type": "CLIError", "message": "Official CLI query failed"}}
        return data

    @staticmethod
    def _failed(data: dict) -> bool:
        return bool(data.get("error")) or data.get("errcode", 0) not in (0, None)

    async def query(self, userid: str, action: str, category: str, method: str | None, arguments: dict) -> dict:
        if category not in COMMANDS or action not in {"list", "call"}:
            raise ValueError("Unsupported CLI query")
        if action == "call":
            if method not in COMMANDS[category] or not isinstance(arguments, dict):
                raise ValueError("Only explicit read-only CLI methods are enabled")
            self._validate(method, arguments)
        async with self._lock:
            identity = await self._run(["identity", "whoami"])
            if self._failed(identity):
                return tool_result(self._public(identity), True)
            context = identity.get("extra_identity_context", "")
            ids = re.findall(r"^ID[:：]\s*(\S+)\s*$", context, re.MULTILINE) if isinstance(context, str) else []
            if not self.bot_id or ids != [self.bot_id, userid]:
                return tool_result({"error": "CLI authorization does not match the bot and authenticated sender", "code": "identity_mismatch"}, True)
            if action == "list":
                return {"transport": "wecom-cli", "tools": [
                    {"name": name, "description": DESCRIPTIONS[name], "inputSchema": {
                        "type": "object", "properties": fields, "required": list(fields), "additionalProperties": False,
                    }} for name, fields in COMMANDS[category].items()
                ]}
            result = await self._run([*method.split("."), "--json", json.dumps(arguments, ensure_ascii=False)])
            return tool_result({"transport": "wecom-cli", "method": method,
                                **self._public(result)}, self._failed(result))

    @staticmethod
    def _public(data: dict) -> dict:
        return {k: v for k, v in data.items() if k not in {"extra_identity_context", "security_notice"}}

    @staticmethod
    def _validate(method: str, arguments: dict):
        fields = COMMANDS["msg"].get(method, COMMANDS["schedule"].get(method))
        if set(arguments) != set(fields):
            raise ValueError("Unexpected or missing CLI arguments")
        if method == "calendar.schedules.list":
            tz = ZoneInfo("Asia/Shanghai")
            try:
                begin, end = [datetime.strptime(arguments[k], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
                              for k in ("begin_time", "end_time")]
            except (TypeError, ValueError):
                raise ValueError("Expected local datetime strings") from None
            now = datetime.now(tz)
            if not now - timedelta(days=30) <= begin < end <= now + timedelta(days=30):
                raise ValueError("Calendar query must be within 30 days of now")
        elif method == "calendar.schedules.get":
            ids = arguments["schedule_ids"]
            if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or any(not isinstance(i, str) or not i.strip() for i in ids):
                raise ValueError("Expected one to twenty schedule IDs")
