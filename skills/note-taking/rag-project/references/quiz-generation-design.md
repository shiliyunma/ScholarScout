# 题库生成设计方案（C + D）

> 2026-07-04 讨论产出。不动代码阶段，仅方案设计。

## 前提

知识库现有两种 chunk 类型：
- `content` — 正文知识点（~4300 child 块，~1700 parent 块）
- `solution` — 答案与解析块（来自教材习题册）

## 方案 C：实时反推 — solution → 题目

**核心思想**：不生成答案，答案从库中 solution 块直接拿，LLM 只做"看了答案反推题目"。

```
用户输入 topic
  → ChromaDB 只检 type=solution
  → 召回该 topic 下的现有答案+解析
  → LLM prompt: "根据以下答案与解析，反推出最匹配的题目"
  → 输出：题目 + [库中原答案] + [库中原解析]
```

### 优点
- 答案准确率 100%（教材原文）
- 改动极小 — 现有检索链路 + 换 system prompt
- 实时，随时可用

### 风险
- 依赖 solution 覆盖度：某 topic 没有 solution 就出不了题
- 一个 solution 可能对应多个知识点，反推方向可能偏
- 反推的题目可能偏窄（单一答案 → 单一题目，出不了综合题）

### Prompt 设计要点
- 明确要求"题目必须与此答案完全匹配，不能添加额外考点"
- 指定输出格式 JSON: `{"question": "...", "answer": "..." , "analysis": "..."}`
- 如果 solution 块包含解析，优先用解析辅助反推

## 方案 D：预生成题库 — parent → 批量出题

**核心思想**：一次遍历所有 parent 块，LLM 批量出题，存入专用 ChromaDB collection。

```
离线阶段：
  加载所有 parent 块（~1700）
  每块送 LLM: "从以下知识点生成 N 道题，附带答案与解析"
  嵌入并存入 chroma collection "quiz_bank"
  元数据: source_doc, parent_id, question_type, difficulty

在线阶段：
  用户搜索 → 检索 quiz_bank → 直接返回命中的题目+答案+解析
  零 LLM 调用，<1 秒
```

### 优点
- 最快（纯向量检索）
- 支持题型多样化（选择/填空/简答）
- 可做难度分级

### 成本估算
- ~1700 parent × 3 题 = ~20-30 万输入 token + ~50 万输出 token
- 建议分批跑（10 parent/批），避免超时
- 先抽 5 个 parent 小规模验证 prompt 质量

### 坑
- 相邻 parent 内容重叠 → 可能出重复题 → 需去重逻辑
- 源文档更新后题库需重跑（快照问题）
- 预生成的角度固定，用户想考的方向不一定匹配

## 实施建议

1. **先做 C 验证可行性** — 半天能跑通，验证 solution 覆盖量够不够
2. **C 跑通后** — 如果 solution 覆盖太少，再做 D 补全
3. **D 的 prompt 可直接复刻 C 的经验**

## 相关文件

- `launch.py` / `app.py` — Web UI 入口（当前无出题模式，需加 mode 切换）
- `query.py` — CLI 入口（已有 `--raw` `--rerank-only` `--no-rewrite`，可加 `--quiz`）
- `config.py` — 集中配置（加 QUIZ_ENABLED, QUIZ_MODE 等参数）
