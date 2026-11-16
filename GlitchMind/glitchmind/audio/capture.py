"""Захват звука: микрофон и «вывод в наушники» (WASAPI loopback, только Windows).

* kind="mic"  — микрофон (то, что говоришь ты);
* kind="loop" — всё, что играет на выбранном устройстве вывода, т. е. голос собеседника
  в звонке, видео и т. п. Устройство вывода = твои наушники.

Для Windows используется PyAudioWPatch (форк PyAudio с поддержкой loopback).
На других системах доступен только микрофон через sounddevice (для разработки).
"""
from __future__ import annotations

import threading
import time
from typing import Callable

import numpy as np

from .dsp import SR, StreamResampler, to_mono

OnAudio = Callable[[np.ndarray, float], None]


class AudioError(Exception):
    pass


def _import_pa():
    try:
        import pyaudiowpatch as pa  # type: ignore
        return pa
    except Exception:  # noqa: BLE001
        return None


def _import_sd():
    try:
        import sounddevice as sd  # type: ignore
        return sd
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ устройства
def list_devices() -> dict:
    """{"backend", "mics": [name], "outputs": [name], "default_mic", "default_output", "loopback_supported"}"""
    res = {"backend": "none", "mics": [], "outputs": [], "default_mic": "", "default_output": "",
           "loopback_supported": False}
    pa = _import_pa()
    if pa is not None:
        try:
            p = pa.PyAudio()
        except Exception as e:  # noqa: BLE001 - нет звуковой подсистемы
            res["backend"] = "wasapi"
            res["error"] = str(e)
            return res
        try:
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
            res["backend"] = "wasapi"
            res["loopback_supported"] = True
            try:
                res["default_mic"] = p.get_device_info_by_index(wasapi["defaultInputDevice"])["name"]
            except Exception:  # noqa: BLE001
                pass
            try:
                res["default_output"] = p.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
            except Exception:  # noqa: BLE001
                pass
            for i in range(p.get_device_count()):
                d = p.get_device_info_by_index(i)
                if d.get("hostApi") == wasapi["index"] and d.get("maxInputChannels", 0) > 0 \
                        and not d.get("isLoopbackDevice"):
                    res["mics"].append(d["name"])
            for lb in p.get_loopback_device_info_generator():
                res["outputs"].append(lb["name"])
        except Exception as e:  # noqa: BLE001
            res["error"] = str(e)
        finally:
            p.terminate()
        return res
    sd = _import_sd()
    if sd is not None:
        try:
            res["backend"] = "sounddevice"
            for d in sd.query_devices():
                if d["max_input_channels"] > 0:
                    res["mics"].append(d["name"])
            try:
                res["default_mic"] = sd.query_devices(kind="input")["name"]
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            res["error"] = str(e)
    return res


# ---------------------------------------------------------------------- источники
class AudioSource:
    def __init__(self, kind: str, device_name: str, on_audio: OnAudio, on_error: Callable[[str], None]):
        self.kind, self.device_name = kind, device_name
        self.on_audio, self.on_error = on_audio, on_error
        self.running = False

    def start(self) -> None:  # pragma: no cover - интерфейс
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover
        raise NotImplementedError


