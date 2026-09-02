from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


APP_DIR_NAME = ".nl2sql"


def _load_env() -> None:
    """Load .env from cwd upward, then home."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".env").exists():
            load_dotenv(str(parent / ".env"))
            return
        if (parent / ".git").exists():
            # project root found but no .env here — keep searching
            break
    home_env = Path.home() / ".env"
    if home_env.exists():
        load_dotenv(str(home_env))


def _find_project_root() -> Path:
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".git").exists():
            return parent
    return cwd


@dataclass
class Config:
    # LLM endpoints
    hf_endpoint: str = "https://atharvamate-qwen2-5-1-5b-nl2sql.hf.space"
    hf_token: str = ""
    omniroute_url: str = "http://localhost:20128/v1/chat/completions"
    omniroute_gen_model: str = "auto/coding:free"
    omniroute_judge_model: str = "no-think/antigravity/claude-sonnet-4-6"

    # Database — set via .env or TUI
    db_path: str = ""
    schema_path: str = ""

    # Engine tuning
    max_finetuned_steps: int = 1
    max_total_steps: int = 3
    docker_image: str = "python:3.12-slim"

    # Infrastructure
    redis_url: str = "redis://localhost:6379"
    session_db_path: str = ""

    project_root: Path = field(default_factory=_find_project_root)

    def __post_init__(self) -> None:
        if not self.session_db_path:
            app_dir = self.project_root / APP_DIR_NAME
            app_dir.mkdir(parents=True, exist_ok=True)
            self.session_db_path = str(app_dir / "sessions.db")

    @classmethod
    def load(cls) -> "Config":
        _load_env()
        return cls(
            hf_endpoint=os.environ.get("HF_ENDPOINT", cls.hf_endpoint),
            hf_token=os.environ.get("HF_TOKEN", ""),
            omniroute_url=os.environ.get("OMNIROUTE_URL", cls.omniroute_url),
            omniroute_gen_model=os.environ.get("OMNIROUTE_GEN_MODEL", cls.omniroute_gen_model),
            omniroute_judge_model=os.environ.get("OMNIROUTE_JUDGE_MODEL", cls.omniroute_judge_model),
            db_path=os.environ.get("DB_PATH", ""),
            schema_path=os.environ.get("SCHEMA_PATH", ""),
            max_finetuned_steps=int(os.environ.get("MAX_FINETUNED_STEPS", cls.max_finetuned_steps)),
            max_total_steps=int(os.environ.get("MAX_TOTAL_STEPS", cls.max_total_steps)),
            docker_image=os.environ.get("DOCKER_IMAGE", cls.docker_image),
            redis_url=os.environ.get("REDIS_URL", cls.redis_url),
            project_root=_find_project_root(),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.schema_path and self.db_path)
