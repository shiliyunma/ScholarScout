---
name: china-campus-recruiting-research
description: "Use when researching a Chinese employer for 校招/秋招."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Jobs, CampusRecruiting, China, CompanyResearch, 校招, 秋招]
    related_skills: [resume-tailoring, resume-optimization-zh, grounded-citations]
---

# China Campus-Recruiting Research (校招/秋招)

Use when the user names a Chinese employer and asks what it does / what campus
roles it hires / how the process works / what the requirements are, or says
"调研这家公司" while job hunting. Goal: a sourced, verifiable profile that lets
the user apply without re-researching anything.

## Deliverable (this is what "done" means)

1. **One Chinese markdown file on disk**, named `<公司>-公司调研与校招信息.md`, written
   into the user's job-hunting workspace (list the workspace first; if it keeps
   dated folders, use the current one). Report the absolute path in chat — this
   user drives files themselves. For a multi-position sweep the file is
   `<主题>-匹配清单-<姓名>-<届>.md` (e.g. 岗位匹配+笔试真题清单) in the same
   dated folder.
2. **A short chat digest**: company one-liner, product direction in <=5 bullets,
   positions/process as a table, the exact 投递 channel + title format, and the
   honest gap statement when the current season has no public announcement.
3. **Sources inline per section** + an explicit check date ("截至 <today> 未检索到 X").
   State absence as "not found as of <date>", never as permanent fact.

## Procedure

1. **Official site first** (no login, highest value). Map the nav: 公司简介
   (scale, funding, 双总部/branches, customer count, labs, honors), 人才招聘
   (in-flight 社招+实习生 JDs — their verbatim 任职资格 is the best interview-prep
   proxy), 产品中心 (**paginate to the last page and enumerate every product** —
   breadth IS the product-direction answer), 公司新闻 (financing, rankings,
   industry certifications), 解决方案. Also read every job-detail page of the
   R&D roles: copy requirement bullets verbatim into the report.
   - **官网的 about 类路径多半要尾斜杠**：`/about/`、`/about/company/` 才有内容，去掉尾斜杠直接 404
     —— **一次 404 不代表该页不存在**。可靠做法是先抓首页 HTML，
     `re.findall(r'href=["\']([^"\']+)["\']', html)` 把导航路径全列出来再照着取。
   - **官网上通常没有管理层信息**：就算看完 公司简介 / 投资者咨询 / 公司动态，也大概率只有成立年份、
     定位、里程碑、地址、电话。**别在官网里反复翻找「老板是谁」**，直接走 §13。
2. **Campus aggregators carry the full 公告 text** when the company only ships an
   image poster. Search `"<公司> <届>春招"` / `"<公司> <届>秋招"` and open the
   aggregator page — it reproduces 岗位表, 流程步骤, 投递方式+邮件标题格式, 截止时间,
   培养模式. Company-profile aggregators (职友集-style) give 规模/融资/办公地址/
   合作院校 even when their salary and 点评 subpages are login-walled.
3. **Official WeChat article** (mp.weixin.qq.com): read the title and the
   上一篇/目录 footer — it exposes the 校招 series. The body is usually an IMAGE
   POSTER, so an empty innerText is EXPECTED, not a fetch failure: do not
   retry-scroll it, go back to step 2 for the same 公告 as text.
4. **Target-school 就业网 / 学院公告** for 宣讲会 entries — they reveal which
   schools the company visits and the season's real timing (e.g. an autumn talk
   dated mid-October of the previous cycle).
5. **Infer the season, never fabricate it.** Historic rhythm: 秋招 starts ~Sep–Oct
   of the preceding year, 春招 ~Mar. Present it as inference and pair it with a
   monitoring checklist (official 人才招聘 page, company WeChat account, target
   schools' job boards, campus aggregator keyword hits, and the HR mailbox —
   which accepts resumes even between announcements).
6. **Position-pool matching (简历 → 多个岗位).** When the ask is "根据我的简历搜索所有
   相关岗位" rather than one employer: read the resume and turn each skill block into
   a 岗位关键词族 (AI Agent/大模型应用、Java/后端、算法/数据、C++/客户端、安全、测试),
   then crawl a campus aggregator's **list** pages and filter locally — query params
   on those lists do not filter. Full crawl recipe + field selectors in
   `references/china-company-research-sources.md`.