class PyAudioSource(AudioSource):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._pa = None
        self._stream = None
        self._rs: StreamResampler | None = None
        self._channels = 1
        self._pyaudio = None

    def _find(self, pa, p):
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        name = (self.device_name or "").strip().lower()
        if self.kind == "loop":
            loops = list(p.get_loopback_device_info_generator())
            if not loops:
                raise AudioError("Не найдено устройств вывода для перехвата звука.")
            if name:
                for lb in loops:
                    if name in lb["name"].lower():
                        return lb
            default_out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            for lb in loops:
                if default_out["name"] in lb["name"]:
                    return lb
            return loops[0]
        # микрофон
        cands = []
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d.get("hostApi") == wasapi["index"] and d.get("maxInputChannels", 0) > 0 \
                    and not d.get("isLoopbackDevice"):
                cands.append(d)
        if name:
            for d in cands:
                if name in d["name"].lower():
                    return d
        try:
            return p.get_device_info_by_index(wasapi["defaultInputDevice"])
        except Exception:  # noqa: BLE001
            if cands:
                return cands[0]
            raise AudioError("Микрофон не найден.") from None

    def start(self) -> None:
        pa = _import_pa()
        if pa is None:
            raise AudioError("Модуль PyAudioWPatch не установлен.")
        self._pyaudio = pa
        p = pa.PyAudio()
        try:
            info = self._find(pa, p)
            self._channels = max(1, int(info["maxInputChannels"]))
            rate = int(info["defaultSampleRate"])
            self._rs = StreamResampler(rate, SR)
            self._stream = p.open(
                format=pa.paInt16, channels=self._channels, rate=rate, input=True,
                frames_per_buffer=int(rate * 0.03), input_device_index=info["index"],
                stream_callback=self._callback,
            )
            self._pa = p
            self.running = True
        except AudioError:
            p.terminate()
            raise
        except Exception as e:  # noqa: BLE001
            p.terminate()
            hint = " Проверь: Параметры Windows → Конфиденциальность → Микрофон." if self.kind == "mic" else ""
            raise AudioError(f"Не удалось открыть устройство: {e}.{hint}") from e

    def _callback(self, in_data, frame_count, time_info, status):
        pa = self._pyaudio
        try:
            x = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
            y = self._rs.process(to_mono(x, self._channels))
            if y.size:
                self.on_audio(y, time.time())
        except Exception as e:  # noqa: BLE001
            self.on_error(f"Ошибка обработки звука: {e}")
        return (in_data, pa.paContinue)

    def stop(self) -> None:
        self.running = False
        try:
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pa is not None:
                self._pa.terminate()
        except Exception:  # noqa: BLE001
            pass
        self._stream = self._pa = None


class SoundDeviceSource(AudioSource):
    """Запасной вариант для микрофона вне Windows (разработка)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._stream = None

    def start(self) -> None:
        sd = _import_sd()
        if sd is None:
            raise AudioError("Не установлены ни PyAudioWPatch, ни sounddevice.")
        try:
            dev = None
            if self.device_name:
                for i, d in enumerate(sd.query_devices()):
                    if d["max_input_channels"] > 0 and self.device_name.lower() in d["name"].lower():
                        dev = i
                        break
            info = sd.query_devices(dev, "input")
            rate = int(info["default_samplerate"])
            rs = StreamResampler(rate, SR)

            def cb(indata, frames, t, status):
                y = rs.process(indata[:, 0].astype(np.float32))
                if y.size:
                    self.on_audio(y, time.time())

            self._stream = sd.InputStream(device=dev, channels=1, samplerate=rate,
                                          blocksize=int(rate * 0.03), callback=cb, dtype="float32")
            self._stream.start()
            self.running = True
        except Exception as e:  # noqa: BLE001
            raise AudioError(f"Не удалось открыть микрофон: {e}") from e

    def stop(self) -> None:
        self.running = False
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:  # noqa: BLE001
            pass
        self._stream = None


def make_source(kind: str, device_name: str, on_audio: OnAudio, on_error: Callable[[str], None]) -> AudioSource:
    if _import_pa() is not None:
        return PyAudioSource(kind, device_name, on_audio, on_error)
    if kind == "loop":
        raise AudioError("Перехват звука из наушников работает только в Windows (WASAPI loopback).")
    return SoundDeviceSource(kind, device_name, on_audio, on_error)


class Ticker(threading.Thread):
    """Дёргает callback каждые interval секунд (нужно для конца фраз при тишине loopback)."""

    def __init__(self, fn: Callable[[], None], interval: float = 0.1):
        super().__init__(daemon=True)
        self.fn, self.interval = fn, interval
        self._stop_evt = threading.Event()

    def run(self) -> None:
        while not self._stop_evt.wait(self.interval):
            try:
                self.fn()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._stop_evt.set()
