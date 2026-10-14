/* 多会话分槽的**纯函数 + 状态机**用例集（node 下从产品文件里抽出来真跑，不碰 DOM）。
 *
 * 用法：node multisession_probe.js <api.js 路径> <app.js 路径>
 * 输出：一行 JSON —— [{name, ok, got}]，由 Python 侧断言（见 test_multisession.py）。
 *
 * 为什么要抽函数源码而不是 require 整个文件：api.js / app.js 是浏览器脚本（顶层用 window /
 * document），require 进来会当场炸。这里按名字把要测的函数源码切出来，放进 `new Function`
 * 里求值 —— 测的仍是**产品文件里的那段源码**，不是复刻版。
 * 依赖 `state` 的函数（turnSlot / sessionRunning / unreadCount）靠 globalThis.state 注入假状态。
 */
"use strict";

const fs = require("fs");
const path = require("path");

const apiPath = path.resolve(process.argv[2] || path.join(__dirname, "..", "..", "src", "scholarscout", "desktop", "ui", "api.js"));
const appPath = path.resolve(process.argv[3] || path.join(__dirname, "..", "..", "src", "scholarscout", "desktop", "ui", "app.js"));
const API = fs.readFileSync(apiPath, "utf8");
const APP = fs.readFileSync(appPath, "utf8");

/** 按名字切出一个顶层函数声明（花括号配平）。切不到就抛 —— 宁可红，也不要静默测了个空。 */
function extractFunction(src, name) {
  const start = src.indexOf("function " + name + "(");
  if (start < 0) throw new Error("源码里找不到 function " + name + "(");
  const open = src.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < src.length; i += 1) {
    const ch = src[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) {
        const body = src.slice(start, i + 1);
        if (body.indexOf("return") < 0) throw new Error(name + " 切出来不像函数体（没有 return）");
        return body;
      }
    }
  }
  throw new Error(name + " 的花括号没配平");
}

function loadFunction(src, name) {
  return new Function("return (" + extractFunction(src, name) + ");")();
}

const eventSessionKey = loadFunction(API, "eventSessionKey");
const unreadFromDone = loadFunction(API, "unreadFromDone");
const turnSlot = loadFunction(APP, "turnSlot");
const sessionRunning = loadFunction(APP, "sessionRunning");
const unreadCount = loadFunction(APP, "unreadCount");

const cases = [];
function check(name, fn) {
  try {
    const detail = fn();
    const ok = detail === true || detail === undefined;
    cases.push({ name: name, ok: ok, got: ok ? "" : String(detail) });
  } catch (error) {
    cases.push({ name: name, ok: false, got: error && error.message ? error.message : String(error) });
  }
}
function expect(actual, expected, label) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  return a === b ? true : `${label || "值"}：期望 ${b}，实际 ${a}`;
}

/* ------------------------------------------------- ① 事件按 session_id 路由 */

check("eventSessionKey：信封带 session_id → 原样取出", () => {
  return expect(eventSessionKey("chat.delta", { session_id: "s1", turn_id: "t1" }), "s1", "会话键");
});

check("eventSessionKey：**没有 session_id → null（不落任何槽）**", () => {
  return expect(eventSessionKey("chat.delta", { turn_id: "t1", text: "x" }), null, "会话键");
});

check("eventSessionKey：params 整个缺失 → null", () => {
  return expect(eventSessionKey("chat.tool_call"), null, "会话键");
});

check("eventSessionKey：session_id 为 null / undefined → null", () => {
  return expect(
    [eventSessionKey("chat.done", { session_id: null }), eventSessionKey("chat.done", { session_id: undefined })],
    [null, null],
    "会话键"
  );
});

check("eventSessionKey：session_id 为空串 → null（空串等于没给）", () => {
  return expect(eventSessionKey("chat.done", { session_id: "" }), null, "会话键");
});

check("eventSessionKey：非字符串 id 一律转成字符串（不许把数字当别的会话）", () => {
  return expect(eventSessionKey("chat.done", { session_id: 7 }), "7", "会话键");
});

check("eventSessionKey：practice.delta 这类无 session_id 的事件 → null（所以它必须在门之前处理）", () => {
  return expect(eventSessionKey("practice.delta", { note_id: "n1", delta: "x" }), null, "会话键");
});

/* ------------------------------------------------------- ② 完成角标真值表 */