7. **笔试 research.** Search `<公司> 笔试` on 牛客 and link the *real* pages:
   `/exam/test/<id>/summary` (真题试卷), `/contestRoom` (per-company banks),
   `/discuss/<id>` (题解帖), plus 赛码/LeetCode/CodeTop/卡码网 — source table,
   per-company formats and the AI-Coding trend live in
   `references/written-test-sources.md`. Link out; never paste reconstructed
 question text as content.
 - 🔴 **「开放窗口」≠「考试时长」**：短信/页面给的 `18:00–21:00` 往往是**可进场区间**，卷面本身可能只有 2 小时
   （实测：按 3 小时窗口写下「提前交卷、时间没花完」，被本人纠正）。记录一场笔试必须问清：**卷面总时长、
   是否分段限时、哪一段最耗时**。
 - **考后当晚就做题型复盘**，并把「本人确认」与「我解读」严格分开：模糊题面（如「用零散小整数配大整数」）
   先按最可能的题型写并标 `我解读`，**同时单列一节待确认项**，别把推断写成真题。复盘里要写题型构成、
   实际考点、本人自评不确定处，以及与同平台/同方向其它笔试的**对照**（同一批人常考同一套骨架：
   接口层/存储层/业务层三层骨架 + 分档测试）。
8. **官方 ATS 岗位板逐岗核验 (last gate before 投递).** Open the employer's own
   recruiting system (`hr-jobs.<corp>.com/edu/`, `wecruit.hotjob.cn/SU.../`,
   `campus.<corp>.com/campus`, `job.10086.cn`) and locate posts with
   `?keywords=<方向>&project=<批次ID>` — the batch id comes from the hrefs behind
   the hub page's 投递 buttons. The **list row itself** carries the decisive label
   (`上海校招实习研发 - 后端开发实习生　职位 ID：A78527` vs
   `北京、上海、深圳校招正式研发 - 后端开发27届校园招聘　职位 ID：A45719`), and the
   detail page `/edu/position/<id>/detail` gives 学历 / 经验年限 / 转正措辞.
   Field list, URL templates and the decision table:
   `references/official-campus-job-boards.md`.
9. **薪资调研 (税前 + 税后).** Any 薪资/年薪/到手 ask produces a **two-column** deliverable
   (税前 vs 税后到手) over the offer range, never a single headline number. Evidence tiers,
   the source ladder, the 档位词 traps, the 税后 model and the band-negotiation playbook live in
   `references/salary-and-takehome-research.md`; get the numbers from
   `scripts/takehome_calc.py` rather than computing tax in your head. When you need a salary
   figure for one specific 岗位+学历, the company's postings on a **school employment portal**
   are the highest-yield place to find it (they publish a 薪资档位 the ATS does not) — and the
   portal's **单位页** (`/company/view?id=<n>`) lists *every* open post with its band in one
   fetch, which beats opening postings one by one. When no portal page exists (mid-size private
   company, 招聘会 投递), 职友集 is the substitute: `www.jobui.com/cmp?keyword=<公司>` finds the
   company id, then `/company/<id>/salary/` (+ `/salary/yingjiesheng/`, `/jobs/shixisheng/`,
   `/jobs/shuoshi/`) is **server-rendered and reachable with plain curl** — parse
   `li.salary-job-title-box` (job | city | band) and the 分布/历年 blocks for the aggregate.
   Label it explicitly as **B 类：招聘报价汇总，非在职员工实发** (the page itself says
   数据取决于在线职位薪酬样本，不代表企业内部真实情况), and never quote its 「应届生」均值 as a
   校招价 — day-rate 实习岗 (100–250 元/天) drag it down to a nonsense 4.9K. Prefer the
   公司自报 band on a posting titled 「（应届生）」 over any aggregate; 看准网/OfferShow/脉脉 for
   this size of company usually return nothing, so say "无公开实发样本" rather than filling the gap.
10. **谈薪用法 (the moment the user has a band).** A published 岗位薪资带 turns the ask from
   "what does it pay" into "what do I do with this" — answer the second one. Deliver, in the
   report and in chat: the go/no-go read of the band's floor, which post to prioritize when
   bands differ inside the company, and the negotiation rules (never echo the band, never open
   with a range, ask the 定档 rule first, quote one number near the top). Full rules: §7 of
   `references/salary-and-takehome-research.md`.

