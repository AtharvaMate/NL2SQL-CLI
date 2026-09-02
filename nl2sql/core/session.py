from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class QueryAttempt:
    step: int
    model_type: str
    sql: str
    result: dict
    judge_verdict: dict | None = None
    error: str | None = None


@dataclass
class SessionEntry:
    id: int | None
    question: str
    schema_text: str
    attempts: list[QueryAttempt] = field(default_factory=list)
    final_sql: str = ""
    success: bool = False
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class SessionStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                schema_text TEXT NOT NULL,
                attempts_json TEXT NOT NULL,
                final_sql TEXT,
                success INTEGER DEFAULT 0,
                timestamp TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

    def save(self, entry: SessionEntry) -> int:
        conn = sqlite3.connect(self.db_path)
        attempts_data = []
        for a in entry.attempts:
            attempts_data.append(
                {
                    "step": a.step,
                    "model_type": a.model_type,
                    "sql": a.sql,
                    "result": a.result,
                    "judge_verdict": a.judge_verdict,
                    "error": a.error,
                }
            )
        cur = conn.execute(
            """
            INSERT INTO sessions (question, schema_text, attempts_json, final_sql, success, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                entry.question,
                entry.schema_text,
                json.dumps(attempts_data),
                entry.final_sql,
                int(entry.success),
                entry.timestamp,
            ),
        )
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return row_id

    def list_sessions(self, limit: int = 50) -> list[SessionEntry]:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT id, question, schema_text, attempts_json, final_sql, success, timestamp "
            "FROM sessions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()

        entries = []
        for row in rows:
            attempts_data = json.loads(row[3])
            attempts = [
                QueryAttempt(
                    step=a["step"],
                    model_type=a["model_type"],
                    sql=a["sql"],
                    result=a["result"],
                    judge_verdict=a.get("judge_verdict"),
                    error=a.get("error"),
                )
                for a in attempts_data
            ]
            entries.append(
                SessionEntry(
                    id=row[0],
                    question=row[1],
                    schema_text=row[2],
                    attempts=attempts,
                    final_sql=row[4] or "",
                    success=bool(row[5]),
                    timestamp=row[6],
                )
            )
        return entries
