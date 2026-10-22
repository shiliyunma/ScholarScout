# MySQL Connection for RAG Logging

## Connection Pattern (Python)

**Option A — mysql-connector-python (recommended for MySQL 8.0+):**

```python
import mysql.connector
conn = mysql.connector.connect(
    host="localhost", port=3306,
    user="root", password="your_password",
    database="ragsearch", autocommit=False
)
```

**Option B — pymysql (lighter, no C extension):**

```python
import pymysql
conn = pymysql.connect(
    host="localhost", port=3306,
    user="root", password="your_password",
    database="ragsearch", charset="utf8mb4"
)
```

**Why `mysql.connector` and not `pymysql`:** MySQL 8.0+ defaults to `caching_sha2_password` auth plugin. `mysql-connector-python` handles it natively; `pymysql` < 1.1.0 does not. In practice, recent pymysql (>=1.1.0) works fine with caching_sha2_password.

### Installing pymysql in a pip-less venv

uv-created venvs don't include pip. If the venv needs pymysql but can't `pip install`:

```bash
# Find system pymysql path
python -c "import pymysql; print(pymysql.__file__)"

# Copy to venv
cp -r "C:/Users/<user>/AppData/Local/Programs/Python/Python312/Lib/site-packages/pymysql" \
  .venv/Lib/site-packages/pymysql
```

Alternatively, use `uv pip install` directly:
```bash
uv pip install pymysql --python .venv/Scripts/python.exe
```

## CLI Quick Check

```bash
mysql -h localhost -P 3306 -u root -p<password> <database> -e "SHOW TABLES;"
```

## Troubleshooting "Access Denied"

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `Access denied for 'admin'@'localhost' (using password: YES)` | Wrong password | Check password with root: `SELECT User, Host, plugin FROM mysql.user WHERE User='admin'` |
| `Access denied (using password: NO)` | Password required but not provided | Add `-p<password>` |
| SSL errors | Windows MySQL CLI + TLS cipher mismatch | Works fine from Python with `mysql.connector` which handles TLS transparently |

## Creating the Log Table

```sql
CREATE TABLE qa_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(64),
    question TEXT NOT NULL,
    question_hash VARCHAR(64),
    retrieval_top_k INT,
    retrieved_chunks JSON,
    reranked_chunks JSON,
    final_context TEXT,
    llm_model VARCHAR(64),
    llm_answer TEXT,
    llm_input_tokens INT,
    llm_output_tokens INT,
    embed_time_ms INT,
    retrieval_time_ms INT,
    rerank_time_ms INT,
    llm_time_ms INT,
    total_time_ms INT,
    embed_model VARCHAR(64),
    chunk_strategy VARCHAR(64),
    reranker_model VARCHAR(64),
    user_rating TINYINT,
    feedback_text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_session (session_id),
    INDEX idx_created (created_at),
    INDEX idx_rating (user_rating)
);
```

## One-time setup (from CLI)

```bash
mysql -h localhost -P 3306 -u root -p<password> ragsearch < path/to/create_table.sql
```
