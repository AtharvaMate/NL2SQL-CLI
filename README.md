# NL2SQL CLI

Agentic NL2SQL CLI with self-correcting query generation, LLM-as-judge evaluation, and Docker-sandboxed execution.

## Overview

NL2SQL CLI converts natural language questions into SQL queries using a progressive multi-step agentic loop. It tries a cheap finetuned model first, escalates to a superior model on failure, and validates each attempt using Docker-sandboxed execution and an LLM judge. Optional Redis caching and Langfuse observability are available.

## Interactive Architecture Diagrams

Two interactive HTML diagrams are included in `docs/`:

| Diagram | File | Description |
|---|---|---|
| **System Architecture** | [`docs/architecture.html`](docs/architecture.html) | Components, boundaries, and data paths |
| **Query Execution Flow** | [`docs/workflow.html`](docs/workflow.html) | Step-by-step query processing with escalation logic |

Open either file in a browser. Both diagrams support pan/zoom, dark/light theme, and search.

## Architecture

```
User (TUI / CLI)
    │
    ▼
CLI Entry (cli.py)
    ├─── Config (core/config.py)   ← reads .env: DB, schema, LLM endpoints
    └─── Textual TUI (tui/app.py)
              │
              ▼
         Agentic Loop (core/engine.py)
              ├── Redis cache check (optional, skip if hit)
              ├── LLM Client (core/llm.py)
              │     ├── Step 1 → HuggingFace finetuned model
              │     └── Steps 2-3 → OmniRoute superior model (with error feedback)
              ├── Docker Sandbox (core/sandbox.py) → Docker container (read-only DB)
              ├── LLM Judge → OmniRoute eval (pass/fail)
              ├── Session Store (core/session.py) → SQLite
              └── Langfuse (core/tracing.py) → trace spans (optional)
```

### Components

| Component | File | Role |
|---|---|---|
| **CLI Entry** | `nl2sql/cli.py` | Command entrypoint; launches TUI or runs bare `query`/`init`/`status` |
| **Config** | `nl2sql/core/config.py` | Loads `.env` from cwd → git root → home; builds `Config` dataclass |
| **Textual TUI** | `nl2sql/tui/app.py` | Rich terminal UI with query input, live log, result table, CSV export |
| **Agentic Loop** | `nl2sql/core/engine.py` | Orchestrates generate → execute → judge with progressive escalation |
| **LLM Client** | `nl2sql/core/llm.py` | OpenAI-compatible HTTP client for SQL generation and judging |
| **Docker Sandbox** | `nl2sql/core/sandbox.py` | Runs SQL in an isolated Docker container against a read-only DB copy |
| **Session Store** | `nl2sql/core/session.py` | Persists query history and all attempts to `~/.nl2sql/sessions.db` |
| **Worker/Cache** | `nl2sql/queue/worker.py` | Optional RQ distributed task + Redis pub/sub for cache-aside pattern |

### Query Execution Flow

1. User submits a natural language question
2. Engine checks Redis cache (semantic hash of question + schema)
3. On cache miss: step 1 — finetuned HuggingFace model generates SQL
4. SQL is executed inside a Docker container (read-only DB copy)
5. LLM judge evaluates correctness
6. On failure: steps 2–3 — OmniRoute superior model retries with error context
7. Final SQL and result are displayed; session is persisted to SQLite

## Configuration

All settings via `.env` in any parent directory up to `$HOME`:

```env
# Required
DB_PATH=./my.db
SCHEMA_PATH=./schema.sql

# LLM endpoints
HF_ENDPOINT=https://your-hf-inference-endpoint
HF_TOKEN=hf_...
OMNIROUTE_URL=https://your-omniroute-url
OMNIROUTE_GEN_MODEL=gpt-4o
OMNIROUTE_JUDGE_MODEL=gpt-4o-mini

# Optional
REDIS_URL=redis://localhost:6379
LANGFUSE_PUBLIC_KEY=pk-...
LANGFUSE_SECRET_KEY=sk-...
LANGFUSE_HOST=https://cloud.langfuse.com
MAX_FINETUNED_STEPS=1
MAX_TOTAL_STEPS=3
```

## Installation

```bash
pip install -e .
nl2sql init        # scaffold .env template
nl2sql             # launch TUI
nl2sql query "How many users signed up last month?"
```

## Key Design Decisions

- **Progressive escalation**: cheap finetuned model first, expensive superior model only on failure — reduces cost
- **Self-correcting loop**: error text and prior SQL are fed back as context on retry
- **Sandbox isolation**: Docker prevents malformed SQL from corrupting the real database
- **Semantic caching**: Redis caches results by `hash(question + schema identifiers)` — 1hr TTL
- **Streaming events**: `AgenticLoop` is an async generator; TUI updates live as steps complete
- **Everything optional**: Docker, Redis, and Langfuse all fall back gracefully when unavailable
