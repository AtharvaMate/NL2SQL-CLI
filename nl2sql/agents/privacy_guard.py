"""
PrivacyGuard agent.

Checks result columns against sensitive keyword patterns and redacts
matching column values in-place. Appends a warning to the privacy audit log.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from nl2sql.core.config import Config
from nl2sql.core.state import GraphState


def _sensitive_columns(columns: list[str], patterns: list[str]) -> list[str]:
    """Return column names that match any sensitive pattern (case-insensitive)."""
    found = []
    for col in columns:
        for pat in patterns:
            if re.search(pat, col, re.IGNORECASE):
                found.append(col)
                break
    return found


def _redact(result: dict, sensitive_cols: list[str]) -> dict:
    """Return a copy of result with sensitive column values replaced by ***REDACTED***."""
    if not sensitive_cols or not result:
        return result
    columns = result.get("columns", [])
    rows = result.get("rows", [])
    redact_indices = {i for i, col in enumerate(columns) if col in sensitive_cols}
    if not redact_indices:
        return result
    new_rows = [
        [("***REDACTED***" if i in redact_indices else v) for i, v in enumerate(row)]
        for row in rows
    ]
    return {**result, "rows": new_rows}


def _log_privacy_event(
    question: str, sql: str, sensitive_cols: list[str], log_path: Path
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "question": question,
        "sql": sql,
        "redacted_columns": sensitive_cols,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


async def run(state: GraphState, config: Config) -> dict:
    result = state.get("final_result") or {}
    columns = result.get("columns", [])

    sensitive = _sensitive_columns(columns, config.sensitive_column_patterns)
    if not sensitive:
        return {"sensitive_columns_found": []}

    redacted_result = _redact(result, sensitive)

    log_path = Path(config.project_root) / ".nl2sql" / "privacy_audit.log"
    _log_privacy_event(
        question=state["question"],
        sql=state["final_sql"],
        sensitive_cols=sensitive,
        log_path=log_path,
    )

    return {
        "sensitive_columns_found": sensitive,
        "final_result": redacted_result,
    }
