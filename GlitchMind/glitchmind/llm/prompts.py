"""Промпты для языковой модели и нормализация её ответа."""
from __future__ import annotations

import time
from functools import lru_cache

from ..knowledge import kb

SCHEMA = """{
  "situation": "1–2 предложения: что происходит в разговоре",
  "their_goal": "чего добивается собеседник (или пустая строка)",
  "dynamics": [{"label": "название приёма/паттерна", "book": "автор книги", "why": "как проявляется и в чём механизм"}],
  "replies": [{"style": "Мягко | Твёрдо | Тактически | Границы", "technique": "название приёма", "book": "автор книги", "text": "готовая реплика для пользователя", "why": "почему сработает"}],
  "questions": ["калиброванный вопрос на «как» или «что», который стоит задать"],
  "avoid": ["чего лучше не делать"],
  "self_coaching": ["замечание о РЕЧИ пользователя, если есть что улучшить"],
  "deception": ["что стоит уточнить и каким вопросом (если есть признаки несоответствий)"],
  "safety": "предупреждение, если в разговоре звучит угроза жизни/самоповреждение, иначе пустая строка"
}"""

SYSTEM_HEAD = f"""Ты — GlitchMind: аналитик и коуч по общению, переговорам и защите границ. Ты помогаешь ОДНОМУ человеку — пользователю (в диалоге он помечен «Я») — в его реальных разговорах. Собеседник помечен «Собеседник».

Твоя опора — пять книг (конспект ниже): Мануэль Смит «Тренинг уверенности в себе», Сьюзен Форвард «Эмоциональный шантаж», Джордж Томпсон «Вербальное дзюдо», Крисс Восс «Никаких компромиссов», Оливия Фокс Кобейн «Харизма».

Правила:
1. Пиши по-русски, коротко и по делу. Реплики для пользователя — готовые к произнесению, 1–3 предложения. Обращайся к собеседнику на «ты», а если в диалоге «вы» — на «вы». Избегай форм прошедшего времени от первого лица, где это можно (род пользователя неизвестен).
2. Дай 3–4 варианта ответа в РАЗНЫХ стилях (Мягко, Твёрдо, Тактически, Границы), у каждого укажи приём и книгу.
3. Никогда не утверждай, что собеседник лжёт. Признаки несоответствий — только как «стоит уточнить»; предложи конкретный вопрос. Помни: надёжного «признака лжи» не существует.
4. Если манипуляции в разговоре нет — так и скажи. Не выдумывай приёмы, которых нет в реплике.
5. Если звучат слова о самоповреждении или реальной опасности — заполни поле safety: рекомендуй спокойно спросить прямо, не оставлять человека одного и при угрозе жизни обратиться в экстренные службы (в России 112).
6. Фрагменты книг пользователя, если они приведены, используй как опору: не цитируй длинно, пересказывай своими словами и называй книгу.
7. Отвечай ТОЛЬКО одним JSON-объектом без пояснений и без markdown, строго такой формы:
{SCHEMA}
"""


@lru_cache(maxsize=1)
def system_prompt() -> str:
    return SYSTEM_HEAD + "\n=== КОНСПЕКТ КНИГ ===\n" + kb.compact_summary()


def format_transcript(transcript: list[dict], limit: int = 14) -> str:
    lines = []
    for u in transcript[-limit:]:
        who = "Я" if u.get("speaker") == "me" else "Собеседник"
        tags = [h["label"] for h in u.get("hits", []) if h.get("group") != "coach"]
        tags += [c["label"] for c in u.get("cues", [])]
        feats = u.get("features") or {}
        extra = []
        if feats.get("latency") is not None:
            extra.append(f"пауза перед ответом {feats['latency']:.1f} с")
        suffix = ""
        if tags or extra:
            suffix = "  [авто-пометки: " + "; ".join(tags + extra) + "]"
        lines.append(f"[{who}] {u['text']}{suffix}")
    return "\n".join(lines)