11. **宣讲会 / 线上活动的作战卡（他说「帮我准备一下今天下午的 X」）。** 交付一份 `3-面试\3-公司资料\<公司>-宣讲会作战卡-MMDD.md`，固定七节：
   ① **开工第一件事**（线上场多为「需回复确认」，把回复原文直接写好给他复制）② 会议硬信息（时间 / 会议号 / 提前几分钟 / 入会方式）
   ③ 公司画像（全部标官网原文）④ 官网导航地图（哪三四个页面值得点）
   ⑤ **口径差异 → 现场必问清单** ⑥ 会上要抓的五类信息 ⑦ 会后动作 + 一页临场速记。
   - **两个来源说不同的入会方式时，写成「稳妥做法优先」**（实测冲突：邀请卡写「腾讯会议 App 扫码」、HR 微信写「微信小程序入会」）→ 让他**提前装好官方 App**，小程序留作兜底；不要在两说之间挑一个当事实。
   - **时间口径同样取严**：邀请卡写 1 小时、HR 微信写 40 分钟 → 按长的准备、把短的当“重点段”。
   - **官网招聘页与台账里的校招口径经常对不上**（官网在招岗位全是社招口径，要求 2–5 年经验 + 日语 2 级；投递邮箱也不同：官网 `job@…` vs 台账记的南京校招箱 `job-nj@…`）→ **不要替他判定哪个对**，把差异做成「现场必问」的一行，让他当场问 HR。
   - **会上必抓的第五类信息是「流程时间点」**：多次跟进都停在同一句话（“后续流程节后安排”）时，这场活动就是唯一能拿到具体日期的机会，把它排到必问第一条。
12. **搜索引擎找不到的官方信息，直接扒官网的原始 HTML。** 两个高命中技巧：
   - **按钮没有 URL 时，去 raw HTML 里 `grep`**：页面上的「点击投递简历」在 DOM 里往往就是 `<a href="mailto:…">`，渲染后看不见。`re.findall(r'mailto:[^"\'\s<]+', html)` 一行就能拿到校招/社招投递箱。
   - **中文 slug 用 percent-encode 请求**（WordPress 站的导航 href 就是全站地图）：`re.findall(r'href=["\']([^"\']+)["\']', html)` 把链接全抓下来后**只看 `/加入我们/`、`/业务介绍/` 这类导航项**，比猜路径快得多。
   按 `references/city-research-zh.md` 的固定章节与来源阶梯做（区级政府概况页 → 人社局人才服务专栏
   → 交通局 → 卫健委 Excel 附件 → 魅力XX 页），结论必须落成「对本人方向的取舍 + 当天现场要问的
   3–4 句话」，官方 Excel 附件用 `scripts/xlsx_text_dump.py` 解析成清单。次要维度（交通/环境/医疗）
   用户若已明说“放次要”就不要再扩写。

13. **问「这家的老板是谁 / 创始人是谁」时，走公众号报道而不是工商页。** 空转顺序：
   ① `weixin.sogou.com/weixin?type=2&query=<公司> 创始人`（§Search engines 那条）；
   ② 官网 `/about/` 系列（大概率只有公司层面）；③ 工商类站点（爱企查/天眼查/企查查）多为 JS 壳，
   `curl` 只拿到空 body，别把预算花在那里。
   交付时要给出**姓名 + 头衔 + 可验证履历 + 多个独立出处**，并遵守两条口径纪律：
   - **头衔标报道年份**：」董事长兼 CEO」这类头衔往往来自几年前的融资稿，**没查到最新工商在任状态就标
     `未核实`**，并提醒用户「面试里只说『创始人是 X』，别自己加『现任 CEO』」。
   - **高管名单可能另有其人**：同一批报道里常出现副总经理/CTO 等其他名字（行业论坛嘉宾名单是好来源），
     一并记下 —— 用户真正要面对的面试官未必是创始人。
   找简历匹配用的关联点时（校友、前东家、同城），把这些写成一句「可用谈资」并标注**是否硬扯**，
   让用户自己决定用不用。

## 拿到 offer / 面试通过之后（含实习 offer）

触发：HR 发来「你这边通过面试了」并给条款，或本人转发这类聊天记录。

1. **先把 HR 原话逐条落盘**（日期 + 时间 + 原话 + 它对应台账哪一栏），**不要转述**。台账那一行同时改三处：
   **状态栏 / 时间栏 / 跟进列**（跟进日＝入职前必须确认完毕的那天）。
