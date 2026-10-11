"""资料归档与索引（docs/19）。

- :mod:`scholarscout.library.naming` —— 从 provider 元数据推命名要的三件：类型 / 年-月 / 真标题
- :mod:`scholarscout.library.index` —— 生成 ``data/library/**/index.md``（给人看的清单）
- :mod:`scholarscout.library.migrate` —— 旧命名 → 新布局的一次性迁移

本包只依赖下层（``paths`` / ``state``），不碰 runtime / rpc / desktop / tools。
"""
