"""Helpers shared by test modules (fixtures live in conftest.py)."""

from pathlib import Path

from alembic.config import Config

API_DIR = Path(__file__).resolve().parents[1]


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    if url:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg
