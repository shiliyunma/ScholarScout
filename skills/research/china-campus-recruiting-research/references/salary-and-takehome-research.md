# 薪资调研: 税前、税后与数据源分级

Consult for any 薪资/年薪/到手 ask about a Chinese employer. The user's hard metric is
**税后到手现金**; a 税前 number alone is an incomplete answer.

## 1. Three evidence tiers — label every number with one of them

| Tier | What it is | How to present it |
|---|---|---|
| **官方明文** | Things the company itself states: 薪数 (保底15薪), 年终奖规则, 补贴明细, 保险, 调薪月份 | Quote verbatim, with the 公告 and its date |
| **平台公开档位** | A figure a third party published: school-portal 薪资档位, job-board 区间·薪数, 聚合站 本科月薪均值 | Quote with source + sample size + date; brackets stay brackets |
| **推算** | Your own range built from the two above | Must name its basis and the discount applied — never sit unlabelled in a table with sourced facts |

A report that only has tiers 1–2 is fine; an empty tier-2 is a **data gap to state**, not a
hole to fill with tier 3 dressed as fact.

## 2. Source ladder that actually yields numbers

1. **高校就业网 / 就业服务平台 — 优先抓“单位页”，不是单个岗位页** — the single best source for a
   校招 salary figure for a specific 岗位 + 学历. A 正方/zftal system exposes:
   - the **单位页** (`company/view?id=<n>`) — **one fetch returns every open post with its band,
     学历, 工作性质 and 专业要求** (e.g. `AI应用开发工程师（2027届）｜14000-22000｜全职｜本科`).
     Start here; it is the whole pay ladder in one page.
   - the **岗位页** (`ckZpgwXq.zf?dwxxid=<id>&zpxxbh=<jobid>`) — full JD + 招聘人数 + 发布时间, and a
     "更多招聘职位" block whose anchors enumerate the same employer's other postings.
   **Granularity differs by portal — crawl at least two before calling a band precise.** The same
   employer published the coarse dropdown bracket 「12000以上」 on one school's portal and the
   numeric band `14,000–22,000` on another. Negotiate from the numeric band; treat the bracket as
   a floor only, and say which portal gave which.
   A second school's employer page is also worth fetching just for 公司规模/性质/简介 when the
   first school has no postings.
2. **校招公告全文** (aggregator reproduction of the poster, or a 学院 announcement) — 薪数,
   年终奖规则, 补贴/餐补/租房/交通, 六险一金, 调薪频率. This is where the *structure* of pay
   comes from, and structure is often worth more than the base number.
3. **Job-board snippets** (BOSS直聘 / 智联 / 猎聘 / 实习僧) — 区间 + 薪数 + 经验/学历 filter.
   Read the snippet; the pages themselves are anti-bot or login-walled.
4. **职友集 / 看准 / 脉脉 salary pages** — snippet only. 职友集 snippets give the useful
   shape: "工资统计来自近一年 <N> 条工资数据" + "本科 ￥X" + 行业均值. N is the sample size and
   must be quoted; 脉脉 samples skew senior, so use them as an upper-bound cross-check only.
5. **公司自己的官网/ATS** — 社招 ranges leak the seniority curve (e.g. a 1–3 年 岗位 range tells
   you where a 校招 number cannot sit), but 校招月薪 is usually unpublished there.

## 3. Reading 档位 words without over-claiming

- 「12000以上」 = a dropdown bracket meaning ≥12000/月. It is a floor, not an offer.
- 「20-30K·15薪」 = range × 薪数. The ·NN薪 is the lever that decides 年薪; extract it.
- 「年薪 6-12k·16薪」 on a job board is usually the platform's typo for **月薪** 6-12k — read
  the 薪数 next to it to decide, and say which reading you used.
- 「保底15薪」 = 12 months + 年终奖 guaranteed ≥3 months. The 年终奖 part usually lands after
  Spring Festival, so a 月均到手 figure is a smoothed number: also publish the ordinary-month
  cash flow (1–11 月) so the user is not surprised by month-to-month variance.

## 4. 税前 → 税后 model (use `scripts/takehome_calc.py`, do not estimate by hand)

Inputs: 税前月薪, 薪数, 城市, 公积金比例, 专项附加扣除(月).

- 个人社保 = **养老 8% + 医疗 2% + 失业 0.5%** (失业率 varies by city: ~0.2% 广州, ~0.3% 深圳).
- 公积金 = 个人与单位**同比例** (5%–12%, 当地定档); **单位那份也进你的账户**, so a 5% company
  is effectively cutting pay.
- 缴费基数 = 实际月薪, bounded by the local 社平 60%–300%. State the cap when a salary is near
  it; otherwise note "均在上限内" so the model is auditable.
- 个税 = 综合所得年度累计: `年工资 − 60000 − 三险一金个人部分 − 专项附加扣除` through the
  annual bracket table.
- 年终奖 = **单独计税**, and the two steps are easy to conflate: **step 1** use `年终奖 ÷ 12` ONLY
  to look up the monthly rate bracket; **step 2** apply that rate to the **full bonus**
  (`年终奖 × 税率 − 速算扣除数`). Passing `年终奖/12` into the bracket as the taxable amount
  understates the tax by roughly 12× and inflates 到手 — a calculator that does this looks
  plausible and is wrong. (The policy runs to **2027-12-31**, so it applies to offers the user
  is signing now.) Compare against 并入综合所得 when the bonus is small.
