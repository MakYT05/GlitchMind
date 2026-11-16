"""Маркеры несоответствий в речи собеседника.

ВАЖНО. Это НЕ детектор лжи. Исследования показывают, что надёжного «признака лжи»
в речи нет: люди (и алгоритмы) угадывают ложь лишь немногим лучше случайного.
Модуль собирает слабые сигналы и показывает, ЧТО СТОИТ УТОЧНИТЬ:

* текст: клятвы, размытая память, уход от ответа, абсолютные слова;
* логика: расхождение чисел и «да/нет» в разных репликах одного человека;
* голос/поведение: отклонение от ЕГО ЖЕ обычного темпа, паузы, высоты тона,
  доли слов-паразитов, длины ответов и доли «я» (по Воссу — «эффект Пиноккио»).

Любой маркер может иметь невинное объяснение (волнение, сложный вопрос, плохая связь).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from statistics import mean, pstdev

from .patterns import find_hits, norm

STOP = {
    "и", "в", "во", "на", "с", "со", "к", "по", "о", "об", "от", "до", "из", "за", "для", "что", "это", "как", "так",
    "но", "а", "же", "ли", "бы", "не", "ни", "я", "ты", "он", "она", "мы", "вы", "они", "мне", "меня", "тебе", "его",
    "ее", "их", "мой", "моя", "мое", "твой", "все", "вот", "ну", "да", "нет", "там", "тут", "то", "уже", "еще", "был",
    "была", "было", "были", "быть", "очень", "просто", "потом", "когда", "если", "чтобы", "часов", "часа", "час", "раз",
    "раза", "тоже", "только", "около", "почти",
}
NEG = {"не", "ни", "нет", "никогда", "никто", "ничего", "никуда", "нигде", "никак"}

NUM_WORDS = {
    "ноль": 0, "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "сто": 100,
}
FIRST_PERSON = {"я", "мне", "меня", "мной", "мною", "мой", "моя", "мое", "мои", "моего", "моей", "мы", "нас", "нам", "наш", "наша"}
THIRD_PERSON = {"он", "она", "они", "его", "ее", "их", "ему", "ей", "им", "кто-то", "кто-нибудь", "люди", "человек", "все", "тот", "та", "те"}
FILLERS = {"э", "ээ", "эээ", "эм", "эмм", "ну", "значит", "короче", "типа", "как бы"}

_WORD = re.compile(r"[a-zа-я0-9]+(?:[-:.][a-zа-я0-9]+)*", re.I)


def tokens(text: str) -> list[str]:
    return _WORD.findall(norm(text))


def extract_numbers(text: str) -> set[str]:
    toks = tokens(text)
    out: set[str] = set()
    prev_tens = None
    for tok in toks:
        if re.fullmatch(r"\d+(?:[:.]\d+)?", tok):
            out.add(re.sub(r"^0+(?=\d)", "", tok))
            prev_tens = None
        elif tok in NUM_WORDS:
            v = NUM_WORDS[tok]
            if prev_tens is not None and v < 10:
                out.discard(str(prev_tens))
                out.add(str(prev_tens + v))
                prev_tens = None
            else:
                out.add(str(v))
                prev_tens = v if (v >= 20 and v % 10 == 0 and v < 100) else None
        else:
            prev_tens = None
    return out


def content_stems(text: str) -> set[str]:
    out: set[str] = set()
    for tok in tokens(text):
        if len(tok) < 3 or tok in STOP or tok in NEG or tok in NUM_WORDS or re.fullmatch(r"[\d:.\-]+", tok):
            continue
        out.add(tok[:5])
    return out


def has_negation(text: str) -> bool:
    return any(t in NEG for t in tokens(text))


@dataclass
class Cue:
    id: str
    label: str
    weight: float
    detail: str
    kind: str  # text | logic | voice

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Baseline:
    maxlen: int = 20
    data: dict = field(default_factory=dict)

    def add(self, name: str, value: float | None) -> None:
        if value is None:
            return
        arr = self.data.setdefault(name, [])
        arr.append(float(value))
        if len(arr) > self.maxlen:
            arr.pop(0)

    def n(self, name: str) -> int:
        return len(self.data.get(name, []))

    def mean(self, name: str) -> float | None:
        arr = self.data.get(name)
        return mean(arr) if arr else None

    def std(self, name: str) -> float:
        arr = self.data.get(name)
        return pstdev(arr) if arr and len(arr) > 1 else 0.0


class DeceptionTracker:
    """Накапливает «обычное» поведение собеседника и ищет отклонения."""

    MIN_BASE = 4

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.base = _Baseline()
        self.statements: list[dict] = []
        self.index = 0.0

    # ------------------------------------------------------------------ основной вход
    def process(self, utt: dict) -> tuple[list[Cue], float]:
        """utt: id, text, features (может быть пустым). Возвращает (маркеры, индекс 0..1)."""
        text = utt.get("text", "")
        feats = utt.get("features") or {}
        toks = tokens(text)
        nwords = len(toks)
        cues: list[Cue] = []

        # --- текстовые
        for h in find_hits(text, groups=("dec",)):
            cues.append(Cue(h["id"], h["label"], h["weight"], f"«{h['snippet']}»", "text"))

        if nwords >= 3 and not text.strip().endswith("?"):
            # --- логические противоречия
            for kind, prev in self._contradictions(text):
                if kind == "numeric":
                    cues.append(Cue("contradiction_num", "Разные цифры", 0.5,
                                    f"Раньше: «{prev['text']}»", "logic"))
                else:
                    cues.append(Cue("contradiction_pol", "«Да» ↔ «нет»", 0.45,
                                    f"Раньше: «{prev['text']}»", "logic"))
                if len([c for c in cues if c.kind == "logic"]) >= 2:
                    break
            self.statements.append({
                "id": utt.get("id"), "text": text, "stems": content_stems(text),
                "nums": extract_numbers(text), "neg": has_negation(text),
            })
            self.statements = self.statements[-40:]

        # --- поведение относительно базовой линии
        if nwords >= 3:
            cues.extend(self._voice_and_style(feats, toks))
            self._update_baseline(feats, toks)
        elif feats.get("latency") is not None:
            self.base.add("latency", feats["latency"])

        p = 1.0
        for c in cues:
            p *= (1.0 - c.weight)
        raw = 1.0 - p
        self.index = max(raw, self.index * 0.8)
        return cues, min(1.0, self.index)

    # --------------------------------------------------------------- внутренние
    def _contradictions(self, text: str) -> list[tuple[str, dict]]:
        stems, nums, neg = content_stems(text), extract_numbers(text), has_negation(text)
        out = []
        if len(stems) < 2:
            return out
        for prev in reversed(self.statements):
            ps = prev["stems"]
            if len(ps) < 2:
                continue
            inter = len(stems & ps)
            union = len(stems | ps)
            j = inter / union if union else 0
            overlap = inter / min(len(stems), len(ps))
            if inter >= 2 and overlap >= 0.75 and nums and prev["nums"] and nums.isdisjoint(prev["nums"]):
                out.append(("numeric", prev))
            elif j >= 0.6 and neg != prev["neg"]:
                out.append(("polarity", prev))
        return out[:2]

    def _voice_and_style(self, feats: dict, toks: list[str]) -> list[Cue]:
        cues: list[Cue] = []
        b = self.base
        n = len(toks)

        lat = feats.get("latency")
        if lat is not None and lat >= 2.0:
            m = b.mean("latency")
            if b.n("latency") < 3 or (m and lat >= 2 * m):
                cues.append(Cue("latency", "Долгая пауза перед ответом", 0.2,
                                f"{lat:.1f} с" + (f" (обычно ≈{m:.1f} с)" if m else ""), "voice"))

        wps = feats.get("wps")
        if wps and b.n("wps") >= self.MIN_BASE:
            m = b.mean("wps")
            if m and abs(wps - m) / m >= 0.35:
                cues.append(Cue("rate", "Темп речи изменился", 0.15,
                                f"{wps:.1f} сл/с (обычно ≈{m:.1f})", "voice"))

        f0 = feats.get("f0_mean")
        if f0 and b.n("f0_mean") >= self.MIN_BASE:
            m = b.mean("f0_mean")
            if m and abs(f0 - m) / m >= 0.12:
                cues.append(Cue("pitch", "Высота голоса сместилась", 0.15,
                                f"{f0:.0f} Гц (обычно ≈{m:.0f})", "voice"))
        f0s = feats.get("f0_std")
        if f0s and b.n("f0_std") >= self.MIN_BASE:
            m = b.mean("f0_std")
            if m and f0s >= 1.5 * m and f0s > 15:
                cues.append(Cue("pitch_var", "Интонация «скачет»", 0.1,
                                f"разброс {f0s:.0f} Гц (обычно ≈{m:.0f})", "voice"))

        fr = self._filler_rate(toks)
        if b.n("filler_rate") >= self.MIN_BASE:
            m = b.mean("filler_rate")
            if m is not None and fr > m + 0.08 and fr * n >= 2:
                cues.append(Cue("fillers", "Больше слов-паразитов", 0.12,
                                f"{fr*100:.0f}% (обычно ≈{m*100:.0f}%)", "voice"))

        if b.n("words") >= self.MIN_BASE:
            m = b.mean("words")
            if m and n >= max(25, 2 * m):
                cues.append(Cue("verbose", "Ответ гораздо длиннее обычного", 0.2,
                                f"{n} слов (обычно ≈{m:.0f}) — «эффект Пиноккио»", "text"))

        if n >= 8 and b.n("fp_ratio") >= self.MIN_BASE:
            m = b.mean("fp_ratio")
            third = sum(1 for t in toks if t in THIRD_PERSON)
            if m and m >= 0.03 and self._fp_ratio(toks) <= 0.5 * m and third >= 2:
                cues.append(Cue("distancing", "Меньше «я», больше третьих лиц", 0.15,
                                "по Воссу, так иногда выглядит дистанцирование", "text"))
        return cues

    def _update_baseline(self, feats: dict, toks: list[str]) -> None:
        b = self.base
        b.add("words", len(toks))
        b.add("wps", feats.get("wps"))
        b.add("f0_mean", feats.get("f0_mean"))
        b.add("f0_std", feats.get("f0_std"))
        b.add("filler_rate", self._filler_rate(toks))
        b.add("fp_ratio", self._fp_ratio(toks))
        b.add("latency", feats.get("latency"))

    @staticmethod
    def _filler_rate(toks: list[str]) -> float:
        return sum(1 for t in toks if t in FILLERS) / max(1, len(toks))

    @staticmethod
    def _fp_ratio(toks: list[str]) -> float:
        return sum(1 for t in toks if t in FIRST_PERSON) / max(1, len(toks))


def index_label(index: float) -> str:
    if index < 0.2:
        return "ровно"
    if index < 0.5:
        return "есть что уточнить"
    return "много маркеров — уточняй"
