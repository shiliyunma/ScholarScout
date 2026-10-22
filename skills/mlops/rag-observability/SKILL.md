---
name: rag-observability
description: "Logging, monitoring, and quality telemetry for RAG systems. Database schema design for Q&A logs, per-stage performance tracking, model versioning, user feedback collection, and using logs to debug retrieval failures and drive iterative improvement."
version: 1.0.0
author: Hermes Agent
tags: [rag, observability, monitoring, telemetry, database, feedback, debugging, mlops]
---

# RAG Observability & Quality Monitoring

## Why This Matters

Most RAG systems stop at "it works" — but without systematic logging, you have no way to:

- **Debug bad answers**: was it a retrieval miss, a reranker failure, or LLM hallucination?
- **Find bottlenecks**: which stage (embedding / ChromaDB / reranker / LLM) is the slowest?
- **A/B test changes**: did switching from bge-small to bge-large actually improve quality?
- **Spot patterns**: what questions fail consistently? What sources are never retrieved?
- **Collect training data**: high-quality Q&A pairs for fine-tuning or evaluation sets

**A RAG system without observability is a black box. Logging only the question and answer is like having error logs without stack traces — you know something is wrong but have no idea where.**

## Core Principles

1. **Log everything you'd need to reproduce the query**: retrieval context, model versions, parameters
2. **Time every stage separately**: optimization without data is guessing
3. **Version your components**: without model/chunk-strategy versions, A/B testing is impossible
4. **Collect feedback**: user ratings turn logs from archive into improvement engine
5. **Hash questions**: detect repeated/duplicate queries for caching or frequency analysis

## Recommended Schema

```sql
CREATE TABLE qa_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(64),              -- session ID for multi-turn conversations
    question TEXT NOT NULL,
    question_hash VARCHAR(64),           -- for dedup / frequency analysis

    -- Retrieval diagnostics (the critical fields for debugging)
    retrieval_top_k INT,
    retrieved_chunks JSON,               -- before-rerank: [{id, source, score, type}]
    reranked_chunks JSON,                -- after-rerank: [{id, source, score, type}]
    final_context TEXT,                   -- actual context fed to the LLM

    -- LLM generation
    llm_model VARCHAR(64),
    llm_answer TEXT,
    llm_input_tokens INT,
    llm_output_tokens INT,

    -- Per-stage timing (ms)
    embed_time_ms INT,
    retrieval_time_ms INT,
    rerank_time_ms INT,
    llm_time_ms INT,
    total_time_ms INT,

    -- Version pinning (enables A/B comparison)
    embed_model VARCHAR(64),             -- e.g. "bge-large-zh-v1.5"
    chunk_strategy VARCHAR(64),          -- e.g. "semantic_split_v2"
    reranker_model VARCHAR(64),          -- e.g. "bge-reranker-base"

    -- User feedback
    user_rating TINYINT,                 -- NULL / 1-5
    feedback_text TEXT,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_session (session_id),
    INDEX idx_created (created_at),
    INDEX idx_rating (user_rating)
);
```

### Field Rationale

| Field | Why It Matters |
|-------|----------------|
| `retrieved_chunks` + `reranked_chunks` | Bad answer? One look tells you if retrieval missed the target or LLM ignored it |
| Per-stage timings | Know the real bottleneck — often reranker or LLM cold-start, not ChromaDB |
| `embed_model` + `chunk_strategy` + `reranker_model` | Compare configs objectively; don't optimize by feel |
| `question_hash` | Detect hot questions for caching; track how many times users ask the same thing |
| `user_rating` | Without it, logs are an archive, not an improvement engine |

## Integration Pattern

Add a logging hook to your `query.py` that fires after each query completes:

```python
import json
import hashlib
import time
from datetime import datetime

def log_query(session_id, question, retrieved_chunks, reranked_chunks,
              final_context, llm_answer, llm_model, timings, config,
              conn=None):
    """Insert a qa_logs record. conn is a mysql.connector or sqlite3 connection."""
    question_hash = hashlib.sha256(question.encode()).hexdigest()[:16]

    record = (
        session_id, question, question_hash,
        config.TOP_K,
        json.dumps(retrieved_chunks, ensure_ascii=False),
        json.dumps(reranked_chunks, ensure_ascii=False),
        final_context,
        llm_model, llm_answer,
        timings.get("input_tokens"), timings.get("output_tokens"),
        timings.get("embed_ms"), timings.get("retrieval_ms"),
        timings.get("rerank_ms"), timings.get("llm_ms"),
        timings.get("total_ms"),
        config.EMBED_MODEL_NAME, config.CHUNK_STRATEGY, config.RERANKER_MODEL_NAME,
    )

    if conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO qa_logs (
                session_id, question, question_hash,
                retrieval_top_k, retrieved_chunks, reranked_chunks, final_context,
                llm_model, llm_answer, llm_input_tokens, llm_output_tokens,
                embed_time_ms, retrieval_time_ms, rerank_time_ms, llm_time_ms, total_time_ms,
                embed_model, chunk_strategy, reranker_model
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, record)
        conn.commit()
```

