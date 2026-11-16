"""Пути: ресурсы приложения и пользовательские данные."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "GlitchMind"
SKIN_EXTS = (".png", ".gif", ".webp", ".jpg", ".jpeg", ".svg")


def resource_dir() -> Path:
    """Папка с ресурсами (ui/…): работает и из исходников, и из PyInstaller."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "glitchmind"
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Пользовательские данные: настройки, библиотека книг, модели, логи."""
    env = os.environ.get("GLITCHMIND_HOME")
    if env:
        p = Path(env)
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
    else:
        p = Path.home() / ".glitchmind"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sub(name: str) -> Path:
    p = data_dir() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def library_dir() -> Path:
    return _sub("library")


def index_dir() -> Path:
    return _sub("index")


def models_dir() -> Path:
    return _sub("models")


def logs_dir() -> Path:
    return _sub("logs")


def skin_file() -> Path | None:
    """Свой маскот: skin.png/.gif/.webp/.jpg/.svg в папке данных (до 8 МБ)."""
    for ext in SKIN_EXTS:
        f = data_dir() / f"skin{ext}"
        try:
            if f.is_file() and f.stat().st_size <= 8 * 1024 * 1024:
                return f
        except OSError:
            continue
    return None
