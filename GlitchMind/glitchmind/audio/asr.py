"""Распознавание речи: faster-whisper (локально, без интернета после загрузки модели)."""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Callable

import numpy as np

# Whisper на шуме/тишине любит «дописывать» типичные фразы из обучающих субтитров.
_HALLUCINATIONS = (
    "субтитры сделал", "субтитры создавал", "субтитры подогнал", "редактор субтитров", "корректор а.",
    "продолжение следует", "спасибо за просмотр", "подписывайтесь на канал", "dimatorzok", "amara.org",
    "thanks for watching", "thank you for watching", "до новых встреч",
)


class ASRError(Exception):
    pass


def clean_text(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    low = t.lower()
    if len(t) < 80 and any(h in low for h in _HALLUCINATIONS):
        return ""
    # схлопываем зацикливание: одно слово подряд >6 раз
    t = re.sub(r"\b(\w+)(?:[ ,.!?-]+\1\b){6,}", r"\1", t, flags=re.I)
    return t.strip()


class ASR:
    def transcribe(self, audio: np.ndarray, language: str = "ru") -> str:  # pragma: no cover
        raise NotImplementedError

    def ensure_loaded(self) -> None:
        pass


class FasterWhisperASR(ASR):
    def __init__(self, model_name: str, device: str, compute_type: str, models_dir: Path,
                 on_status: Callable[[str], None] | None = None):
        self.model_name, self.device, self.compute = model_name or "small", device or "cpu", compute_type or "int8"
        self.models_dir = Path(models_dir)
        self.on_status = on_status or (lambda s: None)
        self._model = None
        self._lock = threading.Lock()

    def ensure_loaded(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            try:
                from faster_whisper import WhisperModel  # type: ignore
            except Exception as e:  # noqa: BLE001
                raise ASRError(f"Не установлен faster-whisper: {e}") from e
            local = self.models_dir / self.model_name
            src = str(local) if local.is_dir() and any(local.iterdir()) else self.model_name
            first = src == self.model_name
            self.on_status("Загружаю модель распознавания" + (" (первый запуск — скачивание, нужен интернет)…" if first else "…"))
            try:
                self._model = WhisperModel(src, device=self.device, compute_type=self.compute,
                                           download_root=str(self.models_dir))
            except Exception as e:  # noqa: BLE001
                if self.device != "cpu":
                    self.device, self.compute = "cpu", "int8"
                    try:
                        self._model = WhisperModel(src, device="cpu", compute_type="int8",
                                                   download_root=str(self.models_dir))
                        return
                    except Exception as e2:  # noqa: BLE001
                        e = e2
                raise ASRError(
                    f"Не удалось загрузить модель «{self.model_name}»: {e}. "
                    f"Если нет интернета — скачай модель заранее и положи в папку {self.models_dir}."
                ) from e

    def transcribe(self, audio: np.ndarray, language: str = "ru") -> str:
        self.ensure_loaded()
        lang = None if language in ("", "auto") else language
        segments, _info = self._model.transcribe(
            audio.astype(np.float32), language=lang, beam_size=1, best_of=1, temperature=0.0,
            condition_on_previous_text=False, vad_filter=False, no_speech_threshold=0.6,
            log_prob_threshold=-1.0, compression_ratio_threshold=2.4, without_timestamps=True,
        )
        parts = []
        for s in segments:
            if getattr(s, "no_speech_prob", 0) > 0.6 and getattr(s, "avg_logprob", 0) < -1.0:
                continue
            parts.append(s.text)
        return clean_text(" ".join(parts))


class FakeASR(ASR):
    """Для тестов: отдаёт заранее заданные реплики по очереди."""

    def __init__(self, lines: list[str] | None = None):
        self.lines = list(lines or [])

    def transcribe(self, audio, language="ru") -> str:
        return self.lines.pop(0) if self.lines else ""
