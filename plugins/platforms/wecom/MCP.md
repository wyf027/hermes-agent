# WeCom queries inside the Codex runtime

Enable the bridge in the active Hermes profile, then restart its gateway:

```yaml
platforms:
  wecom:
    enabled: true
    extra:
      mcp_queries: true
```

Requires the `mcp` extra and `model.openai_runtime: codex_app_server`.
Bot ID and Secret remain in the existing platform configuration / profile `.env`.
No separate connection, OAuth account, or global Codex configuration is needed.

Only WeCom gateway turns receive the `wecom` MCP server. It exposes:

- `wecom_user_info`: current userid from the authenticated inbound message.
- `wecom_list_tools`: discover query schemas for msg, todo, contact, schedule.
- `wecom_query`: invoke the allowlisted read methods against official MCP.

The gateway obtains MCP addresses through `aibot_get_mcp_config` on its existing
WebSocket. Each remote MCP operation has a new session and passes the verified
sender in `x-openclaw-wecom-userid`. A loopback bridge capability binds that sender
to a Codex child; caller arguments cannot override the identity. Bot credentials
and signed remote URLs are never passed to the model. Changing speakers in a
shared conversation retires the previous Codex child. Capabilities expire on
gateway shutdown. Native Hermes model-loop and webhook-mode support are not
provided by this opt-in bridge.

Writes are intentionally unavailable. A query being listed does not imply its
business permission was granted: check `isError` and the remote `errcode`.
`wecom_user_info` reports message context, not a directory API lookup.

Example acceptance prompt (replace the dates):

> 仅查询企业微信：获取当前发言人的 userid、查询最近三天机器人会话、
> 查询最多 5 条待办（非空则获取详情）、查询北京时间今天的日程（非空则获取详情）。
> 每项列出实际工具名、是否成功和结果数。不要使用 Google 日历或本地 todo 替代。
> 不发送消息、不创建或修改任何数据。

Protocol reference: Tencent WeCom's official
[`@wecom/wecom-openclaw-plugin` 2026.7.2](https://www.npmjs.com/package/@wecom/wecom-openclaw-plugin/v/2026.7.2),
`dist/src/mcp/transport.js`. Current official plugin versions also offer the
[WeCom CLI](https://github.com/WecomTeam/wecom-cli).
