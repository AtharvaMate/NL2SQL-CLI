"""
LangGraph StateGraph for NL2SQL multi-agent pipeline.

Flow:
  START
    → parallel_start               (no-op fan-out trigger)
      → [schema_analyzer,          (LLM table selection — Groq)
         cache_lookup]             (Redis cache check)          ← run in PARALLEL
      → dispatch_or_skip           (fan-in: route on cache_hit)
        ├─ cache hit  → privacy_guard → END   (skip all LLM generation)
        └─ cache miss → finetuned_generator
    → finetuned_generator          (generate SQL with finetuned model)
    → syntax_validator             (parse + schema check — no Docker needed)
      → [fail, finetuned]  → superior_generator
      → [fail, superior]   → end_exhausted
      → [pass]             → executor
    → executor                     (run SQL in DockerSandbox)
      → [exec error, finetuned] → superior_generator
      → [exec error, superior]  → superior_generator (with error ctx)
      → [ok]               → judge
    → judge
      → [correct]          → performance_optimizer → privacy_guard → END
      → [wrong, retries left] → superior_generator
      → [wrong, exhausted]    → performance_optimizer → privacy_guard → END
    → superior_generator           (retry with error context)
      → syntax_validator  (same validation loop)
    → performance_optimizer        (conditional SQL rewrite via Groq)
    → privacy_guard                (redact sensitive columns, write audit log)
    → END
"""
from __future__ import annotations

import json
from typing import AsyncIterator

from langgraph.graph import StateGraph, END

from nl2sql.core.config import Config
from nl2sql.core.engine import StepEvent
from nl2sql.core.sandbox import DockerSandbox
from nl2sql.core.session import QueryAttempt
from nl2sql.core.state import GraphState
from nl2sql import agents


# ── node factories (bind config + sandbox via partial) ────────────────────────

def _make_nodes(config: Config, sandbox: DockerSandbox) -> dict:
    from nl2sql.agents import (
        schema_analyzer,
        sql_generator,
        syntax_validator,
        executor,
        judge,
        performance_optimizer,
        privacy_guard,
    )
    from nl2sql.queue.worker import get_redis, _cache_hash, CACHE_KEY

    # ── parallel fan-out trigger (no-op) ──────────────────────────────────────
    async def _parallel_start(state: GraphState) -> dict:
        return {}

    # ── parallel branch 1: Redis cache lookup ─────────────────────────────────
    async def _cache_lookup(state: GraphState) -> dict:
        try:
            r = get_redis(config.redis_url)
            r.ping()
            key = CACHE_KEY.format(hash=_cache_hash(state["question"], state["full_schema"]))
            raw = r.get(key)
            if raw:
                return {"cache_hit": True, "cached_result": json.loads(raw)}
        except Exception:
            pass  # ponytail: Redis unavailable → cache miss, no crash
        return {"cache_hit": False, "cached_result": None}

    # ── parallel branch 2: schema analysis ────────────────────────────────────
    async def _schema_analyzer(state: GraphState) -> dict:
        return await schema_analyzer.run(state, config)

    # ── fan-in dispatch: route on cache_hit after both branches complete ───────
    async def _dispatch_or_skip(state: GraphState) -> dict:
        if state.get("cache_hit") and state.get("cached_result"):
            payload = state["cached_result"]
            return {
                "final_sql": payload.get("sql", ""),
                "final_result": payload.get("result"),
                "success": True,
            }
        return {}

    # ── existing generator / validator / judge nodes (unchanged) ──────────────
    async def _finetuned_generator(state: GraphState) -> dict:
        updates = await sql_generator.run_finetuned(state, config)
        return {**updates, "step": state["step"] + 1}

    async def _superior_generator(state: GraphState) -> dict:
        updates = await sql_generator.run_superior(state, config)
        return {**updates, "step": state["step"] + 1}

    async def _syntax_validator(state: GraphState) -> dict:
        return await syntax_validator.run(state)

    async def _executor(state: GraphState) -> dict:
        return await executor.run(state, config, sandbox)

    async def _judge(state: GraphState) -> dict:
        return await judge.run(state, config)

    async def _performance_optimizer(state: GraphState) -> dict:
        return await performance_optimizer.run(state, config)

    async def _privacy_guard(state: GraphState) -> dict:
        return await privacy_guard.run(state, config)

    return {
        "parallel_start": _parallel_start,
        "cache_lookup": _cache_lookup,
        "schema_analyzer": _schema_analyzer,
        "dispatch_or_skip": _dispatch_or_skip,
        "finetuned_generator": _finetuned_generator,
        "superior_generator": _superior_generator,
        "syntax_validator": _syntax_validator,
        "executor": _executor,
        "judge": _judge,
        "performance_optimizer": _performance_optimizer,
        "privacy_guard": _privacy_guard,
    }


