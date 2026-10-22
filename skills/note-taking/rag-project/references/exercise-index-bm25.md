# 习题索引：MySQL exercise_index + BM25 搜索

## 表结构

```mysql
CREATE TABLE exercise_index (
    id INT AUTO_INCREMENT PRIMARY KEY,
    subject VARCHAR(32),           -- 数据结构/操作系统/计算机组成原理/计算机网络
    chapter INT,                   -- 章号
    chapter_title VARCHAR(256),    -- 章名
    section VARCHAR(16),           -- 节号 e.g. 1.1
    section_title TEXT,            -- 节名（可能含合并正文）
    exercise_num VARCHAR(16),      -- 题号 e.g. 01
    exercise_type VARCHAR(16),     -- 单选 / 综合
    exercise_text MEDIUMTEXT,      -- 题干全文（含选项）
    answer VARCHAR(32),            -- 答案字母
    solution_text MEDIUMTEXT,      -- 解析全文
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_subject (subject),
    INDEX idx_chapter (chapter),
    INDEX idx_section (section)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

## 全文索引（ngram）

```mysql
CREATE FULLTEXT INDEX idx_ft_exercise ON exercise_index
    (exercise_text, section_title, solution_text)
    WITH PARSER ngram;
```

ngram parser 默认 n=2，将中文拆成双字 Token。纯中文搜索效果好，但对含符号的词（B+树、TCP/IP）分词差。

## BM25 + LIKE 双策略（`_search_exercises`）

1. 提取关键词（去掉指令词：给我/相关/一些/习题/题目/练习）
2. **BM25**: 关键词拆成 2-字片段 → `MATCH ... AGAINST (... IN BOOLEAN MODE)`
   - 替换掉 `+\-*()~@` 等 BOOLEAN MODE 操作符
   - `+seg*` 表示必须包含该 2-gram
3. **LIKE**: `%原始关键词%` 兜底
4. 合并去重，BM25 结果排在前面

## 解析脚本 `scripts/build_exercise_index.py`

读取 4 个清洗后 .md 文件：

1. 定位 `本节试题精选`（习题段）和 `答案与解析`（解析段）
2. 解析单选题号 `01. xxx` + 选项 `A. xxx B. xxx`
3. 解析综合题号 `01. 对于两种不同的数据结构...`
4. 解析答案行 `## 01. D 抽象数据类型（ADT）描述了...` → group(1)=题号, group(2)=答案, group(3)=解析
5. 通过 `(章_key, 题型, 题号)` 匹配习题→解析
6. 写入 MySQL（~2,400 条）

## 答案行格式陷阱

答案和解析在同一行：`## 01. D 抽象数据类型（ADT）描述了...`
- 正确正则：`^##\s+(\d{2,})\.\s*([A-Ea-e]+)\s+(.*)$`
- 不要用 `$` 锚点（行尾断言）
- 解析脚本中 `current_solution_lines` 初始化为 `[body_text]` 而非 `[]`，确保解析正文被捕获
