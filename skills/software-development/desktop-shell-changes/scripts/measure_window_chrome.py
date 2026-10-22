"""量一个 Windows 桌面窗口的外壳：非客户区高度 / 拖动位移 / 边缘缩放。

用法（stdlib，无第三方依赖）：
  python measure_window_chrome.py --cmd "C:/path/app.exe" --title ScholarScout
  python measure_window_chrome.py --cmd "C:/py.exe C:/proj/run.py --desktop" --title ScholarScout --drag

输出全 ASCII：中文控制台（GBK）会把 UTF-8 输出变成乱码，看不出来就没法当证据。

为什么要它：ctypes 那一套（EnumWindows 回调 / 窗口矩形 / 合成鼠标）每次都手写很容易错，
而且最容易错的两处是：① 只按标题认窗口（残留旧实例 → 量到旧窗口，误判“改动没生效”）；
② 没确认窗口在前台就发合成鼠标（落到别的窗口上 → 假阴性）。这两条已经内置。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import subprocess
import sys
import time

U = ctypes.windll.user32
K = ctypes.windll.kernel32
U.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
U.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
U.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

APP_IMAGE_HINTS = ("python.exe", "pythonw.exe", "scholarscout.exe", "electron.exe")


def image_name(pid: int) -> str:
    handle = K.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return "?"
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if K.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return "?"
    finally:
        K.CloseHandle(handle)


def windows(title_substr: str) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        if not U.IsWindowVisible(hwnd):
            return True
        n = U.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        U.GetWindowTextW(hwnd, buf, n + 1)
        if title_substr in buf.value:
            pid = wintypes.DWORD()
            U.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append((hwnd, pid.value, buf.value))
        return True

    U.EnumWindows(cb, 0)
    return found


def rect(hwnd: int) -> tuple[int, int, int, int]:
    box = wintypes.RECT()
    U.GetWindowRect(hwnd, ctypes.byref(box))
    return box.left, box.top, box.right, box.bottom


def kill_stale(title_substr: str, drop: int) -> None:
    """只杀看起来是本应用的进程（按映像名），绝不碰资源管理器之类。"""
    for hwnd, pid, title in windows(title_substr):
        if hwnd == drop:
            continue
        if image_name(pid).lower() in APP_IMAGE_HINTS:
            handle = K.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
            if handle:
                K.TerminateProcess(handle, 0)
                K.CloseHandle(handle)
                print("killed stale pid=%s title=%s" % (pid, title[:40]))
    time.sleep(2)


def foreground(hwnd: int) -> bool:
    for _ in range(3):
        U.SetForegroundWindow(hwnd)
        time.sleep(0.5)
    return U.GetForegroundWindow() == hwnd


def drag(x: int, y: int, dx: int, dy: int, steps: int = 8) -> None:
    U.SetCursorPos(x, y)
    time.sleep(0.4)
    U.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.25)
    for step in range(1, steps + 1):
        U.SetCursorPos(x + dx * step // steps, y + dy * step // steps)
        time.sleep(0.1)
    U.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    time.sleep(1.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmd", required=True, help="启动命令（exe 路径，或 'python xxx.py'）")
    ap.add_argument("--title", required=True, help="窗口标题子串")
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE，可重复")
    ap.add_argument("--drag", action="store_true", help="额外做一次标题区拖动试验")
    ap.add_argument("--resize", action="store_true", help="额外做一次右缘缩放试验")
    ap.add_argument("--wait", type=int, default=90, help="等窗口出现的秒数")
    ap.add_argument("--settle", type=int, default=6, help="窗口出现后等界面加载的秒数")
    args = ap.parse_args()

    kill_stale(args.title, drop=0)
    env = None
    if args.env:
        import os

        env = dict(os.environ)
        for pair in args.env:
            key, _, value = pair.partition("=")
            env[key] = value

    cmd = args.cmd.split(" ") if args.cmd.lower().endswith((".py", "--desktop")) else [args.cmd]
    before = {h for h, _, _ in windows(args.title)}
    proc = subprocess.Popen(cmd, cwd=args.cwd, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    target = None
    deadline = time.time() + args.wait
    while time.time() < deadline and target is None:
        for hwnd, pid, title in windows(args.title):
            if hwnd not in before and image_name(pid).lower() in APP_IMAGE_HINTS:
                target = (hwnd, pid, title)
                break
        if target is None:
            time.sleep(2)
    if target is None:
        proc.kill()
        print("FAIL no new window for title %r" % args.title)
        return 1

    hwnd, pid, title = target
    time.sleep(args.settle)
    print("window pid=%s title=%s" % (pid, title[:50]))

    box = wintypes.RECT()
    client = wintypes.RECT()
    U.GetWindowRect(hwnd, ctypes.byref(box))
    U.GetClientRect(hwnd, ctypes.byref(client))
    nonclient = (box.bottom - box.top) - client.bottom
    print("size=%dx%d nonclient_height=%d  -> %s"
          % (box.right - box.left, box.bottom - box.top, nonclient,
             "no native title bar" if nonclient <= 6 else "native title bar present"))

    if not foreground(hwnd):
        print("WARN window did not come to foreground; synthetic input may land elsewhere")

    if args.drag:
        start = rect(hwnd)
        x = start[0] + 120
        y = start[1] + 13
        drag(x, y, 120, 80)
        end = rect(hwnd)
        moved = (end[0] - start[0], end[1] - start[1])
        print("drag_delta=%s  -> %s" % (moved, "draggable" if moved != (0, 0) else "NOT draggable"))

    if args.resize:
        start = rect(hwnd)
        width_before = start[2] - start[0]
        drag(start[2] - 2, start[1] + 200, 80, 0)
        width_after = rect(hwnd)[2] - rect(hwnd)[0]
        print("width %d -> %d  -> %s"
              % (width_before, width_after,
                 "edge resizable" if width_after != width_before else "NOT edge resizable"))

    proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