## How to Use Logs for Debugging

### Bad Answer Diagnoses

```
Q: "进程和线程的区别"
→ LLM gave wrong definition

Check retrieved_chunks JSON:
  - First 5 chunks are from an exercise section ("22. 在多对一的线程模型中...")
  → Problem: retrieval pulled exercises, not content
  → Fix: add metadata filter to exclude type="exercise", or tune embedding

  - Chunks are correct but LLM ignored them
  → Problem: prompt too weak for small model
  → Fix: strengthen system prompt ("synthesize ALL fragments")
```

### Bottleneck Analysis

Query the timing fields to find the slowest stage:

```sql
SELECT AVG(embed_time_ms)     AS avg_embed,
       AVG(retrieval_time_ms) AS avg_retrieval,
       AVG(rerank_time_ms)    AS avg_rerank,
       AVG(llm_time_ms)       AS avg_llm,
       AVG(total_time_ms)     AS avg_total
FROM qa_logs;
```

If `rerank_time_ms` dominates → move reranker to CUDA or reduce TOP_K.
If `llm_time_ms` dominates → use a smaller model or lower quantization.
If `retrieval_time_ms` spikes → check if ChromaDB index needs rebuilding.

### Quality Trend Analysis

```sql
-- How is user satisfaction trending?
SELECT DATE(created_at) AS day,
       COUNT(*)         AS queries,
       AVG(user_rating) AS avg_rating
FROM qa_logs
WHERE user_rating IS NOT NULL
GROUP BY day
ORDER BY day;

-- Which model config gets the best ratings?
SELECT embed_model, reranker_model, llm_model,
       AVG(user_rating) AS avg_rating,
       COUNT(*)         AS samples
FROM qa_logs
WHERE user_rating IS NOT NULL
GROUP BY embed_model, reranker_model, llm_model
ORDER BY avg_rating DESC;
```

## Alternative Storage Options

| Storage | Pros | Cons |
|---------|------|------|
| **MySQL / PostgreSQL** | Full SQL power, joins, indexing | Requires server setup |
| **SQLite** | Zero-config, file-based, good for single-user | No concurrent writes |
| **Log file (JSONL)** | Simplest, append-only, grep-able | No indexing; analysis requires separate tool |
| **ChromaDB itself** | Same DB as your RAG, search logs by question | No aggregation, no feedback queries |

If using MySQL, create the connection once in main() (same pattern as model preloading) and pass it to the logging function.

## Pitfalls

- **Logging the raw retrieved chunks can be large** (up to 10K+ chars per query). Store as JSON text; the column can handle it. For high-volume systems, consider a separate `qa_chunks` table.
- **Don't make logging block the response**. Write logs asynchronously or in a background thread if latency is critical. For personal RAG (<10 queries/day), inline is fine.
- **session_id** is easy to get wrong. Generate it at startup with `uuid.uuid4().hex[:12]` and keep it in memory until the user exits. Don't re-use across restarts unless you explicitly support session persistence.
- **question_hash collision is harmless** at personal scale (SHA256-16 hex is ~10^19 space). Just use the first 16 chars for readability.
- **MySQL auth failures** in China: `caching_sha2_password` is MySQL 8.0+ default. The standard `mysql` CLI and `mysql-connector-python` handle it fine; older libraries (`pymysql` < 1.1.0) may not. Use `mysql-connector-python` or SQLite for simplicity.
- **Don't log the full `final_context` on every query if context is large (10K+ tokens)**. For debugging, the chunk IDs alone (retrieved_chunks + reranked_chunks) are usually enough to reconstruct what happened. Store full context only when user_rating <= 2 or on demand.

## References

- `references/mysql-connection-pattern.md` — MySQL connection setup, auth troubleshooting, and table creation for RAG logging

## Visualization Dashboard (Streamlit)

After logging queries to MySQL, build a Streamlit dashboard for interactive analysis, rating, and export.

### Integration Philosophy: One App, Not Two

