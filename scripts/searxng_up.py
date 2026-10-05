"""把 SearxNG 拉起来 —— 一条命令解决"通用检索不可用"。

为什么需要这个脚本：通用检索走的是**自建 SearxNG**（`deploy/searxng/`），
而它是 Docker 里的一个容器。Docker Desktop 关掉之后容器不会自己回来
（容器设了 `restart: unless-stopped`，但那只在 Docker 守护进程活着时生效），
于是应用里的通用检索会静默失效 —— 真实发生过的故障（检索报连接失败，用户不知道要开 Docker）。

这个脚本把整条链路串成一条命令：
    1. 检查 docker CLI 与守护进程；没起就启动 Docker Desktop 并等就绪
    2. docker compose up -d（容器起来）
    3. 轮询 /config，等到**至少一个引擎被启用**（SearxNG 重启后加载引擎要十几秒，
       太早查会得到空结果，看着像"引擎全挂"）
    4. 用**我们自己的 provider** 打一次真查询自检（不是 curl 容器了事）
    5. 打印结论与后续用法

用法：
    .venv/Scripts/python.exe scripts/searxng_up.py              # 起服务 + 自检
    .venv/Scripts/python.exe scripts/searxng_up.py --status     # 只看状态，不动任何东西
    .venv/Scripts/python.exe scripts/searxng_up.py --query "知识追踪" --limit 5
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from scholarscout.fetch.http import HttpClient  # noqa: E402
from scholarscout.providers.base import Query  # noqa: E402
from scholarscout.providers.searxng import SearxngProvider  # noqa: E402

COMPOSE_DIR = ROOT / "deploy" / "searxng"
BASE_URL = "http://localhost:8080"
#: 官方镜像名（docker-compose.yml 引用的就是它）
IMAGE = "searxng/searxng:latest"
#: 国内可用的镜像源（真机验证过 DAOCLOUD 能拉；拉完重打成官方标签，compose 才认得）
MIRROR_IMAGE = "docker.m.daocloud.io/searxng/searxng:latest"
DOCKER_DESKTOP_CANDIDATES = (
    Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe"),
    Path(r"C:\Program Files (x86)\Docker\Docker\Docker Desktop.exe"),
)

#: Docker Hub 不可达时命令行里会出现的特征串（compose/pull 两种写法都覆盖）
REGISTRY_FAIL_MARKERS = (
    "failed to resolve reference",
    "registry-1.docker.io",
    "no such host",
    "connection attempt failed",
    "TLS handshake timeout",
    "i/o timeout",
)


def _looks_like_registry_unreachable(output: str) -> bool:
    """判断失败是不是"拉不到镜像"（而不是别的问题，比如端口被占）。"""
    text = (output or "").lower()
    return any(marker.lower() in text for marker in REGISTRY_FAIL_MARKERS)


def _pull_via_mirror() -> tuple[int, str]:
    """换镜像源拉取，再重打成官方标签（两层都失败就如实返回错误）。"""
    code, output = run(["docker", "pull", MIRROR_IMAGE], timeout=900)
    if code != 0:
        return code, output
    return run(["docker", "tag", MIRROR_IMAGE, IMAGE], timeout=60)


def run(cmd: list[str], *, timeout: int = 120) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=COMPOSE_DIR)
    except FileNotFoundError:
        return 127, f"找不到命令：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"命令超时（{timeout}s）：{' '.join(cmd)}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def daemon_ready() -> bool:
    code, _ = run(["docker", "info"], timeout=30)
    return code == 0


def start_docker_desktop() -> str:
    for exe in DOCKER_DESKTOP_CANDIDATES:
        if exe.is_file():
            subprocess.Popen([str(exe)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"已发起启动：{exe}"
    return "没找到 Docker Desktop.exe，请手动打开 Docker Desktop"


def wait_for_daemon(seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if daemon_ready():
            return True
        time.sleep(3)
    return False


def enabled_engines(timeout_s: float = 8.0) -> list[str] | None:
    """容器里的启用引擎名单；容器没起返回 None。"""
    try:
        response = httpx.get(f"{BASE_URL}/config", timeout=timeout_s)
        payload = response.json()
    except Exception:
        return None
    engines = payload.get("engines") if isinstance(payload, dict) else None
    if not isinstance(engines, list):
        return None
    return [
        str(item.get("name")) for item in engines if isinstance(item, dict) and item.get("enabled")
    ]


def wait_for_engines(seconds: int) -> list[str]:
    deadline = time.monotonic() + seconds
    last: list[str] = []
    while time.monotonic() < deadline:
        names = enabled_engines()
        if names:
            return names
        last = names or []
        time.sleep(3)
    return last


def main() -> int:
    parser = argparse.ArgumentParser(description="拉起 SearxNG（通用检索）并自检")
    parser.add_argument("--status", action="store_true", help="只看状态，不启动任何东西")
    parser.add_argument("--query", default="knowledge tracing", help="自检用的查询")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--wait", type=int, default=180, help="等 Docker 守护进程就绪的秒数")
    args = parser.parse_args()

    print("== 通用检索（自建 SearxNG）状态检查 ==")
    names = enabled_engines()
    if names:
        print(f"[OK] 容器在跑，启用引擎：{', '.join(names)}")
    else:
        print("[--] 容器不可达（http://localhost:8080 没有响应）")

    if args.status:
        if names:
            return 0
        print("     修复：.venv/Scripts/python.exe scripts/searxng_up.py")
        return 1

    if not names:
        if not daemon_ready():
            print("[1/4] Docker 守护进程没起 →", start_docker_desktop())
            print(f"[1/4] 等守护进程就绪（最多 {args.wait}s）…")
            if not wait_for_daemon(args.wait):
                print("[FAIL] Docker 守护进程一直没就绪。手动打开 Docker Desktop 后重跑本脚本。")
                return 1
            print("[1/4] 守护进程就绪")
        else:
            print("[1/4] Docker 守护进程正常")

        print("[2/4] 起容器（docker compose up -d）…")
        code, output = run(["docker", "compose", "up", "-d"], timeout=300)
        print("     " + (output.splitlines()[-1] if output else "（无输出）"))
        if code != 0 and _looks_like_registry_unreachable(output):
            # 国内常见：镜像不在本地 + registry-1.docker.io 不可达。
            # 手动救法是换镜像源拉取再重打官方标签 —— 这个别人踩过、我们踩过的坑，
            # 脚本里替用户做完，别让"新机器第一次跑"卡在这。
            print("[2/4] Docker Hub 不可达 → 换镜像源（docker.m.daocloud.io）重试")
            code, output = _pull_via_mirror()
            if code != 0:
                print("[FAIL] 换镜像源也没拉下来，原始输出：")
                print(output)
                return 1
            print("     " + (output.splitlines()[-1] if output else "（无输出）"))
            code, output = run(["docker", "compose", "up", "-d"], timeout=300)
            print("     " + (output.splitlines()[-1] if output else "（无输出）"))
        if code != 0:
            print("[FAIL] 容器没起来，原始输出：")
            print(output)
            return 1

        print("[3/4] 等引擎加载（SearxNG 重启后要十几秒，太早查会得到空结果）…")
        names = wait_for_engines(90)
        if not names:
            print("[FAIL] 容器起来了但一个引擎都没启用 —— 检查 deploy/searxng/settings.yml 的")
            print(
                "       use_default_settings.engines.keep_only 与 engines:[{name,disabled:false}]"
            )
            return 1
        print(f"[3/4] 启用引擎：{', '.join(names)}")
    else:
        print("[1/4]-[3/4] 跳过（容器已在运行）")

    print("[4/4] 用我们的 provider 打一次真查询自检…")
    http = HttpClient(respect_robots=True, cache_dir=ROOT / "run" / "fetch_cache")
    try:
        cards = SearxngProvider(http, base_url=BASE_URL).search(
            Query(text=args.query, limit=args.limit)
        )
    except Exception as exc:
        print(f"[FAIL] provider 自检失败：{type(exc).__name__}: {exc}")
        return 1
    finally:
        http.close()

    print(f"[OK] 命中 {len(cards)} 条：")
    for card in cards[:3]:
        print(f"     - {card.title[:60]}")
        print(f"       {card.url[:90]}")
    if not cards:
        print("[FAIL] 容器通了但 provider 拿到 0 条 —— 看引擎是否被目标站点限流/验证码")
        return 1

    print("\n== 结论 ==")
    print("通用检索可用。config.yaml 里保持：")
    print('  search:\n    searxng_base_url: "http://localhost:8080"')
    print("之后每次开着 Docker Desktop 就能用；重启机器后若检索报错，重跑本脚本即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