2. **定位判断写清楚**：**实习 offer ≠ 校招正式 offer**。它的价值＝兜底 + 简历素材；不占秋招名额、通常无违约金
   → **接受它零机会成本**，但必须显式写下「**实习照去、秋招不停**」，否则后面整张排期会误判。
3. **等值折算**：现金只是一项。`等值 ≈ 日薪 × 21.75 + 住宿市场价（若包住）+ 其他补贴`，不然会低估
   （实测：日薪 150 ≈ 3,262/月，叠加同城单间行情后定位从「偏低」变「中位偏上」）。
4. **薪资锚点按岗位性质选，不按公司选**：⚠️ 不要拿同一家公司另一条岗位线的报价当锚（实测：调研里唯一
   「标应届生 + 标薪资 + 在本城」的报价来自 **IC/嵌入式** 岗，而实际岗位是**软件 / AI 应用** → 拿它当锚
   会系统性压价，谈转正前必须换成**本城同类岗位**口径）。
5. **入职前必须书面确认的 10 项**（口头不算）：岗位名与方向 / 出勤要求与起止 / 薪资与补贴口径 /
   **转正评估的时间与标准** / **转正后的岗位与薪资区间或职级框架** / 是否计入试用期 / 合同主体与社保公积金基数 /
   入职材料与是否需要学校盖章 / **能否提前结束**（保留跳槽弹性）/ 住宿的具体配置与免费期之后怎么办。
6. **把入职日当倒排起点**：到岗之前那段就是「**秋招冲刺期**」，把要在此前完成的宣讲/网申/笔面列成硬节点；
   并显式标出**转正汇报时点与毕设答辩期的撞车**（年前汇报 ↔ 毕设期到次年 1 月）。短期协议（三月一签）
   要问清**续签是否重新议薪**。
7. **回执先给三块**：条款表（原话 + 含意）→ 我的定位判断（保底还是主线加成，为什么）→ ⚠️ 仍未确认项按优先级；
   再附一段**可直接粘贴发给 HR 的纯文本**（无下划线、无 ①→｜ 这类符号，编号用 `1 2 3`），**只问未确认项**，
   别把已知项再问一遍。

## Search engines for Chinese company/job queries

- **Crawl Chinese engines with `curl` from the terminal, not only through a browser
  tab.** Bulk queries driven one-by-one through a live tab are slow and stall; e.g.
  `curl -s -m 25 -A "<desktop UA>" "https://so.toutiao.com/search?keyword=<urlencoded>"`
  returns the whole result page in ~1s, and its text layer carries the snippets you need.
- **Engine ladder — rotate, never retry the same host:**
  1. **头条搜索** `so.toutiao.com/search?keyword=<q>` — highest yield for CJK company work:
     it inlines the *source page's own text* (school job boards, aggregators, mirrors) and
     frequently the 薪资金额 verbatim, so one call can replace three page fetches. Strip the
     embedded asset URLs; read only the text after the first result heading.
  2. **360搜索** `www.so.com/s?q=<q>` — best snippet coverage for company facts
     (规模/融资/工资/点评). Result links are `so.com/link?m=...` wrappers.
  3. **手机360** `m.so.com/s?q=<q>` — a *separate* rate-limit bucket from desktop so.com, so
     it works after the desktop host starts refusing.
- **Resolving a `so.com/link?m=...` wrapper: it is not a 302.** Fetch the wrapper and parse the
  BODY for `window.location.replace("http://…")` (fallback: `<meta http-equiv="refresh"
  URL='…'>`). A `curl -I` / `-L` on it reports no redirect at all and looks like a dead end.
  With that one regex you get real URLs out of every 360 result page.
- **Every engine rate-limits after roughly 10–15 automated queries** — you get a 302 with an
  empty body, or a 验证码 page ("访问异常出错"). That is the wall, not a parsing bug. On hitting
  it: rotate to the next engine in the ladder, warm up with a cookie jar (`-c cj.txt -b cj.txt`
  plus one homepage request), and do not hammer the blocked host. 百度 and 搜狗 throw
  click-captchas from the first request — **跳过百度**；**搜狗的「网页」垂直同样会撞验证码，但它的
  微信文章垂直是独立的一路、可用**：`https://weixin.sogou.com/weixin?type=2&query=<urlencoded>`
  把公众号文章正文片段（含标题与来源号名）直接返回到文本层，不用登录、不撞验证码。这是找
  **创始人 / 董事长 / CEO / 融资 / 公司沿革** 这类「只存在于公众号报道里、官网上不写」的信息的
  第一顺位来源：同一段履历常在投资机构号 + 行业媒体号 + 园区官媒上**重复出现**，交叉印证成本极低，
  比全网引擎快且准。
