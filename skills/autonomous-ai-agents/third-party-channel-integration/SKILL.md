---
name: third-party-channel-integration
description: "Use when wiring a messaging channel into a local agent."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [channels, gateway, feishu, wechat, qq, integration]
    category: autonomous-ai-agents
---

# Wiring a third-party messaging channel into a local agent

The user asks to "接入 X" and means two different things — ask which before building:
**drive Hermes from X** (use the existing Hermes platform adapter; configuration work, minutes) or
**give my own app an X channel** (a real subsystem, with thesis/product value). Often both, in that order.

Platform-specific facts (endpoints, IDs, payload shapes, policies) live in `references/<platform>.md`
— `feishu.md`, `wechat.md`, `qq.md`. Every one of those files records **how its facts were verified**
and which are second-hand; re-check before promising a limit, because platforms change policy.

## Procedure

1. **Ask for the user's own successful onboarding transcript first.** If they have already connected the
   platform anywhere (e.g. in Hermes), their terminal session is authoritative: entry command, every
   prompt, the exact URL, the code formats, the success lines. It outranks reading source and cannot be
   wrong. Only then read the reference implementation for the parts the transcript does not reveal.
2. **Establish the platform's inbound limits before designing.** Official bot APIs usually deliver group
   messages only when the bot is @-mentioned, and one app typically holds one long-lived connection.
   Promise nothing the platform cannot deliver.
3. **Decide the connection mode explicitly — it decides whether you need a public address.**
   Long-connection mode (websocket) works behind NAT with no public URL; callback/webhook mode needs a
   public HTTPS endpoint plus an offline or tunnelled host. The mode with no public address is the one to
   recommend; implementing the other protocol by hand is only worth it to avoid a new dependency.
4. **Prove the credentials with one live request before writing any parsing code.** Call the platform's
   token/introspection endpoint with the real credentials and read the actual field names. Then hand that
   captured response *shape* to whoever implements the client — parsing written from documentation or
   imagination is the main source of silent mismatch. Do the same for **outbound**: one real message to the
   final recipient before the channel is called done. Inbound working proves nothing about sending — the
   identity handle is usually app-scoped (see pitfalls) and a bare `400` hides the platform's own error code.
   Accept `code == 0` **and** a non-empty message id as the pass condition; a `200` with a non-zero `code` is
   a failure.
5. **Put credentials in the app's own secret file (e.g. `.env`, git-ignored) and never echo them.**
   Never print a secret, never repeat it in chat, and if one has already landed in a transcript or a
   screenshot, tell the user to rotate it — rotation is cheap, a leaked secret is not.
6. **Default-deny, then narrow.** Ship with an "unknown sender gets nothing" policy (a pairing/approval
   flow) rather than an open allow-all. An `open` policy on a gateway usually requires an explicit
   allow-all opt-in and, without it, can refuse to start *every* adapter — one platform's mistake takes
   the others down too. **Verify it from the outside**: send from an account that is *not* allowlisted and
   confirm it gets a pairing prompt and **no agent turn**. An `all` policy hidden in the secret file is
   otherwise invisible until someone else's traffic starts spending your tokens.
7. **Build the whole inbound path so it is testable without credentials or network.** A stubbed callback
   that walks the entire chain — signature check, message → one agent turn, reply formatting, plus the
   negative cases (bad signature rejected, verification challenge answered) — is the deliverable. Real
   round-trip verification comes later, once a reachable endpoint exists.
8. **State plainly what cannot be automated**: creating the app in the platform's console, choosing
   permission scopes, publishing the app / setting its availability. Provide a checklist and the exact
   place to click, then verify from the app side once the user reports it done.

## Put the platform's limits inside the product, not in a README

Once the channel is real, the user's next ask is "do all of this in the app, and tell me what genuinely
cannot be done". That is a UI/API shape, and it is the same for every platform:

- **One card per platform, one RPC namespace** (`channel.list|status|start|stop|test|set_credentials|guide`,
  platform as a *parameter*). Keep the existing platform-prefixed methods as thin aliases during a dual-write
  window so the current tests and UI keep working — never a big-bang rename.
- **Credentials are write-only — including the tail.** The UI takes them and writes them to the secret file
  atomically; the reply carries booleans only (`app_id_set`, `secret_set`) and nothing else. A masked tail
  (`****3f9c`) looks harmless and is what most implementations reach for, but it is still part of the value,
  it contradicts a "never echo the secret" rule the moment that rule is written down, and it leaves a
  response shape a later feature will widen. Unknown key names must **error**, not be ignored. Pin the rule
  with a test that slides a 4-character window across the reply and asserts no fragment of the secret is in it.
- **Return the platform's limits as machine-readable status** so the UI can print the real reason
  (`active_message_authorized: false`, `qr_expires_at`, …) instead of the user guessing why something failed.
- **Ship the manual steps as a guided checklist with deep links**, and derive each step's done-state from the
  backend (credentials present? SDK importable? connected? allowlist empty?). Do not hardcode "done".
