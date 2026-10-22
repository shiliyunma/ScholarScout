# Messaging platform channel surface

Depth for `hermes-messaging-channels`. The procedure lives in SKILL.md; this file is the per-platform key surface.

## Policy keys (every platform)

`platforms.<name>.extra`: `dm_policy`, `allow_from`, `group_policy`, `group_allow_from`, values `open | allowlist | disabled | pairing`. Per-platform docs page: `$HERMES_HOME/hermes-agent/website/docs/user-guide/messaging/<platform>.md`.

## QQ Bot (Official Bot API v2)

- Adapter: `gateway/platforms/qqbot/` (`adapter.py`, `keyboards.py`, `chunked_upload.py`); secrets `QQ_APP_ID` / `QQ_CLIENT_SECRET` live in `.env`, everything else in `platforms.qqbot.extra`.
- `extra` keys: `app_id`, `client_secret`, `markdown_support` (QQ markdown, msg_type 2), `dm_policy`, `allow_from`, `group_policy`, `group_allow_from`, `stt.{provider,baseUrl,apiKey,model}`.
- Env mirrors: `QQ_ALLOWED_USERS`, `QQ_GROUP_ALLOWED_USERS`, `QQ_ALLOW_ALL_USERS`, `QQBOT_HOME_CHANNEL`, `QQBOT_HOME_CHANNEL_NAME`, `QQ_PORTAL_HOST` (set `sandbox.q.qq.com` for sandbox routing), `QQ_STT_API_KEY`, `QQ_STT_MODEL`.
- Inbound events: `GROUP_AT_MESSAGE_CREATE` (group; the bot must be @-mentioned and the @ is stripped before the model sees the content), `C2C_MESSAGE_CREATE` (DM), guild `AT_MESSAGE_CREATE` / `DIRECT_MESSAGE_CREATE` — guild channels are ACL-checked as group-like.
- Console setup at q.qq.com: enable the C2C / group @-message / guild intents, then invite the bot to the group. Sandbox-mode bots reach only the sandbox test channel.
- Voice: QQ's free `asr_refer_text` first, then the configured STT provider (`glm-asr` via zai by default).
- Home channel for cron and notification delivery: `QQBOT_HOME_CHANNEL`.
- A bot that disconnects immediately usually means bad credentials, a missing intent, or a sandbox-only bot — not an ACL problem.

## Pairing state

`$HERMES_HOME/platforms/pairing/<platform>-approved.json` (openid → `user_name`, `approved_at`), `<platform>-pending.json`, `_rate_limits.json`. `pairing` admits DMs so the handshake can run, but never admits group traffic.

## Adapters sharing `_is_group_allowed`

`weixin.py`, `whatsapp_common.py`, `yuanbao.py`, and every plugin adapter use the same group-policy semantics, so the SKILL.md procedure transfers — only the config venue and inbound event names differ. For Yuanbao group *operations* (group info/members, @mention, stickers) see the `yuanbao` skill instead.

## Toolset defaults

`toolsets.py` maps each platform to `hermes-<platform>`; the messaging bundles are `_bundle(...)` (full core toolset) unless the platform passes an explicit tool list. Narrowing is per-platform in `config.yaml` → `platform_toolsets.<platform>`; read `_HERMES_CORE_TOOLS` in the same file to state exactly what "full access" includes.
