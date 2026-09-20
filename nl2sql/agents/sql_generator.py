"""
SQLGenerator agents — finetuned and superior variants.

Both read GraphState and return {current_sql, current_model_type, step, attempts}.
Superior model receives previous SQL + error as retry context.
"""
from __future__ import annotations

from nl2sql.core.config import Config
from nl2sql.core.llm import LLMClient, FINETUNED_SYSTEM_PROMPT, SUPERIOR_SYSTEM_PROMPT
from nl2sql.core.session import QueryAttempt
from nl2sql.core.state import GraphState


async def run_finetuned(state: GraphState, config: Config) -> dict:
    client = LLMClient(endpoint=config.hf_endpoint, token=config.hf_token)
    step = state["step"]

    try:
        sql = await client.generate_sql(
            question=state["question"],
            schema=state["filtered_schema"],
            system_prompt=FINETUNED_SYSTEM_PROMPT,
        )
    except Exception as e:
        attempt = QueryAttempt(
            step=step, model_type="finetuned", sql="", result={},
            error=f"Generation failed: {e}",
        )
        return {
            "current_sql": "",
            "current_model_type": "finetuned",
            "attempts": state["attempts"] + [attempt],
            "skip_finetuned": True,
            "syntax_valid": False,
            "syntax_error": str(e),
        }

    return {
        "current_sql": sql,
        "current_model_type": "finetuned",
        "skip_finetuned": False,
        "syntax_error": None,
    }


async def run_superior(state: GraphState, config: Config) -> dict:
    client = LLMClient(
        endpoint=config.omniroute_url,
        model=config.omniroute_gen_model,
    )
    step = state["step"]
    attempts = state["attempts"]

    # Pass retry context if we have a previous attempt
    previous_sql = attempts[-1].sql if attempts else None
    previous_error = attempts[-1].error if attempts else None

    try:
        sql = await client.generate_sql(
            question=state["question"],
            schema=state["filtered_schema"],
            system_prompt=SUPERIOR_SYSTEM_PROMPT,
            previous_sql=previous_sql,
            error=previous_error,
        )
    except Exception as e:
        attempt = QueryAttempt(
            step=step, model_type="superior", sql="", result={},
            error=f"Generation failed: {e}",
        )
        return {
            "current_sql": "",
            "current_model_type": "superior",
            "attempts": attempts + [attempt],
            "syntax_valid": False,
            "syntax_error": str(e),
        }

    return {
        "current_sql": sql,
        "current_model_type": "superior",
        "syntax_error": None,
    }