- Search APIs are unreliable for CJK company names: DuckDuckGo times out, and Bing CN silently
  truncates an **unquoted** CJK query to single characters, returning 词典 pages for the first
  character. **Rescue before rotating:** wrap the whole company name in quotes —
  `"<公司全称>" 官网` — the quoted form returns the real result set and its snippet usually
  carries the official host verbatim. Only rotate engines when the quoted form also misses.
- **URL-encode CJK queries in Python, not by shell interpolation.** On Windows/git-bash a Chinese
  string spliced into a command line can reach native `curl` as mojibake, and the engine then
  answers an unrelated token — the symptom is every query returning results about one unrelated
  word (列车 / 化学品 / 词典 pages). Build the encoded query first
  (`Q=$(python -c "import urllib.parse;print(urllib.parse.quote('<query>'))")`) or drive the fetch
  from `execute_code`; `curl -G --data-urlencode "q=$q"` is not a fix for this.
- **牛客 search pages are server-rendered — crawl them like an engine, not like a JS app.**
  `curl -sS -L -A "<desktop UA>" "https://www.nowcoder.com/search?type=post&query=<urlencoded>"`
  returns the result list *with the posts' own text* in the HTML, so one call yields 考点/题型/
  薪资/时间线 fragments no engine snippet gives; `/discuss/<id>` pages come back the same way.
  A title-only or zero-length body means that query shape is rate-limited — change `type`
  (`post` / `all`) or the query wording instead of re-hitting the same URL.
- **Verify a company's own domain by title match, never by name similarity.** `<拼音>.com` is
  routinely parked or an unrelated business (a 通达海-style name can resolve to a movie site),
  while the real host is often an abbreviation (`tdhnet.com.cn`). Take the candidate host from a
  quoted-search snippet, then confirm the `<title>` carries the company's full Chinese name
  before reading anything on the page. **When both TLDs resolve and both carry the right title, response volume is the tiebreaker**: a corporate `.com` is often a near-empty stub (under 1 KB — a logo and a “copyright” line) while the real site lives on `.com.cn` (tens of KB, a full nav). Fetch both and compare sizes before reading; the near-empty one is a dead end, not a research finding. On campus subdomains try **both** TLDs
  (`campus.<corp>.cn` vs `.com`) — one may not resolve at all, which is not evidence the campus
  entry is missing.
- Public site indexes and archives hold old postings (archiver.* job boards,
  Wayback) — good for position history and for confirming a role family exists,
  but always label the year of the batch.

## Hard rules

- **Never present the previous season's openings as the current season's.**
  Label each batch by year (「2026 届春招」), and separate 已确认 (sourced) from
  预计 (inferred). If this season's announcement is not public, say so in the
  first paragraph and give the monitoring list.
- **Never invent positions, salary, process steps, or 投递 details** no source
  states. If only a JD-描述 or a blog summary exists, quote it as such.
- Don't spend calls on login-walled pages — job platforms' company pages render
  empty shells for anonymous browsing; their data survives only in search
  snippets. Read the snippet, skip the page.
- Don't re-fetch an image-poster page for text, and don't OCR it unless asked —
  note the missing detail as a data gap in 备注.
- Copy 投递 mechanics character-exact (mailbox, subject template such as
  【校招应聘】+姓名+岗位, deadline) — a wrong subject line silently fails screening.
- **A mailbox transcribed from a poster/展板 is the highest-risk field — DNS-check it before telling him
  to send anything.** If `socket.gethostbyname(<domain>)` fails, the transcription is wrong (or the
  domain lapsed): say so and route him to the 扫码/网申 channel instead of letting him mail a resume into
  the void. A domain that *does* resolve is not proof of identity — a same-name company elsewhere is
  usually a separate entity with its own site (`bocloud.cn` = 广州博云, not 江苏博云) — so confirm the
  page `<title>` carries the full Chinese name before recording it. Report 解析失败 as "字符待核对".
