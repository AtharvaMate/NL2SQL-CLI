from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator

from nl2sql.core.config import Config
from nl2sql.core.llm import (
    LLMClient,
    FINETUNED_SYSTEM_PROMPT,
    SUPERIOR_SYSTEM_PROMPT,
)
from nl2sql.core.sandbox import DockerSandbox
from nl2sql.core.session import QueryAttempt


@dataclass
class StepEvent:
    step: int
    total: int
    model_type: str
    phase: str
    sql: str = ""
    result: dict | None = None
    judge_verdict: dict | None = None
    error: str | None = None
    success: bool = False
    is_final: bool = False
    best_attempt: QueryAttempt | None = None


class AgenticLoop:
    def __init__(self, config: Config, sandbox: DockerSandbox) -> None:
        self.config = config
        self.sandbox = sandbox

        self.finetuned_client = LLMClient(
            endpoint=config.hf_endpoint,
            token=config.hf_token,
        )
        self.superior_client = LLMClient(
            endpoint=config.omniroute_url,
            model=config.omniroute_gen_model,
        )
        self.judge_client = LLMClient(
            endpoint=config.omniroute_url,
            model=config.omniroute_judge_model,
        )

    async def run(
        self, question: str, schema: str
    ) -> AsyncIterator[StepEvent]:
        attempts: list[QueryAttempt] = []
        best_attempt: QueryAttempt | None = None
        skip_finetuned = False

        for step in range(1, self.config.max_total_steps + 1):
            is_finetuned = step <= self.config.max_finetuned_steps and not skip_finetuned
            model_type = "finetuned" if is_finetuned else "superior"
            client = self.finetuned_client if is_finetuned else self.superior_client
            system_prompt = (
                FINETUNED_SYSTEM_PROMPT if is_finetuned else SUPERIOR_SYSTEM_PROMPT
            )
            total = self.config.max_total_steps

            yield StepEvent(
                step=step,
                total=total,
                model_type=model_type,
                phase="generating",
            )

            # Only pass retry context to the superior model — the finetuned
            # HF endpoint is single-turn and ignores multi-turn messages.
            previous_sql = None
            previous_error = None
            if not is_finetuned and attempts:
                previous_sql = attempts[-1].sql
                previous_error = attempts[-1].error

            try:
                sql = await client.generate_sql(
                    question=question,
                    schema=schema,
                    system_prompt=system_prompt,
                    previous_sql=previous_sql,
                    error=previous_error,
                )
            except Exception as e:
                error_msg = str(e)
                attempt = QueryAttempt(
                    step=step,
                    model_type=model_type,
                    sql="",
                    result={},
                    error=f"Generation failed: {error_msg}",
                )
                attempts.append(attempt)
                yield StepEvent(
                    step=step,
                    total=total,
                    model_type=model_type,
                    phase="error",
                    error=error_msg,
                )
                continue

            yield StepEvent(
                step=step,
                total=total,
                model_type=model_type,
                phase="executing",
                sql=sql,
            )

            result = await self.sandbox.execute_sql(sql)

            if "error" in result and result["error"]:
                attempt = QueryAttempt(
                    step=step,
                    model_type=model_type,
                    sql=sql,
                    result=result,
                    error=result["error"],
                )
                attempts.append(attempt)
                yield StepEvent(
                    step=step,
                    total=total,
                    model_type=model_type,
                    phase="execution_error",
                    sql=sql,
                    result=result,
                    error=result["error"],
                )
                # Finetuned model can't self-correct — hand off to superior
                if is_finetuned:
                    skip_finetuned = True
                continue

            yield StepEvent(
                step=step,
                total=total,
                model_type=model_type,
                phase="judging",
                sql=sql,
                result=result,
            )

            try:
                verdict = await self.judge_client.judge_sql(
                    question=question,
                    schema=schema,
                    sql=sql,
                    result=result,
                )
            except Exception as e:
                verdict = {"correct": True, "reason": f"Judge unavailable: {e}", "suggestion": ""}

            attempt = QueryAttempt(
                step=step,
                model_type=model_type,
                sql=sql,
                result=result,
                judge_verdict=verdict,
            )
            attempts.append(attempt)

            if best_attempt is None or verdict.get("correct", False):
                best_attempt = attempt

            if verdict.get("correct", False):
                yield StepEvent(
                    step=step,
                    total=total,
                    model_type=model_type,
                    phase="success",
                    sql=sql,
                    result=result,
                    judge_verdict=verdict,
                    success=True,
                    is_final=True,
                    best_attempt=attempt,
                )
                return

            error_feedback = verdict.get("reason", "Query deemed incorrect")
            if verdict.get("suggestion"):
                error_feedback += f"\nSuggestion: {verdict['suggestion']}"

            attempt.error = error_feedback

            yield StepEvent(
                step=step,
                total=total,
                model_type=model_type,
                phase="judge_rejected",
                sql=sql,
                result=result,
                judge_verdict=verdict,
                error=error_feedback,
            )

            # Finetuned model is single-turn, will just repeat the same SQL.
            # Skip remaining finetuned steps so superior can iterate with feedback.
            if is_finetuned:
                skip_finetuned = True

        yield StepEvent(
            step=self.config.max_total_steps,
            total=self.config.max_total_steps,
            model_type="superior",
            phase="exhausted",
            is_final=True,
            success=False,
            best_attempt=best_attempt,
        )

    async def prewarm_hf(self) -> bool:
        return await self.finetuned_client.ping()