**Don't build a standalone dashboard tool.** The user will naturally expect both Q&A and log-viewing in one page, not two separate windows. When the existing app is Gradio, migrate the **entire app** to Streamlit rather than keeping Gradio for Q&A and adding Streamlit for logs separately. A single `app.py` is the correct target.

### Navigation: Buttons Over Tabs

Some users find `st.tabs()` too subtle — tabs blend into the header and the user may not notice them. **Prefer button-based navigation** instead:

```python
if "page" not in st.session_state:
    st.session_state.page = "qa"

n1, n2 = st.columns(2)
with n1:
    if st.button("💬 问答", key="nav_qa", use_container_width=True,
                 type="primary" if st.session_state.page == "qa" else "secondary"):
        st.session_state.page = "qa"
        st.rerun()
with n2:
    if st.button("📊 查询日志", key="nav_logs", use_container_width=True,
                 type="primary" if st.session_state.page == "logs" else "secondary"):
        st.session_state.page = "logs"
        st.rerun()

st.divider()

# Then use if/elif on st.session_state.page for each view
if st.session_state.page == "qa":
    ...  # Q&A content
elif st.session_state.page == "logs":
    ...  # Logs content
```

**Why buttons over tabs:**
- More visually prominent — user can't miss them
- Current selection clearly highlighted (primary vs secondary)
- Clicking feels like switching apps, not switching sub-views
- Works identically to tabs for conditional rendering
- No URL hash dependency (tabs require URL fragment for state persistence)

The key implementation detail: use `st.session_state.page` + conditional `if/elif` blocks instead of tab container objects. Initialize in session_state, toggle via `on_click` callbacks or button click checks.

### Architecture

```
app.py (Streamlit, button-switched views)
├── 💬 问答 (button, default)
│   ├── Stats bar (chunk count, doc count, type distribution)
│   ├── Query input + search button
│   ├── Mode radio (知识库/习题库)
│   ├── Source filter dropdown
│   ├── Answer output (markdown)
│   ├── Sources (expandable details)
│   ├── Timing display (per-stage ms)
│   └── Auto-log to MySQL on each search
│
└── 📊 查询日志 (button)
    ├── Stats cards (total/today/rated/avg timings)
    ├── Filter bar (keyword, date range, rating filter)
    ├── Paginated log list (10/page)
    │   └── Each row: expandable detail with chunks JSON,
    │       LLM answer, timing breakdown, star rating widget
    ├── Pagination controls (prev/next + page jump)
    ├── Per-row 5-star rating + feedback save
    └── CSV export (respects current filters, exports all matching rows)
```

### Model Caching in Streamlit

Streamlit reruns on every interaction — models MUST be cached to avoid reloading on every search. Use `@st.cache_resource` (not `@st.cache_data`) for model objects:

```python
@st.cache_resource
def get_embed_model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(EMBED_MODEL_PATH, device=DEVICE)

@st.cache_resource
def get_reranker():
    from sentence_transformers import CrossEncoder
    return CrossEncoder(RERANKER_MODEL_PATH, device="cpu")

@st.cache_resource
def get_collection():
    import chromadb
    client = chromadb.PersistentClient(path=str(INDEX_DIR))
    return client.get_collection("docs")
```

For data that changes (like log stats), use `@st.cache_data(ttl=N)` with a short TTL to prevent stale counts while avoiding a DB call on every interaction.

### Per-Row Interactive Components

Streamlit supports independent per-row controls via unique `key` props. Each row in the log list gets its own select_slider, text_area, and button:

```python
for _, row in df.iterrows():
    with st.container(border=True):
        # ... display row data ...
        with st.expander("📋 查看详情"):
            new_rating = st.select_slider(
                "打分", options=[0,1,2,3,4,5],
                value=current_rating,
                key=f"rate_{row['id']}",  # unique key per row!
            )
            new_fb = st.text_area(
                "备注", value=old_feedback,
                key=f"fb_{row['id']}",    # unique key per row!
            )
            if st.button("💾 保存评分", key=f"save_{row['id']}"):
                save_rating(row['id'], new_rating, new_fb)
                st.rerun()  # refresh to show updated stars
```

**Key rule:** Every interactive component in a loop MUST have a `key` that includes a unique row identifier (`row['id']`, `i`, etc.). Without this, Streamlit cannot distinguish between rows and state collisions occur.

### Gradio → Streamlit Migration Notes

When migrating an existing Gradio RAG app to Streamlit:

| Aspect | Gradio | Streamlit |
|--------|--------|-----------|
| Model loading | Module-level (runs once at import) | `@st.cache_resource` (runs once, survives reruns) |
| Tab layout | `gr.Tabs()` + `gr.TabItem()` | `st.tabs()` returning tuples |
| Stats display | `gr.Markdown()` with HTML | `st.metric()` + columns |
| Source cards | `gr.HTML` with `<details>` | `st.expander()` or `st.container(border=True)` |
| Per-row controls | Not supported without JS | Native via unique `key` props |
| Event binding | `btn.click(fn=..., inputs=..., outputs=...)` | Imperative: `if st.button():` or `if search_btn:` |

### Example: do_search wrapper

The Q&A tab needs a wrapper that calls the query pipeline and captures timing/log data. Import the core functions from `query.py` (retrieve, rerank, build_context, answer_with_llm) and call the cached models via their getter functions:

```python
def do_search(query, mode, source_filter):
    from query import retrieve, rerank, build_context, answer_with_llm

    embed_model = get_embed_model()       # @st.cache_resource
    reranker = get_reranker()              # @st.cache_resource
    collection = get_collection()          # @st.cache_resource

    where = {"type": {"$in": [...]}}
    retrieved, embed_ms, retrieval_ms = retrieve(query, embed_model, collection, where)
    reranked, rerank_ms = rerank(query, retrieved, reranker)
    context = build_context(reranked)
    llm_answer, in_tok, out_tok, llm_ms = answer_with_llm(query, context)

    # Return everything for both display and logging
    return answer_text, sources_html, timing, log_data
```

### Pitfalls

**1. System Python vs venv for Streamlit.** ML dependencies (torch, sentence-transformers, chromadb) live in the project venv. Streamlit and pymysql are often installed system-wide. When launching from a `:logs` command inside query.py (which runs in the venv), use `["python", ...]` (system Python PATH) not `[sys.executable, ...]` (venv Python) in Popen. Otherwise the Streamlit process can't find its own packages.

**2. First-run email prompt.** On first launch, Streamlit prompts for an email. Pass `--server.headless true` to suppress. If that's insufficient, set `STREAMLIT_BROWSER_GATHER_USAGE_STATS=false` in the environment.

**3. Rating save requires rerun.** After `save_rating()`, call `st.rerun()` so the updated star display appears without manual refresh.

**4. Filter changes must reset page.** When any filter (keyword, date, rating) changes, the current page number may exceed the new total. Track prev_filters in session_state and reset `st.session_state.page = 1` on mismatch.

**5. CSV export exports ALL filtered rows** (not just the current page). The export function uses the same WHERE clause but no LIMIT/OFFSET. Use `pd.to_csv(encoding="utf-8-sig")` for Chinese character support in Excel.

**6. ChromaDB in system Python.** If the app is launched with system Python (where Streamlit lives) but chromadb is only in the venv, install chromadb system-wide.

**7. st.cache_data not refreshing.** When you write new data to MySQL (e.g., a new query log), `st.cache_data` won't know it's stale. Either:
- Set a short `ttl` on the cache (e.g., `@st.cache_data(ttl=10)`)
- Call `st.cache_data.clear()` after the write to force refresh

## Password Protection (Streamlit)

When deploying a Streamlit RAG dashboard on a local network, add a login gate before the main app renders. This prevents unauthorized access to query logs, ratings, and model queries.

### Architecture

Place the authentication check **before any page content**, right after imports and `st.set_page_config()`:

```python
PASSWORD = "your_password_here"

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False


def show_login():
    st.markdown(
        """
        <style>
        .login-box {
            max-width: 380px; margin: 120px auto 0;
            padding: 2rem; border-radius: 12px;
            border: 1px solid #444; background: #1a1a2e;
        }
        .login-box h2 { text-align: center; margin-bottom: 1.5rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<div class="login-box">', unsafe_allow_html=True)
    st.markdown("## 🔒 RAG 知识库")
    with st.form("login_form"):
        pwd = st.text_input("请输入访问密码", type="password", placeholder="密码")
        submitted = st.form_submit_button("🔑 登录", type="primary", use_container_width=True)
        if submitted:
            if pwd == PASSWORD:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("❌ 密码错误")
    st.markdown("</div>", unsafe_allow_html=True)


if not st.session_state.authenticated:
    show_login()
    st.stop()
```

### Key details

- **`st.set_page_config()` must be called BEFORE the auth check**, not inside `show_login()`, because it can only be called once per session. Place it immediately after `import streamlit as st`. If you also have a second `st.set_page_config()` call lower in the script (common when migrating from a single-page app), remove it — Streamlit throws `RuntimeError: set_page_config() can only be called once per app session`. One call total, placed before any other Streamlit command.
- **`st.stop()`** prevents any model loading or page rendering until the user authenticates. This avoids loading models (10-30s) before login.
- **`st.rerun()`** after successful auth triggers a fresh render pass where `authenticated=True`, skipping `show_login()`.
- **Password in source code** is acceptable for LAN deployment (deters casual access). For production, use Streamlit Secrets or a reverse proxy with OAuth.
- **Single-password only.** `st.form` + `st.form_submit_button` handles both button click and Enter key submission.