def _passages_block(passages: list[dict]) -> str:
    if not passages:
        return ""
    out = ["", "Фрагменты из книг пользователя (для опоры, не цитируй длинно):"]
    for i, p in enumerate(passages, 1):
        b = kb.book(p.get("book", ""))
        src = f'{b["author"]} — «{b["title"]}»' if b else p.get("doc", "книга")
        text = p["text"]
        if len(text) > 650:
            text = text[:647] + "…"
        out.append(f"[{i}] ({src}) {text}")
    return "\n".join(out)


def build_live_messages(transcript: list[dict], passages: list[dict], note: str = "") -> list[dict]:
    user = "Диалог (последние реплики):\n" + format_transcript(transcript)
    user += _passages_block(passages)
    user += "\n\nЗадача: разбери ситуацию и подскажи, как лучше ответить пользователю СЕЙЧАС."
    if note.strip():
        user += f"\nПожелание пользователя: {note.strip()}"
    return [{"role": "system", "content": system_prompt()}, {"role": "user", "content": user}]


def build_situation_messages(text: str, passages: list[dict], parsed: list[dict] | None = None) -> list[dict]:
    user = "Пользователь описал ситуацию или вставил переписку/диалог:\n\"\"\"\n" + text.strip()[:6000] + "\n\"\"\""
    if parsed:
        tags = []
        for u in parsed:
            for h in u.get("hits", []):
                if h.get("group") != "coach" and h["label"] not in tags:
                    tags.append(h["label"])
        if tags:
            user += "\nАвто-пометки по ключевым фразам: " + "; ".join(tags)
    user += _passages_block(passages)
    user += "\n\nЗадача: разбери ситуацию, назови приёмы и динамику, предложи варианты ответа и вопросы для уточнения."
    return [{"role": "system", "content": system_prompt()}, {"role": "user", "content": user}]


# ------------------------------------------------------------------ нормализация
def _lst(x, limit: int) -> list[str]:
    if isinstance(x, str):
        x = [x] if x.strip() else []
    if not isinstance(x, list):
        return []
    return [str(i).strip() for i in x if str(i).strip()][:limit]


def _book_id(name: str) -> str:
    low = (name or "").lower().replace("ё", "е")
    for b in kb.books():
        last = b["author"].split()[-1].lower().replace("ё", "е")
        if last[:5] in low or b["title"].lower()[:8] in low:
            return b["id"]
    if "кабейн" in low or "cabane" in low:
        return "cabane"
    return ""


def normalize_advice(d: dict, model: str = "") -> dict:
    replies = []
    for r in d.get("replies") or []:
        if isinstance(r, dict) and str(r.get("text", "")).strip():
            replies.append({
                "style": str(r.get("style") or "Тактически").strip()[:20],
                "technique": str(r.get("technique") or "").strip()[:80],
                "book": _book_id(str(r.get("book") or "")),
                "text": str(r["text"]).strip(),
                "why": str(r.get("why") or "").strip(),
            })
        elif isinstance(r, str) and r.strip():
            replies.append({"style": "Тактически", "technique": "", "book": "", "text": r.strip(), "why": ""})
    dynamics = []
    for x in d.get("dynamics") or []:
        if isinstance(x, dict) and (x.get("label") or x.get("why")):
            dynamics.append({"label": str(x.get("label") or "").strip(), "emoji": "🧩",
                             "book": _book_id(str(x.get("book") or "")), "why": str(x.get("why") or "").strip()})
        elif isinstance(x, str) and x.strip():
            dynamics.append({"label": x.strip(), "emoji": "🧩", "book": "", "why": ""})
    safety = str(d.get("safety") or "").strip()
    return {
        "source": "llm", "model": model,
        "situation": str(d.get("situation") or "").strip(),
        "their_goal": str(d.get("their_goal") or "").strip(),
        "dynamics": dynamics[:5],
        "replies": replies[:6],
        "questions": _lst(d.get("questions"), 4),
        "avoid": _lst(d.get("avoid"), 5),
        "self_coaching": _lst(d.get("self_coaching"), 3),
        "deception": _lst(d.get("deception"), 5),
        "safety": safety or None,
        "ts": time.time(),
    }