- **Run the channel as a supervised child process**, not a thread in the UI process: the whole value is
  receiving while the window is closed, and the expensive one-off actions (QR scan, being added to a group)
  should happen once. Decide and state what closing the window means, and give an explicit "stop channels on
  exit" toggle (default: keep running). Ship `start`/`stop` + pid/journal files, make `start` idempotent, and
  make `stop` answer `{ok: false, reason: "not_running"}` rather than pretending it worked.
- **The child talks RPC only — make that structural.** The simplest way to keep a single-writer database
  discipline is to hand the child a context whose database handle is `None`, so it *cannot* open one, and to
  pin it with a test that asserts the child's importable modules never reference the DB class. Discipline
  written in a comment loses to a future contributor; an absent handle does not.
- **Storing the message is not delivering it.** A channel can write an inbound message to the store
  correctly and still look like it lost it, because the UI only refreshes on an event. If the existing
  event fires "only when the title changed" and the title was set when the first message arrived, nothing
  is ever emitted. Emit a creation event when the session is created, and verify the inbound path with the
  UI attached — a database row is not a delivered message.
- **A long-lived connection may have no "disconnect" API** (only connect + reconnect hooks). Then "stop"
  means killing the process by PID, and the reconnect hooks become your health signal.

## Pitfalls

- **Assuming the callback URL is mandatory.** Many platforms default to a long connection. Checking this
  first can turn "needs a tunnel + a public host" into "works from a laptop".
- **Reusing an app that already serves another client.** One app usually means one connection; a second
  client either fights for it or needs its own app. Say so before wiring.
- **Trusting the console's naming.** Permission scopes, event subscriptions, and publish steps are named
  differently in every console and change often — write `unknown` and send the user to look rather than
  inventing a label.
- **Confusing the two kinds of code** a platform hands out. A device/onboarding code shown in a link, and
  an approval code the user sends back into the terminal, are different objects with different lifetimes.
  Name them apart in code and docs.
- **Treating a user handle as portable across apps.** Chat-platform user ids are commonly **scoped to the
  app**, not to the person: an id copied from another integration (or from another client's pairing output)
  fails forever for this app, and the platform reports it only as `400 bad_request`. Get the id from an
  inbound event **of this app**, and on inbound from an allowlisted sender upsert/refresh the stored id
  instead of pasting one by hand. **Put that refresh before the allowlist decision or it can never run**: a
  stored id belonging to another app makes the sender unauthorized, so a refresh written after the drop
  branch is dead code and the bad id is permanent — the table never self-heals. Keep the two separate:
  *recording* a sender must never *approve* them. To repair an existing bad row without asking the user to
  re-pair, read this app's own history for that chat
  (`GET /open-apis/im/v1/messages?container_id_type=chat&container_id=oc_…`) and take `items[].sender.id`
  from an entry whose `sender_type` is `open_id` — by construction that is the id this app can send to.
- **Reporting the HTTP status without the response body.** Platform APIs put the real diagnosis in the body
  (`{"code": 99992361, "msg": "open_id cross app"}` and friends). A send wrapper that logs only
  `请求失败（400）` makes the bug undiagnosable from the app's own logs; carry `code`/`msg` into the log and
  the return value, and keep `code == 0` as the only success signal.
- **Letting the channel layer reach around the app's own architecture.** If the app routes all front-end
  traffic through one RPC surface and funnels all outbound HTTP through one throttled client, the new
  channel is another entry point into that surface — not a second path to the database or the network.
  Extend the layering guard test to register it.
- **A supervised child whose own entry point never loads the secret file.** The transport starts, discovers
  no credentials, prints a friendly "not configured" line and exits 2 — and because the supervisor reports
  only start/stop, the UI looks healthy while nothing is listening. Symptom pair: a log whose last line is
  days old, and a launcher `--check` that fails on credentials the app itself uses happily. Fix at the entry
  point (`load_dotenv()` first), then re-run `--check`, then confirm the live `connected` line — in that
  order, because a stale log and a wrong remote id are indistinguishable from "nothing happened".
- **Sending markdown to a platform that renders plain text.** Most chat platforms do not render markdown:
  `#`/`**`/tables/fences arrive literally or get mangled. Convert to plain text at the send boundary, and
  make the conversion **idempotent** — it will be called twice (a retry, a re-send, a helper that normalises
  before sending). The trap is stripping the fence lines and keeping the code body: on the second pass the
  code's own `# comment` looks like a heading, `| a | b |` looks like a table, and the code is chewed. Keep a
  sentinel pair (e.g. `CODE_OPEN` / `CODE_CLOSE`) **in the output**, so a second pass sees the block as
  protected and returns it unchanged. Test it as a property: idempotence over a table of shapes plus a random
  fuzz, not one hand-written example.
- **File transfer needs a preflight, not a try/except around the upload.** Check the platform's hard limits
  before opening the socket — size ceiling, zero-byte rejection, an allowed `file_type` enumeration with a
  `stream` fallback — and reject locally with the reason attached. Two separate permission scopes are
  usually required (upload vs send), they fail independently, and on staged platforms a scope change only
  takes effect after **publishing a new app version**. Surface that as a checklist item; a bare 400 from the
  upload endpoint does not tell the user any of it.
