# 笔试 (written test) sources & formats — 中文校招

## Rules

- **题目只给链接.** The user asks for real links to questions, not reproduced text.
  Hand over the URL of the platform page / 真题帖. Never paste a reconstructed
  question list as if it were the paper.
- **Open each link once before shipping the list** (see SKILL.md hard rules).
  Some 牛客 pages are JS-hydrated: `document.title` + a tiny innerText still means
  the URL is valid — judge by title, not by body length.
- Hub links (platform entry) survive time; single-paper links are stable too but
  present them as examples of a pattern so the report does not rot.

## Question banks / 真题 entry points

| Source | URL | Yields |
|---|---|---|
| 牛客·公司真题(笔试) 总入口 | https://www.nowcoder.com/contestRoom | 各公司历年笔试真题列表；`?categories=0` 只留技术(软件)类 |
| 牛客·具体试卷 | https://www.nowcoder.com/exam/test/\<id\>/summary | 真卷 + 全真模拟；标题带公司+岗位+日期，可挑同岗位族的卷子 |
| 牛客·知识点专项练习 | https://www.nowcoder.com/intelligentTest | Java/C++/数据结构/操作系统/网络/数据库 选择题 |
| 牛客·在线编程 | https://www.nowcoder.com/activity/oj?tab=0 | 算法笔面试篇（另有 SQL / 前端 tab） |
| 牛客·模拟笔试 | https://www.nowcoder.com/mockexam/MockExam | 卡时间的线上模考 |
| 牛客·试题广场 | https://www.nowcoder.com/questionCenter | 按知识点刷真题、找同类题 |
| 牛客·讨论区真题解析 | https://www.nowcoder.com/discuss/\<id\> | 公司笔试原题+题解（选择题型、编程题、智力题），最稀缺的一类 |
| 牛客·站内搜索取链接 | https://www.nowcoder.com/search?query=\<公司\>+笔试&type=all | 抽 `/exam/test/`、`/discuss/` 链接批量产出真题入口 |
| 赛码网·真题练习 | https://www.acmcoder.com/#/practice/index | 名企真题 + 往届成绩线（腾讯/阿里/字节/美团/京东/滴滴/华为/网易） |
| LeetCode 中国 | https://leetcode.cn/problemset/ ｜ https://leetcode.cn/studyplan/coding-interviews/ ｜ https://leetcode.cn/problem-list/top-100-liked-questions/ | 刷题主库 + 学习计划 + 热题 100 |
| CodeTop | https://codetop.cc/home | 按公司/频度排序的高频面试题 |
| 卡码网 KamaCoder | https://kamacoder.com/ | 大厂笔试的 ACM 输入输出模式训练 |

## Format knowledge (durable)

- 岗位族决定卷面：研发岗 = 在线编程 + 选择题（数据结构/操作系统/网络/数据库/Linux）；
  算法岗 = 编程为主 + 机器学习/数学；运营商与国企 = 赛码或自研考试客户端，常含行测与性格测试，
  部分省级公司**没有专业题**；安全厂商 = 编程 + 安全基础（等保、漏排/IDS/日志审计概念）；
  AI 应用/大模型岗 = 近年新增 **AI Coding** 题型（用 LLM 写或改代码、写 prompt、
  前后端小交互），只刷 LeetCode 不够。检索 `牛客 AI Coding 题型` 看当年总结。
- 流程常见为 网申 → 在线笔试/测评 → 技术面 → 综合面 → Offer，但安全行业有批次
  **直接面试无笔试**，也有公司写"笔试（部分岗位）"。所以笔试要求必须写成
  "公司 + 批次 + 来源链接"，不要上升成行业通则。
- 先确认这一批用哪家平台：牛客 / 赛码 / 自研客户端决定界面与调试体验（有些禁切屏、
  单机位摄像头、性格测试上百题）。平台信息来自公告流程页、牛客面经帖（搜索
  `<公司> 笔试 体验`）或往届真题页面本身。

## Where the per-company answer comes from

1. 校招公告/聚合站详情页的"招聘流程"分步（最权威，直接引用并附链接）。
2. 牛客 `/discuss` 面经与真题帖（写到"面试官流程 + 题型 + 平台"级别）。
3. `/exam/test/<id>/summary` 的真卷标题（含公司、岗位、日期）——证明该公司的卷子长什么样。

## 用牛客搜索页取真实考情（SSR 抓取配方）

这是**一份卷面情报的最快来源**，比引擎 snippets 具体一个量级：

```bash
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
# 逐条换 query（中文要 URL 编码），一次一个 term，落盘再解析
curl -sS -L --max-time 25 -A "$UA" \
  "https://www.nowcoder.com/search?type=post&query=<urlencoded 公司名+笔试>"
```

