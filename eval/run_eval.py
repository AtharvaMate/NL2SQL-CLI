#!/usr/bin/env python3
"""
Run evaluation suite on NL2SQL system using the Spider benchmark dataset.

Spider dev.json is the source of truth — 1034 questions across 20 databases.
Scoring: execution accuracy (primary) + judge fallback.
Spider-format schema option matches the training distribution of the finetuned model.

Usage:
    python eval/run_eval.py                          # agentic, all 1034
    python eval/run_eval.py --mode finetuned         # finetuned only
    python eval/run_eval.py --limit 100              # first 100 entries
    python eval/run_eval.py --schema-format spider   # Spider compact schema
    python eval/run_eval.py --split test             # use test.json instead
"""
import argparse
import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from nl2sql.core.config import Config
from nl2sql.core.engine import AgenticLoop
from nl2sql.core.sandbox import DockerSandbox


# ── Spider data loading ───────────────────────────────────────────────────────

def _find_db(spider_root: Path, db_id: str) -> Path:
    """Locate the .sqlite file for a given Spider db_id."""
    for subdir in ("database", Path("spider_data") / "test_database"):
        p = spider_root / subdir / db_id / f"{db_id}.sqlite"
        if p.exists():
            return p
        p2 = spider_root / subdir / db_id / f"{db_id}.db"
        if p2.exists():
            return p2
    raise FileNotFoundError(f"No .sqlite found for db_id={db_id} under {spider_root}")


def load_spider(spider_root: Path, split: str = "dev", limit: int | None = None) -> list[dict]:
    """
    Load Spider entries from dev.json / test.json.
    Returns list of dicts with keys: id, question, expected_sql, db_id, db_path.
    """
    json_file = spider_root / "spider_data" / f"{split}.json"
    if not json_file.exists():
        raise FileNotFoundError(f"Spider split not found: {json_file}")

    raw = json.loads(json_file.read_text(encoding="utf-8"))
    if limit:
        raw = raw[:limit]

    entries = []
    for i, item in enumerate(raw):
        db_id = item["db_id"]
        try:
            db_path = _find_db(spider_root, db_id)
        except FileNotFoundError:
            print(f"  [SKIP] {db_id} — .sqlite not found", file=sys.stderr)
            continue
        entries.append({
            "id": str(i + 1),
            "question": item["question"],
            "expected_sql": item["query"],
            "db_id": db_id,
            "db_path": db_path,
        })
    return entries


# ── Schema extraction ─────────────────────────────────────────────────────────

def get_schema_ddl(db_path: Path) -> str:
    """Raw DDL from sqlite_master — all CREATE TABLE statements."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
            " AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return "\n\n".join(r[0] for r in rows)
    finally:
        conn.close()


def get_schema_spider_format(db_path: Path) -> str:
    """
    Compact Spider training format:
      Table: table_name, columns: [col1 (type), col2 (type), ...]
    Matches the format the finetuned model was trained on.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        parts = []
        for (table,) in tables:
            cols = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
            col_strs = [f"{c[1]} ({c[2]})" for c in cols]
            parts.append(f"Table: {table}, columns: [{', '.join(col_strs)}]")
        return "\n".join(parts)
    finally:
        conn.close()


# ── Execution accuracy ────────────────────────────────────────────────────────

def execute_sql(db_path: Path, sql: str) -> list[tuple] | None:
    """Execute SQL and return sorted rows, or None on error."""
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(sql).fetchall()
        conn.close()
        # normalise: sort rows, stringify values for order-agnostic comparison
        return sorted(tuple(str(v) for v in row) for row in rows)
    except Exception:
        return None


def execution_match(db_path: Path, expected_sql: str, generated_sql: str) -> bool:
    """True if both SQLs produce identical result sets (order-agnostic)."""
    expected = execute_sql(db_path, expected_sql)
    generated = execute_sql(db_path, generated_sql)
    if expected is None or generated is None:
        return False
    return expected == generated


# ── Single test runner ────────────────────────────────────────────────────────

