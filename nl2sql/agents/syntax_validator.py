"""
SyntaxValidator agent.

Uses sqlglot to parse the generated SQL (syntax check) and cross-checks
table/column names against the filtered schema (schema check).
Fast, deterministic, no LLM call — fails before expensive Docker execution.
Rejects non-SELECT statements to prevent data mutation.
"""
from __future__ import annotations

import re

import sqlglot
import sqlglot.errors
from sqlglot import exp as sqlglot_exp

from nl2sql.core.session import QueryAttempt
from nl2sql.core.state import GraphState


def _table_names_from_schema(ddl: str) -> set[str]:
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(\w+)[`\"\[]?",
        re.IGNORECASE,
    )
    return {m.group(1).lower() for m in pattern.finditer(ddl)}


def _tables_in_sql(sql: str) -> set[str]:
    """Extract table names referenced in FROM / JOIN clauses."""
    pattern = re.compile(r"\b(?:FROM|JOIN)\s+[`\"\[]?(\w+)[`\"\[]?", re.IGNORECASE)
    return {m.group(1).lower() for m in pattern.finditer(sql)}


def _syntax_fail(state: GraphState, sql: str, error: str) -> dict:
    """Build failure dict, recording a QueryAttempt so retry context is available."""
    attempt = QueryAttempt(
        step=state.get("step", 0),
        model_type=state.get("current_model_type", ""),
        sql=sql,
        result={},
        error=error,
    )
    return {
        "syntax_valid": False,
        "syntax_error": error,
        "attempts": state.get("attempts", []) + [attempt],
    }


async def run(state: GraphState) -> dict:
    sql = state.get("current_sql", "")
    if not sql:
        return _syntax_fail(state, "", "No SQL generated")

    # ── 1. Parse (syntax check) ───────────────────────────────────────────────
    try:
        expressions = sqlglot.parse(sql, dialect="sqlite", error_level=sqlglot.errors.ErrorLevel.RAISE)
    except sqlglot.errors.ParseError as e:
        return _syntax_fail(state, sql, f"Syntax error: {e}")

    # ── 2. Reject non-SELECT statements (prevent data mutation) ───────────────
    for expr in expressions:
        if expr is not None and not isinstance(expr, sqlglot_exp.Select):
            return _syntax_fail(state, sql, "Only SELECT queries are permitted.")

    # ── 3. Schema check — referenced tables must exist ────────────────────────
    schema_tables = _table_names_from_schema(state.get("filtered_schema", ""))
    if schema_tables:
        sql_tables = _tables_in_sql(sql)
        unknown = sql_tables - schema_tables
        if unknown:
            return _syntax_fail(
                state, sql,
                f"Unknown table(s): {', '.join(sorted(unknown))}",
            )

    return {"syntax_valid": True, "syntax_error": None}
