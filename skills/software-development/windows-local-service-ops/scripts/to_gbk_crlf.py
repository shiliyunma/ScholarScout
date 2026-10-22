"""把一个（或一批）.bat / .cmd / .ps1 转成中文 Windows 能正确解析的形式：GBK + CRLF + chcp 936。

为什么必须过一遍：write_file（以及任何 UTF-8 写工具）产出的 .bat，在中文 Windows 上会被 cmd 按
控制台码页 936(GBK) 解码 -> 中文全变乱码 -> **乱码被当命令执行**，终端刷出
    '鐗堬級' 不是内部或外部命令，也不是可运行的程序
    '瀵嗙爜' 不是内部或外部命令，也不是可运行的程序
（鐗堬級=「版）」、瀵嗙爜=「密码」）。另一种表现是中间几段 echo 整段消失而 exit code 仍是 0。

用法：
    py -3.12 to_gbk_crlf.py <文件> [<文件> ...]
"""
import os
import shutil
import sys
import time


def to_crlf(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def fix(path: str) -> str:
    raw = open(path, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]

    try:
        txt = raw.decode("utf-8")
        src = "utf-8"
    except UnicodeDecodeError:
        try:
            txt = raw.decode("gbk")
            src = "gbk(已是)"
        except UnicodeDecodeError:
            return f"[跳过] {path}: 既不是 UTF-8 也不是 GBK，人工看"

    pure_ascii = all(ord(c) < 128 for c in txt)

    if not pure_ascii:
        if "chcp 65001" in txt:
            txt = txt.replace("chcp 65001", "chcp 936")
        if "chcp 936" not in txt:
            lines = txt.splitlines()
            insert_at = 1 if lines and lines[0].strip().lower().startswith("@echo off") else 0
            lines.insert(insert_at, "chcp 936 >nul")
            txt = "\r\n".join(lines) + "\r\n"

    out_txt = to_crlf(txt)

    bak = path + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, bak)

    if all(ord(c) < 128 for c in out_txt):
        out = out_txt.encode("ascii")
        enc = "ascii"
    else:
        try:
            out = out_txt.encode("gbk")
        except UnicodeEncodeError as e:
            return f"[失败] {path}: 有 GBK 编不了的字符 -> {e}"
        assert out.decode("gbk") == out_txt, "GBK 往返不一致"
        enc = "gbk"

    open(path, "wb").write(out)

    # 回读断言：写盘的东西必须能解回原文
    back = open(path, "rb").read()
    assert back == out, "回读不一致"
    assert b"\r\n" in back or b"\n" not in back, "还有裸 LF"

    n_nonascii = sum(1 for line in back.split(b"\r\n") if any(c > 127 for c in line))
    return (
        f"[已修] {os.path.basename(path):30s} {src} -> {enc}, CRLF, "
        f"{len(back)} 字节, 非ASCII行={n_nonascii}   (备份 {os.path.basename(bak)})"
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for a in sys.argv[1:]:
        print(fix(a))