async def run_single_test(
    entry: dict,
    config: Config,
    schema_format: str,
) -> dict:
    db_path: Path = entry["db_path"]

    schema = (
        get_schema_spider_format(db_path)
        if schema_format == "spider"
        else get_schema_ddl(db_path)
    )

    sandbox = DockerSandbox(str(db_path), config.docker_image)
    loop = AgenticLoop(config, sandbox)

    start = time.time()
    final_sql = ""
    judge_verdict = None
    attempts = 0
    finetuned_calls = superior_calls = judge_calls = 0

    initial_sup_in  = loop.superior_client.total_input_tokens
    initial_sup_out = loop.superior_client.total_output_tokens

    async for ev in loop.run(entry["question"], schema):
        attempts = ev.step

        if ev.phase == "generating":
            if ev.model_type == "finetuned":
                finetuned_calls += 1
            else:
                superior_calls += 1
        if ev.phase == "judging":
            judge_calls += 1
        if ev.sql:
            final_sql = ev.sql
        if ev.judge_verdict:
            judge_verdict = ev.judge_verdict
        if ev.is_final:
            if ev.best_attempt:
                if ev.best_attempt.judge_verdict:
                    judge_verdict = ev.best_attempt.judge_verdict
                if ev.best_attempt.sql:
                    final_sql = ev.best_attempt.sql

    elapsed = time.time() - start

    # Cost: superior model only (finetuned HF Space is free, Groq judge is free)
    sup_in  = loop.superior_client.total_input_tokens  - initial_sup_in
    sup_out = loop.superior_client.total_output_tokens - initial_sup_out
    cost = (sup_in * 15.0 + sup_out * 75.0) / 1_000_000

    # ── Scoring: execution accuracy first, judge as fallback ──────────────────
    exec_correct = execution_match(db_path, entry["expected_sql"], final_sql) if final_sql else False

    if exec_correct:
        correct = True
        scoring = "exec"
    elif judge_verdict:
        correct = judge_verdict.get("correct", False)
        scoring = "judge"
    else:
        correct = False
        scoring = "none"

    return {
        "id":              entry["id"],
        "db_id":           entry["db_id"],
        "question":        entry["question"],
        "expected_sql":    entry["expected_sql"],
        "final_sql":       final_sql,
        "correct":         correct,
        "scoring":         scoring,
        "attempts":        attempts,
        "finetuned_calls": finetuned_calls,
        "superior_calls":  superior_calls,
        "judge_calls":     judge_calls,
        "time_sec":        round(elapsed, 2),
        "cost_usd":        round(cost, 6),
    }


# ── Eval runner ───────────────────────────────────────────────────────────────

async def run_eval(
    entries: list[dict],
    config: Config,
    schema_format: str,
    output_path: Path,
    concurrency: int,
) -> None:
    import csv

    results: list[dict | None] = [None] * len(entries)
    sem = asyncio.Semaphore(concurrency)
    total = len(entries)

    async def run_one(i: int, entry: dict) -> None:
        async with sem:
            print(f"  [{i+1}/{total}] {entry['db_id']}: {entry['question'][:55]}...")
            try:
                r = await run_single_test(entry, config, schema_format)
                results[i] = r
                mark = "✓" if r["correct"] else "✗"
                print(f"    {mark} ({r['scoring']}, {r['attempts']} steps, {r['time_sec']}s)")
            except Exception as e:
                print(f"    ✗ CRASH: {e}")
                results[i] = {
                    "id": entry["id"], "db_id": entry["db_id"],
                    "question": entry["question"],
                    "expected_sql": entry["expected_sql"], "final_sql": "",
                    "correct": False, "scoring": "crash",
                    "attempts": 0, "finetuned_calls": 0, "superior_calls": 0,
                    "judge_calls": 0, "time_sec": 0, "cost_usd": 0,
                }

    await asyncio.gather(*[run_one(i, e) for i, e in enumerate(entries)])

    # Write CSV
    fields = [
        "id", "db_id", "question", "expected_sql", "final_sql",
        "correct", "scoring", "attempts", "finetuned_calls",
        "superior_calls", "judge_calls", "time_sec", "cost_usd",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)

    # Summary
    correct_n = sum(1 for r in results if r["correct"])
    exec_n    = sum(1 for r in results if r.get("scoring") == "exec")
    judge_n   = sum(1 for r in results if r.get("scoring") == "judge" and r["correct"])
    acc = correct_n / total * 100

    print(f"\n{'='*60}")
    print(f"Accuracy:       {correct_n}/{total}  ({acc:.1f}%)")
    print(f"  via exec:     {exec_n}")
    print(f"  via judge:    {judge_n}")
    print(f"Avg time:       {sum(r['time_sec'] for r in results)/total:.1f}s")
    print(f"Total cost:     ${sum(r['cost_usd'] for r in results):.4f}")
    print(f"Results saved:  {output_path}")
    print(f"{'='*60}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="NL2SQL eval on Spider benchmark")
    parser.add_argument("--mode", choices=["finetuned", "superior", "agentic"],
                        default="agentic")
    parser.add_argument("--split", choices=["dev", "test"], default="dev",
                        help="Spider split to evaluate on")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit number of entries (default: all)")
    parser.add_argument("--schema-format", choices=["ddl", "spider"], default="ddl",
                        help="ddl=raw CREATE TABLE (default), spider=compact training format")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    spider_root = Path(__file__).parent.parent / "spider_databases"
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)

    entries = load_spider(spider_root, split=args.split, limit=args.limit)
    print(f"Loaded {len(entries)} Spider {args.split} entries")

    config = Config.load()
    if args.mode == "finetuned":
        config.max_total_steps = 1
        config.max_finetuned_steps = 1
    elif args.mode == "superior":
        config.max_finetuned_steps = 0
        config.max_total_steps = 1

    date_str = datetime.now().strftime("%Y%m%d")
    suffix = f"_limit{args.limit}" if args.limit else ""
    sf = f"_{args.schema_format}" if args.schema_format == "spider" else ""
    output = Path(args.output) if args.output else \
        results_dir / f"{args.mode}_{args.split}{suffix}{sf}_{date_str}.csv"

    asyncio.run(run_eval(entries, config, args.schema_format, output, args.concurrency))


if __name__ == "__main__":
    main()
