from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click

from nl2sql.core.config import Config


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None:
        _launch_tui()


@main.command()
@click.option("--db", required=True, type=click.Path(exists=True), help="Path to SQLite database file")
@click.option("--schema", required=True, type=click.Path(exists=True), help="Path to schema .sql file")
@click.option("--hf-token", default="", help="HuggingFace API token")
def init(db: str, schema: str, hf_token: str) -> None:
    """Write DB_PATH and SCHEMA_PATH to .env (creates it if missing)."""
    from dotenv import set_key
    from nl2sql.core.config import _find_project_root
    env_file = _find_project_root() / ".env"
    env_file.touch(exist_ok=True)
    set_key(str(env_file), "DB_PATH", str(Path(db).resolve()))
    set_key(str(env_file), "SCHEMA_PATH", str(Path(schema).resolve()))
    if hf_token:
        set_key(str(env_file), "HF_TOKEN", hf_token)
    click.echo(f"Config written to {env_file}")


@main.command()
@click.argument("question")
@click.option("--json-output", is_flag=True, help="Output result as JSON")
def query(question: str, json_output: bool) -> None:
    config = Config.load()
    if not config.is_configured:
        click.echo("Not configured. Run 'nl2sql init' first.", err=True)
        sys.exit(1)

    from nl2sql.core.engine import AgenticLoop
    from nl2sql.core.sandbox import DockerSandbox

    schema_text = Path(config.schema_path).read_text(encoding="utf-8")
    sandbox = DockerSandbox(db_path=config.db_path, docker_image=config.docker_image)
    engine = AgenticLoop(config, sandbox)

    async def _run() -> None:
        if sandbox.is_docker_available:
            await sandbox.start()

        final_event = None
        async for event in engine.run(question, schema_text):
            if not json_output:
                if event.phase == "generating":
                    model = "finetuned" if event.model_type == "finetuned" else "superior"
                    click.echo(f"[Step {event.step}/{event.total}] Generating ({model})...")
                elif event.phase == "executing":
                    click.echo(f"  SQL: {event.sql}")
                elif event.phase == "execution_error":
                    click.echo(f"  Error: {event.error}")
                elif event.phase == "judge_rejected":
                    reason = event.judge_verdict.get("reason", "") if event.judge_verdict else ""
                    click.echo(f"  Judge: FAIL - {reason}")
                elif event.phase == "success":
                    click.echo(f"  Judge: PASS")
                    final_event = event
                elif event.phase == "exhausted":
                    click.echo("  All attempts exhausted.")
                    final_event = event
            else:
                if event.is_final:
                    final_event = event

        if final_event and json_output:
            output = {
                "question": question,
                "sql": final_event.sql or (final_event.best_attempt.sql if final_event.best_attempt else ""),
                "result": final_event.result or (final_event.best_attempt.result if final_event.best_attempt else {}),
                "success": final_event.success,
            }
            click.echo(json.dumps(output, indent=2))
        elif final_event and not json_output:
            sql = final_event.sql or (final_event.best_attempt.sql if final_event.best_attempt else "")
            result = final_event.result or (final_event.best_attempt.result if final_event.best_attempt else {})
            if sql:
                click.echo(f"\nFinal SQL: {sql}")
            if result.get("columns"):
                click.echo(f"Columns: {', '.join(result['columns'])}")
                for row in result.get("rows", [])[:20]:
                    click.echo(f"  {row}")
                if result.get("row_count", 0) > 20:
                    click.echo(f"  ... ({result['row_count']} total rows)")

        if sandbox.is_running:
            await sandbox.stop()

    asyncio.run(_run())


@main.command()
def status() -> None:
    config = Config.load()
    click.echo(f"Configured: {config.is_configured}")
    click.echo(f"DB: {config.db_path or 'Not set'}")
    click.echo(f"Schema: {config.schema_path or 'Not set'}")
    click.echo(f"HF Endpoint: {config.hf_endpoint}")
    click.echo(f"OmniRoute: {config.omniroute_url}")
    click.echo(f"Gen Model: {config.omniroute_gen_model}")
    click.echo(f"Judge Model: {config.omniroute_judge_model}")

    from nl2sql.core.sandbox import DockerSandbox
    sandbox = DockerSandbox(db_path=config.db_path or ".")
    click.echo(f"Docker: {'Available' if sandbox.is_docker_available else 'Unavailable'}")


def _launch_tui() -> None:
    from nl2sql.tui.app import NL2SQLApp

    app = NL2SQLApp()
    app.run()


if __name__ == "__main__":
    main()
