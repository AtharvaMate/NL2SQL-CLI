"""
PerformanceOptimizer agent.

Conditionally rewrites approved SQL for performance using Groq llama-3.3-70b-versatile.
Triggers when the query contains JOINs/subqueries/HAVING OR schema has >5 tables.
Skipped for simple single-table queries (no LLM cost on the fast path).
"""
from __future__ import annotations

import re

from nl2sql.core.config import Config
from nl2sql.core.llm import LLMClient
from nl2sql.core.state import GraphState

_COMPLEX_PATTERNS = re.compile(
    r"\b(JOIN|UNION|HAVING|EXISTS|SUBQUERY)\b", re.IGNORECASE
)

OPTIMIZER_SYSTEM_PROMPT = (
    "You are a SQLite performance expert. Given a working SQL query and its schema, "
    "rewrite it to be more efficient while returning identical results.\n\n"
    "Rules:\n"
    "1. Output ONLY the optimized SQL — no explanation, no markdown fences.\n"
    "2. If the query is already optimal, output it unchanged.\n"
    "3. Use only tables and columns present in the schema.\n"
    "4. Prefer explicit column lists over SELECT *.\n"
    "5. Push WHERE filters as early as possible.\n\n"
    "Schema:\n{schema}"
)


def _should_optimize(state: GraphState) -> bool:
    sql = state.get("final_sql", "").upper()
    table_count = len(state.get("schema_tables", []))
    return bool(_COMPLEX_PATTERNS.search(sql)) or table_count > 5


async def run(state: GraphState, config: Config) -> dict:
    if not _should_optimize(state):
        return {"optimized_sql": None}  # skipped — simple query

    sql = state["final_sql"]

    # Use Groq if key available, else fall back to superior model
    if config.groq_api_key:
        client = LLMClient(
            endpoint="https://api.groq.com/openai/v1/chat/completions",
            token=config.groq_api_key,
            model=config.groq_optimizer_model,
        )
    else:
        client = LLMClient(
            endpoint=config.omniroute_url,
            model=config.omniroute_gen_model,
        )

    try:
        messages = [
            {"role": "system", "content": OPTIMIZER_SYSTEM_PROMPT.format(schema=state["filtered_schema"])},
            {"role": "user", "content": f"Optimize this SQL:\n\n{sql}"},
        ]
        optimized = await client.generate(messages, temperature=0.0, max_tokens=512)
        # strip markdown if model wraps it anyway
        optimized = re.sub(r"^```(?:sql)?\s*", "", optimized.strip())
        optimized = re.sub(r"\s*```$", "", optimized).strip()
        return {"optimized_sql": optimized or None}
    except Exception:
        return {"optimized_sql": None}  # ponytail: optimizer is best-effort, never block result
