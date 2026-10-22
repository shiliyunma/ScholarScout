# -*- coding: utf-8 -*-
"""校验一个工作区的 .vscode/ 配置：JSON 能否解析 + 配置里引用的解释器/脚本是否真实存在。

用法：python verify_vscode_configs.py [工作区根目录，默认当前目录]
退出码：有问题 = 1。

为什么需要：JSON 写对了不等于跑得通——路径拼错、解释器不在、脚本改名这几种错
write_file 的语法校验一个都拦不住，但 F5 时会直接失败。
"""
import json
import os
import re
import sys


def strip_vars(s, root):
    return s.replace("${workspaceFolder}", root).replace("${file}", "<当前文件>")


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    vsc = os.path.join(root, ".vscode")
    problems, checked = [], 0

    if not os.path.isdir(vsc):
        print(f"!! 没有 .vscode 目录：{vsc}")
        return 1

    for fn in sorted(os.listdir(vsc)):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(vsc, fn)
        checked += 1
        try:
            data = json.load(open(p, encoding="utf-8"))
            print(f"OK  JSON 解析通过  .vscode/{fn}")
        except Exception as e:
            problems.append((fn, f"JSON 解析失败: {e}"))
            continue

        if fn == "launch.json":
            for cfg in data.get("configurations", []):
                prog = strip_vars(str(cfg.get("program", "")), root)
                if prog and "<" not in prog and not os.path.exists(prog):
                    problems.append((fn, f"配置「{cfg.get('name')}」的 program 不存在: {prog}"))
                elif prog and "<" not in prog:
                    print(f"    OK  配置「{cfg.get('name')}」-> {os.path.relpath(prog, root)}")

        elif fn == "tasks.json":
            for task in data.get("tasks", []):
                cmd = strip_vars(str(task.get("command", "")), root)
                if not cmd:
                    continue
                exists = os.path.exists(cmd) or bool(re.match(r"^[\w\.\-]+$", cmd))
                if not exists:
                    problems.append((fn, f"任务「{task.get('label')}」的 command 不存在: {cmd}"))
                else:
                    print(f"    OK  任务「{task.get('label')}」-> {os.path.basename(cmd)}")

        elif fn == "mcp.json":
            for name, srv in (data.get("servers") or {}).items():
                cmd = strip_vars(str(srv.get("command", "")), root)
                if cmd and not os.path.exists(cmd) and not re.match(r"^[\w\.\-]+$", cmd):
                    problems.append((fn, f"服务器「{name}」的 command 不存在: {cmd}"))
                elif cmd:
                    print(f"    OK  MCP 服务器「{name}」-> {os.path.basename(cmd)}")
                for arg in srv.get("args", []):
                    a = strip_vars(str(arg), root)
                    if a.endswith(".py") and not os.path.exists(a):
                        problems.append((fn, f"服务器「{name}」的 args 里的脚本不存在: {a}"))

    env = os.path.join(vsc, ".env")
    if not os.path.exists(env):
        problems.append((".env", "缺失：settings.json 的 python.envFile 会指向它（代理/编码等环境变量靠它注入调试会话）"))
    else:
        print("OK  .vscode/.env 存在")

    print(f"\n检查了 {checked} 个 JSON；问题 {len(problems)} 个")
    for f, msg in problems:
        print(f"  !! .vscode/{f}: {msg}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
