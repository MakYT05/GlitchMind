"""Движок анализа без языковой модели: быстрые подсказки по правилам.

Работает мгновенно и офлайн. Если подключена языковая модель, её более глубокий разбор
приходит следом и заменяет эти подсказки.
"""
from __future__ import annotations

import re
import time

from . import patterns as pt
from . import replies as rp
from .deception import DeceptionTracker, index_label

PRESSURE_IDS = {"fear_threat", "pressure", "attack", "self_punish"}
DECAY = (1.0, 0.8, 0.6, 0.45, 0.3, 0.2)
ADVICE_DECAY = (1.0, 0.7, 0.5, 0.35)

SAFETY_TEXT = (
    "⚠️ Прозвучали слова о вреде для себя. Не пропускай их мимо ушей: спокойно спроси прямо, "
    "всё ли в безопасности. Если угроза жизни реальна — экстренные службы (в России 112), "
    "и не оставляй человека одного."
)

DECEPTION_DISCLAIMER = (
    "Это повод уточнить, а не доказательство лжи: те же признаки бывают у честных людей при волнении."
)


def parse_dialog(text: str) -> list[dict]:
    """Разбивает свободный текст на реплики. Префиксы «Я:» и «Он/Она/Собеседник:» задают говорящего."""
    items: list[dict] = []
    for i, raw in enumerate(l for l in text.splitlines() if l.strip()):
        m = re.match(r"^\s*(я|me|мой ответ)\s*[:\-—]\s*(.*)$", raw, re.I)
        if m:
            speaker, body = "me", m.group(2)
        else:
            m = re.match(r"^\s*(он|она|они|собеседник|им|them|начальник|клиент|партнер|партнёр)\s*[:\-—]\s*(.*)$", raw, re.I)
            speaker, body = ("them", m.group(2)) if m else ("them", raw.strip())
        if body.strip():
            items.append({"id": i + 1, "speaker": speaker, "text": body.strip(), "features": {}})
    return items


