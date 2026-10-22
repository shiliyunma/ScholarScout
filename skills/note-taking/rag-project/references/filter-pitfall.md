# solution 块渗入知识库检索 — 问题记录

## 现象

知识库模式下，返回的参考来源中混入了习题答案/解析块（`type=solution`），导致 LLM 回答中引用了习题答案内容，与正文知识点混淆。

## 根因

过滤条件将 `solution` 纳入了知识库模式：

**query.py L423 / app.py L155：**
```python
where = {"type": {"$in": ["content", "solution"]}} if not args.practice
```

所以知识库的候选集 = content + solution。当用户搜索知识点时，解析块（"15.【解答】..."）因为文风接近、关键词重叠，可能被召回并排进 Top-5。

## 修复方向

将知识库改为只检索 `content`，习题库保留 `exercise + solution`：

```python
# 知识库模式（改后）
where = {"type": {"$in": ["content"]}}

# 习题库模式（不变）
where = {"type": {"$in": ["exercise", "solution"]}}
```

## 副作用评估

- 如果用户希望"回答时自动引用解析内容"（某些知识点在解析里有详细解释），单纯去掉 solution 可能导致信息丢失
- 替代方案：保留 solution 但在 prompt 中让 LLM 区分"正文"和"解析"来源，但 prompt 约束不够可靠
- **当前状态：未修复。** 因为涉及代码修改，需用户确认计划后动手。

## 涉及文件

- `E:\RAGLearn\query.py` L423
- `E:\RAGLearn\app.py` L155

两处需同步修改。
