<div align="center">

# NL2SQL CLI

**Agentic natural language → SQL with parallel schema analysis, Redis cache lookup, self-correcting generation, LLM-as-judge evaluation, and Docker-sandboxed execution.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.2%2B-6366f1?logo=python&logoColor=white)](https://github.com/langchain-ai/langgraph)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e)](LICENSE)
[![Built with Textual](https://img.shields.io/badge/TUI-Textual-6366f1)](https://textual.textualize.io)

</div>

---

## What it does

NL2SQL CLI converts plain-English questions into SQL queries against a local SQLite database. It runs a **9-agent LangGraph pipeline** with a parallel fan-out at the start: a Redis cache lookup and a Groq-powered schema analyzer fire concurrently. On a cache miss the pipeline generates SQL with a cheap finetuned model, validates it, runs it in a Docker sandbox, scores it with an LLM judge, and — only on failure — escalates to a superior model with the full error context fed back. A performance optimizer and a PII privacy guard run on the way out.

```
$ nl2sql query "How many active employees earn above their department average?"

∙ parallel_start → [cache_lookup ‖ schema_analyzer] (concurrent)
∙ Schema: Relevant tables: employees, departments
◆ [1/3] Generating SQL… (finetuned)
  ✓ SQL:  SELECT e.first_name, e.salary FROM employees e
          JOIN (SELECT department_id, AVG(salary) avg_sal
                FROM employees WHERE is_active=1 GROUP BY department_id) da
          ON da.department_id = e.department_id
          WHERE e.is_active=1 AND e.salary > da.avg_sal
◆ Executing in Docker sandbox…
  ✓ PASS  Correct result set — 2 rows matched expected output
┌─────────────┬────────────┐
│ first_name  │ salary     │
├─────────────┼────────────┤
│ Bob         │ 200000.0   │
│ CEO         │ 9999999.99 │
└─────────────┴────────────┘
```

---

## Architecture

<img src="docs/architecture.svg" alt="NL2SQL CLI — Agentic Multi-Agent Pipeline" width="100%"/>

> Interactive version with pan/zoom, dark mode, search, and 3 guided views: [`docs/architecture.html`](docs/architecture.html)

---

## Pipeline: How It Works

### 1 — Parallel Fan-out (new in v2)

Every query enters a **LangGraph `parallel_start` node** that immediately fans out to two branches running **concurrently**:

| Branch | Node | What it does |
|---|---|---|
| **Branch 1** | `cache_lookup` | Computes `SHA-256(normalize(question) + schema identifiers)`, issues a Redis `GET`. Hit → skip all LLM agents entirely. |
| **Branch 2** | `schema_analyzer` | Calls Groq (`qwen/qwen3.8-27b`) to select only the relevant tables from the full DDL, producing a filtered schema for downstream agents. |

LangGraph's `add_edge(["cache_lookup", "schema_analyzer"], "dispatch_or_skip")` fan-in waits for **both** branches before proceeding.

```
parallel_start
  ├──► cache_lookup       (Redis GET — parallel branch 1)
  └──► schema_analyzer    (Groq LLM table selection — parallel branch 2)
            ↓ (fan-in: both must complete)
  dispatch_or_skip
  ├── cache hit  ──► privacy_guard ──► END   (zero LLM inference)
  └── cache miss ──► finetuned_generator ──► … (full pipeline)
```

### 2 — SQL Generation (Step 1)

`finetuned_generator` calls a **Qwen2.5-1.5B model finetuned on NL2SQL** hosted on HuggingFace. Fast and cheap — covers the majority of common business queries.

### 3 — Syntax Validation

`syntax_validator` runs **sqlglot** to parse the generated SQL and enforces a `SELECT`-only rule. No `INSERT`, `UPDATE`, `DELETE`, or `DROP` can reach the executor. Schema column names are cross-checked against the filtered DDL.

- **Pass** → executor
- **Fail (finetuned model)** → superior_generator
- **Fail (superior model)** → performance_optimizer (give up on this path)

### 4 — Sandboxed Execution

`executor` runs the SQL inside a **Docker container** (`python:3.12-slim`) against a read-only copy of the database. A malformed or destructive query cannot touch the real file. Falls back to local SQLite if Docker is unavailable.

### 5 — LLM-as-Judge

`judge` calls Groq (`qwen/qwen3.8-27b`, or OmniRoute as fallback) with the question, the generated SQL, and the result set. Returns a structured verdict: `correct: true/false` + `reason` + `suggestion`.

- **Correct** → performance_optimizer
- **Wrong + retries left** → superior_generator
- **Wrong + exhausted** → performance_optimizer (best attempt)

### 6 — Superior Generator (Steps 2–3)

`superior_generator` calls **OmniRoute** with the prior failed SQL and the **exact execution or judge error** as context. This is strictly more information than a fresh prompt — the model can self-correct rather than guessing again.

### 7 — Performance Optimizer

`performance_optimizer` calls **llama-3.3-70b-versatile** via Groq to rewrite complex SQL (multiple JOINs, subqueries, HAVING clauses) for readability and performance. Runs conditionally — simple queries skip it.

### 8 — Privacy Guard

`privacy_guard` scans result column names against configurable patterns (`email`, `ssn`, `phone`, `credit_card`, `password`, `address`). Matching columns are redacted in the response and every redaction is written to `.nl2sql/privacy_audit.log`.

---

## 9 Specialist Agents

| Agent | File | Model / Tool | Role |
|---|---|---|---|
| `cache_lookup` | `core/graph.py` | Redis | SHA-256 semantic cache lookup — parallel with schema_analyzer |
| `schema_analyzer` | `agents/schema_analyzer.py` | Groq `qwen/qwen3.8-27b` | Selects relevant tables from full DDL — parallel with cache_lookup |
| `finetuned_generator` | `agents/sql_generator.py` | Qwen2.5-1.5B (HF) | Step 1 SQL generation — fast, cheap, NL2SQL finetuned |
| `syntax_validator` | `agents/syntax_validator.py` | sqlglot | SQL parse + SELECT-only enforcement + schema column check |
| `executor` | `agents/executor.py` | DockerSandbox | Runs SQL in isolated container against read-only DB copy |
| `superior_generator` | `agents/sql_generator.py` | OmniRoute auto/coding | Steps 2–3 retry with prior SQL + exact error context |
| `judge` | `agents/judge.py` | Groq `qwen/qwen3.8-27b` | LLM-as-judge correctness scoring with reason + suggestion |
| `performance_optimizer` | `agents/performance_optimizer.py` | Groq `llama-3.3-70b-versatile` | Conditional SQL rewrite for complex queries |
| `privacy_guard` | `agents/privacy_guard.py` | Regex | PII column redaction + audit log write |

---

## Complete Flow Diagram

```
Question + Schema
        │
        ▼
  parallel_start  ──────────────────────────────────────┐
        │                                                │
   cache_lookup ──── Redis GET ──►  HIT ──► privacy_guard ──► END
        │                                                │
  schema_analyzer ── Groq LLM ── filtered DDL ──────────┘
        │                         (fan-in: both required)
        ▼
  dispatch_or_skip
        │ MISS
        ▼
  finetuned_generator (Qwen2.5-1.5B · Step 1)
        │
        ▼
  syntax_validator (sqlglot · SELECT-only)
        ├─ invalid (finetuned) ──► superior_generator ──┐
        ├─ invalid (superior)  ──► performance_optimizer │
        └─ valid ──────────────────────────────────────  │
                                                         │
        ▼                                                │
  executor (DockerSandbox / local SQLite fallback)       │
        ├─ exec error + retries ──► superior_generator ──┤
        ├─ exec error + exhausted ──► performance_opt   │
        └─ ok ──────────────────────────────────────────  │
                                                         │
        ▼                                                │
  judge (Groq qwen3.8-27b)                              │
        ├─ wrong + retries ──► superior_generator ───────┤
        ├─ wrong + exhausted ──► performance_optimizer    │
        └─ correct ──────────────────────────────────────  │
                                                         │
  superior_generator (OmniRoute · error context) ────────┘
        └──► syntax_validator  (retry loop)
                                                         
        ▼  (all paths converge)
  performance_optimizer (llama-3.3-70b · conditional)
        ▼
  privacy_guard (PII redact · audit log)
        │
        ├──► Redis SETEX (1hr TTL)
        ├──► sessions.db (SQLite history)
        └──► Langfuse span (optional tracing)
        │
        ▼
       END
```

**Why two models?** The finetuned HuggingFace model is fast and cheap — ideal for queries in its training distribution (~60–70% of real business queries). The OmniRoute superior model handles novel or complex queries, and receives the previous SQL and exact error as context so it can self-correct rather than starting from scratch.

**Why parallel start?** Schema analysis (a Groq LLM call) and Redis cache lookup are completely independent. Running them concurrently means the fan-in completes as soon as both finish — in practice they both complete in ~10ms if Redis is local, with no serialization penalty.

**Why Docker?** SQL execution happens inside an isolated container against a read-only copy of the database. A malformed or destructive query cannot touch the real file. The container receives the SQL string directly, not a shell command.

**Why semantic caching?** Redis caches by `SHA-256(normalize(question) + schema identifiers)`. A repeated or paraphrased question that produces the same hash returns instantly without touching any LLM.

---

## Installation

```bash
pip install -e .
```

**Requirements:** Python ≥ 3.10, Docker (optional but recommended), Redis (optional).

---

## Quick Start

```bash
# 1. Write config to .env
nl2sql init --db ./my.db --schema ./schema.sql

# 2. Launch the TUI
nl2sql

# 3. Or run a one-shot query
nl2sql query "Show the top 5 products by revenue this quarter"

# 4. JSON output for scripting
nl2sql query "Total orders today" --json-output
```

---

## Configuration

All settings are read from `.env` — searched from the current directory up to `$HOME`.

```env
# ── Required ──────────────────────────────────────────
DB_PATH=./database/production.db
SCHEMA_PATH=./database/schema.sql

# ── Finetuned model (Step 1) ──────────────────────────
HF_ENDPOINT=https://your-endpoint.huggingface.cloud
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxx

# ── Superior model (Steps 2-3) ────────────────────────
OMNIROUTE_URL=https://your-omniroute-host
OMNIROUTE_GEN_MODEL=auto/coding:free
OMNIROUTE_JUDGE_MODEL=no-think/antigravity/claude-sonnet-4-6

# ── Schema analyzer + judge (Groq) ────────────────────
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxx
GROQ_JUDGE_MODEL=qwen/qwen3.8-27b
GROQ_OPTIMIZER_MODEL=llama-3.3-70b-versatile

# ── Optional: Caching ─────────────────────────────────
REDIS_URL=redis://localhost:6379       # omit to disable

# ── Optional: Observability ───────────────────────────
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com

# ── Tuning ────────────────────────────────────────────
MAX_FINETUNED_STEPS=1   # steps before escalating to superior model
MAX_TOTAL_STEPS=3       # maximum total retry attempts
```

---

## Project Layout

```
nl2sql/
├── cli.py                    # Click entrypoint — init / query / status
├── core/
│   ├── config.py             # Config loader (.env → typed dataclass)
│   ├── engine.py             # AgenticLoop shim → run_graph()
│   ├── graph.py              # LangGraph StateGraph — all 9 agents + routing
│   ├── llm.py                # OpenAI-compatible async HTTP client
│   ├── sandbox.py            # DockerSandbox + local SQLite fallback
│   ├── session.py            # SQLite session history store
│   ├── state.py              # GraphState TypedDict + reducers
│   └── tracing.py            # Langfuse integration (optional)
├── agents/
│   ├── schema_analyzer.py    # Groq LLM table selection
│   ├── sql_generator.py      # Finetuned + superior SQL generation
│   ├── syntax_validator.py   # sqlglot parse + SELECT-only guard
│   ├── executor.py           # DockerSandbox SQL runner
│   ├── judge.py              # LLM-as-judge correctness evaluation
│   ├── performance_optimizer.py  # Groq SQL rewrite for complex queries
│   └── privacy_guard.py      # PII column redaction + audit log
├── tui/
│   ├── app.py                # Textual NL2SQLApp
│   └── dialogs/help_dialog.py
└── queue/
    └── worker.py             # Redis cache helpers (SHA-256 key)

db/
├── hr.db                     # Sample HR database
├── hr.sql                    # HR schema
├── edge_cases.db             # Comprehensive edge-case test database
└── edge_cases.sql            # 13 tables: NULLs, self-joins, unicode,
                              # type affinity traps, empty tables,
                              # reserved-word columns, LIKE wildcards

docs/
├── architecture.json         # Archify source spec
├── architecture.html         # Interactive diagram (pan/zoom/dark mode)
└── architecture.svg          # Static embed (this README)

eval/
├── run_eval.py               # Full Spider benchmark (asyncio.gather, --concurrency)
├── run_mini_eval.py          # Sequential 50-entry mini eval
├── test_quick.py             # Single-case smoke test
├── test_timing.py            # Per-phase timing diagnostic
└── analyze.py                # CSV result comparison across modes
```

---

## TUI Keybindings

| Key | Action |
|---|---|
| `Ctrl+N` | New session |
| `Ctrl+E` | Export results to CSV |
| `F1` | Help |
| `Ctrl+C` | Quit |

---

## Edge-Case Test Database

`db/edge_cases.db` is a comprehensive SQLite database for testing NL2SQL robustness across 13 tables and 2 views:

| Edge Case | How it's covered |
|---|---|
| NULL handling | Nullable `salary`, `email`, `location`, `manager_id`, `product_id` |
| Self-referential joins | `employees.manager_id → employees.id` (manager hierarchy) |
| Many-to-many | `projects ↔ employees` via `project_assignments`; `employees ↔ skills` |
| Unicode data | Employee names: `García López`, `山田太郎`; Japanese notes |
| Numeric extremes | Zero salary, negative salary (clawback), `9999999.99` CEO salary |
| Date extremes | Epoch `1970-01-01`, far-future `2099-12-31` in `audit_log` |
| Empty result set | `empty_table` — intentionally unpopulated |
| Type affinity traps | `price_text TEXT` (price stored as string), `qty_real REAL` (qty as float) |
| Reserved-word columns | `"table"`, `"action"`, `"when"` in `audit_log` |
| LIKE wildcard collision | Tags containing literal `%` and `_` characters |
| Duplicate rows | Same first name (two Alice employees), same salary on hire |
| Boolean as integer | `is_active INTEGER` (0/1), `discontinued INTEGER` |
| Cascading joins | Orders → Products → Categories; Assignments → Projects → Tags |

---

## Graceful Degradation

Every external dependency degrades cleanly — the core agentic loop has no hard requirements:

| Component | Present | Absent |
|---|---|---|
| Docker | SQL runs in isolated container | Falls back to local read-only SQLite |
| Redis | Results cached with 1hr TTL | Cache skipped; every query hits LLMs |
| Groq API | `schema_analyzer` + `judge` + `performance_optimizer` use Groq | Falls back to OmniRoute for all LLM calls |
| Langfuse | Full LLM span tracing | No-op; no impact on results |

---

## License

MIT — see [LICENSE](LICENSE).
