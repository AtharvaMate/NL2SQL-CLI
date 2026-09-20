from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from nl2sql.core.config import Config
from nl2sql.core.llm import LLMClient
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

    async def run(
        self, question: str, schema: str
    ) -> AsyncIterator[StepEvent]:
        from nl2sql.core.graph import run_graph
        async for ev in run_graph(question, schema, self.config, self.sandbox):
            yield ev

    async def prewarm_hf(self) -> bool:
        return await self.finetuned_client.ping()
