"""Runtime settings, read from environment variables (and an optional .env file)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent
DATA_DIR = REPO_DIR / "data"


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    model: str
    record: bool
    db_path: str
    replay_dir: Path
    stakeholders_path: Path
    interviews_dir: Path
    users_path: Path
    demo_dir: Path = DATA_DIR / "demo"


def load_settings() -> Settings:
    load_dotenv(REPO_DIR / ".env")
    return Settings(
        llm_provider=os.getenv("LP_LLM_PROVIDER", "replay").strip().lower(),
        model=os.getenv("LP_MODEL", "claude-opus-5-5"),
        record=_flag("LP_RECORD"),
        db_path=os.getenv("LP_DB_PATH", str(REPO_DIR / "leadership_pilot.db")),
        replay_dir=DATA_DIR / "replays",
        stakeholders_path=DATA_DIR / "stakeholders.json",
        interviews_dir=DATA_DIR / "interviews",
        users_path=DATA_DIR / "users.json",
    )
