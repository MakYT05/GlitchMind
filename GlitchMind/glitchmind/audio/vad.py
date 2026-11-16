"""Энергетический VAD: режет непрерывный поток на отдельные фразы."""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .dsp import FRAME, SR


@dataclass
class Segment:
    audio: np.ndarray
    t_start: float        # начало буфера (с pre-roll)
    t_voice_start: float  # когда реально началась речь
    t_end: float          # когда речь закончилась


class Segmenter:
    """Принимает куски 16 кГц моно и вызывает on_segment(Segment) на каждую законченную фразу.

    Временные метки — «настенные» (time.time()). Метод tick() нужен потому, что захват звука
    из наушников (WASAPI loopback) молчит, пока ничего не играет, — конец фразы определяем по часам.
    """

    FRAME_SEC = FRAME / SR

    def __init__(self, on_segment: Callable[[Segment], None], sensitivity: float = 0.5,
                 hangover: float = 0.8, min_voice: float = 0.35, max_len: float = 22.0, preroll: float = 0.3):
        self.on_segment = on_segment
        self.sens = sensitivity
        self.hangover, self.min_voice, self.max_len = hangover, min_voice, max_len
        self._lock = threading.Lock()
        self._noise = 0.003
        self._left = np.zeros(0, dtype=np.float32)
        self._preroll = deque(maxlen=max(1, int(preroll / self.FRAME_SEC)))
        self.level = 0.0
        self._reset_speech()

    def set_sensitivity(self, s: float) -> None:
        self.sens = min(1.0, max(0.0, float(s)))

    def _reset_speech(self) -> None:
        self._in_speech = False
        self._run = 0
        self._buf: list[np.ndarray] = []
        self._voiced = 0
        self._last_voice_t = 0.0
        self._t_start = 0.0
        self._t_voice_start = 0.0

    def _threshold(self) -> float:
        ratio = 6.0 - 4.0 * self.sens
        abs_min = 0.006 * (2.0 - self.sens)
        return max(abs_min, self._noise * ratio)

    # ---------------------------------------------------------------- вход
    def feed(self, chunk: np.ndarray, t_end: float) -> None:
        with self._lock:
            if chunk.size == 0:
                return
            self._tick(t_end - chunk.size / SR)
            x = np.concatenate([self._left, chunk.astype(np.float32, copy=False)])
            nfr = x.size // FRAME
            for k in range(nfr):
                frame = x[k * FRAME:(k + 1) * FRAME]
                t_frame_end = t_end - (x.size - (k + 1) * FRAME) / SR
                self._frame(frame, t_frame_end)
            self._left = x[nfr * FRAME:].copy()

    def tick(self, now: float) -> None:
        with self._lock:
            self._tick(now)

    def flush(self, now: float | None = None) -> None:
        with self._lock:
            if self._in_speech:
                self._finalize()

    # ------------------------------------------------------------ внутреннее
    def _tick(self, now: float) -> None:
        if self._in_speech and now - self._last_voice_t > self.hangover:
            self._finalize()

    def _frame(self, frame: np.ndarray, t_end: float) -> None:
        r = float(np.sqrt(np.mean(np.square(frame))))
        self.level = 0.6 * self.level + 0.4 * min(1.0, r / 0.15)
        voiced = r > self._threshold()
        if not voiced and r < self._noise * 3 + 1e-4:
            self._noise = 0.97 * self._noise + 0.03 * max(r, 1e-4)

        if not self._in_speech:
            self._preroll.append(frame)
            self._run = self._run + 1 if voiced else 0
            if self._run >= 3:
                self._in_speech = True
                self._buf = list(self._preroll)
                self._t_start = t_end - len(self._buf) * self.FRAME_SEC
                self._t_voice_start = t_end - 3 * self.FRAME_SEC
                self._voiced = 3
                self._last_voice_t = t_end
            return

        self._buf.append(frame)
        if voiced:
            self._voiced += 1
            self._last_voice_t = t_end
        elif t_end - self._last_voice_t > self.hangover:
            self._finalize()
            return
        if len(self._buf) * self.FRAME_SEC > self.max_len:
            self._finalize()

    def _finalize(self) -> None:
        buf, voiced, t_start, t_vs, t_end = self._buf, self._voiced, self._t_start, self._t_voice_start, self._last_voice_t
        self._reset_speech()
        self._preroll.clear()
        if voiced * self.FRAME_SEC < self.min_voice or not buf:
            return
        # обрезаем хвост тишины, оставляя ~0.25 с
        keep_until = (t_end - t_start) + 0.25
        nkeep = max(1, min(len(buf), int(keep_until / self.FRAME_SEC)))
        audio = np.concatenate(buf[:nkeep])
        try:
            self.on_segment(Segment(audio=audio, t_start=t_start, t_voice_start=t_vs, t_end=t_end))
        except Exception:  # noqa: BLE001 - ошибка потребителя не должна ломать захват
            pass
