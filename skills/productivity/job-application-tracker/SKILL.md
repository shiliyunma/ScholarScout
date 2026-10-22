---
name: job-application-tracker
description: Use when running a 校招投递 campaign — 台账 + resume variants.
---

# 投递台账与投递执行（校招/秋招）

Use for a multi-day job-application push: one ledger file is the single source of truth,
one resume variant per application direction, and every application preceded by a verified
official entry + hard-gate check.

## 1. Ledger schema (one file, append-only)

`投递台账.md` at the campaign root. Sections, in this order:

1. **投递主表** — one row per application: `# | 投递日期 | 公司 | 岗位（照抄JD名） | 城市 | 官方入口 | 投递方式 | 岗位编号/内推码 | 状态 | 笔试/面试时间 | 下次跟进 | 备注`.
   Pre-fill target rows with 状态 `待投` so the user only fills the date + req id.
2. **待投队列** — sorted by deadline, P0/P1/P2 (deadline first, then local-city + best match).
3. **每日投递计数** — 日期 / 当日投递 / 累计, used as the campaign's acceptance check.
4. **笔试/面试进展**, 5. **人脉与线下记录** (HR 姓名/微信/内推码/下一步), 6. **已挂归档** (原因+教训),
   7. **硬门槛自查** (硕士卡学历、CET6、经验年限 — check BEFORE applying), 8. **待办与决策记录**.

Fixed status vocabulary so the table stays filterable:
`待投 → 已投 → 已查看 → 笔试待考 → 笔试完成 → 面试邀约 → 面试完成 → 已挂 / Offer / 无回复（7天跟进）`.

Same-day rule: whatever is learned offline (HR contact, 内推码, 笔试平台与题型) goes into the
ledger within 30 minutes, or it is lost.

## 2. Resume variant build (docx, style-preserving)

Never hand-edit the docx. Recipe in `references/resume-variant-recipe.md`:

1. Unzip `word/document.xml`, dump paragraph/run structure (index, bold flag, text).
2. Builder-exported Chinese templates (超级简历/wondercv) store **every line twice**
   (`mc:Choice` + `mc:Fallback`) — assert each replacement hits exactly **2** occurrences instead of replace_all.
3. Replace only `w:t` text, never `pPr`/`rPr`, so layout stays identical.
4. Render to PDF with LibreOffice, render page 1 to PNG, and *look at it* to confirm no textbox overflow.

Variant checklist: 求职意向 names the role family the strongest evidence actually supports;
flagship project moves to project slot #1; role keywords appear in both 求职意向 and the first
专业技能 line (HR screens on keywords).

## 3. Entry verification before applying

- Aggregator detail pages (wondercv etc.) give deadline / city / degree gate / JD keywords, but their
  "立即投递" contact info sits **behind a login** — use them for discovery only.
- Take the official entry from the employer's own campus site (`campus.tcl.com/campus`,
  `hr.sensetime.com/campus`, `job.10086.cn`), open it, then record the real URL in the ledger.
- Read the degree gate explicitly: many "AI" posts are 硕士-only while sibling intern/见习 posts accept
  本科 — applying to the wrong one is a guaranteed filter-out.
- Some employers mandate an email subject-line format (`【校招应聘】姓名+岗位`,
  `【2027届管培生应聘+岗位方向+姓名+学校】`). Record the exact string and obey it verbatim.

## 4. Division of labour with the user

Applications need the user's own accounts, phone verification and QR scanning: the agent prepares
resume + entry list + per-company talking points, the user submits, then reports `投递日期 + 岗位编号`
for the agent to write into the ledger. Never claim an application was submitted on the user's behalf.

## Pitfalls

- **LibreOffice stdout**: `subprocess.run(..., text=True, capture_output=True)` crashes with
  `UnicodeDecodeError` on GBK error output — run `soffice` from the shell, or drop `text=True`.
- **Unverifiable numbers**: metrics the user cannot reproduce live (`-44%`, `误差<1%`) get demoted to
  action/method descriptions in the 投递版; keep the number only if the user can demo the measurement.
- **QR codes in scraped DOM** are usually `data:image/png;base64,...`; filter `data:` URLs out of
  browser output or the tool result blows past the size limit.
- **CJK queries** typed into bing/baidu via the browser return junk; prefer `web_search` with
  `<公司名> 校园招聘 官网`, or open the guessed official domain directly.
