"""Configuration loading. Reads config.toml next to the app (or $CC_CONFIG)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    d = Path(os.environ.get("CC_DATA_DIR", ROOT / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class NotesFolder:
    path: str
    course: str = ""


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.raw.get(section, {}).get(key, default)

    # --- convenience accessors -------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return _data_dir()

    @property
    def db_path(self) -> Path:
        return Path(os.environ.get("CC_DB", self.data_dir / "command_center.db"))

    @property
    def timezone(self) -> str:
        return self.get("server", "timezone", "America/New_York")

    @property
    def passcode(self) -> str:
        return os.environ.get("CC_PASSCODE", self.get("server", "passcode", "")) or ""

    @property
    def notes_folders(self) -> list[NotesFolder]:
        notes = self.raw.get("notes", {})
        out = []
        for f in notes.get("folders", []) or []:
            if isinstance(f, dict) and f.get("path"):
                out.append(NotesFolder(path=str(f["path"]), course=str(f.get("course", "") or "")))
        return out

    @property
    def notes_options(self) -> dict[str, Any]:
        return {
            "extensions": [".md", ".txt", ".pdf", ".docx", ".tex"],
            "max_file_mb": 40,
            "ignore": ["node_modules", ".git", "__pycache__", ".obsidian"],
            **self.raw.get("notes", {}).get("options", {}),
        }

    @property
    def courses(self) -> dict[str, dict[str, str]]:
        return self.raw.get("courses", {}) or {}

    def resolve(self, p: str) -> Path:
        path = Path(p).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        return path


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path or os.environ.get("CC_CONFIG", ROOT / "config.toml"))
    if not p.exists():
        return Config(raw={}, path=None)
    with open(p, "rb") as fh:
        return Config(raw=tomllib.load(fh), path=p)


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(cfg: Config) -> None:
    global _config
    _config = cfg
