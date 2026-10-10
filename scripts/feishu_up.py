"""一键起飞书链路：起监听 → 起隧道（tee 到文件）→ 解析公网地址 → 回打 url_verification 自检。

为什么需要它：飞书链路要三样东西同时活着 —— 本地回调监听、公网隧道、飞书后台里的回调地址。
现状是"两个手工后台进程 + 手动从 banner 复制隧道 URL"，关机就没了、地址重启就变。
本脚本把这条链拉成一条命令（docs/17 §3.9）：

    1. 读 config.yaml 检查前提（监听开关 / 出站开关 / 端口 / 路径）；不满足就**说清楚缺哪个键**并退出
    2. 起本地回调监听（已在监听则复用，**不重复起**）；只绑 127.0.0.1，公网交给隧道
    3. 起隧道并把输出 **tee 到 run/feishu-tunnel.log**（公网 URL 只在 banner 里，不落盘就取不到）
    4. 从输出里解析公网地址、拼出回调 URL（带 /feishu/webhook），**回打一次 url_verification 自检**
       （200 且回对 challenge 才算成功）
    5. 打印监听地址 / 公网回调 URL / 自检结果 / 下一步；失败就打印**哪一步失败 + 怎么排查**
    6. 之后阻塞保活；Ctrl-C 一并停掉**本脚本起的**隧道与监听（外部已起的进程绝不动）

隧道命令可配（换固定域名时**不用改代码**）：
    --tunnel-cmd "..."            命令行优先级最高
    SCHOLARSCOUT_TUNNEL_CMD=...   环境变量次之
    都不给 → 默认 DEFAULT_TUNNEL_CMD（ssh 的 localhost.run 免费隧道；地址临时、重启会变）。
    要固定地址见 docs/17 §3.9。

用法：
    .venv/Scripts/python.exe scripts/feishu_up.py
    .venv/Scripts/python.exe scripts/feishu_up.py --allow-no-reply          # 只收不发
    .venv/Scripts/python.exe scripts/feishu_up.py --tunnel-cmd "cloudflared tunnel --url http://127.0.0.1:{port} --no-autoupdate"
双击入口：项目根 ``飞书一键起飞.bat``。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scholarscout.channels import feishu  # noqa: E402
from scholarscout.config import Config, RuntimeProfile, load_config  # noqa: E402

#: 隧道命令默认值：ssh 的 localhost.run 免费隧道（`{port}` 运行时替换成本地监听端口）。
DEFAULT_TUNNEL_CMD = (
    "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
    "-o ServerAliveInterval=30 -R 80:localhost:{port} nokey@localhost.run"
)
#: 隧道命令的可配置环境变量（`--tunnel-cmd` 优先级更高）。
TUNNEL_CMD_ENV = "SCHOLARSCOUT_TUNNEL_CMD"
#: 隧道输出落盘位置（URL 只在 banner 里，必须 tee 到文件才拿得到）。
TUNNEL_LOG = ROOT / "run" / "feishu-tunnel.log"
#: 等隧道 banner 里出现公网地址的最长秒数。
TUNNEL_URL_WAIT_S = 60.0

#: 公网地址识别：覆盖 localhost.run（lhr.life）、cloudflared quick tunnel、ngrok。
TUNNEL_URL_RE = re.compile(
    r"https?://[A-Za-z0-9][A-Za-z0-9.-]*\."
    r"(?:lhr\.life|localhost\.run|trycloudflare\.com|ngrok-free\.app|ngrok\.io)"
)


# ------------------------------------------------------------------ 纯函数（可单独测）


def resolve_tunnel_cmd(
    port: int, *, override: str | None = None, env: Mapping[str, str] | None = None
) -> str:
    """隧道命令：`--tunnel-cmd` > 环境变量 `SCHOLARSCOUT_TUNNEL_CMD` > 内置默认。

    用 `str.replace` 而不是 `str.format`：命令里可能有别的花括号，`format` 会当场炸。
    """
    source = env if env is not None else os.environ
    raw = (override or source.get(TUNNEL_CMD_ENV) or DEFAULT_TUNNEL_CMD).strip()
    return raw.replace("{port}", str(int(port)))


def parse_public_url(text: str) -> str | None:
    """从隧道输出里取**最后**一条公网地址（隧道重连换域名时会打印多条，取最新的那条）。"""
    matches = TUNNEL_URL_RE.findall(text or "")
    return matches[-1] if matches else None


def build_callback_url(base: str, path: str) -> str:
    """把公网基址与回调路径拼成飞书后台要填的完整 URL（路径不能少，漏了就 404）。"""
    base = (base or "").strip().rstrip("/")
    suffix = path if path.startswith("/") else "/" + path
    return base + suffix


def listener_address(port: int, path: str, host: str = "127.0.0.1") -> str:
    """本地监听地址（用于打印与本地自检）。"""
    suffix = path if path.startswith("/") else "/" + path
    return f"http://{host}:{int(port)}{suffix}"


def check_prerequisites(runtime: RuntimeProfile, *, allow_no_reply: bool = False) -> list[str]:
    """检查 `config.runtime` 里的四个前提，返回**缺哪些**（空列表 = 全满足）。

    这就是"不满足就说清楚缺哪个键、不要默默跑"那一条：缺什么列什么，逐条给出怎么补。
    `allow_no_reply=True` 时把"出站开关关着"从错误降级为放行（只收不发场景）。
    """
    problems: list[str] = []
    if not runtime.feishu_listener_enabled:
        problems.append("runtime.feishu_listener_enabled 不是 true（不开就收不到飞书事件）")
    if not runtime.feishu_reply_enabled and not allow_no_reply:
        problems.append(
            "runtime.feishu_reply_enabled 不是 true（关着只记录、不会把回答发回飞书；"
            "确实只收不发就加 --allow-no-reply）"
        )
    port = runtime.feishu_listener_port
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        problems.append(f"runtime.feishu_listener_port 无效：{port!r}（应为 1-65535 的整数）")
    path = runtime.feishu_listener_path
    if not isinstance(path, str) or not path.startswith("/"):
        problems.append(
            f"runtime.feishu_listener_path 无效：{path!r}（应以 / 开头，如 /feishu/webhook）"
        )
    return problems


def _http_post_json(url: str, payload: Mapping[str, object], timeout: float) -> tuple[int, object]:
    """POST 一个 JSON 并读回 JSON（连不上/超时/非 JSON 都如实返回，不抛）。"""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            try:
                return int(response.status), json.loads(body)
            except json.JSONDecodeError:
                return int(response.status), body
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except Exception:
        return 0, None


def self_check(
    callback_url: str,
    *,
    timeout: float = 10.0,
    post: Callable[[str, Mapping[str, object], float], tuple[int, object]] = _http_post_json,
    challenge: str | None = None,
) -> tuple[bool, str]:
    """回打一次 `url_verification`：**200 且回对 challenge 才算通过**。

    `post` 可注入（桩测不真联网）。返回 `(是否通过, 人话说明)`。
    """
    token = challenge or "scholarscout-" + secrets.token_hex(6)
    status, payload = post(callback_url, {"type": "url_verification", "challenge": token}, timeout)
    if status == 200 and isinstance(payload, dict) and payload.get("challenge") == token:
        return True, f"url_verification 通过（challenge={token} 原样回显）"
    if status == 0:
        return False, f"连不上 {callback_url}（隧道没通 / 监听没起 / 地址已失效）"
    return False, f'回包不对：HTTP {status}，body={payload!r}（期望 200 + {{"challenge": ...}}）'


def is_listening(
    port: int,
    path: str,
    *,
    host: str = "127.0.0.1",
    timeout: float = 2.0,
    post: Callable[[str, Mapping[str, object], float], tuple[int, object]] = _http_post_json,
) -> bool:
    """本地回打一次 url_verification：通了 = 已有监听在跑（就复用它，不重复起）。"""
    ok, _ = self_check(listener_address(port, path, host), timeout=timeout, post=post)
    return ok


def set_console_title(title: str) -> bool:
    """给控制台窗口设标题（Windows，UTF-16 直传，绕开控制台代码页；失败静默返回 False）。"""
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(title)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ 进程句柄


@dataclass
class Tunnel:
    """隧道进程 + 它的输出（URL 从输出里解析，日志同时落盘）。"""

    cmd: str
    log_path: Path
    proc: subprocess.Popen | None = None
    url: str | None = None
    output: str = ""
    error: str = ""

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - 兜底强杀
                self.proc.kill()


@dataclass
class ListenerHandle:
    """本地回调监听句柄。`started=True` = 本脚本起的（Ctrl-C 要停）；False = 复用外部进程。"""

    address: str
    started: bool
    ctx: object | None = None
    server: object | None = None

    def stop(self) -> None:
        server = self.server
        if server is not None:
            with contextlib.suppress(Exception):  # 收尾不能反过来把进程搞崩
                server.stop()
        ctx = self.ctx
        if ctx is not None:
            with contextlib.suppress(Exception):
                ctx.close()


# ------------------------------------------------------------------ 起监听 / 起隧道（真实现）


def start_listener(port: int, path: str) -> ListenerHandle:
    """起本地回调监听（复用渠道层 `feishu.run_listener`，只绑 127.0.0.1）。"""
    from scholarscout.app import build_context
    from scholarscout.fetch import feishu_send

    ctx = build_context()
    runtime = ctx.config.runtime
    sender = feishu_send.build_sender(ctx.http, enabled=runtime.feishu_reply_enabled)
    try:
        server = feishu.run_listener(ctx, sender=sender, port=int(port), path=path, block=False)
    except Exception:
        ctx.close()
        raise
    bound_host, bound_port = server.address
    return ListenerHandle(
        address=listener_address(bound_port, path, bound_host),
        started=True,
        ctx=ctx,
        server=server,
    )


def start_tunnel(
    cmd: str,
    *,
    log_path: Path = TUNNEL_LOG,
    wait_url_s: float = TUNNEL_URL_WAIT_S,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Tunnel:
    """起隧道子进程，把输出 **tee 到 log_path**，并从 banner 里解析公网地址。

    - `popen` / `sleep` / `monotonic` 可注入（桩测不真起进程、不真等待）；
    - `popen` 抛 `OSError`（命令找不到等）→ 返回带 `error`、`proc=None` 的 Tunnel
      （调用方据此报"隧道启动失败"）；
    - 起了但等不到 URL → 返回带 `error`、`proc` 非空、`url=None` 的 Tunnel
      （调用方据此报"解析不到公网地址"）。
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        return Tunnel(cmd=cmd, log_path=log_path, error=f"隧道进程没起来：{exc}")

    buffer: list[str] = []
    found: dict[str, str | None] = {"url": None}

    def _reader() -> None:
        stream = getattr(proc, "stdout", None)
        if stream is None:  # pragma: no cover - 无输出的极少数情况
            return
        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                for line in stream:
                    log_file.write(line)
                    log_file.flush()
                    buffer.append(line)
                    if found["url"] is None:
                        found["url"] = parse_public_url(line)
        except Exception:  # pragma: no cover - 读线程兜底，不影响主流程
            return

    threading.Thread(target=_reader, name="feishu-tunnel-reader", daemon=True).start()

    deadline = monotonic() + wait_url_s
    while found["url"] is None and proc.poll() is None and monotonic() < deadline:
        sleep(0.2)

    url = found["url"]
    error = ""
    if url is None:
        if proc.poll() is not None:
            error = "隧道进程已退出（命令是否可用？端口是否被占？看日志里的报错）"
        else:
            error = f"等 {wait_url_s:.0f}s 没从隧道输出里解析到公网地址"
    return Tunnel(
        cmd=cmd, log_path=log_path, proc=proc, url=url, output="".join(buffer), error=error
    )


# ------------------------------------------------------------------ 编排


@dataclass
class Deps:
    """可注入的依赖：桩测只替换这里（不真起进程、不真联网），主流程一字不改。"""

    load_config: Callable[..., Config]
    is_listening: Callable[[int, str], bool]
    start_listener: Callable[[int, str], ListenerHandle]
    start_tunnel: Callable[..., Tunnel]
    self_check: Callable[..., tuple[bool, str]]


def default_deps() -> Deps:
    return Deps(
        load_config=load_config,
        is_listening=is_listening,
        start_listener=start_listener,
        start_tunnel=start_tunnel,
        self_check=self_check,
    )


def _teardown(listener: ListenerHandle | None, tunnel: Tunnel | None = None, *, say=print) -> None:
    """收尾：只停**本脚本起的**隧道与监听，外部进程绝不动。"""
    if tunnel is not None:
        tunnel.stop()
        say(f"[停止] 隧道已停（日志：{tunnel.log_path}）")
    if listener is None:
        return
    if listener.started:
        listener.stop()
        say("[停止] 本地监听已停（本脚本起的）")
    else:
        say("[停止] 外部监听进程未动（不是本脚本起的）")


def run(
    argv: list[str] | None = None,
    *,
    deps: Deps | None = None,
    say: Callable[[str], None] = print,
    hold: bool = True,
) -> int:
    """一键起飞主流程。返回进程退出码：0 成功；2 前提没满足；3 起监听失败；4 隧道启动失败；
    5 解析不到公网地址；6 公网自检失败。"""
    parser = argparse.ArgumentParser(
        prog="feishu_up", description="一键起飞书链路（本地监听 + 公网隧道 + 自检）"
    )
    parser.add_argument(
        "--tunnel-cmd",
        default=None,
        help=f"隧道命令（默认读 ${TUNNEL_CMD_ENV}，再退到内置 localhost.run）",
    )
    parser.add_argument("--port", type=int, default=None, help="覆盖 config.yaml 里的监听端口")
    parser.add_argument(
        "--allow-no-reply", action="store_true", help="出站开关关着也继续（只收不发场景）"
    )
    args = parser.parse_args(argv)

    deps = deps or default_deps()
    config = deps.load_config()
    runtime = config.runtime
    port = int(args.port or runtime.feishu_listener_port)
    path = runtime.feishu_listener_path

    if sys.stdout.isatty():
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        set_console_title(f"ScholarScout 飞书一键起飞 · 端口 {port} · {stamp}")

    say("== ScholarScout 一键起飞书链路 ==")

    problems = check_prerequisites(runtime, allow_no_reply=args.allow_no_reply)
    if problems:
        say("[×] config.yaml 前提没满足，先补这几项（不默默跑）：")
        for item in problems:
            say("    - " + item)
        say("    改完 config.yaml 再跑本脚本。")
        return 2
    reply_note = "开" if runtime.feishu_reply_enabled else "关（--allow-no-reply：只收不发）"
    say(f"[✓] 前提满足：监听开、端口 {port}、路径 {path}、出站 {reply_note}")

    listener: ListenerHandle | None = None
    if deps.is_listening(port, path):
        say(f"[✓] 本地监听已在运行（{listener_address(port, path)}），复用，不重复起")
        listener = ListenerHandle(address=listener_address(port, path), started=False)
    else:
        try:
            listener = deps.start_listener(port, path)
        except Exception as exc:
            say(f"[×] 起本地监听失败：{type(exc).__name__}: {exc}")
            say(f"    排查：端口 {port} 是否被占（netstat -ano | findstr :{port}）；")
            say("          或把 config.yaml 的 runtime.feishu_listener_port 换个空闲端口。")
            return 3
        say(f"[✓] 本地监听已起：{listener.address}")

    cmd = resolve_tunnel_cmd(port, override=args.tunnel_cmd)
    say(f"[…] 起隧道：{cmd}")
    tunnel = deps.start_tunnel(cmd, log_path=TUNNEL_LOG)
    if tunnel.proc is None:
        say(f"[×] 隧道启动失败：{tunnel.error or '进程没起来'}")
        first = cmd.split()[0] if cmd.split() else cmd
        say(
            f"    排查：命令「{first}」是否可用；或用 --tunnel-cmd / ${TUNNEL_CMD_ENV} 换一条隧道命令。"
        )
        _teardown(listener, say=say)
        return 4
    if tunnel.url is None:
        say(f"[×] 解析不到公网地址：{tunnel.error}")
        say(f"    隧道日志（含真实 banner）：{tunnel.log_path}")
        say(
            "    排查：看日志里有没有 https://* 的 URL；隧道进程是否立刻退出（ssh 被拒 / 端口被占）。"
        )
        _teardown(listener, tunnel, say=say)
        return 5

    callback_url = build_callback_url(tunnel.url, path)
    say(f"[✓] 公网回调地址：{callback_url}")
    say(f"    隧道日志：{tunnel.log_path}")

    ok, detail = deps.self_check(callback_url)
    if not ok:
        say(f"[×] 公网自检失败：{detail}")
        say(f"    排查：① 本地监听是否还活着（{listener_address(port, path)}）；")
        say(f"          ② 隧道是否把 {path} 转发到了本机；")
        say("          ③ 打印的地址是否已失效（免费隧道会重连换域名，重跑本脚本）。")
        _teardown(listener, tunnel, say=say)
        return 6
    say(f"[✓] 自检通过：{detail}")

    say("")
    say("== 结果 ==")
    say(
        f"    本地监听：{listener.address}  [{'本脚本已起' if listener.started else '复用外部进程'}]"
    )
    say(f"    公网回调：{callback_url}")
    say(f"    自检结果：✅ {detail}")
    say("== 下一步（去飞书后台填）==")
    say(f"    1. 应用 → 事件订阅 → 请求地址填：{callback_url}")
    say("    2. 应用 → 事件订阅 → 订阅事件勾上「接收消息 im.message.receive_v1」")
    say("    3. 保存时飞书会立刻打一次 url_verification；本脚本已先自检过，应直接通过。")
    say(
        "    备注：免费隧道地址是临时的、重启会变；要固定地址见 docs/17 的「一键启动与固定地址方案」。"
    )

    if not hold:
        _teardown(listener, tunnel, say=say)
        return 0

    say("")
    say("[保活中] 本地监听与隧道都在跑；按 Ctrl-C 停止（只停本脚本起的，不动外部进程）。")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        say("")
        say("[停止] 收到 Ctrl-C，正在收尾…")
        _teardown(listener, tunnel, say=say)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
