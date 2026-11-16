"""Обработка звука на numpy: ресемплинг, высота тона, признаки фразы."""
from __future__ import annotations

import numpy as np

SR = 16000
FRAME = 320  # 20 мс при 16 кГц


class StreamResampler:
    """Потоковый ресемплер в 16 кГц моно (box-фильтр + линейная интерполяция)."""

    def __init__(self, sr_in: int, sr_out: int = SR):
        self.sr_in, self.sr_out = int(sr_in), int(sr_out)
        self.step = self.sr_in / self.sr_out
        self.k = max(1, int(round(self.step)))
        self._tail = np.zeros(self.k - 1, dtype=np.float32)
        self._buf = np.zeros(0, dtype=np.float32)
        self._pos = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if self.sr_in == self.sr_out or x.size == 0:
            return x
        if self.k > 1:
            ext = np.concatenate([self._tail, x])
            y = np.convolve(ext, np.ones(self.k, dtype=np.float32) / self.k, mode="valid")
            self._tail = ext[-(self.k - 1):].copy()
        else:
            y = x
        buf = np.concatenate([self._buf, y])
        n = buf.size
        if n < 2:
            self._buf = buf
            return np.zeros(0, dtype=np.float32)
        count = int(np.floor((n - 1 - self._pos) / self.step)) + 1
        if count <= 0:
            self._buf = buf
            return np.zeros(0, dtype=np.float32)
        idx = self._pos + np.arange(count) * self.step
        out = np.interp(idx, np.arange(n), buf).astype(np.float32)
        nxt = self._pos + count * self.step
        drop = int(np.floor(nxt))
        self._buf = buf[drop:]
        self._pos = nxt - drop
        return out


def to_mono(x: np.ndarray, channels: int) -> np.ndarray:
    if channels > 1:
        usable = (x.size // channels) * channels
        return x[:usable].reshape(-1, channels).mean(axis=1)
    return x


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)))) if x.size else 0.0


def estimate_f0(x: np.ndarray, sr: int = SR, fmin: float = 75, fmax: float = 400,
                frame_ms: int = 40, hop_ms: int = 20) -> np.ndarray:
    """Оценка основного тона автокорреляцией по голосовым кадрам. Возвращает массив частот, Гц."""
    n = int(sr * frame_ms / 1000)
    hop = int(sr * hop_ms / 1000)
    if x.size < n * 2:
        return np.zeros(0)
    lag_min, lag_max = int(sr / fmax), int(sr / fmin)
    starts = np.arange(0, x.size - n, hop)
    energies = np.array([rms(x[s:s + n]) for s in starts])
    if energies.size == 0:
        return np.zeros(0)
    thr = max(0.004, 0.4 * float(np.percentile(energies, 90)))
    win = np.hanning(n)
    f0s = []
    for s, e in zip(starts, energies):
        if e < thr:
            continue
        seg = x[s:s + n].astype(np.float64)
        seg = (seg - seg.mean()) * win
        spec = np.fft.rfft(seg, 2 * n)
        ac = np.fft.irfft(spec * np.conj(spec))[: lag_max + 2]
        if ac[0] <= 1e-9:
            continue
        ac = ac / ac[0]
        lag = lag_min + int(np.argmax(ac[lag_min:lag_max + 1]))
        if ac[lag] < 0.35 or lag <= lag_min or lag >= lag_max:
            continue
        # параболическая интерполяция пика
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        denom = a - 2 * b + c
        shift = 0.5 * (a - c) / denom if abs(denom) > 1e-9 else 0.0
        f0s.append(sr / (lag + shift))
    return np.array(f0s)


def utterance_features(audio: np.ndarray, nwords: int) -> dict:
    """Признаки фразы для сравнения «как человек говорит обычно»."""
    dur = audio.size / SR
    feats: dict = {"duration": round(dur, 2), "rms_db": round(float(20 * np.log10(rms(audio) + 1e-9)), 1)}
    n = audio.size // FRAME
    if n >= 5:
        frames = audio[: n * FRAME].reshape(n, FRAME)
        e = np.sqrt(np.mean(np.square(frames), axis=1))
        thr = max(0.004, 0.25 * float(np.percentile(e, 90)))
        active = e > thr
        speech_time = float(active.sum()) * FRAME / SR
        feats["speech_time"] = round(speech_time, 2)
        feats["wps"] = round(nwords / max(0.5, speech_time), 2) if nwords else 0.0
        idx = np.flatnonzero(active)
        pauses, paused = 0, 0.0
        if idx.size > 1:
            gaps = np.diff(idx) - 1
            for g in gaps:
                if g * FRAME / SR >= 0.35:
                    pauses += 1
                    paused += g * FRAME / SR
        feats["pauses"] = pauses
        feats["pause_ratio"] = round(paused / dur, 3) if dur else 0.0
    f0 = estimate_f0(audio)
    if f0.size >= 5:
        feats["f0_mean"] = round(float(np.mean(f0)), 1)
        feats["f0_std"] = round(float(np.std(f0)), 1)
        feats["voiced_ratio"] = round(f0.size / max(1, (audio.size - 640) // 320), 2)
    return feats
