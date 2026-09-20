#!/usr/bin/env python3
"""Ultra-minimal eval test - single case with verbose output."""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from nl2sql.core.config import Config
from nl2sql.core.engine import AgenticLoop
from nl2sql.core.sandbox import DockerSandbox
import sqlite3


async def test_one():
    datasets_dir = Path(__file__).parent / "datasets"
    db_path = datasets_dir / "ecommerce.db"
    
    question = "How many customers are there?"
    
    # Get schema
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    schema = "\n\n".join(row[0] for row in cursor.fetchall())
    conn.close()
    
    config = Config.load()
    config.max_total_steps = 1
    config.max_finetuned_steps = 1
    
    print(f"Question: {question}")
    print(f"Config: max_steps={config.max_total_steps}, finetuned_steps={config.max_finetuned_steps}")
    print()
    
    sandbox = DockerSandbox(str(db_path), config.docker_image)
    loop = AgenticLoop(config, sandbox)
    
    start = time.time()
    judge_verdict = None
    
    async for event in loop.run(question, schema):
        elapsed = time.time() - start
        print(f"[{elapsed:.1f}s] Step {event.step} - {event.phase}")
        
        if event.sql:
            print(f"  SQL: {event.sql}")
        if event.judge_verdict:
            judge_verdict = event.judge_verdict
            print(f"  Judge: {judge_verdict.get('correct')} - {judge_verdict.get('reason', '')[:60]}")
        if event.error:
            print(f"  Error: {event.error[:80]}")
        if event.is_final:
            print(f"  Final: success={event.success}")
            break
    
    total_time = time.time() - start
    print(f"\nTotal time: {total_time:.1f}s")
    print(f"Judge verdict: {judge_verdict}")


if __name__ == "__main__":
    asyncio.run(test_one())
