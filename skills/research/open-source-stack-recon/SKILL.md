---
name: open-source-stack-recon
description: "Use when 判断一个开源项目是否涉及某技术栈（Go/Gin/NATS/SSO…）并给有依据的学习路径。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [技术栈取证, 开源项目, 调研, 学习路径, 大浩]
    category: research
---

# 开源项目技术栈取证（轻量档）

大浩的典型问法：「帮我学 X 项目，它是否涉及 A/B/C？怎么学，**简短精炼，要有依据**，需要学特定资料的给网址。」

这不是写讲义、也不是搭六件套工作台（那是 `repo-learning-workbench`），**产出是一屏以内的判定表 + 依据 + 学习顺序**。判定错了比写得少严重得多——他要拿去面试、写简历。

## Procedure

1. **拉源码**：`codeload.github.com` 直连下 zip（先 `master` 再 `main`），解到 `$LOCALAPPDATA/Temp` 这类 ASCII 路径。单文件 raw 走 `https://ghproxy.net/https://raw.githubusercontent.com/...`。完整命令见 `references/tech-stack-recon.md`。
2. **逐项取证：清单 + 全树，两处都要**。清单（`go.mod` / `package.json` / `requirements.txt`）说明"声明了"，树内 grep 说明"真在用"，只查一处都可能错。
3. **要给"以前是不是也这样"就用历史 tag 的清单证**（翻三五个 tag 的 `go.mod`，比翻 commit 快）。
4. **网址先 curl 测活**（`-o /dev/null -w '%{http_code}'`），只写 200 的；403/404 换官方文档或规范页。
5. **按依赖顺序给学习路径**（概念 → 语言 → 源码骨架 → 动手跑通），每步一个已验证 URL + 预计投入，并点明先读哪几个目录。
6. **收尾**：一句话路线 + 明确剔掉项目里其实没有的那项（"X 要另找项目练，别挂在这个项目上"）。

## 铁律

1. **技术栈只认清单 + grep，不认记忆。** 凭印象说某项目用 Gin，实测全仓 `grep -rl 'gin-gonic'` 零命中、HTTP 层其实是 `valyala/fasthttp`（一百多个文件引用），逐 tag 查 `go.mod` 也都不是 gin。一条记错的框架写进简历就是硬伤。
2. **否定结论给两个零命中**（清单零 + 全树零），grep 带词界 `\bnats\b`，否则 `coordinates`、`natsort` 之类子串造假阳性；结论写"零命中"，不写"大概没有"。
3. **能力类结论按官方限定词写**：例如"OpenID Certified™，限定 Basic/Implicit/Hybrid/Form Post/Config 五个 profile"，不要升格成"全套认证"。
4. **"简短精炼"不等于省依据**：每条 是/否 后面都必须能指到文件、tag 或官方页；但也不要借机复述架构、铺目录树。
5. **顺手给他"项目里没有的那项怎么学"**：从项目里学不到的（比如 Gin），单独指定项目练；并提醒源码里没证实的框架不要写进简历。

## 交付形状

```
## 一、是否涉及
| 技术 | 是/否 | 依据（实测） |   ← 每行都能被他自己复核

> 它真实的技术栈：一行（含存储/中间件，证明读过源码）

## 二、怎么学（按依赖顺序，不按项目文件顺序）
第 0 步 · 概念（URL，投入）
第 1 步 · 语言（URL）
第 2 步 · 读源码骨架（按这个目录顺序，别乱翻）
第 3 步 · 动手跑通（URL）
第 4 步 · 项目里没有但你想学的，单独处理
```

表在前、路径在后、依据贯穿全篇；整篇控制在一屏多一点。

## Files

- `references/tech-stack-recon.md` — 取证命令集：源码获取（含 raw 被 reset 的绕法）、清单/全树 grep、历史 tag 对比、URL 测活。
