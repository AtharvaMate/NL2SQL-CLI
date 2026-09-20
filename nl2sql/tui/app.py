from __future__ import annotations

import asyncio
import csv
import json
import uuid
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, Static, Input, RichLog
from rich.syntax import Syntax
from rich.text import Text
from rich.table import Table
from rich.panel import Panel

from nl2sql.core.config import Config
from nl2sql.core.engine import AgenticLoop
from nl2sql.core.sandbox import DockerSandbox
from nl2sql.core.session import SessionStore, SessionEntry
from nl2sql.queue.worker import (
    get_redis,
    _cache_hash,
    CACHE_KEY,
    CACHE_TTL,
)


BRAND_MARK = """\
[bold bright_cyan]◆[/bold bright_cyan] [bold white]Co[/bold white][bold bright_magenta]Code[/bold bright_magenta] [dim]NL2SQL[/dim]"""


class NL2SQLApp(App):
    TITLE = "CoCode NL2SQL"
    SUB_TITLE = "Talk to your database"

    CSS = """\
    Screen { background: $surface; }

    #brand-bar {
        dock: top;
        height: 3;
        padding: 1 2;
        background: #1a1a2e;
    }

    #log {
        height: 1fr;
        margin: 0 0;
    }

    #bottom-panel {
        dock: bottom;
        height: auto;
        background: $surface;
    }

    #config-strip {
        height: 3;
        padding: 0 1;
        background: #1a1a2e;
    }
    #config-strip .cfg-box {
        width: 1fr;
        height: 3;
        padding: 0 1;
    }
    .cfg-icon {
        width: 4;
        padding: 1 0 0 0;
        color: #bb86fc;
    }
    .cfg-input {
        width: 1fr;
    }
    #config-divider {
        width: 3;
        padding: 1 1 0 1;
        color: #444466;
    }
    #config-led {
        width: auto;
        min-width: 24;
        padding: 1 1 0 0;
        color: $success;
    }

    #prompt-bar {
        height: 3;
        background: #12121e;
        padding: 0 1;
    }
    #prompt-icon {
        width: 4;
        padding: 1 0 0 0;
        color: #00e5ff;
    }
    #query-input {
        width: 1fr;
    }

    #status-bar {
        height: 1;
        background: #0d0d1a;
        color: #666688;
        padding: 0 2;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit_app", "Quit", show=True),
        Binding("ctrl+n", "new_session", "New", show=True),
        Binding("ctrl+e", "export_results", "Export", show=True),
        Binding("f1", "show_help", "Help", show=True),
    ]

    def __init__(self, config: Config | None = None) -> None:
        super().__init__()
        self.config = config or Config.load()
        self.sandbox: DockerSandbox | None = None
        self.engine: AgenticLoop | None = None
        self.session_store: SessionStore | None = None
        self.current_result: dict | None = None
        self.current_sql: str = ""
        self.schema_text: str = ""
        self._redis = None  # set on mount if Redis is available
        self._session_id: str = str(uuid.uuid4())

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(BRAND_MARK, id="brand-bar", markup=True)
        yield RichLog(id="log", highlight=True, markup=True, wrap=True, auto_scroll=True)

        with Vertical(id="bottom-panel"):
            with Horizontal(id="config-strip"):
                with Horizontal(classes="cfg-box"):
                    yield Static("◇", classes="cfg-icon")
                    yield Input(
                        value=self.config.db_path,
                        placeholder="database path",
                        id="db-input",
                        classes="cfg-input",
                    )
                yield Static("│", id="config-divider")
                with Horizontal(classes="cfg-box"):
                    yield Static("◈", classes="cfg-icon")
                    yield Input(
                        value=self.config.schema_path,
                        placeholder="schema path",
                        id="schema-input",
                        classes="cfg-input",
                    )
                yield Static("", id="config-led")

            with Horizontal(id="prompt-bar"):
                yield Static("›", id="prompt-icon")
                yield Input(placeholder="Ask a question in natural language…", id="query-input")

            yield Static("", id="status-bar")

        yield Footer()

    @property
    def log_widget(self) -> RichLog:
        return self.query_one("#log", RichLog)

    def on_mount(self) -> None:
        self.session_store = SessionStore(self.config.session_db_path)
        self._try_connect_redis()

        if self.config.is_configured:
            self._boot()
            self._set_config_status(True)
        else:
            self._set_config_status(False)
            self._log_system("Set DB and Schema paths, then press Enter.")

    def _try_connect_redis(self) -> None:
        """Connect to Redis for caching only."""
        try:
            r = get_redis(self.config.redis_url)
            r.ping()
            self._redis = r
            self._log_system(f"Redis cache connected · session {self._session_id[:8]}")
        except Exception:
            self._log_system("Redis unavailable — cache disabled, running locally")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("db-input", "schema-input"):
            self._apply_config()
            return
        question = event.value.strip()
        if not question:
            return
        event.input.value = ""
        if not self.engine:
            self._log_error("Not configured — set DB and Schema paths first.")
            return
        if not self.schema_text:
            self._log_error("No schema loaded — check schema path.")
            return
        asyncio.create_task(self._run_query(question))

    def _apply_config(self) -> None:
        db_path = self.query_one("#db-input", Input).value.strip()
        schema_path = self.query_one("#schema-input", Input).value.strip()

        errors = []
        if db_path and not Path(db_path).exists():
            errors.append(f"DB not found: {db_path}")
        if schema_path and not Path(schema_path).exists():
            errors.append(f"Schema not found: {schema_path}")

        if errors:
            self._set_config_status(False, " · ".join(errors))
            return

        changed = (db_path != self.config.db_path) or (schema_path != self.config.schema_path)
        self.config.db_path = str(Path(db_path).resolve()) if db_path else ""
        self.config.schema_path = str(Path(schema_path).resolve()) if schema_path else ""

        if self.config.is_configured:
            self._persist_paths()
            self._set_config_status(True)
            if changed:
                self._boot()
                self._log_system(f"Config updated — {Path(db_path).name} + {Path(schema_path).name}")
        else:
            self._set_config_status(False, "Both paths required")

    def _persist_paths(self) -> None:
        from dotenv import set_key
        env_file = self.config.project_root / ".env"
        env_file.touch(exist_ok=True)
        set_key(str(env_file), "DB_PATH", self.config.db_path)
        set_key(str(env_file), "SCHEMA_PATH", self.config.schema_path)

    def _set_config_status(self, ok: bool, msg: str = "") -> None:
        widget = self.query_one("#config-led", Static)
        if ok:
            db = Path(self.config.db_path).name
            sc = Path(self.config.schema_path).name
            widget.update(f"[green]●[/green] [dim]{db} + {sc}[/dim]")
        else:
            widget.update(f"[red]●[/red] [dim]{msg or 'Not configured'}[/dim]")

    def _boot(self) -> None:
        schema_path = Path(self.config.schema_path)
        self.schema_text = schema_path.read_text(encoding="utf-8") if schema_path.exists() else ""

        self.sandbox = DockerSandbox(
            db_path=self.config.db_path,
            docker_image=self.config.docker_image,
        )
        self.engine = AgenticLoop(self.config, self.sandbox)

        from nl2sql.core.tracing import enable as enable_tracing
        tracing = enable_tracing()

        docker = "docker" if self.sandbox.is_docker_available else "local"
        cache = " · cache" if self._redis else ""
        trace = " · tracing" if tracing else ""
        self._status(f"◆ {Path(self.config.db_path).name} · {docker}{cache}{trace}")
        asyncio.create_task(self._prewarm())

    async def _prewarm(self) -> None:
        self._log_system("Waking HF endpoint…")
        try:
            ok = await self.engine.prewarm_hf()
            msg = "HF endpoint ready." if ok else "HF endpoint sleeping — first query may take ~30s."
        except Exception:
            msg = "HF endpoint unreachable — will retry on first query."
        self._log_system(msg)

    async def _run_query(self, question: str) -> None:
        self._log_user(question)
        self._status("Processing…")
        # Cache check is now handled inside the graph (parallel cache_lookup node).
        await self._run_query_local(question)

    async def _run_query_local(self, question: str) -> None:
        """Run the agentic loop locally (no Redis workers)."""
        log = self.log_widget

        if self.sandbox and not self.sandbox.is_running and self.sandbox.is_docker_available:
            self._log_system("Starting Docker sandbox…")
            try:
                await self.sandbox.start()
                self._log_system("Docker sandbox ready.")
            except Exception as e:
                self._log_system(f"Docker unavailable, local mode: {e}")

        async for ev in self.engine.run(question, self.schema_text):
            if ev.phase == "cache_hit":
                log = self.log_widget
                self._log_system("Cache hit — returning cached result")
                self.current_sql = ev.sql
                self.current_result = ev.result
                if ev.sql:
                    syntax = Syntax(ev.sql, "sql", theme="monokai", line_numbers=False, padding=1, word_wrap=True)
                    log.write(Panel(syntax, title="[bold]SQL[/bold]", border_style="bright_cyan", expand=True, subtitle="[dim]cached[/dim]"))
                if ev.result:
                    self._log_result_table(ev.result)
                self._status("✓ Cache hit")
                self._save_session(question)
                return

            elif ev.phase == "schema_analyzed":
                self._log_system(f"Schema: {ev.sql}")  # ev.sql holds "Relevant tables: ..."

            elif ev.phase == "generating":
                t = Text()
                t.append(f"\n ◆ [{ev.step}/{ev.total}] ", style="bold bright_magenta")
                t.append(f"Generating SQL… ({ev.model_type})", style="bright_magenta")
                log.write(t)
                self._status(f"Step {ev.step}/{ev.total} — generating…")

            elif ev.phase == "syntax_error":
                self._log_warn(f"Syntax/schema error: {ev.error} — escalating…")

            elif ev.phase == "executing":
                syntax = Syntax(ev.sql, "sql", theme="monokai", line_numbers=False, padding=1, word_wrap=True)
                log.write(Panel(syntax, title="[bold]SQL[/bold]", border_style="bright_cyan", expand=True, subtitle="[dim]executed[/dim]"))
                self._status(f"Step {ev.step}/{ev.total} — executing…")

            elif ev.phase == "execution_error":
                self._log_error(f"Exec error: {ev.error}")

            elif ev.phase == "judging":
                self._status(f"Step {ev.step}/{ev.total} — judging…")

            elif ev.phase == "judge_rejected":
                reason = (ev.judge_verdict or {}).get("reason", "Unknown")
                suggestion = (ev.judge_verdict or {}).get("suggestion", "")
                self._log_judge(False, reason, suggestion)

            elif ev.phase == "success" and ev.is_final:
                reason = (ev.judge_verdict or {}).get("reason", "Correct")
                self._log_judge(True, reason)
                self._log_result_table(ev.result)
                self.current_result = ev.result
                self.current_sql = ev.sql
                self._status("✓ Query successful")

            elif ev.phase == "optimized":
                self._log_system(f"SQL optimized by PerformanceOptimizer")
                syntax = Syntax(ev.sql, "sql", theme="monokai", line_numbers=False, padding=1, word_wrap=True)
                log.write(Panel(syntax, title="[bold]SQL[/bold] [dim](optimized)[/dim]", border_style="bright_green", expand=True))
                self.current_sql = ev.sql

            elif ev.phase == "privacy_redacted":
                self._log_warn(f"PrivacyGuard: {ev.error}")

            elif ev.phase == "exhausted":
                if ev.best_attempt:
                    self._log_warn("All attempts exhausted — best attempt:")
                    syntax = Syntax(ev.best_attempt.sql, "sql", theme="monokai", line_numbers=False, padding=1, word_wrap=True)
                    log.write(Panel(syntax, title="[bold]SQL[/bold] [dim](best effort)[/dim]", border_style="yellow", expand=True))
                    if ev.best_attempt.result:
                        self._log_result_table(ev.best_attempt.result)
                    self.current_sql = ev.best_attempt.sql
                    self.current_result = ev.best_attempt.result
                else:
                    self._log_error("All attempts failed — try rephrasing.")
                self._status("Exhausted — best attempt shown")

            elif ev.phase == "error":
                self._log_error(f"Error: {ev.error}")

        # ── cache successful result ───────────────────────────────────────────
        if self._redis and self.current_result and "error" not in self.current_result:
            cache_key = CACHE_KEY.format(hash=_cache_hash(question, self.schema_text))
            payload = json.dumps({"sql": self.current_sql, "result": self.current_result})
            self._redis.setex(cache_key, CACHE_TTL, payload)

        self._save_session(question)

    def _save_session(self, question: str) -> None:
        if self.session_store:
            entry = SessionEntry(
                id=None,
                question=question,
                schema_text=self.schema_text,
                final_sql=self.current_sql,
                success=bool(self.current_result and "error" not in self.current_result),
            )
            self.session_store.save(entry)

    # ── log formatters ─────────────────────────────────────────────────────────

    def _log_user(self, q: str) -> None:
        t = Text()
        t.append("\n › ", style="bold bright_cyan")
        t.append(q, style="bold white")
        self.log_widget.write(t)

    def _log_system(self, msg: str) -> None:
        t = Text()
        t.append("   ∙ ", style="dim bright_magenta")
        t.append(msg, style="dim")
        self.log_widget.write(t)

    def _log_error(self, msg: str) -> None:
        t = Text()
        t.append(" ✗ ERROR ", style="bold white on #c62828")
        t.append(f" {msg}", style="#ef5350")
        self.log_widget.write(t)

    def _log_warn(self, msg: str) -> None:
        t = Text()
        t.append(" ▲ WARN ", style="bold #1a1a2e on #ffb300")
        t.append(f" {msg}", style="#ffb300")
        self.log_widget.write(t)

    def _log_judge(self, passed: bool, reason: str, suggestion: str = "") -> None:
        t = Text()
        if passed:
            t.append(" ✓ PASS ", style="bold white on #2e7d32")
            t.append(f" {reason}", style="#66bb6a")
        else:
            t.append(" ✗ FAIL ", style="bold white on #c62828")
            t.append(f" {reason}", style="#ef5350")
        self.log_widget.write(t)
        if suggestion:
            h = Text()
            h.append("   → ", style="dim #ff9800")
            h.append(suggestion, style="#ff9800")
            self.log_widget.write(h)

    def _log_result_table(self, result: dict | None) -> None:
        if not result:
            return
        columns = result.get("columns", [])
        rows = result.get("rows", [])
        if not columns:
            self.log_widget.write(Text("   No columns returned.", style="dim"))
            return

        table = Table(
            title=f"[bold]Results[/bold] [dim]({result.get('row_count', len(rows))} rows)[/dim]",
            border_style="#4a4a6a",
            header_style="bold bright_cyan",
            title_style="bright_cyan",
            show_lines=False,
            show_edge=True,
            expand=True,
            padding=(0, 1),
        )
        for col in columns:
            table.add_column(col, style="white")
        # ponytail: 50 rows max, add pagination when someone actually hits this
        for i, row in enumerate(rows[:50]):
            style = "on #1a1a2e" if i % 2 == 0 else ""
            table.add_row(*[str(v) for v in row], style=style)
        if len(rows) > 50:
            table.add_row(*["…" for _ in columns], style="dim")
        self.log_widget.write(table)

    def _status(self, text: str) -> None:
        try:
            self.query_one("#status-bar", Static).update(f" {text}")
        except Exception:
            pass

    # ── actions ───────────────────────────────────────────────────────────────

    def action_quit_app(self) -> None:
        self.exit()

    def action_new_session(self) -> None:
        self.log_widget.clear()
        self.current_result = None
        self.current_sql = ""
        self._session_id = str(uuid.uuid4())
        self._status("New session")

    def action_export_results(self) -> None:
        if not self.current_result:
            self._status("No results to export")
            return
        export_path = self.config.project_root / ".nl2sql" / "export.csv"
        export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(export_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if self.current_result.get("columns"):
                writer.writerow(self.current_result["columns"])
            for row in self.current_result.get("rows", []):
                writer.writerow(row)
        self._status(f"Exported → {export_path}")

    @work
    async def action_show_help(self) -> None:
        from nl2sql.tui.dialogs.help_dialog import HelpDialog
        await self.push_screen_wait(HelpDialog())

    async def on_unmount(self) -> None:
        if self.sandbox and self.sandbox.is_running:
            await self.sandbox.stop()
