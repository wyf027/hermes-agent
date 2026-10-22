"""Stdio MCP facade for the gateway's sender-bound, read-only WeCom bridge."""
from __future__ import annotations

import os
from typing import Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ToolAnnotations

mcp = FastMCP("wecom", instructions=(
    "Use these tools for 企业微信 queries. First discover the category's schemas, "
    "then query the official WeCom MCP service. User identity is supplied by the "
    "gateway. Never substitute Google Calendar or local agent todos for WeCom data. "
    "Check errcode and isError; an empty successful list is different from a failure. "
    "Fetch details for nonempty todo or schedule ID lists. Use Asia/Shanghai for dates."
))
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
Category = Literal["msg", "todo", "contact", "schedule"]


async def query(data: dict) -> dict:
    url = os.environ["WECOM_MCP_BRIDGE_URL"]
    token = os.environ["WECOM_MCP_CAPABILITY"]
    async with httpx.AsyncClient(timeout=65, trust_env=False) as client:
        response = await client.post(url, json=data, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return response.json()


@mcp.tool(annotations=READ)
async def wecom_user_info() -> dict:
    """获取当前企业微信发言人的 userid，来自网关已认证的消息上下文。"""
    return await query({"action": "user"})


@mcp.tool(annotations=READ)
async def wecom_list_tools(category: Category) -> dict:
    """查看企业微信查询接口及参数。msg=消息会话，todo=待办，contact=用户，schedule=日程。"""
    return await query({"action": "list", "category": category})


@mcp.tool(annotations=READ)
async def wecom_query(category: Category, method: str, arguments: dict) -> CallToolResult:
    """按 wecom_list_tools 返回的 schema 查询企业微信；只允许只读接口。原样保留错误标志。"""
    result = await query({"action": "call", "category": category, "method": method, "arguments": arguments})
    if "content" in result:
        return CallToolResult.model_validate(result)
    import json
    from mcp.types import TextContent
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))], isError=True)


if __name__ == "__main__":
    mcp.run(transport="stdio")
