"""量一个 Windows 应用的窗口，并能把某个区域抓成图（只用标准库）。

    python win_window_probe.py --kill MyApp.exe
    python win_window_probe.py --title MyApp
    python win_window_probe.py --title MyApp --launch "C:/app/MyApp.exe" --shot top.bmp --width 900 --height 60

典型用途：证明“原生标题栏还在不在”“窗口尺寸对不对”，或改完界面后抓真机实况。

关键做法（都是踩出来的）：
- **按“启动前后的窗口集合差集”取窗口**，别只按标题找 —— 旧实例还开着时会量到旧窗口，
  得到的尺寸/边框全是上一版的；
- 抓图前 `SetForegroundWindow` 并**验证** `GetForegroundWindow()==hwnd`，否则拍到压在它上面的窗口；
- 写 `.bmp` 时行序要**自下而上**（`GetDIBits` 交出来的是自上而下，直接照抄会得到翻转图，白浪费一次看图）；
- 合成鼠标事件（`SetCursorPos`+`mouse_event`）未必被内嵌网页收到 —— 要测交互，优先程序化 `dispatchEvent`
  再看一个真实副作用（例如窗口 `screenX/screenY` 拖动前后变没变）。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import subprocess
import sys
import time
from pathlib import Path

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]


def image_name(pid: int) -> str:
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return "?"
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value.rsplit("\\", 1)[-1]
        return "?"
    finally:
        kernel32.CloseHandle(handle)


def windows(title_part: str | None = None) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if title_part and title_part not in buffer.value:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append((hwnd, pid.value, buffer.value))
        return True

    user32.EnumWindows(callback, 0)
    return found


def kill_by_image(name: str) -> int:
    killed = 0
    for _hwnd, pid, _title in windows():
        if image_name(pid).lower() != name.lower():
            continue
        handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if handle:
            kernel32.TerminateProcess(handle, 0)
            kernel32.CloseHandle(handle)
            killed += 1
    return killed


def rect(hwnd: int) -> tuple[int, int, int, int]:
    box = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(box))
    return box.left, box.top, box.right, box.bottom


def foreground(hwnd: int, tries: int = 4) -> bool:
    for _ in range(tries):
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.5)
    return user32.GetForegroundWindow() == hwnd


def capture(hwnd: int, out: Path, width: int, height: int) -> None:
    left, top, _right, _bottom = rect(hwnd)
    screen_dc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)
    gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, left, top, 0x00CC0020)  # SRCCOPY

    class Header(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class Info(ctypes.Structure):
        _fields_ = [("bmiHeader", Header), ("bmiColors", wintypes.DWORD * 3)]

    info = Info()
    info.bmiHeader.biSize = ctypes.sizeof(Header)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height  # 负值：让 GetDIBits 交出自上而下的行
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 24
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0)

    row_bytes = (width * 3 + 3) & ~3
    pixels = bytearray()
    for y in range(height - 1, -1, -1):  # 自下而上写：.bmp 文件格式要求
        start = y * row_bytes
        pixels += buffer.raw[start : start + width * 3]
    header = b"BM" + (14 + 40 + len(pixels)).to_bytes(4, "little") + b"\x00\x00\x00\x00"
    header += (54).to_bytes(4, "little")
    dib = (40).to_bytes(4, "little") + width.to_bytes(4, "little")
    dib += height.to_bytes(4, "little")  # 正值：文件里的行序自下而上
    dib += (1).to_bytes(2, "little") + (24).to_bytes(2, "little") + (0).to_bytes(4, "little")
    dib += len(pixels).to_bytes(4, "little")
    dib += (2835).to_bytes(4, "little") * 2 + (0).to_bytes(4, "little") * 2
    out.write_bytes(header + dib + bytes(pixels))
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(0, screen_dc)


def main() -> int:
    parser = argparse.ArgumentParser(description="量/抓一个 Windows 应用的窗口")
    parser.add_argument("--title", help="窗口标题包含这个子串")
    parser.add_argument("--launch", help="先启动这个命令（整串引起来）")
    parser.add_argument("--wait", type=int, default=90, help="等窗口出现的秒数")
    parser.add_argument("--shot", help="抓图输出路径（.bmp）")
    parser.add_argument("--width", type=int, default=900)
    parser.add_argument("--height", type=int, default=60)
    parser.add_argument("--kill", help="收尾：按进程名杀掉实例")
    args = parser.parse_args()

    if args.kill:
        print("已杀实例：", kill_by_image(args.kill))
        return 0

    before = {hwnd for hwnd, _pid, _t in windows()}
    proc = None
    if args.launch:
        proc = subprocess.Popen(args.launch, shell=True)

    target = None
    deadline = time.time() + args.wait
    while time.time() < deadline and target is None:
        for hwnd, pid, title in windows(args.title):
            if hwnd in before:
                continue  # 旧的同标题窗口：别量它
            target = (hwnd, pid, title)
            break
        if target is None:
            time.sleep(2)
    if target is None:
        print("!! 没等到新窗口")
        if proc:
            proc.kill()
        return 1

    hwnd, pid, title = target
    time.sleep(3)
    box = wintypes.RECT()
    client = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(box))
    user32.GetClientRect(hwnd, ctypes.byref(client))
    win_w = box.right - box.left
    win_h = box.bottom - box.top
    caption = win_h - client.bottom
    print(f"窗口 pid={pid} 标题={title[:40]}")
    print(f"窗口 {win_w}x{win_h}｜客户区 {client.right}x{client.bottom}｜非客户区高 {caption}px")
    print("=>", "没有原生标题栏" if caption <= 6 else f"原生标题栏+边框约 {caption}px")

    if args.shot:
        if not foreground(hwnd):
            print("!! 置前失败，抓图可能拍到别的窗口（别当证据用）")
        out = Path(args.shot)
        out.parent.mkdir(parents=True, exist_ok=True)
        capture(hwnd, out, min(args.width, win_w), min(args.height, win_h))
        print("已写：", out)

    if proc:
        proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