class Engine:
    def __init__(self) -> None:
        self.tracker = DeceptionTracker()

    def reset(self) -> None:
        self.tracker.reset()

    # -------------------------------------------------------------- реплика
    def analyze_utterance(self, u: dict) -> dict:
        speaker, text = u.get("speaker", "them"), u.get("text", "")
        if speaker == "me":
            return {"hits": pt.find_hits(text, groups=pt.GROUPS_ME), "cues": [],
                    "deception_index": round(self.tracker.index, 3), "safety": False}
        groups = tuple(g for g in pt.GROUPS_THEM if g != "dec")
        hits = pt.find_hits(text, groups=groups)
        cues, index = self.tracker.process(u)
        return {
            "hits": hits,
            "cues": [c.to_dict() for c in cues],
            "deception_index": round(index, 3),
            "safety": pt.safety_flag(text),
        }

    # --------------------------------------------------------------- метры
    def meters(self, transcript: list[dict]) -> dict:
        them = [u for u in transcript if u.get("speaker") == "them"][-6:][::-1]
        pressure = manip = 1.0
        for u, d in zip(them, DECAY):
            for h in u.get("hits", []):
                w = h["weight"] * d
                if h["id"] in PRESSURE_IDS:
                    pressure *= (1 - w)
                if h["group"] in ("manip", "pressure"):
                    manip *= (1 - w)
        return {
            "pressure": round(100 * (1 - pressure)),
            "manip": round(100 * (1 - manip)),
            "deception": round(100 * self.tracker.index),
            "deception_label": index_label(self.tracker.index),
        }

    # -------------------------------------------------------------- советы
    def build_advice(self, transcript: list[dict], note: str = "") -> dict:
        them = [u for u in transcript if u.get("speaker") == "them"][::-1]
        me = [u for u in transcript if u.get("speaker") == "me"][::-1]

        scores: dict[str, float] = {}
        for u, d in zip(them[:4], ADVICE_DECAY):
            for h in u.get("hits", []):
                if h["id"] in rp.CATEGORIES:
                    scores[h["id"]] = scores.get(h["id"], 0.0) + h["weight"] * d
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        top = [cid for cid, s in ranked if s >= 0.3][:2]

        # эмоции в последней реплике собеседника
        emotion_ids: list[str] = []
        if them:
            emotion_ids = [h["id"] for h in them[0].get("hits", []) if h["group"] == "emotion"]

        phrases = [rp.CATEGORIES[c]["phrase"] for c in top]
        if phrases:
            situation = "Собеседник " + " и ".join(phrases) + "."
        elif them:
            situation = "Явных манипулятивных приёмов не видно — это спокойная реплика."
        else:
            situation = "Пока нет реплик собеседника для анализа."
        if emotion_ids:
            situation += " Эмоция: " + pt.pattern(emotion_ids[0]).label.lower() + "."

        dynamics = []
        for c in top:
            p = pt.pattern(c)
            dynamics.append({"label": p.label, "emoji": p.emoji, "book": p.book,
                             "why": rp.CATEGORIES[c]["dynamic"]})
        their_goal = rp.CATEGORIES[top[0]]["goal"] if top else ""

        replies: list[dict] = []
        if top:
            replies += list(rp.CATEGORIES[top[0]]["replies"])[:4]
            if len(top) > 1:
                have = {r["technique"] for r in replies}
                for r in rp.CATEGORIES[top[1]]["replies"]:
                    if r["technique"] not in have:
                        replies.append(r)
                        break
        if emotion_ids and emotion_ids[0] in rp.EMOTION_LABELS:
            label, question = rp.EMOTION_LABELS[emotion_ids[0]]
            replies.append(rp.R("Тактически", "Метка эмоции", "voss", f"{label} {question}",
                                "Назвать эмоцию и замолчать — один из главных приёмов тактической эмпатии."))
        if not replies and them:
            replies = self._general_replies(them[0]["text"])

        avoid: list[str] = []
        questions: list[str] = []
        for c in top:
            avoid += rp.CATEGORIES[c]["avoid"]
            questions += rp.CATEGORIES[c]["questions"]
        if not top and them:
            avoid, questions = list(rp.GENERAL_AVOID), list(rp.GENERAL_QUESTIONS)

        coaching: list[str] = []
        for u in me[:3]:
            for h in u.get("hits", []):
                tip = rp.COACH_TIPS.get(h["id"])
                if tip and tip not in coaching:
                    coaching.append(tip)

        deception: list[str] = []
        for u in them[:2]:
            for c in u.get("cues", []):
                line = f'{c["label"]} — {c["detail"]}'
                if line not in deception:
                    deception.append(line)
        if deception:
            deception.append(DECEPTION_DISCLAIMER)

        safety = SAFETY_TEXT if any(u.get("safety") for u in them[:3]) else None

        return {
            "source": "rules",
            "situation": situation,
            "dynamics": dynamics,
            "their_goal": their_goal,
            "replies": replies[:6],
            "avoid": self._uniq(avoid)[:5],
            "questions": self._uniq(questions)[:4],
            "self_coaching": coaching[:3],
            "deception": deception[:6],
            "safety": safety,
            "meters": self.meters(transcript),
            "sources": [],
            "ts": time.time(),
            "note": note,
        }

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _uniq(items: list[str]) -> list[str]:
        seen, out = set(), []
        for x in items:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    @staticmethod
    def _general_replies(last_text: str) -> list[dict]:
        words = re.findall(r"[\w\-]+", last_text.strip(), re.UNICODE)
        mirror = " ".join(words[-3:]) if len(words) >= 3 else " ".join(words)
        quote = last_text.strip()
        sentences = re.split(r"(?<=[.!?…])\s+", quote)
        quote = (sentences[-1] if sentences else quote).strip().rstrip(".!?…")
        if len(quote) > 110:
            quote = quote[:107].rstrip() + "…"
        out = []
        for r in rp.GENERAL_REPLIES:
            r = dict(r)
            r["text"] = r["text"].replace("{mirror}", mirror).replace("{quote}", quote)
            out.append(r)
        return out
