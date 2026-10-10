/* 设置页「飞书接入」卡片的**无头渲染探针**：在 node 里用一份极简 DOM 桩真跑一遍
 * `renderFeishuCard`（不引任何依赖，也不进浏览器），把渲染出的文本与断言回给 Python 侧。
 *
 * 用法：node feishu_card_probe.js <profile.js 的路径>
 * 输出：一行 JSON —— {text, checks:[{name, ok, got}]}
 *
 * 为什么值得真跑一遍（静态读文本查不出来）：
 * - 卡片只准显示「已配置／未配置」，任何密钥值都不许落到页面文本里 —— 这里往
 *   `feishu.status` / `feishu.probe` 的回包里**故意塞进诱饵密钥**，断言渲染文本里找不到它们；
 * - 配对码「生成前不显示、生成后显示并有倒计时」只有点一遍按钮才看得到；
 * - 自检要显示机器人名与 token 有效期，且**绝不能把 token 值显示出来**。
 *
 * profile.js 顶层只有 var 声明（无可执行语句），函数声明在 vm 里会落到全局，
 * 所以能用 `vm.runInContext` 加载后直接取 `renderFeishuCard`。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const target =
  process.argv[2] ||
  path.join(__dirname, "..", "..", "src", "scholarscout", "desktop", "ui", "profile.js");
const source = fs.readFileSync(path.resolve(target), "utf8");

/* --------------------------------------------------------------- 极简 DOM 桩 */
function makeEl(tag) {
  const node = {
    tag: tag,
    className: "",
    style: {},
    type: "",
    title: "",
    hidden: false,
    value: "",
    _text: "",
    children: [],
    _handlers: {},
    classList: {
      _on: {},
      toggle(cls, on) {
        this._on[cls] = on === undefined ? !this._on[cls] : !!on;
      },
      add(cls) {
        this._on[cls] = true;
      },
      remove(cls) {
        delete this._on[cls];
      },
      contains(cls) {
        return !!this._on[cls];
      }
    },
    set textContent(v) {
      this._text = String(v);
      this.children = [];
    },
    get textContent() {
      if (this.children.length) {
        return this.children.map((c) => c.textContent).join("");
      }
      return this._text;
    },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    addEventListener(type, fn) {
      (this._handlers[type] = this._handlers[type] || []).push(fn);
    },
    querySelector() {
      return null;
    },
    setAttribute() {},
    removeAttribute() {},
    getAttribute() {
      return null;
    },
    focus() {}
  };
  return node;
}

const elementsById = {};
function el(id) {
  if (!elementsById[id]) elementsById[id] = makeEl("#" + id);
  return elementsById[id];
}

function plainHint(text) {
  const p = makeEl("p");
  p.textContent = text;
  return p;
}

/* -------------------------------------------- 诱饵：必须不出现在渲染文本里 */
const LEAKS = {
  app_id: "cli_LEAK_APPID",
  app_secret: "SECRET_LEAK_DO_NOT_SHOW",
  tenant_access_token: "t-LEAKTOKEN",
  token: "t-LEAKTOKEN"
};

const responses = {
  "feishu.status": Object.assign(
    {
      app_id_set: true,
      secret_set: false,
      listener_enabled: true,
      listener_port: 9000,
      connection_mode: "webhook",
      group_policy: "mention",
      dm_policy: "pairing",
      temp_open: false,
      bot_name: null
    },
    LEAKS
  ),
  "feishu.pairing.list": {
    pending: [{ user_id: "ou_pending", user_name: "张三", age_seconds: 30, claimed: true }],
    approved: [{ user_id: "ou_ok", user_name: "李四" }],
    temp_open: false
  },
  "feishu.pairing.create": { code: "12345678", ttl_seconds: 3600 },
  "feishu.probe": Object.assign(
    { ok: true, bot_name: "大浩的AI毕设", bot_open_id: "ou_bot", token_expire: 7200, domain: "feishu" },
    LEAKS
  )
};

