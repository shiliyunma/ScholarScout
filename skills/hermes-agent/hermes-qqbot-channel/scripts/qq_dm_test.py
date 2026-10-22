# -*- coding: utf-8 -*-
"""QQ 通道出站自测：给本人私聊发一条消息，验证“发”这一侧是否通。

用法：python qq_dm_test.py [要发送的文本]
凭据来源（按顺序）：$HERMES_HOME/.env 的 QQ_APP_ID / QQ_CLIENT_SECRET / QQ_ALLOWED_USERS，
或 config.yaml 里的 platforms.qqbot.extra.app_id / client_secret。
只打印掩码后的标识，绝不回显 secret。

为什么只有私聊能自测：群主动消息在腾讯侧被 40034105 挡住（需后台申请权限），私聊主动消息可用。
"""
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request

HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")


def pick(text, key):
    m = re.search(rf'^{key}\s*[:=]\s*["\']?([^\s"\']+)', text, re.M)
    return m.group(1) if m else None


def load(path):
    try:
        return io.open(path, encoding="utf-8").read()
    except OSError:
        return ""


env = load(os.path.join(HOME, ".env"))
cfg = load(os.path.join(HOME, "config.yaml"))

app_id = pick(cfg, "app_id") or pick(env, "QQ_APP_ID") or pick(env, "QQBOT_APP_ID")
secret = (pick(env, "QQ_CLIENT_SECRET") or pick(env, "QQBOT_CLIENT_SECRET")
          or pick(cfg, "client_secret"))
user = pick(env, "QQ_ALLOWED_USERS")

print("app_id:", (app_id[:4] + "…") if app_id else "缺失",
      "| secret:", "有" if secret else "缺失",
      "| 目标 openid:", (user[:6] + "…") if user else "缺失")
if not (app_id and secret and user):
    sys.exit("缺凭据或目标 openid，无法自测")


def post(url, payload, headers):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


tok = post("https://bots.qq.com/app/getAppAccessToken",
           {"appId": app_id, "clientSecret": secret},
           {"Content-Type": "application/json"})
at = tok.get("access_token")
print("取 token:", "OK" if at else tok)
if not at:
    sys.exit("取 token 失败（网络层不可达时这里也会失败）")

text = " ".join(sys.argv[1:]) or "【连通性测试】Hermes 出站自测消息。"
headers = {"Authorization": f"QQBot {at}", "Content-Type": "application/json"}
try:
    res = post(f"https://api.sgroup.qq.com/v2/users/{user}/messages",
               {"content": text, "msg_type": 0}, headers)
    print("发送结果:", json.dumps(res, ensure_ascii=False)[:300])
except urllib.error.HTTPError as e:
    print("发送失败 HTTP", e.code, e.read().decode()[:300])
