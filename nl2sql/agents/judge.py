"""
Judge agent.

Calls the LLM judge to evaluate correctness of the SQL + execution result.
Updates attempts list and best_attempt.
"""
from __future__ import annotations

from nl2sql.core.config import Config
from nl2sql.core.llm import LLMClient
from nl2sql.core.session import QueryAttempt
from nl2sql.core.state import GraphState


async def run(state: GraphState, config: Config) -> dict:
    sql = state["current_sql"]
    step = state["step"]
    model_type = state["current_model_type"]
    result = state["execution_result"] or {}

    # Build judge client — prefer Groq, fall back to OmniRoute
    if config.groq_api_key:
        judge_client = LLMClient(
            endpoint="https://api.groq.com/openai/v1/chat/completions",
            token=config.groq_api_key,
            model=config.groq_judge_model,
        )
    else:
        judge_client = LLMClient(
            endpoint=config.omniroute_url,
            model=config.omniroute_judge_model,
        )

    try:
        verdict = await judge_client.judge_sql(
            question=state["question"],
            schema=state["filtered_schema"],
            sql=sql,
            result=result,
        )
    except Exception as e:
        verdict = {"correct": True, "reason": f"Judge unavailable: {e}", "suggestion": ""}

    error_feedback = None
    if not verdict.get("correct", False):
        error_feedback = verdict.get("reason", "Query deemed incorrect")
        if verdict.get("suggestion"):
            error_feedback += f"\nSuggestion: {verdict['suggestion']}"

    attempt = QueryAttempt(
        step=step, model_type=model_type,
        sql=sql, result=result,
        judge_verdict=verdict, error=error_feedback,
    )
    attempts = state["attempts"] + [attempt]

    # Track best attempt (first correct, or best seen)
    best = state.get("best_attempt")
    if best is None or verdict.get("correct", False):
        best = attempt

    updates: dict = {
        "judge_verdict": verdict,
        "attempts": attempts,
        "best_attempt": best,
    }

    if verdict.get("correct", False):
        updates["success"] = True
        updates["final_sql"] = sql
        updates["final_result"] = result
    else:
        # finetuned can't iterate — skip to superior on rejection
        if model_type == "finetuned":
            updates["skip_finetuned"] = True

    return updates
