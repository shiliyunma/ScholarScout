# 无头 Chrome 真实时钟验证（浏览器交付物的证明链）

目标：拿到两样东西，都来自**真实执行**——(1) 页面里的自检结果 JSON，(2) 真实渲染帧的 PNG。

## 为什么不能用 `--virtual-time-budget`

它会冻结虚拟时钟：`performance.now()` 不前进，于是任何"跑 N 步计时"的结果全是 0，
`requestAnimationFrame` 驱动的仿真也可能一步都不推进。
它仍然适合**快速查错**（页面会瞬间跑完）和**读 DOM**，但不适合测量。

| 目的 | 做法 |
|---|---|
| 快速查语法/运行时错误、读自检结果 | `--dump-dom "file:///...?autotest=1"` + 足够大的 `--virtual-time-budget`，然后从 dump 出的 HTML 里正则取 `<pre id="report">` 的内容（耗时字段会是 0，忽略即可） |
| 真实耗时、真实渲染 | 本地 HTTP 服务 + 真实时钟 Chrome + 页面 POST 回传（下面这个套路） |

## 套路：本地服务托管页面，页面把结果 POST 回来

`tools/headless_test.py`（可复用的骨架）：

```python
import http.server, socketserver, subprocess, os, time, threading, json, base64
ROOT = "D:/proj"            # 换成项目根目录
REPORT = os.path.join(ROOT, "docs", "report.json")
SHOTS = []

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)   # 直接托管项目目录，路径同源，无跨域问题
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
        if self.path.startswith("/shot"):
            tag = self.path.split("tag=")[-1]
            open(f"{ROOT}/docs/render-{tag}.png", "wb").write(
                base64.b64decode(body.decode("ascii", "ignore").split(",", 1)[-1]))
            SHOTS.append(tag)
        else:
            open(REPORT, "wb").write(body)
        self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass

socketserver.TCPServer.allow_reuse_address = True
httpd = socketserver.TCPServer(("127.0.0.1", 8321), Handler)   # 每个任务换端口
threading.Thread(target=httpd.serve_forever, daemon=True).start()
if os.path.exists(REPORT): os.remove(REPORT)

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
args = [CHROME, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
        "--no-sandbox", "--no-first-run", "--disable-extensions", "--mute-audio",
        "--window-size=960,600", r"--user-data-dir=C:\Users\<u>\AppData\Local\Temp\cr-unique",
        "http://127.0.0.1:8321/index.html?autotest=1&shots=1"]
chrome = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while time.time() - t0 < 400:                 # 轮询产物文件，不要盲等固定秒数
    if os.path.exists(REPORT) and len(SHOTS) >= 3: break
    if chrome.poll() is not None: break
    time.sleep(1)
chrome.kill(); httpd.shutdown()
```

要点：

- **不传** `--virtual-time-budget` → 真实时钟，`performance.now()` 可信。
- 页面**只在 http(s) 下**才 POST（`if (location.protocol.indexOf('http') === 0)`），`fetch` 外面再包 `try/catch`，
  这样同一份文件双击打开时完全不受影响。
- 页面把结果写进 `<pre id="report">` **同时** POST，两条出口互为保险。
- 轮询产物文件而不是 sleep 固定时长；给 Chrome 一个 300–400s 的上限，超时就打印已收到的部分。
- 收尾 `chrome.kill()` + `httpd.shutdown()`，否则端口/进程残留会让下一次跑失败。

## 抓真实渲染帧（浏览器截图不可用/不够时的正解）

页面侧，在 `rAF` 回调里 `render()` 之后立刻：

```js
const w = canvas.width, h = canvas.height, buf = new Uint8Array(w * h * 4);
canvas.getContext('webgl2').readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, buf);
const off = document.createElement('canvas'); off.width = w; off.height = h;
const ctx = off.getContext('2d'); const img = ctx.createImageData(w, h);
for (let y = 0; y < h; y++)                       // GL 原点在左下，逐行翻转
  img.data.set(buf.subarray((h - 1 - y) * w * 4, (h - y) * w * 4), y * w * 4);
ctx.putImageData(img, 0, 0);
fetch('/shot?tag=3d', { method: 'POST', body: off.toDataURL('image/png') });
```