- **岗位级核验，不要公司级结论.** Degree gate, hiring track and 转正 wording vary
  *inside* one batch: an aggregator's company-level summary ("该公司正式岗要硕士")
  is routinely true for the algorithm-research posts and false for the
  engineering/application posts in the same batch. Open each posting's JD and read
  its label line before telling the user a gate applies, then record the 职位ID
  (it is what makes follow-up and re-finding trivial later).
- **Internships: read the batch, then read the 转正 wording.** A post published
  inside the graduating class's campus batch (面向毕业时间窗口的本硕博) is 校招实习,
  not 日常/散招实习 — but conversion is labelled *per posting*
  (实习表现优异，有实习转正机会). No 转正/留用 wording in the JD means *unstated*:
  say so, tell the user to ask HR, and when a 本科可投 正式 post exists in the same
  direction, recommend that one first and keep the internship as fallback.
- **招聘会现场那批岗位 ≠ 他被约面/被录的岗位。** 现场易拉宝/展板列的是**那天摆出来的岗**；据此下
  「匹配度 / 期望值」结论就是错的（实测：现场 8 个岗全是嵌入式/芯片，连着几天据此写「与简历不匹配、
  期望值不高」，HR 后来确认实际岗位是做 RAG + 把检索封装成 MCP 工具对外服务，与主线高度对口）。
  **拿到 HR / 面试官的一手确认前，方向栏只写「待确认」，不写匹配度判断、不写「期望值不高」。**
- **When fresh primary evidence contradicts an earlier note, correct it out loud.**
  Rewrite the stale cell/sentence (never append an "actually…" correction under it),
  state what was wrong and why, and log it so the old conclusion is not re-derived.
- **Open every link before shipping it.** A list of N positions is a claim that N
  URLs resolve; open them in the browser and mark any you could not confirm
  (CDP block, DNS fail) as 未验证 inline instead of dropping it silently.
- **A recruiting entry is confirmed by its redirect target, not by a rendered page.** ATS hosts
  are JS SPAs that return 0 bytes to `curl` (北森 `*.zhiye.com`; Moka `moka-apply` cookie with a
  `Location: /campus-recruitment/<slug>/<id>/` hop, including white-labelled `campus.<corp>.cn`).
  Read `curl -sS -i` headers: the 302 `Location` alone proves the entry is live and gives the
  canonical URL to record, so never report "入口待补" from an empty body alone.
