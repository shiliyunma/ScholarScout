# Feishu / Lark channel (verified notes)

Split by confidence: **verified by live call or user transcript**, **read from source**, **unknown**.

## Onboarding sequence (from the user's own successful run)

Entry point is the gateway setup wizard (numbered platform menu), not the top-level setup command.
Choosing the platform offers two paths: *create a new bot automatically (recommended)* or *enter an
existing App ID / App Secret manually*. The automatic path prints:

```
Connecting to Feishu / Lark... done.
Open this URL in Feishu / Lark on your phone:
https://open.feishu.cn/page/launcher?user_code=XXXX-XXXX&from=<app>&tp=<app>
Tip: pip install qrcode  to display a scannable QR code here next time
Fetching configuration results....
✓ Bot created: <bot name>
```

Key readings:

- The wizard **polls** for completion; it does not ask the user to paste credentials back after creating
  the app in the browser.
- `user_code` is **4 uppercase alphanumerics + dash + 4** and is **issued by the platform**
  (the adapter appends `&from=…&tp=…` to a server-provided `verification_uri_complete`).
  The `from` / `tp` parameters' semantics are **unknown** — keep them configurable rather than asserting
  what they mean.
- QR rendering is an optional enhancement (needs an extra package); it must not become a required dependency.

Then three policy questions: DM authorization (pairing approval / allow all / only listed IDs),
group handling (respond only when @-mentioned / disable), and an optional home chat ID for scheduled
sends. The completion block prints App ID, domain and bot name — **never the app secret** — and offers to
restart the gateway.

## Pairing codes (the app's own mechanism, not a platform API)

The user messages the bot, receives a short code, and submits it in the terminal; approval then writes the
sender's **user open id** (`ou_…`) into the allowlist. Source-verified properties: 8 characters from an
alphabet excluding easily confused glyphs, **only the salted hash is stored**, TTL ~1 hour, a small number
of pending codes per platform, lockout after a handful of wrong attempts.

This is the clean way to learn the user's own id — better than temporarily opening the policy and reading
the channel directory. Distinguish it from the bot's own `open_id` (returned by the bot-info endpoint),
which is **not** what goes in a user allowlist.

## Connection modes

- **Long connection (websocket) is the default** and needs **no public address** — the right choice behind
  NAT. Enabling it is what makes the whole flow feel effortless.
- Callback/webhook mode is the alternative and requires a verification token and/or encrypt key plus a
  publicly reachable HTTPS endpoint (tunnel or cloud host).
- The platform SDK is installed lazily by the host app's setup path; an app with a dependency allowlist
  cannot use it and must speak the protocol itself.
- **A standalone launch script must load the secret file itself.** Entry points that go through the app's
  context inherit its `.env` loading; a script that only `import`s modules does not, so the credentials
  reader sees an empty environment — the launcher prints `✓ SDK installed` / `✗ credentials not configured`
  while `.env` is perfectly fine, and exits without ever connecting. Load `.env` at the top of `main()`,
  **not** inside the function the tests stub (otherwise unit tests start reading real credentials).

## Live-verified API shapes

```
POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
     body {"app_id": ..., "app_secret": ...}
  -> {"code": 0, "msg": "ok", "tenant_access_token": "<42 chars>", "expire": 7200}

GET  https://open.feishu.cn/open-apis/bot/v3/info
     header Authorization: Bearer <tenant_access_token>
  -> {"code": 0, "msg": "ok", "bot": {"app_name": "<bot name>", "open_id": "ou_...", ...}}
```

Field-name traps: the token field is `tenant_access_token` (not `token`), its lifetime is `expire` in
**seconds**, the bot's display name is `bot.app_name`, and `bot.open_id` is the **bot's** id. Treat
`code == 0` as the only success signal.

Running that first call with the user's real credentials is the cheapest possible proof that the app
credentials work — and it yields the bot name for free.

## Console-side hard limits (verified — how to re-check each)