check("unreadFromDone：别的会话完成 → 打角标", () => {
  return expect(unreadFromDone("s1", "s2", false), true, "角标");
});

check("unreadFromDone：**当前显示的会话**完成 → 不打角标", () => {
  return expect(unreadFromDone("s1", "s1", false), false, "角标");
});

check("unreadFromDone：已经是未读 → 保持未读（不许被后来到达的事件顺手清掉）", () => {
  return expect(unreadFromDone("s1", "s2", true), true, "角标");
});

check("unreadFromDone：拿不到 session_id → 不打（宁可漏打，也不许打错会话）", () => {
  return expect(
    [unreadFromDone("", "s2", false), unreadFromDone(null, "s2", false), unreadFromDone(undefined, "s2", false)],
    [false, false, false],
    "角标"
  );
});

check("unreadFromDone：当前会话为空（还没打开任何会话）也算后台完成 → 打", () => {
  return expect(unreadFromDone("s1", "", false), true, "角标");
});

/* --------------------------------------- ③ 每会话槽的建 / 切 / 清（状态机） */

check("turnSlot：create=false 时不建（读不到就返回 null，不偷建）", () => {
  globalThis.state = { turns: {} };
  return expect(turnSlot("s1", false), null, "槽");
});

check("turnSlot：空 key / null / undefined 一律不建（不许有\"无名槽\"）", () => {
  globalThis.state = { turns: {} };
  const got = [turnSlot("", true), turnSlot(null, true), turnSlot(undefined, true)];
  return expect([got[0], got[1], got[2], Object.keys(globalThis.state.turns).length], [null, null, null, 0], "槽");
});

check("turnSlot：create=true 建出的槽字段就是那六个（多一个少一个都是改契约）", () => {
  globalThis.state = { turns: {} };
  const slot = turnSlot("s1", true);
  return expect(Object.keys(slot).sort(), ["messageId", "running", "spinner", "trace", "turnId", "unread"], "字段");
});

check("turnSlot：同一个会话反复取到的是**同一个槽**（不是每次新建）", () => {
  globalThis.state = { turns: {} };
  const first = turnSlot("s1", true);
  first.trace = { marker: 1 };
  return expect(turnSlot("s1", true) === first && turnSlot("s1", true).trace.marker, 1, "槽");
});

check("sessionRunning：**别的会话在跑不算**（这是\"切会话还能发问\"的根）", () => {
  globalThis.state = { turns: {} };
  turnSlot("s1", true).running = true;
  return expect([sessionRunning("s1"), sessionRunning("s2")], [true, false], "在跑");
});

check("sessionRunning：没有这个会话的槽时返回 false（不是 undefined）", () => {
  globalThis.state = { turns: {} };
  return expect(sessionRunning("nope"), false, "在跑");
});

check("切会话不动别人的槽：running / unread / trace 都按 key 各自留着", () => {
  globalThis.state = { turns: {} };
  const a = turnSlot("s1", true);
  a.running = true;
  a.trace = { tag: "A" };
  a.unread = true;
  const b = turnSlot("s2", true);
  b.running = false;
  b.trace = { tag: "B" };
  // 再切回 s1 读一次（真实界面里就是 openSession("s1")）
  const back = turnSlot("s1", false);
  return expect([sessionRunning("s2"), back.running, back.unread, back.trace.tag, b.trace.tag], [false, true, true, "A", "B"], "槽");
});

check("清角标只清那一个会话（`slot.unread = false` 的效果）", () => {
  globalThis.state = { turns: {} };
  turnSlot("s1", true).unread = true;
  turnSlot("s2", true).unread = true;
  turnSlot("s1", true).unread = false; // openSession 里就是这一句
  return expect(unreadCount(), 1, "未读数");
});

check("unreadCount：数的是**未读会话数**，不是未读轮数", () => {
  globalThis.state = { turns: {} };
  turnSlot("s1", true).unread = true;
  turnSlot("s2", true).unread = true;
  turnSlot("s3", true).unread = false;
  return expect(unreadCount(), 2, "未读数");
});

check("unreadCount：空状态是 0（顶栏计数按钮该隐藏）", () => {
  globalThis.state = { turns: {} };
  return expect(unreadCount(), 0, "未读数");
});

process.stdout.write(JSON.stringify(cases));