- **State coverage, never imply "all".** Name the sample and the window ("已筛选最新
  1,752 条收录，2026.08.27–09.15"); an aggregator is a sample of the market, and its
  entries lag the official 投递入口, so tell the user to re-confirm before applying.
- **Crawl in bounded batches, persist each batch, resume by page number.** Append
  every batch to a JSON file in `$BH_AGENT_WORKSPACE` instead of holding results in
  memory: a long single-call crawl can lose the run part-way, and resuming from the
  last completed page in a fresh browser session costs one call.
- **薪资永远双列：税前 / 税后到手，并把计算假设写在表上方。** 到手现金 is this user's
  hard metric; a 税前 number alone is not an answer. State 个人社保比例、公积金比例、专项
  附加扣除、年终奖是否单独计税、社保基数是否按实际工资 so the numbers can be re-derived or
  challenged. Also give a 敏感度 table for the two levers that actually move 到手 (公积金比例,
  薪数) — that is what makes an offer comparable.
- **档位词不是承诺值，推算值必须与已核实值物理分开。** A school-platform salary cell like
  「12000以上」 is a dropdown bracket (≥12000/月); a job-board 「20-30K·15薪」 is a range plus
  薪数. Quote them as brackets with source and date, and never let a 推算 range sit unlabelled
  in a table that also holds sourced facts — a 推算 row must name its basis and sample size.
- **先问薪数，再谈月薪。** 13/14/15/16 薪 is the largest single lever on 年薪; a report that
  compares only 月薪 misleads the reader (12K×15 beats 14K×13).
- **同一岗位在不同平台上，薪资口径的粗细不同——取值细的那条。** A portal's dropdown bracket
  (「12000以上」) and the employer's own numeric band on another school's 单位页
  (14,000–22,000) can describe the same post. Quote the **numeric band**, name the platform it
  came from, and keep the coarse bracket as a dated cross-check rather than deleting it — the
  band is what negotiation anchors on, the bracket merely proves the floor.
- **薪资带是对方的成本区间，不是用户的报价单。** When the employer publishes a 岗位薪资带,
  a bare number is not the deliverable: add a 谈薪用法 section. Never read the band back to HR
  and never volunteer a range — the first party to name a range anchors at its bottom, so
  "我期望 14–22K" hands the employer 14. Ask the 定档规则 first (面试评级 vs 本硕固定档); if
  forced to quote, quote **one number** near the top of the band backed by named projects; when
  pushed to the floor, trade non-salary levers (公积金比例 5%→12% ≈ 2.4 万/年, 补贴是否随月发,
  评级复核) instead of grinding 月薪. Keep the band out of the resume and out of public group
  chats with HR, and tell the user plainly that in 校招 the real lever is a competing offer.
- **同一家公司内部，岗位之间的 band 是不一样的。** Rank the posts by band before advising
  (研发/引擎/技术美术 > 应用开发/后端/安全 > 测试 is the common ordering) — the gap is worth
  1–3K × 薪数 per year, so "投哪个岗" is a money decision, not a preference one.
- **口头报出的薪资先拆口径再比价；横向对照表只用「同城市、同届、同类岗」的已核实口径。** 宣讲会/HR
  口头给的「第一年 X–Y 万」常带折价条款（培训期工资打折、试用期系数、年终补发而非随月发），不拆开就没法与
  别家比 —— 入表前必须问清三件：**几薪 / 含哪些项 / 打折条款怎么算**。比价表的每一行都要标来源与日期，
  平台档位与官方明文分开标，不要把推算值混进已核实值。表格末尾必须给一句**档位结论 + 资源分配判断**
  （「这一家属于哪一档 → 保底还是主力」），只把数字排一排等于没结论。

## 维护工作区里的台账 / 清单（用户批量回报时）

- **跨平台入站审计：他说「都处理完了吗」时，先拉全量入站再回答。** 同一用户的同一批转
  发会落在**不同平台的不同会话**里（私聊 / 另一通道 / 群），只看当前会话上下文就答“处理完了”是误报：
  `grep -a "inbound message" "$LOCALAPPDATA/hermes/logs/gateway.log" | grep -a "<YYYY-MM-DD>"`
  是唯一完整的清单（`msg=''` 的是图片/卡片，按时间戳对上）。并行会话可能已把某几条入了库，
  所以**先拿关键词 `grep` 工作区核对，只补做真没落盘的**，最后逐条给状态（✅已处理/⚠️未处理/—空消息）
  并单列一节「需要用户出手」的清单。
- **入站图片先去网关缓存取，再从聊天平台的媒体 URL 下载。** Hermes 已把入站图片落盘在
  `<hermes>\cache\images\img_*.jpg`，按修改时间筛最近几小时即可；平台侧链接多带过期 `rkey`，是兜底。
  复制进情报目录时**按用途重命名**（`<公司>-<用途>-N.jpg`），不留 `img_*.jpg`。
- **批量新增行：用正则找主表最后一行做锚点**
  （`max(i for i,l in enumerate(L) if re.match(r"^\|\s*\d+\s*\|", l))`），行号取**两册合并后的最大号 +1**
  （行号全局唯一，不按册各自计数）。
  ⚠️ **读到的台账是快照，插入那一刻必须再重扫一遍**：同一批转发会同时从另一通道进来、
  由并行会话写同一份台账，几分钟前读到的最大号可能已被它占用（真发生过同一个 `#86` 被两家公司占用）。
  规矩：**插入前当场重扫两册取 `max(已投, 未投)+1`，插入后立刻断言「两册内无重号且两册行号互不重叠」**。
  发现重号时**按公司名定位到那一行再改号**——只按 `| NN |` 匹配会同时命中冲突的两行，改错一条。
- **回执里的计数当场用脚本算**：「已投 N 条 / M 家」由 `len(rows)` 与去重公司名集合得出，
  不沿用上一轮的数字；同一家公司投多个岗位时条数 ≠ 家数。
- **❌ 不投 / 不报 / 专业不符的条目也要留行**：状态列写 `❌ 不投（原因）`、备注写判据与来源
  （“需求专业不含计算机/软件”“工作地在洛阳”）。删行会让下一次收到同一家宣讲时重做一遗调研；
  留行就是“别重复研究”的锚点。
- **用 execute_code 写中文表格行时，双引号字符串里不要放中文引号**（`“ ”` 会在代码链路里被
  规范化成 ASCII `"`）——会 `SyntaxError` 并让**整个 cell 完全不执行**，等于这一轮落盘全白做。
  行内引用统一用 `「」`/`【】`（或把整串写成单引号），并**核对 cell 打印出来的行号**再当成功。
- **后补的记录按时间插到最前，并重新编号**：用户批量报「昨天投的这几家」时，把它们插到台账顶部，
  并在同一次修改里改掉所有交叉引用（待投队列的 `已在主表 #N`、待办行、硬门槛行）。
  指针腐烂是写了第二天之后最常见的故障。
- **计数口径写在表旁**：验收看「公司家数」时，同一公司多个岗位只计一次，岗位列进备注。
- **每行都要有下次跟进日**：邮件/网申 +7 天，HR 直投（电话/微信）+3 天；空着等于死行。
- **同一公司第 3 次投递前先设止损**：在那一行写清还投不投、几号改走官网/公众号跟进。
- **非面试环节（人才测评等）单独立行/立列**：否则漏斗在「已投」和「笔试」之间留一段莫名空白。
- 已投的目标在待投队列里改成 `✅ 已完成：…（主表 #N）`，不要删行。
- 写文件时给表格加状态词表（`待投 → 已投 → 已查看 → 笔试待考 → 笔试完成 → 面试邀约 → 面试完成 →
  已挂 / Offer / 无回复（7天跟进）`），固定词表才能继续按状态筛选。

## Report shape (keep this order)

公司概况表 (全称/规模/性质/融资/双总部+地址/客户数/实验室与院校合作/主要荣誉) →
产品方向分层 (平台与运营层 / 检测审计层 / 基础设备层 / 云与服务层) →
岗位与要求表 (职位|类型|发布时间|逐条要求) →
校招专章 (岗位表含方向说明、投递方式与邮件格式、流程分步、截止日、培养模式) →
历年节奏表 (批次|时间|形式) →
监控清单 → 投递建议 → 参考链接汇总 → 备注 (数据缺口).

## Closing fit section (user preference — always include)

End with an actionable fit judgement, not a data dump:

- Map the strongest 1-2 roles onto the user's stated target direction (AI Agent /
  LLM application engineering) and say how to reframe existing projects in that
  vocabulary.
