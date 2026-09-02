"""
RQ worker task + Redis pub/sub progress publishing + Langfuse tracing.

This module is imported by both:
  - The TUI (to enqueue jobs and subscribe to progress)
  - The RQ worker process (to execute run_query_job)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from typing import TYPE_CHECKING

import redis as _redis

if TYPE_CHECKING:
    from nl2sql.core.config import Config

QUEUE_NAME = "nl2sql"
RESULT_TTL = 3600        # 1 hour — matches cache TTL
CACHE_TTL = 3600         # ponytail: fixed 1hr, make configurable if stale results become an issue
PROGRESS_CHANNEL = "query:{job_id}:progress"
RESULT_KEY = "query:{job_id}:result"
CACHE_KEY = "cache:sql:{hash}"


# ── helpers ──────────────────────────────────────────────────────────────────

def get_redis(redis_url: str) -> _redis.Redis:
    return _redis.from_url(redis_url, decode_responses=True)


def _cache_hash(question: str, schema: str) -> str:
    """Semantic hash: normalize question, use only table/column names from schema."""
    q = re.sub(r"[^\w\s]", "", question.lower()).strip()
    # extract identifiers from schema (words after CREATE TABLE / column defs)
    identifiers = " ".join(re.findall(r"\b[a-zA-Z_]\w*\b", schema))
    return hashlib.sha256(f"{q}||{identifiers}".encode()).hexdigest()[:32]


def _publish(r: _redis.Redis, job_id: str, event: dict) -> None:
    r.publish(PROGRESS_CHANNEL.format(job_id=job_id), json.dumps(event))


# ── RQ task (runs inside worker container) ───────────────────────────────────

def run_query_job(job_id: str, question: str, schema: str, db_path: str) -> dict:
    """
    Executed by an RQ worker. Runs AgenticLoop, emits progress via pub/sub,
    caches and returns the final result. Traced with Langfuse.
    """
    from nl2sql.core.config import Config
    from nl2sql.core.engine import AgenticLoop
    from nl2sql.core.sandbox import DockerSandbox

    config = Config.load()
    r = get_redis(config.redis_url)

    # ── cache check ──────────────────────────────────────────────────────────
    cache_key = CACHE_KEY.format(hash=_cache_hash(question, schema))
    cached = r.get(cache_key)
    if cached:
        result = json.loads(cached)
        _publish(r, job_id, {"phase": "cache_hit", "step": 0, "total": 0})
        r.setex(RESULT_KEY.format(job_id=job_id), RESULT_TTL, cached)
        return result

    # ── Langfuse trace ───────────────────────────────────────────────────────
    trace = None
    try:
        from langfuse import Langfuse
        lf = Langfuse(
            public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
            secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
            host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
        trace = lf.trace(name="nl2sql-query", input={"question": question})
    except Exception:
        pass  # ponytail: langfuse is optional, don't crash if not configured

    # ── patch LLMClient to emit Langfuse spans ───────────────────────────────
    if trace:
        _instrument_llm(trace)

    # ── run agentic loop ─────────────────────────────────────────────────────
    sandbox = DockerSandbox(db_path=db_path, docker_image=config.docker_image)
    engine = AgenticLoop(config, sandbox)

    final_result: dict = {"error": "No result produced"}
    final_sql: str = ""

    async def _run() -> None:
        nonlocal final_result, final_sql
        async for ev in engine.run(question, schema):
            # publish every step event so TUI can display live progress
            event_dict = {
                "phase": ev.phase,
                "step": ev.step,
                "total": ev.total,
                "model_type": ev.model_type,
                "sql": ev.sql,
                "error": ev.error,
                "judge_verdict": ev.judge_verdict,
                "success": ev.success,
            }
            _publish(r, job_id, event_dict)

            if ev.phase == "success":
                final_result = ev.result or {}
                final_sql = ev.sql
            elif ev.phase == "exhausted" and ev.best_attempt:
                final_result = ev.best_attempt.result or {}
                final_sql = ev.best_attempt.sql

    asyncio.run(_run())

    # ── cache successful result ───────────────────────────────────────────────
    payload = {"sql": final_sql, "result": final_result}
    payload_json = json.dumps(payload)
    if "error" not in final_result:
        r.setex(cache_key, CACHE_TTL, payload_json)

    # store result for TUI to retrieve
    r.setex(RESULT_KEY.format(job_id=job_id), RESULT_TTL, payload_json)

    if trace:
        try:
            trace.update(output=payload)
            lf.flush()
        except Exception:
            pass

    return payload


def _instrument_llm(trace) -> None:
    """Monkey-patch LLMClient.generate to add Langfuse spans."""
    from nl2sql.core.llm import LLMClient

    original_generate = LLMClient.generate

    async def _traced_generate(self, messages, temperature=0.0, max_tokens=512):
        span = trace.span(name=f"llm-{self.model or 'hf'}", input={"messages": messages})
        try:
            result = await original_generate(self, messages, temperature, max_tokens)
            span.end(output={"content": result})
            return result
        except Exception as e:
            span.end(output={"error": str(e)})
            raise

    LLMClient.generate = _traced_generate
