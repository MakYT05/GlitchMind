"""Доступ к встроенной базе знаний (конспекты пяти книг)."""
from __future__ import annotations

import re

from .books_data import BOOKS

_BY_ID = {b["id"]: b for b in BOOKS}


def books() -> list[dict]:
    return BOOKS


def book(book_id: str) -> dict | None:
    return _BY_ID.get(book_id)


def book_label(book_id: str) -> str:
    b = _BY_ID.get(book_id)
    return f'{b["author"]} — «{b["title"]}»' if b else ""


def short_label(book_id: str) -> str:
    b = _BY_ID.get(book_id)
    return b["author"].split()[-1] if b else ""


def technique(book_id: str, tech_id: str) -> dict | None:
    b = _BY_ID.get(book_id)
    if not b:
        return None
    for t in b["techniques"]:
        if t["id"] == tech_id:
            return t
    return None


def technique_count() -> int:
    return sum(len(b["techniques"]) for b in BOOKS)


def compact_summary(max_what: int = 150) -> str:
    """Короткая сводка для системного промпта ЛЛМ."""
    lines: list[str] = []
    for b in BOOKS:
        lines.append(f'### {b["author"]} — «{b["title"]}»')
        lines.append(b["core"])
        for t in b["techniques"]:
            what = t["what"]
            if len(what) > max_what:
                what = what[: max_what - 1].rstrip() + "…"
            lines.append(f'- {t["name"]}: {what}')
        for d in b.get("deception", []):
            lines.append(f"- (признак лжи) {d}")
    return "\n".join(lines)


def guess_book_from_name(name: str) -> str:
    """Определяет, к какой из пяти книг относится файл, по имени."""
    low = re.sub(r"[_\-.]+", " ", name.lower().replace("ё", "е"))
    best, best_hits = "", 0
    for b in BOOKS:
        hits = sum(1 for k in b["keywords"] if k in low)
        if hits > best_hits:
            best, best_hits = b["id"], hits
    return best
