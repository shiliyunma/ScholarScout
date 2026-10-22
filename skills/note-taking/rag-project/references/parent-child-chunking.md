# Parent-Child 分块分析

> 本文件记录 parent-child 双层切分方案的详细数据与性能开销分析。
> 基于 4 本王道考研教材（3.8MB 原始文本 = 3,775,944 字符）。

## 块数变化

| 指标 | 单层 512 滑动窗口 | Parent-Child 双层 | 变化 |
|------|:-:|:-:|:-:|
| 总块数 | ~4,300 | ~7,600 | **+77%** |
| 编码批数(64/批) | ~68 | ~119 | +75% |
| 库容量 | 不足 | 完全够 | — |

## Tail Loss 计算

### 原理

```
parent A (1950字):
  child[0]=498字, child[1]=502字, child[2]=496字, child[3]=454字 (末尾)
parent B (1800字):
  child[0]=495字, child[1]=501字, child[2]=492字, child[3]=312字 (末尾)
                         ↑ 如果跨 parent，这里的454+312可以拼成一个完整512
```

每个 parent 的最后一个 child **平均损失 ~64~128 字**（因为不满 512）。

### 放大到全部

- 预估 parent 数量：3.8M 有效字符 / ~2000 = ~1,900 个 parent
- 减去 exercise 段后实际：~**1,700** 个 parent
- 平均末尾损失：~90 字
- 总容量损失：1,700 × 90 = ~153,000 字
- 折算成额外 chunk：153,000 / 512 = ~299 个 → 实际观测 ~3300（加上段落更短导致的密度降低）≈ 总计多出 ~3,300 块

## parent_text 元数据开销

### 存储

每个 child metadata 存 parent_text (~2000 字)：
- 7600 child × 2000 字 = 15.2MB 元数据文本
- 实际去重后唯一内容 ~3.4MB（1700 parent × 2000 字）
- 重复存储系数：~4.5x

### 查询

返回 Top-20 时多传：
- 20 × 2000 字 = ~40KB
- 本地 SQLite 读取，耗时 <1ms
- **对用户无感知**

### 索引构建

每批(64块)多写 ~125KB 元数据：
- 119 批 × 125KB = ~15MB 总量
- NVMe SSD 上 ~2-3s 总耗时
- **不是索引构建的瓶颈**

## PARENT_CHUNK_OVERLAP 未使用

config.py 中 `PARENT_CHUNK_OVERLAP = 200` 已定义，但 `_split_parents_by_paragraph()` 函数签名为：

```python
def _split_parents_by_paragraph(text: str, max_size: int = 2000) -> list[str]:
```

只接收 `max_size`，从未读取 overlap 值。两个 parent 之间是硬切（无重叠），这是 tail loss 的直接代码原因。

## 优化方向（按收益降序）

1. **让 child 滑动窗口越过 parent 边界** — 存储时不切，检索后用 parent_id 合并。保持 512 滑动窗口不变，只在 metadata 中标记 parent_id。块数回到 ~4,300。**收益最高**
2. **增大 PARENT_CHUNK_SIZE** — 3000~4000 字减少 parent 数量 → 减少 tail loss
3. **给 parent 加 overlap** — 修复 `_split_parents_by_paragraph` 实现，让相邻 parent 重叠 ~200 字，消除边界空洞