# ── edge routing functions ─────────────────────────────────────────────────────

def _route_after_dispatch(state: GraphState) -> str:
    """Cache hit → skip generation entirely; miss → normal pipeline."""
    return "privacy_guard" if state.get("cache_hit") else "finetuned_generator"


def _route_after_syntax(state: GraphState) -> str:
    if state.get("syntax_valid"):
        return "executor"
    model = state.get("current_model_type", "finetuned")
    if model == "finetuned":
        return "superior_generator"
    # superior produced invalid SQL — give up
    return "performance_optimizer"


def _route_after_executor(state: GraphState) -> str:
    if state.get("execution_error"):
        attempts = state.get("attempts", [])
        total = state.get("total_steps", 3)
        if len(attempts) < total:
            return "superior_generator"
        return "performance_optimizer"  # exhausted — stop looping
    return "judge"


def _route_after_judge(state: GraphState) -> str:
    if state.get("success"):
        return "performance_optimizer"
    attempts = state.get("attempts", [])
    total = state.get("total_steps", 3)
    if len(attempts) < total:
        return "superior_generator"
    return "performance_optimizer"  # exhausted — best attempt already set


# ── graph builder ──────────────────────────────────────────────────────────────

def build_graph(config: Config, sandbox: DockerSandbox) -> StateGraph:
    nodes = _make_nodes(config, sandbox)

    g = StateGraph(GraphState)
    for name, fn in nodes.items():
        g.add_node(name, fn)

    # ── parallel fan-out: parallel_start → [cache_lookup, schema_analyzer] ────
    g.set_entry_point("parallel_start")
    g.add_edge("parallel_start", "cache_lookup")
    g.add_edge("parallel_start", "schema_analyzer")

    # ── fan-in: wait for BOTH branches, then dispatch ─────────────────────────
    g.add_edge(["cache_lookup", "schema_analyzer"], "dispatch_or_skip")
    g.add_conditional_edges("dispatch_or_skip", _route_after_dispatch)

    # ── existing pipeline (unchanged) ─────────────────────────────────────────
    g.add_edge("finetuned_generator", "syntax_validator")
    g.add_conditional_edges("syntax_validator", _route_after_syntax)
    g.add_conditional_edges("executor", _route_after_executor)
    g.add_edge("superior_generator", "syntax_validator")
    g.add_conditional_edges("judge", _route_after_judge)
    g.add_edge("performance_optimizer", "privacy_guard")
    g.add_edge("privacy_guard", END)

    return g.compile()


# ── public async generator — yields StepEvents for TUI compatibility ───────────

