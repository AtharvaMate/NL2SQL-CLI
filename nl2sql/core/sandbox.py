from __future__ import annotations

import asyncio
import shutil
import sqlite3
import tempfile
from pathlib import Path


class DockerSandbox:
    def __init__(self, db_path: str, docker_image: str = "python:3.12-slim") -> None:
        self.db_path = Path(db_path)
        self.docker_image = docker_image
        self._container_id: str | None = None
        self._temp_dir: tempfile.TemporaryDirectory | None = None

    async def start(self) -> None:
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database file not found: {self.db_path}")

        self._temp_dir = tempfile.TemporaryDirectory()
        temp_db = Path(self._temp_dir.name) / self.db_path.name
        shutil.copy2(str(self.db_path), str(temp_db))

        cmd = [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            f"nl2sql-sandbox-{id(self)}",
            "-v",
            f"{self._temp_dir.name}:/data",
            "-w",
            "/data",
            self.docker_image,
            "sleep",
            "3600",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"Failed to start Docker container: {stderr.decode()}")

        self._container_id = stdout.decode().strip()

    async def execute_sql(self, sql: str) -> dict:
        if self._container_id is None:
            return await self._execute_local(sql)

        import base64
        sql_b64 = base64.b64encode(sql.encode()).decode()
        py_script = (
            "import sqlite3, json, base64\n"
            "try:\n"
            f"    sql = base64.b64decode('{sql_b64}').decode()\n"
            f"    conn = sqlite3.connect('/data/{self.db_path.name}')\n"
            "    cur = conn.cursor()\n"
            "    cur.execute(sql)\n"
            "    cols = [d[0] for d in cur.description] if cur.description else []\n"
            "    rows = [list(r) for r in cur.fetchall()] if cur.description else []\n"
            "    conn.close()\n"
            "    print(json.dumps({'columns': cols, 'rows': rows, 'row_count': len(rows)}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'columns': [], 'rows': [], 'row_count': 0, 'error': str(e)}))\n"
        )

        cmd = [
            "docker",
            "exec",
            self._container_id,
            "python3",
            "-c",
            py_script,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            return {
                "columns": [],
                "rows": [],
                "row_count": 0,
                "error": stderr.decode().strip(),
            }

        import json

        try:
            return json.loads(stdout.decode())
        except json.JSONDecodeError:
            return {
                "columns": [],
                "rows": [],
                "row_count": 0,
                "error": stdout.decode().strip(),
            }

    async def _execute_local(self, sql: str) -> dict:
        try:
            conn = sqlite3.connect(str(self.db_path))
            cur = conn.cursor()
            cur.execute(sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchall()] if cur.description else []
            conn.close()
            return {"columns": columns, "rows": rows, "row_count": len(rows)}
        except sqlite3.Error as e:
            return {
                "columns": [],
                "rows": [],
                "row_count": 0,
                "error": str(e),
            }

    async def stop(self) -> None:
        if self._container_id:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "stop",
                self._container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            self._container_id = None

        if self._temp_dir:
            self._temp_dir.cleanup()
            self._temp_dir = None

    @property
    def is_docker_available(self) -> bool:
        import subprocess

        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    @property
    def is_running(self) -> bool:
        return self._container_id is not None
