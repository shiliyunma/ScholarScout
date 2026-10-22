"""``py.exec`` 的沙箱执行器与审批门（docs/24）。

两个模块各管一件事，**互不越界**：

- :mod:`scholarscout.sandbox.runner` —— 「怎么跑」：独立子进程 + Windows Job Object
  （内存/进程数/超时杀整棵树）+ 子进程内的 ``sys.addaudithook``；
- :mod:`scholarscout.sandbox.approval` —— 「准不准跑」：硬底线、风险分类、四选项授权、
  按「类别 × 路径范围」记规则、连拒熔断；
- :mod:`scholarscout.sandbox.rules` —— 「规则存哪里」：``<home>/run/sandbox_rules.json``。

**这不是安全边界**（docs/24 §2 写死了这句话）：能拦住的是「模型写在 Python 里的越界访问」，
拦不住「用户点了允许」这件事本身 —— 真正的主护栏是**运行前的人工确认**。
"""

from __future__ import annotations

__all__: list[str] = []
