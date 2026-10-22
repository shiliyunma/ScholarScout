# 技术栈取证命令集

## 1. 拿源码（本机可用路径）

```bash
cd "$LOCALAPPDATA/Temp" && rm -rf x_x && mkdir x_x && cd x_x
curl -sSL --max-time 120 -o x.zip https://codeload.github.com/<owner>/<repo>/zip/refs/heads/master   # 先 master 再 main
unzip -q -o x.zip && ls
```

- 单文件（某历史 tag 的 `go.mod`）走代理前缀：
  `curl -sSL "https://ghproxy.net/https://raw.githubusercontent.com/<owner>/<repo>/<tag>/go.mod" -o gm_<tag>.mod`
- `raw.githubusercontent.com` 直连常被 connection reset；`codeload` 直连可用，十几 MB 也就几十秒。
- 解压目录用 ASCII 路径（`$LOCALAPPDATA/Temp`），中文路径会让原生工具崩。

## 2. 清单 + 全树，两处都要

| 生态 | 清单文件 | 树内搜 |
|---|---|---|
| Go | `go.mod`（`go 1.x` 给语言版本，`module` 行给模块路径） | `--include='*.go'` |
| Node | `package.json` | `--include='*.ts' --include='*.tsx'` |
| Python | `requirements.txt` / `pyproject.toml` | `--include='*.py'` |

```bash
grep -in '<dep>' go.mod                        # 依赖是否声明
grep -rl '<dep>' --include='*.go' . | wc -l    # 代码里是否真在用（文件数=强度证据）
grep -rinE '\b<nats>\b' --include='*.go' --include='*.md' --include='*.yml' . | head
```

- 否定结论 = 两个零命中（清单零 + 全树零），结论里写"零命中"。
- 顺带看它真实的 HTTP/框架栈（`grep -inE 'fasthttp|chi|gorilla|echo|fiber|gin-gonic' go.mod`），这常常正是用户猜错的那一项。

## 3. 历史 tag 对比（回答"以前是不是也这样"）

```bash
for t in v4.30.0 v4.34.6 v4.38.19; do
  curl -sSL "https://ghproxy.net/https://raw.githubusercontent.com/<o>/<r>/$t/go.mod" -o gm_$t.mod
  echo -n "$t size=$(wc -c < gm_$t.mod) "; grep -oE 'gin-gonic/[a-z]+|valyala/fasthttp|fosite' gm_$t.mod | sort -u | tr '\n' ' '; echo
done
```

注意 `wc -c` 只有几百字节可能是抓到了错误页，换个 tag 或换镜像前缀重试。

## 4. 能力归属看代码组织

```bash
ls internal            # 顶层能力目录（authentication / oidc / middlewares / handlers …）
grep -rl 'http.Handler' --include='*.go' internal | head
find . -name '*.go' | wc -l    # 规模，顺带证明"真读了"
```

## 5. 给网址前测活

```bash
for u in <url...>; do printf '%s ' "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 25 -L "$u")"; echo "$u"; done
```

只留 200；403/404 换官方文档/规范页（概念类可给 oauth.com、Auth0 文档、openid.net 规范原文，比中文二手博客稳）。

## 6. 可复用的学习路径骨架

| 步 | 内容 | 为什么这个顺序 |
|---|---|---|
| 0 | 协议/领域概念（先懂上下文） | 不懂 OIDC 读 OIDC 源码全是天书 |
| 1 | 语言基础（只会不会就补） | 只补项目真用到的那几样（context / error / channel） |
| 2 | 源码骨架：入口 → 配置 → 中间件 → handler → 领域包 | 按数据流读，不按目录字母序 |
| 3 | 动手跑通两条最小链路 | 简历能写的是这部分 |
| 4 | 项目里没有但那项技术：另找项目 | 别把两件事混成一条路线 |