| Wanted | Verdict | How it was verified |
|---|---|---|
| Reset the App Secret via API | ❌ none — console only | Walked the SDK's whole `api/application/v6/**` tree: **no secret/reset endpoint**; then searched the reference host app's adapter for `app_secret` / `reset_secret` = **0 hits** (so even the host cannot do it) |
| Create the app | ⚠️ semi-auto: a device-code registration flow issues the link and credentials, the human still confirms | `POST https://accounts.feishu.cn/oauth/v1/app/registration`, `action ∈ {init, begin, poll}`, `archetype=<agent-flavour>, auth_method=client_secret` → returns `device_code` + `verification_uri_complete` |
| Enable the bot capability / subscribe to events / grant scopes / publish a version | ❌ none — console only | Same SDK tree + host adapter search |
| Disconnect a long connection | ❌ no public API (the real semantics are "the process exits") | SDK's `ws/client.py` exposes **only `start()`**; `_disconnect()`/`_reconnect()` are private |
| Connection-health signal | ✅ public hooks exist | `on_reconnecting` / `on_reconnected` on the same client → feed these into the UI's "reconnecting / reconnected" state |

**Consequence for the console checklist:** in long-connection mode the user must **not** fill a callback URL —
that step exists only for webhook mode, and leaving it out is what makes this path feel effortless.

**First-import cost:** the SDK's initial import is slow and **variable** — measured 7.7 s and 116 s on the
same machine on different runs. Never assume a hang; print the warning in the launcher and the docs.

**Stop = kill by PID.** With no disconnect API, "stop the channel" means terminating the process precisely
(Windows/git-bash: `MSYS_NO_PATHCONV=1 taskkill /F /PID <pid>`), and the reconnect hooks are the only health
signal you get.

## Outbound: text must be plain, files have a hard ceiling

- **The platform does not render Markdown.** `**bold**`, `#`, table pipes arrive as literal characters.
  Convert outbound text to plain text (drop heading markers and emphasis, flatten tables row by row,
  keep line breaks) while keeping the code block's indentation. The local UI keeps rendering Markdown —
  one formatter per surface, never one shared string.
- **The converter must be idempotent, so the fence markers have to survive the conversion.** Stripping the
  fence lines and keeping the code body is the trap: the converter gets called twice (a retry, a re-send, a
  helper that normalises first), and on the second pass the code's own `# comment` reads as a heading and
  `| a | b |` as a table — the code gets chewed while every test on a single call still passes. Emit a
  sentinel pair (`CODE_OPEN` / `CODE_CLOSE`) **inside the output** around each code block and return a block
  already wrapped in sentinels unchanged. Test it as a property: idempotence across a table of shapes
  (headings, bold, tables, fences, nested quoting, inline code, empty input) plus random fuzz — not one
  hand-written example.
- **Send a file in two calls**: `POST /open-apis/im/v1/files` (multipart `file_type`, `file`, optional
  `file_name` / `duration`) → `data.file_key`; then a normal message with `msg_type=file` and
  `content={"file_key": …}`.
- **Ceiling is 30 MB and empty files are rejected.** `file_type` is an enum
  (`opus/mp4/pdf/doc/xls/ppt/stream`) and **`stream` accepts any type** — archives, CSV, binaries go there.
  Check the size before uploading and return the reason instead of failing silently.
- **Two scopes plus a publish**: `im:resource` to upload, `im:message` to send; posting as the bot with
  `open_id` also needs `im:message.send_as_bot`. A scope change does nothing until a **version is
  published** — that is the usual "I granted the permission and it still 403s".
- **Proactive messages: DM only.** A group proactive send needs the group *owner* to enable it in the
  client (40034105 when off), so scheduled reminders target a user `open_id`, never a group.
- **Fix the recipient inside the sender.** Build the sender with the target id and let the send function
  take text only; a bug then cannot address the wrong user, and a test can assert the signature has no
  recipient parameter.

## Outbound identity: `open_id` is scoped to the app (live-verified)

