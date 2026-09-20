#!/usr/bin/env python3
"""
Quick mini evaluation on Spider — defaults to 50 entries, sequential.
Shares all logic with run_eval.py via direct import.

Usage:
    python eval/run_mini_eval.py                         # finetuned, 50 entries
    python eval/run_mini_eval.py --mode agentic          # full pipeline
    python eval/run_mini_eval.py --n 20                  # 20 entries
    python eval/run_mini_eval.py --schema-format spider  # Spider compact schema
"""
import argparse
import asyncio
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from nl2sql.core.config import Config
from eval.run_eval import load_spider, run_single_test


async def run_mini(
    entries: list[dict],
    config: Config,
    schema_format: str,
    output_path: Path,
) -> None:
    results = []
    total = len(entries)

    print(f"\n{'='*60}")
    print(f"Spider mini-eval  |  {total} entries  |  {datetime.now():%H:%M:%S}")
    print(f"{'='*60}\n")

    for i, entry in enumerate(entries, 1):
        print(f"[{i}/{total}] {entry['db_id']}: {entry['question'][:55]}...")
        try:
            r = await run_single_test(entry, config, schema_format)
            results.append(r)
            mark = "✓" if r["correct"] else "✗"
            print(f"  {mark} ({r['scoring']}, {r['attempts']} steps, {r['time_sec']}s, ${r['cost_usd']:.4f})")
        except Exception as e:
            print(f"  ✗ CRASH: {e}")
            results.append({
                "id": entry["id"], "db_id": entry["db_id"],
                "question": entry["question"],
                "expected_sql": entry["expected_sql"], "final_sql": "",
                "correct": False, "scoring": "crash",
                "attempts": 0, "finetuned_calls": 0, "superior_calls": 0,
                "judge_calls": 0, "time_sec": 0, "cost_usd": 0,
            })

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

    correct_n = sum(1 for r in results if r["correct"])
    exec_n    = sum(1 for r in results if r.get("scoring") == "exec")
    judge_n   = sum(1 for r in results if r.get("scoring") == "judge" and r["correct"])
    acc = correct_n / total * 100

    print(f"\n{'='*60}")
    print(f"Accuracy:     {correct_n}/{total}  ({acc:.1f}%)")
    print(f"  via exec:   {exec_n}")
    print(f"  via judge:  {judge_n}")
    print(f"Avg time:     {sum(r['time_sec'] for r in results)/total:.1f}s")
    print(f"Total cost:   ${sum(r['cost_usd'] for r in results):.4f}")
    print(f"Saved to:     {output_path}")
    print(f"{'='*60}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Spider mini-eval (sequential, fast feedback)")
    parser.add_argument("--mode", choices=["finetuned", "superior", "agentic"],
                        default="finetuned")
    parser.add_argument("--n", type=int, default=50,
                        help="Number of Spider dev entries to evaluate (default: 50)")
    parser.add_argument("--schema-format", choices=["ddl", "spider"], default="ddl",
                        help="ddl=raw DDL (default), spider=compact training format")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    spider_root = Path(__file__).parent.parent / "spider_databases"
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)

    entries = load_spider(spider_root, split="dev", limit=args.n)
    print(f"Loaded {len(entries)} Spider dev entries")

    config = Config.load()
    if args.mode == "finetuned":
        config.max_total_steps = 1
        config.max_finetuned_steps = 1
    elif args.mode == "superior":
        config.max_finetuned_steps = 0
        config.max_total_steps = 1

    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    sf = f"_{args.schema_format}" if args.schema_format == "spider" else ""
    output = Path(args.output) if args.output else \
        results_dir / f"mini_{args.mode}_n{args.n}{sf}_{date_str}.csv"

    asyncio.run(run_mini(entries, config, args.schema_format, output))


if __name__ == "__main__":
    main()
