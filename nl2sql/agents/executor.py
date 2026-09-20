"""
Executor agent.

Runs the current SQL in the DockerSandbox and writes the result to state.
"""
from __future__ import annotations

from nl2sql.core.config import Config
from nl2sql.core.sandbox import DockerSandbox
from nl2sql.core.session import QueryAttempt
from nl2sql.core.state import GraphState


async def run(state: GraphState, config: Config, sandbox: DockerSandbox) -> dict:
    sql = state["current_sql"]
    step = state["step"]
    model_type = state["current_model_type"]

    result = await sandbox.execute_sql(sql)

    if result.get("error"):
        attempt = QueryAttempt(
            step=step, model_type=model_type,
            sql=sql, result=result, error=result["error"],
        )
        return {
            "execution_result": result,
            "execution_error": result["error"],
            "attempts": state["attempts"] + [attempt],
            # finetuned can't self-correct — escalate immediately
            "skip_finetuned": True if model_type == "finetuned" else state.get("skip_finetuned", False),
        }

    return {
        "execution_result": result,
        "execution_error": None,
    }