- Sending to a user `open_id` that belongs to **another** Feishu app fails with HTTP 400 and body
  `{"code": 99992361, "msg": "open_id cross app"}`. This is the failure mode of pasting an id obtained
  elsewhere (another integration, another client's pairing output, a console copy) into the allowlist.
- Get the id from an inbound event **of this app**: `event.sender.sender_id.open_id`. On inbound from an
  allowlisted sender, upsert/refresh the paired row (`source=inbound`); never write for non-allowlisted
  senders. That makes a wrong-app id structurally unreachable.
- **The refresh cannot heal a wrong id if it sits after the authorization branch.** A stored id from another
  app means the user is *not* allowlisted, so the message is rejected — or acked as a pairing request —
  before the upsert ever runs, and the row stays wrong forever. Two ways out: record the sender **before**
  the branch and keep approval a separate decision (record ≠ approve, so this does not open the allowlist),
  or repair the row by hand. Repair recipe, verified live: read the history of the DM chat
  `GET /open-apis/im/v1/messages?container_id_type=chat&container_id=oc_<chat>&page_size=5&sort_type=ByCreateTimeDesc`
  (scope `im:message:readonly`), take the item whose `sender.id_type == "open_id"` and `id` starts `ou_`,
  `update feishu_paired_users set user_id=? where user_id=?`, then send one real DM and require `code == 0`
  plus a `message_id`.
- **A dead transport and a wrong id look identical to the user** ("I sent a message and nothing happened").
  Diagnose in this order: is the process alive; does its log contain `connected to wss://…` and a fresh
  timestamp (a log frozen days ago means the bot has not been running at all); then the id. Both causes
  were present at once in practice, so fixing one is not enough.
- **Surface the body.** A wrapper that reports only `400` / `bad_request` hides the code above; carry
  `code`/`msg` into the log and the returned outcome. `code == 0` stays the only success signal.
- **Inbound working ≠ outbound working.** Receiving rides the event channel; sending rides
  `im/v1/messages` plus scopes plus an app-scoped id. Send one real DM to the real recipient before
  declaring the channel done — no stub can catch a cross-app id or an unpublished scope change.

## Receiving: acknowledge, deduplicate, and surface

- **Acknowledge immediately — after the allowlist, before the agent turn** ("accepted, processing").
  The platform retries when nothing answers within a few seconds, so dedupe on `message_id` (bounded
  deque + set; 256 is plenty) or the same message runs twice.
- Keep the ack behind the outbound-enabled switch: with outbound off, run silently rather than pretending
  a reply was sent. An ack failure must never fail the turn — log it and continue.
- An inbound message is not "handled" until it reaches the app's own UI as well as its store; see the
  parent skill's pitfall about event-less writes.

## Policy traps

- The default policy for unknown senders is *silent drop* — no error, no log, no reply. "The bot never
  answers" should be diagnosed as policy first, before anything about connectivity.
- An allowlist must be a comma-separated scalar; a JSON array literal lands as a single string and matches
  nothing.
- Switching a policy to allow-all without the matching allow-all opt-in can stop the gateway (and with it
  every other platform) from starting at all.
- Policy changes need a restart: the adapter reads its access-control lists at construction time.

## Porting shape into an app that forbids new dependencies

| Piece | Where it goes |
|---|---|
| Channel module | its own entry-point layer, peer of the desktop UI, allowed to talk only to the RPC surface |
| Inbound transport | **long-connection module (preferred, no public address)**; only if the dependency allowlist forbids the SDK, fall back to a standard-library HTTP callback listener with an HMAC signature check (header lookup case-insensitive) |
| Pairing codes | the app's own store (a migration), hashed, with TTL and lockout |
| Setup wizard | a CLI subcommand and/or a settings card mirroring the reference wizard's question order |
| Self-test | stubbed callback walking the full chain, no credentials, no network — including the negative cases |
| Docs | one design doc with `file:line` evidence for every borrowed mechanism, and an explicit list of what cannot be automated |

The inbound round trip still needs either a public callback address or the long-connection mode; the
console-side app creation always stays manual.
