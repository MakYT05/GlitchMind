"""Контроллер: связывает захват звука → распознавание → анализ → советы (правила + языковая модель)."""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from typing import Callable

from . import __version__, paths
from .analysis.engine import Engine, parse_dialog
from .audio import capture
from .audio.asr import ASR, ASRError, FasterWhisperASR
from .audio.capture import AudioError, Ticker
from .audio.dsp import utterance_features
from .audio.vad import Segment, Segmenter
from .config import SettingsStore
from .demo import DEMO_SCRIPT
from .events import EventBus
from .knowledge import kb
from .knowledge.library import Library
from .llm import prompts
from .llm.client import LLMClient, LLMError, parse_json_loose

log = logging.getLogger("glitchmind")

CHANNEL_NAMES = {"mic": "Микрофон", "loop": "Наушники"}
EMPTY_METERS = {"pressure": 0, "manip": 0, "deception": 0, "deception_label": "ровно"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё0-9]+", text.lower())


def _public(u: dict) -> dict:
    keys = ("id", "speaker", "text", "t", "features", "hits", "cues", "deception_index", "safety", "source")
    return {k: u.get(k) for k in keys}


class Controller:
    def __init__(self, store: SettingsStore, bus: EventBus, library: Library,
                 asr_factory: Callable[[object, Callable[[str], None]], ASR] | None = None,
                 source_factory: Callable | None = None):
        self.store, self.bus, self.lib = store, bus, library
        self._asr_factory = asr_factory or self._default_asr_factory
        self._source_factory = source_factory or capture.make_source
        self.engine = Engine()
        self.transcript: list[dict] = []
        self._next_id = 1
        self.meters = dict(EMPTY_METERS)
        self.last_advice: dict | None = None
        self.last_situation: dict | None = None
        self.llm_status = {"state": "idle", "message": ""}

        self.listening = False
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._sources: dict[str, capture.AudioSource] = {}
        self._segmenters: dict[str, Segmenter] = {}
        self._ticker: Ticker | None = None
        self._tick_n = 0

        self._asr: ASR | None = None
        self._asr_key: tuple | None = None
        self._asr_q: queue.Queue = queue.Queue(maxsize=8)
        self._asr_thread: threading.Thread | None = None

        self._adv_cv = threading.Condition()
        self._adv_req: dict | None = None
        self._adv_id = 0
        self._adv_thread: threading.Thread | None = None

        self._demo_stop = threading.Event()
        self._demo_thread: threading.Thread | None = None
        self._sit_id = 0

        self._start_workers()

    # ================================================================ воркеры
    def _start_workers(self) -> None:
        self._adv_thread = threading.Thread(target=self._advice_loop, name="advice", daemon=True)
        self._adv_thread.start()
        self._asr_thread = threading.Thread(target=self._asr_loop, name="asr", daemon=True)
        self._asr_thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._demo_stop.set()
        try:
            self.stop_listening()
        except Exception:  # noqa: BLE001
            pass
        with self._adv_cv:
            self._adv_cv.notify_all()

    # ================================================================ состояние
    def state(self) -> dict:
        s = self.store.get()
        with self._lock:
            return {
                "version": __version__,
                "listening": self.listening,
                "channels": {"mic": s.use_mic, "loop": s.use_loop},
                "active": {k: True for k in self._sources},
                "transcript": [_public(u) for u in self.transcript[-200:]],
                "meters": self.meters,
                "advice": self.last_advice,
                "situation": self.last_situation,
                "llm": self.llm_status,
                "settings": self.store.public(),
                "library": self.lib.status(),
                "demo": bool(self._demo_thread and self._demo_thread.is_alive()),
                "has_skin": paths.skin_file() is not None,
                "data_dir": str(paths.data_dir()),
                "techniques": kb.technique_count(),
            }

    def _emit_state(self) -> None:
        s = self.store.get()
        self.bus.publish("state", {
            "listening": self.listening,
            "channels": {"mic": s.use_mic, "loop": s.use_loop},
            "active": {k: True for k in self._sources},
        })

    def _status(self, kind: str, state: str, message: str = "") -> None:
        self.bus.publish("status", {"kind": kind, "state": state, "message": message})

    def _error(self, source: str, message: str) -> None:
        log.error("%s: %s", source, message)
        self.bus.publish("error", {"source": source, "message": message})

    # ================================================================ прослушка
    def start_listening(self) -> None:
        with self._lock:
            if self.listening:
                return
            s = self.store.get()
            kinds = [k for k, use in (("mic", s.use_mic), ("loop", s.use_loop)) if use]
            if not kinds:
                raise AudioError("Включи хотя бы один канал: микрофон или наушники.")
            errors: list[str] = []
            for kind in kinds:
                seg = Segmenter(lambda sg, k=kind: self._on_segment(k, sg), sensitivity=s.vad_sensitivity)
                dev = s.mic_device if kind == "mic" else s.loop_device
                try:
                    src = self._source_factory(
                        kind, dev, lambda ch, t, sgm=seg: sgm.feed(ch, t),
                        lambda msg, k=kind: self._error(k, msg))
                    src.start()
                except AudioError as e:
                    errors.append(f"{CHANNEL_NAMES[kind]}: {e}")
                    continue
                self._segmenters[kind] = seg
                self._sources[kind] = src
            if not self._sources:
                raise AudioError("; ".join(errors))
            self.listening = True
            self._ticker = Ticker(self._tick, 0.1)
            self._ticker.start()
        for e in errors:
            self._error("audio", e)
        self._emit_state()
        self._warm_asr_async()

    def stop_listening(self) -> None:
        with self._lock:
            was = self.listening
            self.listening = False
            if self._ticker:
                self._ticker.stop()
                self._ticker = None
            for src in list(self._sources.values()):
                try:
                    src.stop()
                except Exception:  # noqa: BLE001
                    pass
            for seg in self._segmenters.values():
                seg.flush()
            self._sources.clear()
            self._segmenters.clear()
        if was:
            self.bus.publish("levels", {"mic": 0, "loop": 0})
            self._emit_state()

    def set_channels(self, mic: bool | None = None, loop: bool | None = None) -> None:
        patch = {}
        if mic is not None:
            patch["use_mic"] = bool(mic)
        if loop is not None:
            patch["use_loop"] = bool(loop)
        self.store.update(patch)
        if self.listening:
            self.stop_listening()
            s = self.store.get()
            if s.use_mic or s.use_loop:
                try:
                    self.start_listening()
                except AudioError as e:
                    self._error("audio", str(e))
        self._emit_state()

    def _tick(self) -> None:
        now = time.time()
        self._tick_n += 1
        levels = {}
        for kind, seg in list(self._segmenters.items()):
            seg.tick(now)
            seg.level *= 0.85
            levels[kind] = round(min(1.0, seg.level), 3)
        if self._tick_n % 2 == 0 and levels:
            self.bus.publish("levels", {"mic": levels.get("mic", 0), "loop": levels.get("loop", 0)})

    def _on_segment(self, kind: str, seg: Segment) -> None:
        try:
            self._asr_q.put_nowait((kind, seg))
        except queue.Full:
            try:
                self._asr_q.get_nowait()
                self._asr_q.put_nowait((kind, seg))
            except (queue.Empty, queue.Full):
                pass
            self._status("asr", "busy", "Распознавание не успевает — часть фраз пропущена. Возьми модель поменьше.")

    # ================================================================ распознавание
    @staticmethod
    def _default_asr_factory(s, on_status) -> ASR:
        return FasterWhisperASR(s.asr_model, s.asr_device, s.asr_compute, paths.models_dir(), on_status)

    def _get_asr(self) -> ASR:
        s = self.store.get()
        key = (s.asr_model, s.asr_device, s.asr_compute)
        if self._asr is None or self._asr_key != key:
            self._asr = self._asr_factory(s, lambda m: self._status("asr", "loading", m))
            self._asr_key = key
        return self._asr

    def _warm_asr_async(self) -> None:
        def run():
            try:
                self._get_asr().ensure_loaded()
                self._status("asr", "ready", "Модель распознавания готова")
            except ASRError as e:
                self._error("asr", str(e))
                self._status("asr", "error", str(e))
            except Exception as e:  # noqa: BLE001
                self._error("asr", f"Ошибка загрузки модели: {e}")
        threading.Thread(target=run, daemon=True, name="asr-warm").start()

    def _asr_loop(self) -> None:
        while not self._stop.is_set():
            try:
                kind, seg = self._asr_q.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                text = self._get_asr().transcribe(seg.audio, self.store.get().language)
            except ASRError as e:
                self._error("asr", str(e))
                self._asr = None
                continue
            except Exception as e:  # noqa: BLE001
                self._error("asr", f"Ошибка распознавания: {e}")
                continue
            text = (text or "").strip()
            words = _words(text)
            if not words:
                continue
            feats = utterance_features(seg.audio, len(words))
            speaker = "me" if kind == "mic" else "them"
            self._ingest(speaker, text, feats, seg.t_voice_start, seg.t_end, source=kind)

    # ================================================================ реплики
    def _is_echo(self, text: str, t0: float) -> bool:
        """Микрофон поймал то, что играет в динамиках: схожая фраза собеседника только что прозвучала."""
        w = set(_words(text))
        if len(w) < 3:
            return False
        for u in reversed(self.transcript[-4:]):
            if u["speaker"] != "them" or abs(t0 - (u.get("t1") or 0)) > 6:
                continue
            ow = set(_words(u["text"]))
            if ow and len(w & ow) / len(w | ow) >= 0.6:
                return True
        return False

    def _ingest(self, speaker: str, text: str, feats: dict | None, t0: float, t1: float, source: str) -> dict | None:
        feats = dict(feats or {})
        with self._lock:
            if source == "mic" and self._is_echo(text, t0):
                return None
            if speaker == "them" and "latency" not in feats and self.transcript:
                last = self.transcript[-1]
                if last["speaker"] == "me" and last.get("t1"):
                    gap = t0 - last["t1"]
                    if 0 <= gap <= 10:
                        feats["latency"] = round(gap, 2)
            u = {"id": self._next_id, "speaker": speaker, "text": text, "features": feats,
                 "t": time.time(), "t0": t0, "t1": t1, "source": source}
            self._next_id += 1
            u.update(self.engine.analyze_utterance(u))
            self.transcript.append(u)
            del self.transcript[:-500]
            self.meters = self.engine.meters(self.transcript)
            meters = dict(self.meters)
        self.bus.publish("utterance", _public(u))
        self.bus.publish("meters", meters)
        if speaker == "them" and self.store.get().auto_advice:
            self.request_advice()
        return u

    def add_manual(self, speaker: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        now = time.time()
        self._ingest("me" if speaker == "me" else "them", text, {}, now, now, source="manual")

    def clear(self) -> None:
        with self._lock:
            self.transcript.clear()
            self.engine.reset()
            self.meters = dict(EMPTY_METERS)
            self.last_advice = None
        self.bus.publish("cleared", {})
        self.bus.publish("meters", self.meters)

    # ================================================================ советы
    def request_advice(self, note: str = "") -> None:
        with self._adv_cv:
            self._adv_id += 1
            self._adv_req = {"id": self._adv_id, "note": note}
            self._adv_cv.notify()

    def _advice_loop(self) -> None:
        while not self._stop.is_set():
            with self._adv_cv:
                while self._adv_req is None and not self._stop.is_set():
                    self._adv_cv.wait(0.5)
                req, self._adv_req = self._adv_req, None
            if req is None:
                continue
            try:
                self._do_advice(req)
            except Exception as e:  # noqa: BLE001
                log.exception("advice failed")
                self._error("advice", f"Ошибка подсказки: {e}")

    def _llm_client(self) -> LLMClient | None:
        s = self.store.get()
        if not s.llm_enabled or not s.llm_model:
            return None
        return LLMClient(s.llm_provider, s.llm_url, s.llm_model, s.llm_api_key,
                         s.llm_temperature, s.llm_timeout, s.llm_num_ctx)

    def _retrieve(self, transcript: list[dict]) -> list[dict]:
        s = self.store.get()
        if s.rag_top_k <= 0:
            return []
        them = [u for u in transcript if u["speaker"] == "them"][-2:]
        query_parts = [u["text"] for u in them]
        books: list[str] = []
        for u in them:
            for h in u.get("hits", []):
                query_parts.append(h["label"])
                if h.get("book") and h["book"] not in books:
                    books.append(h["book"])
        return self.lib.search(" ".join(query_parts), s.rag_top_k, books)

    @staticmethod
    def _source_refs(passages: list[dict]) -> list[dict]:
        out = []
        for p in passages:
            b = kb.book(p.get("book", ""))
            out.append({"book": b["author"] if b else "", "doc": p["doc"], "snippet": p["text"][:240]})
        return out

    def _merge_llm(self, rules: dict, llm: dict, passages: list[dict]) -> dict:
        llm["meters"] = rules.get("meters", self.meters)
        llm["sources"] = self._source_refs(passages)
        if rules.get("safety") and not llm.get("safety"):
            llm["safety"] = rules["safety"]
        for key in ("deception", "self_coaching"):
            if not llm.get(key) and rules.get(key):
                llm[key] = rules[key]
        if not llm.get("replies"):
            llm["replies"] = rules.get("replies", [])
        return llm

    def _ask_llm(self, client: LLMClient, messages: list[dict]) -> dict:
        raw = client.chat(messages, json_mode=(client.provider == "ollama"))
        d = parse_json_loose(raw)
        if d is None:
            return {"source": "llm", "model": client.model, "situation": raw.strip()[:1200], "replies": [],
                    "dynamics": [], "questions": [], "avoid": [], "self_coaching": [], "deception": [],
                    "safety": None, "ts": time.time(), "unparsed": True}
        return prompts.normalize_advice(d, client.model)

    def _do_advice(self, req: dict) -> None:
        with self._lock:
            tr = list(self.transcript)
        rules = self.engine.build_advice(tr, req.get("note", ""))
        self.last_advice = rules
        self.bus.publish("advice", rules)
        if not any(u["speaker"] == "them" for u in tr):
            return
        client = self._llm_client()
        if client is None:
            s = self.store.get()
            if s.llm_enabled and not s.llm_model:
                self._llm_state("no_model", "ИИ-модель не выбрана — работают быстрые подсказки по правилам.")
            else:
                self._llm_state("off", "")
            return
        self._llm_state("thinking", client.model)
        try:
            passages = self._retrieve(tr)
            llm = self._ask_llm(client, prompts.build_live_messages(tr, passages, req.get("note", "")))
        except LLMError as e:
            self._llm_state("error", str(e))
            return
        if self._adv_req is not None:  # пришёл более свежий запрос — этот ответ устарел
            return
        merged = self._merge_llm(rules, llm, passages)
        self.last_advice = merged
        self.bus.publish("advice", merged)
        self._llm_state("ready", client.model)

    def _llm_state(self, state: str, message: str = "") -> None:
        self.llm_status = {"state": state, "message": message}
        self._status("llm", state, message)

    # ---------------------------------------------------------------- ситуация
    def analyze_situation(self, text: str, note: str = "") -> int:
        self._sit_id += 1
        sid = self._sit_id
        threading.Thread(target=self._situation_worker, args=(sid, text, note), daemon=True,
                         name="situation").start()
        return sid

    def _situation_worker(self, sid: int, text: str, note: str) -> None:
        eng = Engine()
        items = parse_dialog(text)
        for u in items:
            u.update(eng.analyze_utterance(u))
        rules = eng.build_advice(items, note)
        rules["id"] = sid
        self.last_situation = rules
        self.bus.publish("situation", rules)
        client = self._llm_client()
        if client is None:
            return
        self._llm_state("thinking", client.model)
        try:
            passages = self._retrieve(items)
            llm = self._ask_llm(client, prompts.build_situation_messages(text, passages, items))
        except LLMError as e:
            self._llm_state("error", str(e))
            self.bus.publish("situation", {"id": sid, "source": "llm_error", "message": str(e)})
            return
        merged = self._merge_llm(rules, llm, passages)
        merged["id"] = sid
        self.last_situation = merged
        self.bus.publish("situation", merged)
        self._llm_state("ready", client.model)

    # ================================================================ настройки
    def update_settings(self, patch: dict) -> dict:
        before = self.store.get()
        after = self.store.update(patch)
        audio_changed = any(getattr(before, k) != getattr(after, k)
                            for k in ("use_mic", "use_loop", "mic_device", "loop_device"))
        if self.listening and audio_changed:
            self.stop_listening()
            if after.use_mic or after.use_loop:
                try:
                    self.start_listening()
                except AudioError as e:
                    self._error("audio", str(e))
        for seg in self._segmenters.values():
            seg.set_sensitivity(after.vad_sensitivity)
        if (before.llm_url, before.llm_model, before.llm_provider, before.llm_enabled) != \
                (after.llm_url, after.llm_model, after.llm_provider, after.llm_enabled):
            self._llm_state("idle", "")
        self._emit_state()
        return self.store.public()

    def llm_test(self) -> dict:
        s = self.store.get()
        client = LLMClient(s.llm_provider, s.llm_url, s.llm_model, s.llm_api_key, timeout=s.llm_timeout)
        return client.test()

    # ================================================================ демо
    def run_demo(self, on: bool) -> None:
        if not on:
            self._demo_stop.set()
            return
        if self._demo_thread and self._demo_thread.is_alive():
            return
        self._demo_stop.clear()
        self._demo_thread = threading.Thread(target=self._demo_loop, daemon=True, name="demo")
        self._demo_thread.start()

    def _demo_loop(self) -> None:
        self.clear()
        self.bus.publish("demo", {"on": True})
        for delay, speaker, text, feats in DEMO_SCRIPT:
            if self._demo_stop.wait(delay):
                break
            now = time.time()
            self._ingest(speaker, text, dict(feats), now, now, source="demo")
        self.bus.publish("demo", {"on": False})
