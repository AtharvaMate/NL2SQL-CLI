"""
Langfuse v3+ tracing — uses start_as_current_observation.
Call enable() once at boot. No-ops silently if keys are missing.
"""
from __future__ import annotations

import os
import time


def enable() -> bool:
    """Patch LLMClient.generate with Langfuse v3+ spans. Returns True if enabled."""
    pub = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sec = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not pub or not sec:
        return False

    try:
        from langfuse import Langfuse
        from nl2sql.core.llm import LLMClient

        lf = Langfuse(
            public_key=pub,
            secret_key=sec,
            host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )

        original_generate = LLMClient.generate

        async def _traced(self, messages, temperature=0.0, max_tokens=512):
            model_name = self.model or "hf-finetuned"
            t0 = time.monotonic()

            with lf.start_as_current_observation(
                name=model_name,
                as_type="generation",
                input=messages,
                model=model_name,
                model_parameters={"temperature": temperature, "max_tokens": max_tokens},
            ):
                try:
                    result = await original_generate(self, messages, temperature, max_tokens)
                    lf.update_current_span(
                        output=result,
                        metadata={"latency_s": round(time.monotonic() - t0, 2)},
                    )
                    return result
                except Exception as e:
                    lf.update_current_span(output={"error": str(e)})
                    raise

        LLMClient.generate = _traced
        lf.flush()
        return True

    except Exception as e:
        return False
