"""资料归档与索引（docs/19）。

- :mod:`scholarscout.library.naming` —— 从库与 provider 元数据推命名要的：类型 / 发表年月 / 收录年月 / 真标题 / 会话目录名
- :mod:`scholarscout.library.index` —— 生成 ``data/library/**/index.md``（给人看的清单）
- :mod:`scholarscout.library.migrate` —— 任意已知旧布局 → 当前布局的一次性迁移（布局是参数）

本包只依赖下层（``paths`` / ``state``），不碰 runtime / rpc / desktop / tools。
"""