## Cache Management After MySQL Writes

When the Q&A tab writes a new log or the Logs tab saves a rating, `@st.cache_data` won't know the data changed.

**Approach 1 — Short TTL** (good for stats that auto-refresh):
```python
@st.cache_data(ttl=10)
def load_log_stats():
    ...
```

**Approach 2 — Explicit clear** (needed after writes):
```python
def save_rating(log_id, rating, feedback=""):
    conn = get_mysql_conn()
    with conn.cursor() as cur:
        cur.execute("UPDATE qa_logs SET user_rating=%s, feedback_text=%s WHERE id=%s",
                    (rating, feedback if feedback else None, log_id))
    conn.commit()
    conn.close()
    st.cache_data.clear()  # Force refresh on next read
```

Call `st.cache_data.clear()` after any INSERT or UPDATE. For single-user RAG the cost is negligible.

## Integrating Q&A with Logs in One App

### Import pattern for timing-aware query functions

The query pipeline functions should return timing data alongside results:

```python
# query.py
def retrieve(query, embed_model, collection, where=None):
    t0 = time.perf_counter()
    q_embedding = embed_model.encode([query]).tolist()[0]
    embed_time_ms = int((time.perf_counter() - t0) * 1000)

    results = collection.query(query_embeddings=[q_embedding], n_results=RETRIEVAL_TOP_K, where=where)
    retrieval_time_ms = int((time.perf_counter() - t0) * 1000) - embed_time_ms

    docs = [...]
    return docs, embed_time_ms, retrieval_time_ms


def rerank(query, docs, reranker):
    if len(docs) <= TOP_K:
        return docs, 0
    t0 = time.perf_counter()
    pairs = [(query, d["text"]) for d in docs]
    scores = reranker.predict(pairs)
    scored = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
    rerank_time_ms = int((time.perf_counter() - t0) * 1000)
    return [d for d, _ in scored[:TOP_K]], rerank_time_ms


def answer_with_llm(query, context):
    t0 = time.perf_counter()
    response = client.chat.completions.create(...)
    llm_time_ms = int((time.perf_counter() - t0) * 1000)
    usage = getattr(response, "usage", None)
    return response.choices[0].message.content, \
           (usage.prompt_tokens if usage else None), \
           (usage.completion_tokens if usage else None), \
           llm_time_ms
```

### do_search wrapper (Streamlit)

In `app.py`, wrap the pipeline in a single function returning structured data for both display and logging:

```python
def do_search(query, mode, source_filter):
    from query import retrieve, rerank, build_context, answer_with_llm

    where = {"type": {"$in": ["content", "solution"]}} if mode == "知识库" \
            else {"type": {"$in": ["exercise", "solution"]}}
    if source_filter and source_filter != "全部":
        where["source"] = source_filter

    retrieved, embed_ms, retrieval_ms = retrieve(query, embed_model, collection, where=where)
    if not retrieved:
        return None, None, None, None, None

    reranked, rerank_ms = rerank(query, retrieved, reranker)
    context = build_context(reranked)
    llm_answer, in_tok, out_tok, llm_ms = answer_with_llm(query, context)

    answer_text = llm_answer if llm_answer else "（未配置 LLM）"
    sources_html = _format_sources(reranked)

    timing = {
        "embed_ms": embed_ms, "retrieval_ms": retrieval_ms,
        "rerank_ms": rerank_ms, "llm_ms": llm_ms,
        "total_ms": embed_ms + retrieval_ms + rerank_ms + llm_ms,
    }

    log_data = {
        "retrieved_chunks": retrieved, "reranked_chunks": reranked,
        "context": context, "answer": llm_answer,
        "in_tokens": in_tok, "out_tokens": out_tok,
        "embed_ms": embed_ms, "retrieval_ms": retrieval_ms,
        "rerank_ms": rerank_ms, "llm_ms": llm_ms,
    }

    return answer_text, sources_html, timing, reranked, log_data
```

## Related Skills

- **`local-rag`** — build pipeline (ingest → embed → retrieve → answer)
- **`personal-rag`** — setup with ChromaDB + embeddings + LLM; contains the detailed dashboard reference file at `references/rag-streamlit-dashboard.md`
