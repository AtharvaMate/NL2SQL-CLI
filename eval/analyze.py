#!/usr/bin/env python3
"""Analyze and compare evaluation results."""
import csv
import sys
from pathlib import Path
from collections import defaultdict


def load_results(csv_path: Path) -> list[dict]:
    """Load results from CSV."""
    results = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Convert numeric fields
            row["correct"] = row["correct"].lower() == "true"
            row["attempts"] = int(row["attempts"]) if row["attempts"] else 0
            row["finetuned_calls"] = int(row["finetuned_calls"]) if row["finetuned_calls"] else 0
            row["superior_calls"] = int(row["superior_calls"]) if row["superior_calls"] else 0
            row["judge_calls"] = int(row["judge_calls"]) if row["judge_calls"] else 0
            row["time_sec"] = float(row["time_sec"]) if row["time_sec"] else 0
            row["cost_usd"] = float(row["cost_usd"]) if row["cost_usd"] else 0
            results.append(row)
    return results


def compute_stats(results: list[dict]) -> dict:
    """Compute summary statistics."""
    total = len(results)
    correct = sum(1 for r in results if r["correct"])
    
    return {
        "total": total,
        "correct": correct,
        "accuracy": correct / total * 100 if total > 0 else 0,
        "avg_attempts": sum(r["attempts"] for r in results) / total if total > 0 else 0,
        "avg_time": sum(r["time_sec"] for r in results) / total if total > 0 else 0,
        "total_finetuned_calls": sum(r["finetuned_calls"] for r in results),
        "total_superior_calls": sum(r["superior_calls"] for r in results),
        "total_judge_calls": sum(r["judge_calls"] for r in results),
        "total_cost": sum(r["cost_usd"] for r in results),
    }


def find_corrections(finetuned_results: list[dict], agentic_results: list[dict]) -> list[dict]:
    """Find cases where agentic succeeded but finetuned failed."""
    corrections = []
    
    # Build lookup by test_id
    finetuned_by_id = {r["test_id"]: r for r in finetuned_results}
    
    for agentic_case in agentic_results:
        test_id = agentic_case["test_id"]
        if test_id in finetuned_by_id:
            finetuned_case = finetuned_by_id[test_id]
            if not finetuned_case["correct"] and agentic_case["correct"]:
                corrections.append({
                    "test_id": test_id,
                    "question": agentic_case["question"],
                    "finetuned_sql": finetuned_case["final_sql"],
                    "agentic_sql": agentic_case["final_sql"],
                    "attempts": agentic_case["attempts"],
                })
    
    return corrections


def print_comparison_table(stats_by_mode: dict):
    """Print comparison table."""
    modes = ["finetuned", "superior", "agentic"]
    
    print("\n" + "="*80)
    print("EVALUATION COMPARISON")
    print("="*80)
    print(f"\n{'Metric':<25} {'Finetuned':<15} {'Superior':<15} {'Agentic':<15}")
    print("-"*80)
    
    for mode in modes:
        if mode not in stats_by_mode:
            continue
    
    if all(m in stats_by_mode for m in modes):
        ft = stats_by_mode["finetuned"]
        sup = stats_by_mode["superior"]
        ag = stats_by_mode["agentic"]
        
        print(f"{'Accuracy':<25} {ft['accuracy']:>6.1f}%        {sup['accuracy']:>6.1f}%        {ag['accuracy']:>6.1f}%")
        print(f"{'Correct / Total':<25} {ft['correct']:>3}/{ft['total']:<10} {sup['correct']:>3}/{sup['total']:<10} {ag['correct']:>3}/{ag['total']:<10}")
        print(f"{'Avg Attempts':<25} {ft['avg_attempts']:>6.1f}         {sup['avg_attempts']:>6.1f}         {ag['avg_attempts']:>6.1f}")
        print(f"{'Avg Time (sec)':<25} {ft['avg_time']:>6.1f}         {sup['avg_time']:>6.1f}         {ag['avg_time']:>6.1f}")
        print(f"{'Total Finetuned Calls':<25} {ft['total_finetuned_calls']:>6}         {sup['total_finetuned_calls']:>6}         {ag['total_finetuned_calls']:>6}")
        print(f"{'Total Superior Calls':<25} {ft['total_superior_calls']:>6}         {sup['total_superior_calls']:>6}         {ag['total_superior_calls']:>6}")
        print(f"{'Total Judge Calls':<25} {ft['total_judge_calls']:>6}         {sup['total_judge_calls']:>6}         {ag['total_judge_calls']:>6}")
        print(f"{'Total Cost (USD)':<25} ${ft['total_cost']:>6.4f}       ${sup['total_cost']:>6.4f}       ${ag['total_cost']:>6.4f}")
        
        # Calculate improvements
        ft_to_ag_accuracy = ag['accuracy'] - ft['accuracy']
        ft_to_ag_cost_increase = (ag['total_cost'] / ft['total_cost'] - 1) * 100 if ft['total_cost'] > 0 else 0
        
        print("\n" + "-"*80)
        print("KEY FINDINGS:")
        print(f"  • Agentic improved accuracy by {ft_to_ag_accuracy:+.1f} percentage points over finetuned")
        print(f"  • Agentic used {ag['avg_attempts']:.1f}x attempts on average")
        print(f"  • Cost increase: {ft_to_ag_cost_increase:+.1f}% for {ft_to_ag_accuracy:+.1f}% accuracy gain")
    
    print("="*80 + "\n")


def print_correction_examples(corrections: list[dict], max_examples: int = 3):
    """Print examples of self-correction."""
    if not corrections:
        print("No correction examples found.\n")
        return
    
    print("\n" + "="*80)
    print(f"SELF-CORRECTION EXAMPLES ({len(corrections)} total corrections)")
    print("="*80 + "\n")
    
    for i, corr in enumerate(corrections[:max_examples], 1):
        print(f"Example {i}: Test {corr['test_id']}")
        print(f"Question: {corr['question']}")
        print(f"Finetuned SQL (FAILED):\n  {corr['finetuned_sql']}")
        print(f"Agentic SQL (SUCCESS after {corr['attempts']} attempts):\n  {corr['agentic_sql']}")
        print()


def main():
    results_dir = Path(__file__).parent / "results"
    
    # Find latest results for each mode
    files_by_mode = defaultdict(list)
    for csv_file in results_dir.glob("*.csv"):
        mode = csv_file.stem.rsplit("_", 1)[0]
        files_by_mode[mode].append(csv_file)
    
    # Use latest file for each mode
    stats_by_mode = {}
    results_by_mode = {}
    
    for mode in ["finetuned", "superior", "agentic"]:
        if mode in files_by_mode:
            latest = sorted(files_by_mode[mode])[-1]
            print(f"Loading {mode}: {latest.name}")
            results = load_results(latest)
            results_by_mode[mode] = results
            stats_by_mode[mode] = compute_stats(results)
    
    if not stats_by_mode:
        print("No result files found in eval/results/")
        print("Run: python eval/run_eval.py --mode [finetuned|superior|agentic]")
        sys.exit(1)
    
    # Print comparison
    print_comparison_table(stats_by_mode)
    
    # Find correction examples
    if "finetuned" in results_by_mode and "agentic" in results_by_mode:
        corrections = find_corrections(
            results_by_mode["finetuned"],
            results_by_mode["agentic"]
        )
        print_correction_examples(corrections)


if __name__ == "__main__":
    main()