- 必须在同一帧内读：绘制缓冲合成后会被清空。
- 只设一个"几秒后抓图"的开关（`state.captureAt`），由 `frame()` 检查——不要把抓图塞进业务逻辑。
- 抓图前先把被测对象推进到稳定状态（同步跑几百步），否则抓到的是初始随机态，看不出问题。
- **"随时间怎么变"类的要求要抓时间序列**：同一机位、同一距离，只改 warm 步数（如 240/480/720/960），
  文件名带 tag（`render-ts1..4.png`）；**不要在每帧重新取景**，否则几张图不可比。四张能看出不同的
  形状/朝向才算达标（判据怎么定见 `metric-driven-tuning.md`）。
- **性能基准必须独占跑**：同时开两个无头 Chrome 会抢 CPU，把同一份代码的每步耗时测高 50%+
  （实测 12000 实体 10.1ms → 16ms）——据此下"跑不动"的结论是错的。
- 抓回来的 PNG **一定用 `vision_analyze` 看过**再说"渲染正确"。数值全绿但画面空的组合很常见。

## 从无头 Chrome 里捞出 JS 报错

```bash
"$CHROME" --headless=new --disable-gpu --enable-unsafe-swiftshader --no-sandbox \
  --enable-logging=stderr --v=1 --user-data-dir=... "http://127.0.0.1:PORT/app.html" \
  > /dev/null 2> docs/chrome-log.txt &
sleep 45; kill %1
grep -iE "CONSOLE|Uncaught|GL_INVALID" docs/chrome-log.txt
```

- 一行 `INFO:CONSOLE:<line> "Uncaught SyntaxError: ..."` 就能定位整个脚本没跑起来的原因。
- 同样的日志里会混入大量无关的 updater/网络噪音（`拒绝访问`、`ERR_CONNECTION_TIMED_OUT`），只 grep 你要的词。
- `GL_INVALID_OPERATION: glDrawArrays: Feedback loop formed between Framebuffer and active Texture` =
  正在渲染的 FBO 附件同时被当成激活的采样纹理。画之前 `gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, null);` 解绑。

## 看外观的极简抓图（界面校验）

页面是纯静态、不需要 POST 回传时，一条命令就够（不启本地服务）：

```bash
"$BROWSER" --headless=new --disable-gpu --virtual-time-budget=6000 \
  --window-size=1280,860 --screenshot=out.png "file:///D:/proj/ui/index.html"
```

- 这里 `--virtual-time-budget` **不是用来测时间，而是等 JS 跑完**：不设它抓到的是一张只渲染了骨架 HTML 的画面
  （图上内容缺失，跟“样式写错”长得很像，很容易误判）。
- 要抓某个非默认状态（浅/深色、面板展开、tab 切换）时，**调页面自己的状态入口函数**（如 `applyTheme('dark')`），
  不要往 DOM 里注入 class —— 页面启动时会按系统偏好再 apply 一次，把注入的覆盖掉，你看到的是默认态。
- 拓下来的图**逐张用 `vision_analyze` 看**（浅、深各一张）。用户对配色的要求（“跟我在用的应用一致、不要深蓝”
  这类）只有看图能确认，测试全绿不算数。

## 无头环境的读数怎么用

- 无头下是软件光栅（SwiftShader），**帧率不代表用户机器**。报"每步 CPU 毫秒 / 每秒多少步"这类与显卡无关的数，
  并明确说明渲染部分是软件跑的；需要渲染耗时就单独拆一个 pass 计时。
- `navigator` 里的 UA/GPU 名可以一并记进 report，标明"这份数据是在什么环境下测的"。
- 视口开小（如 480×320）能让渲染抢走的 CPU 少一点，仿真本身的计时不受影响。