- Output both: **税后到手年/月均** and **含公积金总收益** (到手 + 个人+单位公积金), because the
 公积金 account is real money (租房可提取).

**The counter-intuitive result to state out loud:** an 应届生 effective tax rate is only
**~1%–5%** at 18–30 万 税前, because the 5000/月 threshold, the 三险一金 deduction and
(if opted) the 1500/月 租房扣除 eat most of the base. So 税前差一万 ≈ 到手差一万 — do not let
the user discount a 税前 comparison as "virtual".

**Sensitivity to always publish:** 公积金 12% vs 5% moves ordinary-month cash by ~945 元/月 at
15K while the account loses ~2.52 万/年 (personal + employer). City only shifts the result by
<1% (社保个人比例差异), so a city choice is about rent and 生活成本, not tax.

## 5. Report shape for a salary ask

1. 一句话结论 + the 3–5 numbers that matter (with tier labels).
2. 公司档案表 (全称/成立/规模/产品/融资/地址) — sourced.
3. 薪酬结构表: 薪数, 年终奖规则, 项目分红, 保险, 补贴明细 (each with its 公告 source).
4. 可核实的薪资档位表 (岗位 | 数字 | 来源 | 日期) — this is the anchor the user negotiates from.
   When the number is a real **band** (not a bracket), follow it with the **谈薪用法** section (§7).
5. 税前→税后 表 with the assumption block above it.
6. 敏感度表 (公积金比例 / 薪数 / 是否有专项附加扣除).
7. 数据缺口 — list explicitly what the company does not publish.
8. 来源清单 with URLs so every number can be re-checked.
9. 谈 offer 反问清单 — lead with **这个岗位的薪资带按什么定档（面试评级 / 本硕固定档）**, then 薪数,
   公积金比例与基数, 年终奖发放时间, 分红规则, 补贴是否随月发。

## 6. Pitfalls

- **Do not attribute a number to the wrong company.** Aggregator search snippets interleave
  other employers' postings on the same platform; verify the employer name in the snippet/cell
  before quoting (a 20–30 万/年 目标年薪 snippet that belongs to a different studio is exactly
  the error this prevents).
- 高校就业平台 salary cells and 薪数 come from different sources — cross them, and when they
  disagree, publish both rather than averaging.
- Salary pages of 职友集/看准/脉脉 need login: read the snippet, do not burn calls on the page.
- Re-verify 社保基数上下限 yearly (~July adjustment) and prefer the province 人社厅 notice over
  any 计算器 site.

## 7. 拿到薪资带之后：决策 / 选岗 / 谈判

A 岗位薪资带 (e.g. `14,000–22,000`) is the **employer's cost range, not the user's price**. The
same number does three jobs; keep them apart and deliver all three.

**A. 决策 (go / no-go).** Convert the band's floor and ceiling to 税后到手 with the same 薪数, and
judge the floor against the user's real cost of living in that city. Publish both ends — a
midpoint hides the risk.

**B. 选岗 (which post to apply to).** Bands differ *inside* one company (研发/引擎/技术美术 >
应用开发/后端/安全 > 前端/中台/测试 is the common ordering). Say which post to prioritize and why:
the gap is worth 1–3K × 薪数 per year, so it is a money decision rather than a taste one. Keep
the user's target direction as first choice and name the higher-band post as parallel second投.

**C. 谈判 (how to use it in the room).**

| 动作 | 对错 | 机制 |
|---|---|---|
| 把 band 原样报给 HR | ❌ | first party to name a range anchors at its bottom; "我期望 14–22K" hands over 14 |
| 先问定档规则 | ✅ | 按面试评级定档？还是本硕固定档？— moves the talk from "你值多少" to "你们怎么定档" |
| 被逼报数时报**一个数** | ✅ | ~band 的 70–80%，依据 = 薪数结构 + 具名项目 + JD 原话 |
| 说"14K 我也能接受" | ❌ | 校招 offer 报了底基本不往上补 |
| 被压到带底就换非薪资条款 | ✅ | 公积金 5%→12%（≈2.4 万/年）、补贴是否随月发、评级复核、转正时间 |
| 跨公司比月薪 | ❌ | compare **年薪 = 月薪 × 薪数**; 12/13/16 薪 makes 月薪 comparisons lie |
| 把 band 写进简历 / 在群里问 HR | ❌ | both volunteer the anchor |

- **被问到信息来源时**：不编、也不说满 — "贵司在高校就业网公示的岗位薪资带是 X–Y，我希望落在偏上",
  then pivot to 定档规则.
- **话术卡**：close the answer with 3–5 verbatim lines (反问定档 / 报价 / 被压价 / 收尾) so the user
  can read them out. The user asks for this explicitly rather than wanting the reasoning restated.
- **Say the quiet part**: in 校招 the real lever is a competing offer, not rhetoric — so advise
  parallel投递 over polishing 话术.
