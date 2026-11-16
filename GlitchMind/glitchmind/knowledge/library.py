"""Библиотека пользовательских книг: индексация и поиск по фрагментам (BM25).

Именно так приложение «знает» книги: полный текст остаётся только на компьютере пользователя,
а в разбор подтягиваются самые подходящие фрагменты.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import Counter
from pathlib import Path

from . import kb
from .parsers import SUPPORTED, ParseError, extract_text

CHUNK_TARGET = 700
CHUNK_MAX = 1100
MAX_CHUNKS_PER_DOC = 60000

_STOP = {
    "и", "в", "во", "на", "с", "со", "к", "по", "о", "об", "от", "до", "из", "за", "для", "что", "это", "как", "так",
    "но", "а", "же", "ли", "бы", "не", "ни", "я", "ты", "он", "она", "мы", "вы", "они", "мне", "меня", "тебе", "его",
    "ее", "их", "все", "вот", "ну", "да", "нет", "там", "тут", "то", "уже", "еще", "был", "была", "было", "были",
    "быть", "очень", "когда", "если", "чтобы", "тоже", "только", "или", "при", "над", "под", "без", "про", "чем",
}
_WORD = re.compile(r"[a-zа-я0-9]+", re.I)


def stem_tokens(text: str) -> list[str]:
    out = []
    for w in _WORD.findall(text.lower().replace("ё", "е")):
        if len(w) < 3 or w in _STOP:
            continue
        out.append(w[:6] if len(w) > 6 else w)
    return out


def chunk_text(text: str) -> list[str]:
    paras = [re.sub(r"[ \t ]+", " ", p).strip() for p in re.split(r"\r?\n", text)]
    paras = [p for p in paras if p]
    chunks: list[str] = []
    cur = ""

    def flush() -> None:
        nonlocal cur
        if cur.strip():
            chunks.append(cur.strip())
        cur = ""

    for p in paras:
        if len(p) > CHUNK_MAX:  # длинный абзац режем по предложениям
            for s in re.split(r"(?<=[.!?…])\s+", p):
                if len(cur) + len(s) + 1 > CHUNK_MAX:
                    flush()
                cur += (" " if cur else "") + s
                if len(cur) >= CHUNK_TARGET:
                    flush()
            continue
        if len(cur) + len(p) + 1 > CHUNK_MAX:
            flush()
        cur += (" " if cur else "") + p
        if len(cur) >= CHUNK_TARGET:
            flush()
    flush()
    return chunks[:MAX_CHUNKS_PER_DOC]


def guess_book(name: str, text: str) -> str:
    g = kb.guess_book_from_name(name)
    if g:
        return g
    head = text[:6000].lower().replace("ё", "е")
    best, best_hits = "", 1  # нужно ≥2 совпадений по ключевым словам
    for b in kb.books():
        hits = sum(1 for k in b["keywords"] if k in head)
        if hits > best_hits:
            best, best_hits = b["id"], hits
    return best


class Library:
    def __init__(self, lib_dir: Path, idx_dir: Path):
        self.lib_dir = Path(lib_dir)
        self.idx_dir = Path(idx_dir)
        self.lib_dir.mkdir(parents=True, exist_ok=True)
        self.idx_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._docs: dict[str, dict] = {}
        self._chunks: list[tuple[str, int]] = []
        self._len: list[int] = []
        self._post: dict[str, list[tuple[int, int]]] = {}
        self._avgdl = 1.0
        self._load()
        self._rebuild()

    # ------------------------------------------------------------------ загрузка
    def _load(self) -> None:
        for f in self.idx_dir.glob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if d.get("id") and d.get("chunks"):
                    self._docs[d["id"]] = d
            except (OSError, ValueError):
                continue

    def _save_doc(self, d: dict) -> None:
        (self.idx_dir / f'{d["id"]}.json').write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    def _rebuild(self) -> None:
        with self._lock:
            self._chunks, self._len, self._post = [], [], {}
            for doc_id, d in self._docs.items():
                for i, ch in enumerate(d["chunks"]):
                    toks = stem_tokens(ch)
                    idx = len(self._chunks)
                    self._chunks.append((doc_id, i))
                    self._len.append(max(1, len(toks)))
                    for tok, tf in Counter(toks).items():
                        self._post.setdefault(tok, []).append((idx, tf))
            self._avgdl = (sum(self._len) / len(self._len)) if self._len else 1.0

    # --------------------------------------------------------------- добавление
    def add_bytes(self, name: str, data: bytes, save_copy: bool = True) -> dict:
        name = Path(name).name
        text = extract_text(name, data)
        chunks = chunk_text(text)
        if not chunks:
            raise ParseError("В файле не найдено текста")
        doc_id = hashlib.sha1(name.encode("utf-8") + str(len(data)).encode()).hexdigest()[:10]
        doc = {
            "id": doc_id, "name": name, "book": guess_book(name, text), "size": len(data),
            "chars": sum(len(c) for c in chunks), "added": time.time(), "chunks": chunks,
        }
        with self._lock:
            if save_copy:
                try:
                    (self.lib_dir / name).write_bytes(data)
                except OSError:
                    pass
            self._docs[doc_id] = doc
            self._save_doc(doc)
            self._rebuild()
        return self._meta(doc)

    def add_path(self, path: str | Path) -> dict:
        p = Path(path)
        return self.add_bytes(p.name, p.read_bytes(), save_copy=p.parent != self.lib_dir)

    def rescan(self) -> dict:
        """Подхватывает книги, которые пользователь просто положил в папку library."""
        added, errors = [], []
        known = {d["name"] for d in self._docs.values()}
        for f in sorted(self.lib_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in SUPPORTED and f.name not in known:
                try:
                    added.append(self.add_path(f)["name"])
                except (ParseError, OSError) as e:
                    errors.append(f"{f.name}: {e}")
        return {"added": added, "errors": errors}

    def remove(self, doc_id: str) -> bool:
        with self._lock:
            d = self._docs.pop(doc_id, None)
            if not d:
                return False
            try:
                (self.idx_dir / f"{doc_id}.json").unlink(missing_ok=True)
                (self.lib_dir / d["name"]).unlink(missing_ok=True)
            except OSError:
                pass
            self._rebuild()
            return True

    def set_book(self, doc_id: str, book_id: str) -> bool:
        with self._lock:
            d = self._docs.get(doc_id)
            if not d or (book_id and not kb.book(book_id)):
                return False
            d["book"] = book_id
            self._save_doc(d)
            return True

    # -------------------------------------------------------------------- чтение
    @staticmethod
    def _meta(d: dict) -> dict:
        return {k: d[k] for k in ("id", "name", "book", "size", "chars", "added")} | {"chunks": len(d["chunks"])}

    def docs(self) -> list[dict]:
        with self._lock:
            return sorted((self._meta(d) for d in self._docs.values()), key=lambda m: m["name"].lower())

    def status(self) -> list[dict]:
        """По каждой из пяти книг: есть ли встроенный конспект и загружена ли пользовательская копия."""
        docs = self.docs()
        out = []
        for b in kb.books():
            mine = [d for d in docs if d["book"] == b["id"]]
            out.append({
                "id": b["id"], "author": b["author"], "title": b["title"], "emoji": b["emoji"], "color": b["color"],
                "notes": len(b["techniques"]), "files": mine, "chunks": sum(d["chunks"] for d in mine),
            })
        other = [d for d in docs if not kb.book(d["book"])]
        if other:
            out.append({"id": "", "author": "Другие книги", "title": "без привязки", "emoji": "📄", "color": "#7a8794",
                        "notes": 0, "files": other, "chunks": sum(d["chunks"] for d in other)})
        return out

    def search(self, query: str, top_k: int = 3, boost_books: list[str] | None = None) -> list[dict]:
        toks = list(dict.fromkeys(stem_tokens(query)))
        if not toks or top_k <= 0:
            return []
        with self._lock:
            n = len(self._chunks)
            if not n:
                return []
            k1, b = 1.5, 0.75
            scores: dict[int, float] = {}
            for t in toks:
                post = self._post.get(t)
                if not post:
                    continue
                df = len(post)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                for idx, tf in post:
                    denom = tf + k1 * (1 - b + b * self._len[idx] / self._avgdl)
                    scores[idx] = scores.get(idx, 0.0) + idf * tf * (k1 + 1) / denom
            if not scores:
                return []
            boost = set(boost_books or [])
            ranked = []
            for idx, s in scores.items():
                doc = self._docs[self._chunks[idx][0]]
                if doc["book"] in boost:
                    s *= 1.25
                ranked.append((s, idx))
            ranked.sort(reverse=True)
            out = []
            for s, idx in ranked[:top_k]:
                doc_id, ci = self._chunks[idx]
                doc = self._docs[doc_id]
                out.append({
                    "doc_id": doc_id, "doc": doc["name"], "book": doc["book"],
                    "text": doc["chunks"][ci], "score": round(s, 3),
                })
            return out