const scout = {
  call(name) {
    return Promise.resolve(responses[name] || {});
  }
};

const sandbox = {
  document: { createElement: makeEl },
  el,
  plainHint,
  scout,
  localStorage: {
    getItem() {
      return null;
    },
    setItem() {},
    removeItem() {}
  },
  window: {},
  setInterval() {
    return 1; // 倒计时定时器：探针不需要它真的走，避免进程被挂住
  },
  clearInterval() {},
  console
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);

const checks = [];
function check(name, ok, got) {
  checks.push({ name: name, ok: !!ok, got: ok ? "" : String(got || "") });
}

function findByText(root, tag, text) {
  const out = [];
  (function walk(node) {
    if (!node || typeof node !== "object") return;
    if (node.tag === tag && node.textContent === text) out.push(node);
    (node.children || []).forEach(walk);
  })(root);
  return out;
}

function findButtonContaining(root, needle) {
  const out = [];
  (function walk(node) {
    if (!node || typeof node !== "object") return;
    if (node.tag === "button" && node.textContent.indexOf(needle) !== -1) out.push(node);
    (node.children || []).forEach(walk);
  })(root);
  return out;
}

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

(async function main() {
  const renderFeishuCard = sandbox.renderFeishuCard;
  check("取到了 renderFeishuCard", typeof renderFeishuCard === "function");
  if (typeof renderFeishuCard !== "function") {
    process.stdout.write(JSON.stringify({ text: "", checks: checks }));
    return;
  }

  const body = makeEl("div");
  await renderFeishuCard(body);
  await flush();

  const text = body.textContent;
  check("凭据只报布尔：App ID 显示已配置", /App ID已配置/.test(text), text);
  check("凭据只报布尔：App Secret 显示未配置", /App Secret未配置/.test(text), text);
  check(
    "任何密钥值都不出现在渲染文本里（诱饵全找不到）",
    Object.keys(LEAKS).every((k) => text.indexOf(LEAKS[k]) === -1),
    text
  );
  check(
    "策略文案：三选一且给中文",
    text.indexOf("配对审批（推荐）") !== -1 &&
      text.indexOf("放行全部私聊") !== -1 &&
      text.indexOf("仅白名单") !== -1,
    text
  );
  check("生成配对码前不显示明文码", text.indexOf("12345678") === -1, text);
  check("有生成配对码的按钮", findButtonContaining(body, "配对码").length === 1, text);
  check("有「自检（联网）」按钮", findByText(body, "button", "自检（联网）").length === 1, text);

  /* ---- 点「生成配对码」：明文码与倒计时出现，仍无密钥 ---- */
  const gen = findButtonContaining(body, "配对码")[0];
  gen._handlers.click[0]();
  await flush();
  await flush();
  const afterCode = body.textContent;
  check("点生成后显示配对码明文", afterCode.indexOf("12345678") !== -1, afterCode);
  check("配对码带倒计时提示（后过期）", afterCode.indexOf("后过期") !== -1, afterCode);

  /* ---- 点「自检（联网）」：显示机器人名与有效期，token 值不显示 ---- */
  const probe = findByText(body, "button", "自检（联网）")[0];
  probe._handlers.click[0]();
  await flush();
  await flush();
  const afterProbe = body.textContent;
  check("自检显示机器人名", afterProbe.indexOf("机器人名：大浩的AI毕设") !== -1, afterProbe);
  check(
    "自检显示 token 有效期但不含 token 值",
    afterProbe.indexOf("120 分钟") !== -1 && afterProbe.indexOf("t-LEAKTOKEN") === -1,
    afterProbe
  );

  process.stdout.write(JSON.stringify({ text: afterProbe, checks: checks }));
})().catch((error) => {
  process.stdout.write(
    JSON.stringify({
      text: "",
      checks: [{ name: "probe 崩溃", ok: false, got: (error && error.stack) || String(error) }]
    })
  );
});
