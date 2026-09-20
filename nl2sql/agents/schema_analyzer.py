"""
SchemaAnalyzer agent.

Uses Groq LLM to intelligently select relevant tables from the schema
based on the natural language question. Multi-agent: this agent performs
schema analysis before SQL generation.
"""
from __future__ import annotations

import json
import re

from nl2sql.core.config import Config
from nl2sql.core.llm import LLMClient
from nl2sql.core.state import GraphState


SCHEMA_SELECTION_PROMPT = """\
You are a SQL query planner. Given a question, determine which database tables are needed.

Available tables:
{table_summary}

Question: {question}

Analyze step-by-step:
1. What information does the question ask for?
2. Which table(s) contain that information?
3. Does the question mention relationships or "by" clauses that require joins?

Guidelines:
- Single entity queries (e.g., "employees with salary > X"): return that table only
- Multi-entity queries (e.g., "employees AND their departments"): return both tables
- Aggregations grouped by another entity (e.g., "by department"): return both tables
- Ignore unrelated tables completely

Output format: JSON array of lowercase table names only.
Examples:
- For "count employees": ["employees"]
- For "employees with their department names": ["employees", "departments"]
- For "average salary by department": ["employees", "departments"]

Your answer (JSON array only):
"""


def _parse_tables(ddl: str) -> dict[str, str]:
    """Return {table_name: full_create_block} for each CREATE TABLE in DDL."""
    pattern = re.compile(
        r"(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(\w+)[`\"\[]?\s*\(.*?\);)",
        re.IGNORECASE | re.DOTALL,
    )
    return {m.group(2).lower(): m.group(1) for m in pattern.finditer(ddl)}


def _extract_columns(ddl_block: str) -> list[str]:
    """Extract column names from a CREATE TABLE block."""
    # Match column definitions (simple heuristic: word before type or constraint)
    col_pattern = re.compile(r"\b(\w+)\s+(?:INTEGER|TEXT|REAL|BLOB|NUMERIC|VARCHAR|CHAR|DATE|TIMESTAMP)", re.IGNORECASE)
    return [m.group(1).lower() for m in col_pattern.finditer(ddl_block) if m.group(1).lower() not in ("primary", "foreign", "key", "not", "null", "unique", "check", "default")]


def _build_table_summary(tables: dict[str, str]) -> str:
    """Build a concise summary of all tables and their columns."""
    lines = []
    for name, ddl in tables.items():
        cols = _extract_columns(ddl)
        lines.append(f"- {name}: {', '.join(cols[:8])}")  # first 8 columns
    return "\n".join(lines)


async def _llm_select_tables(question: str, tables: dict[str, str], config: Config) -> set[str]:
    """Use Groq LLM to select relevant tables."""
    if not config.groq_api_key:
        # Fallback to keyword matching if Groq not configured
        return _keyword_fallback(question, tables)

    table_summary = _build_table_summary(tables)
    client = LLMClient(
        endpoint="https://api.groq.com/openai/v1/chat/completions",
        token=config.groq_api_key,
        model=config.groq_judge_model,  # use same model as judge (qwen/qwen3.8-27b)
    )

    try:
        prompt = SCHEMA_SELECTION_PROMPT.format(
            table_summary=table_summary,
            question=question,
        )
        response = await client.generate(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
        )
        # Parse JSON array from response
        response = response.strip()
        if response.startswith("```"):
            response = re.sub(r"^```(?:json)?\s*", "", response)
            response = re.sub(r"\s*```$", "", response)
        selected = json.loads(response)
        if isinstance(selected, list):
            return {name.lower() for name in selected if name.lower() in tables}
    except Exception:
        pass  # ponytail: LLM call failed, fall back to keyword matching

    return _keyword_fallback(question, tables)


def _keyword_fallback(question: str, tables: dict[str, str]) -> set[str]:
    """Keyword-based fallback when LLM is unavailable."""
    q_lower = question.lower()
    matched = set()
    for name in tables:
        if re.search(rf"\b{re.escape(name)}\b", q_lower):
            matched.add(name)
    return matched if matched else set(tables.keys())


async def run(state: GraphState, config: Config) -> dict:
    tables = _parse_tables(state["full_schema"])
    
    # Use LLM to intelligently select relevant tables
    selected_names = await _llm_select_tables(state["question"], tables, config)
    
    # Build filtered schema from selected tables
    if selected_names:
        filtered_tables = {name: tables[name] for name in selected_names if name in tables}
    else:
        filtered_tables = tables  # fallback to full schema
    
    filtered_ddl = "\n\n".join(filtered_tables.values()) if filtered_tables else state["full_schema"]
    
    return {
        "schema_tables": list(filtered_tables.keys()),  # Return only selected tables
        "filtered_schema": filtered_ddl,
    }