- 去标签后的正文里能拿到：**题型配比**（"15 道选择题 + 3 道编程题"）、**具体考点**
  （数据结构/OS/网络/数据库分别考什么）、**平台与输入模式**（ACM 模式 / 代码填空 /
  看代码写输出）、**编程题难度自述**（"就一道贪心，5 分钟写完"）、
  **整条时间线**（投递→笔试→一面→二面→HR 各在哪天）。
- **考生的"原话"比二手总结值钱**：他们把最反直觉的一条留着（"没有算法，主要是高精度"、
  "写错两个方法名，没加括号"）。这些是复习优先级，不是趣闻。
- 同一批结果里常混入**同名/近名公司**（"迈瑞/迈瑞医疗"、"吉塔/喆塔"）——用岗位名与
  城市交叉确认，别把别家的卷面写进这家档案。
- 抓不到（title-only / 空 body）= 该查询被限流：换 `type=post`/`type=all`、换关键词组合
  （`<公司> 笔试` → `<公司> 面经`），或隔一会儿再来；**不要**对同一 URL 连打。
- 原始 HTML 与去标签文本**都落盘**（`raw-nowcoder-*.html` + `<公司>-考情汇总.md`），
  报告里逐条标出处 URL——这些帖子会被删，出处是唯一能回溯的东西。

## 交付：备考文档的两栏标注（真题 vs 同类题）

任何"真题 + 同类题"的备考交付，**逐题标注来源**：

- 🔴 **真题** = 考生明确回忆到的题目或题型（附出处），原题不臆造；
- ⚪ **同类题** = 按真题题型自拟的覆盖题。

再加一张**「备考预判 vs 实测」偏差表**（哪些预判的重灾区没考、哪些被推翻、下一场按什么
口径备）——它比题库本身更能改下一轮的复习优先级。考生自述的难度信号（"就一道算法题，
很简单"）直接写进战术建议：**别在算法难题上耗时间**。

## 考后当晚复盘（四段式 + 两条硬坑）

**卷面情报只能从两个地方取：本人回忆、卷面本身。**

- 🔴 **平台页上的「开放窗口」不是考试时长**：页面写 18:00–21:00，实测整卷就是 **2 小时**。
  把窗口当时长会推出「他提前交卷 / 时间没花完」的错误结论，进而给出错误的改进建议。
  时长、题量、时间分配、做题顺序 **逐项问清**再写进复盘。
- **逐字采纳本人回忆**，自己补的解读单独标 `我解读`，拿不准的进「待确认」小节 ——
  复盘是下一场复习优先级的依据，写进去的猜会变成下一轮的事实。

**四段式**：① 卷面实况（题型 / 题量 / 时长 / 时间分配 / 做题顺序）② **与考前预判的偏差对照**
（把「预判 vs 实测」写成表 —— 这是整份复盘里信息量最大的一节，它校准的是下一轮的备考投入）
③ 逐题记录 + 与「同平台、同方向」旧卷的对照（手感可直接复用）④ 暴露的问题 → 下次怎么改
（每条给「归因 + 改法」，不给「下次注意」）。

落盘位置沿用 `4-刷题\<公司>笔试-<MMDD>\真题复盘-<MMDD>.md`，并在当日进度里挂一行指针。

## AI Coding 题（AI 应用 / 大模型岗的新主项）

- **一家真实卷面**（AI 软件开发岗，全卷 2 小时）：**2 道编程题 + 1 道 AI Coding，无选择题**；
  AI Coding 先做、占约 75 分钟（≈ 63% 时长）。**别按「几十道选择 + 编程」的常规卷面备** ——
  这类岗整卷压在写代码上，题库型选择题的投入产出比很低。
- **AI Coding 给三层骨架 + 一个业务域，各家模板高度同构**：接口层 `http.py` / 存储层
  `store.py`|`data.py` / 业务层 `kitting.py`|`service.py`。**先看文件名词根猜业务域**
  （`kitting` ＝ 物料齐套、`order` ＝ 订单、`ledger` ＝ 账务），数据结构与命名直接对着业务起，
  省掉大量重新理解需求的时间。
- **评分按「测试档位」递进给分**（实测四档：冒烟 → 基础业务 → 进阶业务 → 高难度业务）。
  应试策略就是 **从上往下一档一档锁分**：宁可「冒烟＋基础＋进阶稳过、高难度只做一半」，
  也别为了追高难度特性把冒烟跑挂 —— **打分表本身就是应试说明书**。
- **评分标准里明写「与 AI 助手的对话过程」** → 得分点是 需求拆解 → 定接口/数据结构 →
  让 AI 按层填实现 → 自己跑测试逐档过，而不是手写算法。
- **两条真丢分点**：① 拿到含糊业务描述**不先拆需求就动手**，尤其「高级特性」这类成批条目容易漏
  —— 前 3–5 分钟把「基础数据结构 / CRUD / 高级特性」逐条抄成清单再动手；
  ② **不在 AI Coding 上设时间封顶** —— 定一个硬性切出点（如 60–70 分钟），到点先去做编程题
  （编程题是「确定能拿到」的分），做完再回来补高级特性。
