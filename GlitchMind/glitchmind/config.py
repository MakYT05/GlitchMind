"""Настройки приложения (JSON в папке данных)."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, asdict, fields
from pathlib import Path

from . import paths


@dataclass
class Settings:
    # распознавание речи
    language: str = "ru"
    asr_model: str = "small"
    asr_device: str = "cpu"
    asr_compute: str = "int8"
    # аудио
    use_mic: bool = True
    use_loop: bool = True
    mic_device: str = ""
    loop_device: str = ""
    vad_sensitivity: float = 0.5
    # советы
    auto_advice: bool = True
    # языковая модель
    llm_enabled: bool = True
    llm_provider: str = "ollama"          # ollama | openai
    llm_url: str = "http://127.0.0.1:11434"
    llm_model: str = ""
    llm_api_key: str = ""
    llm_temperature: float = 0.4
    llm_timeout: int = 240
    llm_num_ctx: int = 8192
    rag_top_k: int = 3
    # интерфейс
    fx_glitch: bool = True
    consent_ack: bool = False


_SECRET = {"llm_api_key"}


def _coerce(value, default):
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        return int(float(value))
    if isinstance(default, float):
        return float(value)
    return "" if value is None else str(value)


class SettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else paths.data_dir() / "config.json"
        self._lock = threading.RLock()
        self._s = Settings()
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self._apply(raw)

    def _apply(self, patch: dict) -> None:
        defaults = Settings()
        for f in fields(Settings):
            if f.name in patch:
                try:
                    setattr(self._s, f.name, _coerce(patch[f.name], getattr(defaults, f.name)))
                except (TypeError, ValueError):
                    pass
        self._s.vad_sensitivity = min(1.0, max(0.0, self._s.vad_sensitivity))
        self._s.llm_temperature = min(2.0, max(0.0, self._s.llm_temperature))
        self._s.rag_top_k = min(8, max(0, self._s.rag_top_k))

    def get(self) -> Settings:
        with self._lock:
            return Settings(**asdict(self._s))

    def update(self, patch: dict) -> Settings:
        with self._lock:
            patch = dict(patch or {})
            # пустой ключ из формы не должен затирать сохранённый
            if patch.get("llm_api_key") in ("", None):
                patch.pop("llm_api_key", None)
            elif patch.get("llm_api_key") == "__clear__":
                patch["llm_api_key"] = ""
            self._apply(patch)
            self._save()
            return self.get()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self._s), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def public(self) -> dict:
        """Настройки для UI: без секретов."""
        d = asdict(self.get())
        for k in _SECRET:
            d[k + "_set"] = bool(d.pop(k))
        return d