async def run_graph(
    question: str,
    schema: str,
    config: Config,
    sandbox: DockerSandbox,
) -> AsyncIterator[StepEvent]:
    """
    Run the full multi-agent graph and yield StepEvent objects so existing
    TUI and worker code needs no changes to its rendering logic.
    """
    graph = build_graph(config, sandbox)

    initial_state: GraphState = {
        "question": question,
        "full_schema": schema,
        "filtered_schema": schema,
        "schema_tables": [],
        "current_sql": "",
        "current_model_type": "finetuned",
        "skip_finetuned": False,
        "syntax_valid": False,
        "syntax_error": None,
        "execution_result": None,
        "execution_error": None,
        "judge_verdict": None,
        "optimized_sql": None,
        "sensitive_columns_found": [],
        "attempts": [],
        "best_attempt": None,
        "cache_hit": False,
        "cached_result": None,
        "final_sql": "",
        "final_result": None,
        "success": False,
        "step": 0,
        "total_steps": config.max_total_steps,
    }

    total = config.max_total_steps
    current_step = 0
    # accumulate state as nodes emit updates
    accumulated: GraphState = dict(initial_state)  # type: ignore[assignment]

    async for event in graph.astream(initial_state, stream_mode="updates"):
        node_name = next(iter(event))
        updates = event[node_name] or {}
        # merge updates into accumulated so final read doesn't need a second invoke
        accumulated.update(updates)

        # ── yield TUI-compatible StepEvents per node ──────────────────────────

        if node_name == "parallel_start":
            pass  # no-op, no event

        elif node_name == "cache_lookup":
            if updates.get("cache_hit"):
                payload = updates["cached_result"]
                yield StepEvent(
                    step=0, total=total, model_type="",
                    phase="cache_hit",
                    sql=payload.get("sql", ""),
                    result=payload.get("result"),
                )

        elif node_name == "schema_analyzer":
            tables = updates.get("schema_tables", [])
            yield StepEvent(
                step=0, total=total, model_type="",
                phase="schema_analyzed",
                sql=f"Relevant tables: {', '.join(tables) or 'all'}",
            )

        elif node_name == "dispatch_or_skip":
            pass  # routing node — cache_lookup already emitted the event

        elif node_name in ("finetuned_generator", "superior_generator"):
            current_step = updates.get("step", current_step)
            model = updates.get("current_model_type", "finetuned")
            yield StepEvent(
                step=current_step, total=total, model_type=model,
                phase="generating",
            )

        elif node_name == "syntax_validator":
            if not updates.get("syntax_valid", True):
                yield StepEvent(
                    step=current_step, total=total, model_type="",
                    phase="syntax_error",
                    error=updates.get("syntax_error"),
                )

        elif node_name == "executor":
            sql = accumulated.get("current_sql", "")
            if updates.get("execution_error"):
                yield StepEvent(
                    step=current_step, total=total, model_type="",
                    phase="execution_error",
                    sql=sql,
                    error=updates.get("execution_error"),
                )
            else:
                yield StepEvent(
                    step=current_step, total=total, model_type="",
                    phase="executing",
                    sql=sql,
                )

        elif node_name == "judge":
            verdict = updates.get("judge_verdict", {})
            succeeded = updates.get("success", False)
            if succeeded:
                yield StepEvent(
                    step=current_step, total=total,
                    model_type=accumulated.get("current_model_type", ""),
                    phase="success",
                    sql=updates.get("final_sql", ""),
                    result=updates.get("final_result"),
                    judge_verdict=verdict,
                    success=True,
                    is_final=False,
                )
            else:
                yield StepEvent(
                    step=current_step, total=total, model_type="",
                    phase="judge_rejected",
                    judge_verdict=verdict,
                    error=verdict.get("reason"),
                )

        elif node_name == "performance_optimizer":
            optimized = updates.get("optimized_sql")
            if optimized:
                yield StepEvent(
                    step=current_step, total=total, model_type="",
                    phase="optimized",
                    sql=optimized,
                )

        elif node_name == "privacy_guard":
            sensitive = updates.get("sensitive_columns_found", [])
            if sensitive:
                yield StepEvent(
                    step=current_step, total=total, model_type="",
                    phase="privacy_redacted",
                    error=f"Redacted columns: {', '.join(sensitive)}",
                )

    # ── emit final event from accumulated state (no second invoke) ────────────
    if accumulated.get("success"):
        sql = accumulated.get("final_sql", "")
        optimized = accumulated.get("optimized_sql")
        yield StepEvent(
            step=current_step, total=total, model_type="",
            phase="success",
            sql=sql,
            result=accumulated.get("final_result"),
            judge_verdict=accumulated.get("judge_verdict"),
            success=True,
            is_final=True,
            best_attempt=accumulated.get("best_attempt"),
        )
        # Emit optimized SQL as advisory if available and different
        if optimized and optimized != sql:
            yield StepEvent(
                step=current_step, total=total, model_type="",
                phase="optimized",
                sql=optimized,
            )
    else:
        best = accumulated.get("best_attempt")
        yield StepEvent(
            step=total, total=total, model_type="superior",
            phase="exhausted",
            is_final=True,
            success=False,
            best_attempt=best,
        )
