from __future__ import annotations

from typing import Annotated, Literal
from typing_extensions import TypedDict

from nl2sql.core.session import QueryAttempt


def _keep_last(a, b):
    """Reducer: always keep the most recent value."""
    return b


def _append(a: list, b: list) -> list:
    """Reducer: append new items to list."""
    return a + b


class GraphState(TypedDict):
    # ── inputs ────────────────────────────────────────────────────────────────
    question: str
    full_schema: str                # raw schema DDL as passed in

    # ── SchemaAnalyzer output ─────────────────────────────────────────────────
    filtered_schema: str            # DDL containing only relevant tables
    schema_tables: list[str]        # table names extracted from full schema

    # ── SQLGenerator output ───────────────────────────────────────────────────
    current_sql: str
    current_model_type: Literal["finetuned", "superior"]
    skip_finetuned: bool            # set True after finetuned fails

    # ── SyntaxValidator output ────────────────────────────────────────────────
    syntax_valid: bool
    syntax_error: str | None

    # ── Executor output ───────────────────────────────────────────────────────
    execution_result: dict | None
    execution_error: str | None

    # ── Judge output ──────────────────────────────────────────────────────────
    judge_verdict: dict | None

    # ── PerformanceOptimizer output ───────────────────────────────────────────
    optimized_sql: str | None

    # ── PrivacyGuard output ───────────────────────────────────────────────────
    sensitive_columns_found: list[str]

    # ── retries + history ─────────────────────────────────────────────────────
    attempts: list[QueryAttempt]    # all attempts so far
    best_attempt: QueryAttempt | None

    # ── cache (populated by parallel cache_lookup node) ───────────────────────
    cache_hit: bool                 # True if Redis returned a cached result
    cached_result: dict | None      # {"sql": str, "result": dict} on hit

    # ── final ─────────────────────────────────────────────────────────────────
    final_sql: str
    final_result: dict | None
    success: bool
    step: int                       # current step number (for TUI display)
    total_steps: int