- Derive a 笔试/面试 prep list from the verbatim JD requirements (what to revise,
  what is cheap insurance).
- Name same-city / same-industry alternatives as hedging when the employer is a
  mid-size vertical vendor.

## Source depth

`references/china-company-research-sources.md` — per-source: how to reach it,
what it yields, and its caveats, plus the aggregator list-crawl recipe and verified
multi-company 校招官网 entry points. Extend it when a new source class proves useful.

`references/official-campus-job-boards.md` — reading an employer's own ATS board:
URL shapes, the fields that decide an application (批次/用工形式/职位ID/学历/经验/
转正措辞/驻场), the 正式-vs-校招实习 decision table, and hand-off rules when the
board is unreachable.

`references/written-test-sources.md` — 笔试题库/真题 entry points, per-company
paper format, and what to check before a test. Consult it for any 笔试 ask.

`references/salary-and-takehome-research.md` — 薪资调研: evidence tiers (官方明文 /
平台档位 / 推算), the source ladder that actually yields numbers (单位页 first), how to read
薪数/档位词, the 税前→税后 model and its assumptions, the reporting shape for a salary
comparison, and §7 谈薪用法 — turning a published 岗位薪资带 into a go/no-go read, a post
priority order, and negotiation lines that do not hand the employer the bottom of the band.

`references/city-research-zh.md` — 城市级调研：固定章节骨架、来源阶梯（区级政府概况页 /
人社局人才服务专栏的补贴原文 / 交通局 / 卫健委控费监测表 Excel / 魅力XX 页）、
核实状态标记（✅ 官方原文 / ➖ 公开常识 / ⚠️ 未核实）与「现场四问」清单。

`scripts/xlsx_text_dump.py` — 纯标准库把 .xlsx 转 TSV（政府站点常把权威清单放在 Excel 附件里，
沙箱里未必有 pandas）：`python scripts/xlsx_text_dump.py <file.xlsx> [sheet] [max_rows]`。

`scripts/takehome_calc.py` — runnable 税前→税后 calculator (个人三险 + 公积金双档 + 年度累计
个税 + 年终奖单独计税 + 公积金账户/含公积金总收益). CLI:
`python scripts/takehome_calc.py 15000 15 杭州 0.12 1500`.
