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

### Official CLI for messages and calendar

The legacy WebSocket MCP configuration API can return `846610` for `msg` and
`schedule` even when the corresponding CLI services are authorized. Install
Tencent's official `@wecom/cli` (validated with 1.3.4), initialize it with the
same bot's existing credentials, and verify `auth show --status` reports
`authorized`. Keep its encrypted state inside this Hermes profile:

```sh
npm install --prefix /absolute/profile/wecom-cli @wecom/cli@1.3.4
WECOM_CLI_CONFIG_DIR=/absolute/profile/wecom-cli-config wecom-cli auth init --manual
```

Use the installed CLI's full path for initialization if it is not on PATH. Add
these settings under `platforms.wecom.extra`, alongside `mcp_queries: true`:

```yaml
cli_path: /absolute/path/to/wecom-cli
cli_config_dir: /absolute/profile/wecom-cli-config
```

`cli_config_dir` defaults to `<active Hermes profile>/wecom-cli-config`. For
launchd/services without Node on PATH, use the native executable installed under
`node_modules/@wecom/cli-<platform>-<arch>/bin/wecom-cli`. Restart the gateway.
When configured, only the `msg` and `schedule` categories use the CLI, through
the same sender-bound MCP facade; other categories retain the existing path.

The exposed CLI methods are `message.aibot.sessions.list`,
`calendar.schedules.list`, and `calendar.schedules.get`. No arbitrary command
or write method is accepted. Each query verifies `identity whoami` matches both
the gateway bot and the authenticated sender. Unrecognized identity output or
a different sender fails closed. This CLI route is limited to the authorizing
user, including in group chats. Its child environment excludes inherited CLI
tokens, endpoints, custom headers, and proxies; bot secrets are never in query
arguments. No automatic login or permission expansion happens at query time.

Message queries return up to 20 recent robot sessions, not historical message
bodies. Date filtering and display limits must be applied to those returned
sessions, not interpreted as a full history search. CLI calendar queries cover
the current +/-30-day window; timestamps are interpreted as Asia/Shanghai.
Successful CLI responses may omit `errcode`; use `isError` rather than inventing
one. External failures retain their error code and `isError=true`.

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
Configuration failures carry `isError=true` and the original `errcode`; they
must not be interpreted as a successful empty query or automatically described
as missing authorization.

Initial legacy-MCP acceptance on 2026-10-22: authenticated sender identity succeeded;
`get_todo_list(limit=5)` returned `errcode=0` and an empty first page. The server
describes this todo API as limited to records created by this bot. Configuration
requests for `msg`, `schedule`, and `contact` returned `846610`. Those remote
queries did not work through that legacy path, even when the app's authorization page showed
the corresponding category as authorized. Do not substitute another data source.

Official CLI acceptance on 2026-10-22: `message.aibot.sessions.list` succeeded
with one session; `calendar.schedules.list` succeeded with zero schedules for
2026-10-22 through 2026-10-29 (Asia/Shanghai). Empty schedules are a valid query
result, not evidence that the user's other dates are empty. `contact` remains
on the legacy path and has not been fixed by this change.

A prompt for the existing user/todo queries:

> 使用 wecom_user_info 返回我的 userid，并注明来自已认证消息上下文。
> 然后用 wecom_list_tools(todo) 获取参数，调用 get_todo_list 查询最多 5 条
> 本机器人创建的待办。非空则取详情；说明错误码、分页状态和查询范围。

Example acceptance prompt (replace the dates):

> 仅查询企业微信：获取当前发言人的 userid、查询最近三天机器人会话、
> 查询最多 5 条待办（非空则获取详情）、查询北京时间今天的日程（非空则获取详情）。
> 每项列出实际工具名、是否成功和结果数。不要使用 Google 日历或本地 todo 替代。
> 不发送消息、不创建或修改任何数据。

Protocol reference: Tencent WeCom's official
[`@wecom/wecom-openclaw-plugin` 2026.7.2](https://www.npmjs.com/package/@wecom/wecom-openclaw-plugin/v/2026.7.2),
`dist/src/mcp/transport.js`. Current official plugin versions also offer the
[WeCom CLI](https://github.com/WecomTeam/wecom-cli).
