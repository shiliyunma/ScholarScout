# RAG Accuracy Optimization — Decision Guide

## Diagnostic Flow

```
Is precision bad (wrong chunks retrieved)?
├─ Are chunks irrelevant or about wrong topic?
│  └─ → Add Hybrid Search (BM25 + Embedding)
├─ Are chunks relevant but incomplete context?
│  └─ → Add Parent Document Retriever
├─ Are chunks from wrong chapter?
│  └─ → Add Metadata Filtering
└─ Chunks look good but LLM answer is wrong?
   └─ → Not a retrieval problem → tune LLM prompt or model

Is recall bad (missing relevant chunks)?
├─ User query too vague or ambiguous?
│  └─ → Add Query Rewrite
├─ Query has critical exact-match terms?
│  └─ → Add Hybrid Search (BM25 catches them)
├─ Need info from multiple sections?
│  └─ → Add Multi-round Retrieval
└─ Never found by any single query?
   └─ → Add Query Rewrite (multiple angles)
```

## Tuning Parameters

### Hybrid Search α

```
α=1.0  → pure embedding (current behavior)
α=0.7  → good balance, embedding-dominant
α=0.5  → equal weight, catches more exact matches
α=0.3  → BM25-dominant, for very terminology-heavy docs
```

Start at α=0.7. If exact-match recall is critical, lower toward 0.5.

### Query Rewrite Prompt Structure

```
System: You are a search query reformulator for a textbook RAG system.
Given a user question, generate 2-3 independent search queries that
would retrieve the most relevant textbook passages. Each query should
cover a different angle: technical terms, related concepts, broader
category.

Output format (one per line, no numbering):
query 1
query 2
query 3
```

### Metadata Filter Granularity

| Granularity | Pros | Cons | Best for |
|-------------|------|------|----------|
| Chapter-level | Simpler ingest, fewer keys | May be too broad | Long multi-chapter documents |
| Section-level | More precise | More parsing work | Structured textbooks |
| Topic-level (3-5 per chapter) | Best precision | Manual annotation needed | Curated knowledge bases |

### Multi-round Decision Prompt

After first retrieval round, ask LLM:

```
You have retrieved the following passages. Is the information sufficient
to answer the user's question? Answer YES or NO.
If NO, generate one additional search query that would fill the gap.
```

Only do a second round (never third) — diminishing returns after that.
