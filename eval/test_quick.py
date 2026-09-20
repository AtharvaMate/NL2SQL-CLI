#!/usr/bin/env python3
"""Quick test of eval framework with just 3 cases."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from nl2sql.core.config import Config
from nl2sql.core.engine import AgenticLoop
from nl2sql.core.sandbox import DockerSandbox
import sqlite3


async def test_single_case():
    """Test a single simple case."""
    datasets_dir = Path(__file__).parent / "datasets"
    db_path = datasets_dir / "ecommerce.db"
    
    # Simple test: count customers
    question = "How many customers are there?"
    expected_sql = "SELECT COUNT(*) FROM customers"
    
    print(f"Testing: {question}")
    print(f"Expected SQL: {expected_sql}")
    
    # Get schema
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    schema = "\n\n".join(row[0] for row in cursor.fetchall())
    conn.close()
    
    print(f"\nSchema loaded ({len(schema)} chars)")
    
    # Create sandbox and loop
    config = Config.load()
    config.max_total_steps = 1
    config.max_finetuned_steps = 1
    
    print(f"HF Endpoint: {config.hf_endpoint}")
    print(f"OmniRoute URL: {config.omniroute_url}")
    
    sandbox = DockerSandbox(str(db_path), config.docker_image)
    loop = AgenticLoop(config, sandbox)
    
    print("\nRunning agentic loop...")
    
    try:
        async for event in loop.run(question, schema):
            print(f"  Step {event.step}: {event.phase}")
            if event.sql:
                print(f"    SQL: {event.sql[:80]}")
            if event.error:
                print(f"    Error: {event.error[:80]}")
            if event.is_final:
                print(f"    Final: success={event.success}")
                if event.best_attempt:
                    print(f"    Result rows: {event.best_attempt.result.get('row_count', 0)}")
                break
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
    
    print("\nTest complete!")


if __name__ == "__main__":
    asyncio.run(test_single_case())
