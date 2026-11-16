import io
import zipfile

import pytest

from glitchmind.config import SettingsStore
from glitchmind.knowledge import parsers
from glitchmind.knowledge.library import Library, chunk_text, stem_tokens
from glitchmind.knowledge.parsers import ParseError

TXT = ("Эмоциональный шантаж строится на страхе, долге и вине. Шантажист давит на чувство вины.\n\n" * 5
       + "Техника заезженной пластинки состоит в спокойном повторении своей позиции.\n\n" * 5)
FB2 = ('<?xml version="1.0" encoding="utf-8"?><FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
       '<body><section><title><p>Глава</p></title><p>Зеркала: повторите последние слова собеседника с вопросительной интонацией.</p>'
       '<p>Калиброванные вопросы начинаются с как и что.</p></section></body></FictionBook>').encode()


def make_docx(text):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("word/document.xml", f"<w:document><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:document>")
    return b.getvalue()


def make_epub(text):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", '<package><manifest><item id="c1" href="ch1.xhtml"/></manifest><spine><itemref idref="c1"/></spine></package>')
        z.writestr("OEBPS/ch1.xhtml", f"<html><head><title>x</title></head><body><p>{text}</p></body></html>")
    return b.getvalue()


@pytest.fixture
def lib(tmp_path):
    return Library(tmp_path / "lib", tmp_path / "idx")


def test_formats_parse():
    assert "Зеркала" in parsers.extract_text("a.fb2", FB2)
    assert "присутствия" in parsers.extract_text("a.docx", make_docx("Харизма из присутствия, силы и теплоты"))
    assert "теплоты" in parsers.extract_text("a.epub", make_epub("Харизма из присутствия, силы и теплоты"))
    html = "<html><script>x=1</script><p>привет</p></html>".encode("utf-8")
    assert "привет" in parsers.extract_text("a.html", html) and "x=1" not in parsers.extract_text("a.html", html)
    assert parsers.extract_text("a.txt", "Привет мир".encode("cp1251")) == "Привет мир"
    assert parsers.extract_text("a.txt", "Привет".encode("utf-16")) == "Привет"


def test_parse_errors():
    with pytest.raises(ParseError):
        parsers.extract_text("a.exe", b"x")
    with pytest.raises(ParseError):
        parsers.extract_text("a.docx", b"not a zip")
    with pytest.raises(ParseError):
        parsers.extract_text("a.epub", b"not a zip")
    with pytest.raises(ParseError):
        parsers.extract_text("a.pdf", b"%PDF-1.4 broken")


def test_chunking_respects_limits():
    text = "\n".join(["Это предложение для проверки. " * 5] * 100)
    chunks = chunk_text(text)
    assert len(chunks) > 5 and all(len(c) <= 1300 for c in chunks)
    long_par = "Слово. " * 600
    assert all(len(c) <= 1300 for c in chunk_text(long_par))
    assert chunk_text("   \n\n  ") == []


def test_stemming_matches_inflections():
    assert set(stem_tokens("чувство вины")) & set(stem_tokens("чувством вину"))
    assert stem_tokens("и в на") == []


def test_add_and_search_and_attribution(lib):
    m = lib.add_bytes("Форвард_Эмоциональный_шантаж.txt", TXT.encode("cp1251"))
    assert m["book"] == "forward" and m["chunks"] >= 1
    lib.add_bytes("Восс.fb2", FB2)
    lib.add_bytes("cabane.docx", make_docx("Харизма состоит из присутствия, силы и теплоты."))
    r = lib.search("как справиться с чувством вины шантажист", 2)
    assert r and r[0]["doc"].startswith("Форвард") and r[0]["book"] == "forward"
    assert lib.search("зеркала повторить слова", 1)[0]["doc"] == "Восс.fb2"
    assert lib.search("присутствие сила теплота", 1)[0]["doc"] == "cabane.docx"
    assert lib.search("абракадабра", 3) == []
    assert lib.search("вина", 0) == []


def test_book_boost_prefers_flagged_book(lib):
    lib.add_bytes("a.txt", "Вина и обида в отношениях, вина мешает.".encode())
    lib.add_bytes("b.txt", "Вина и обида в отношениях, вина мешает.".encode())
    ids = {d["name"]: d["id"] for d in lib.docs()}
    lib.set_book(ids["b.txt"], "forward")
    assert lib.search("вина обида", 1, boost_books=["forward"])[0]["doc"] == "b.txt"


def test_status_lists_five_books_and_other(lib):
    lib.add_bytes("Смит.txt", TXT.encode())
    lib.add_bytes("мусор.txt", "Совсем другая книга про кошек и собак и котят.".encode())
    st = lib.status()
    assert [s["id"] for s in st[:5]] == ["smith", "forward", "thompson", "voss", "cabane"]
    assert st[0]["chunks"] >= 1
    assert st[-1]["id"] == "" and st[-1]["files"][0]["name"] == "мусор.txt"


def test_persistence_remove_and_rescan(tmp_path):
    a = Library(tmp_path / "lib", tmp_path / "idx")
    a.add_bytes("Томпсон.txt", TXT.encode())
    b = Library(tmp_path / "lib", tmp_path / "idx")  # новый экземпляр читает индекс с диска
    assert b.search("пластинки позиции", 1)
    (tmp_path / "lib" / "Восс_extra.txt").write_text("Калиброванные вопросы на как и что.", encoding="utf-8")
    r = b.rescan()
    assert r["added"] == ["Восс_extra.txt"] and r["errors"] == []
    (tmp_path / "lib" / "bad.docx").write_bytes(b"junk")
    assert b.rescan()["errors"]
    doc = next(d for d in b.docs() if d["name"] == "Томпсон.txt")
    assert b.remove(doc["id"]) and not b.remove(doc["id"])
    assert not (tmp_path / "lib" / "Томпсон.txt").exists()


def test_set_book_validates(lib):
    m = lib.add_bytes("x.txt", TXT.encode())
    assert lib.set_book(m["id"], "voss") and not lib.set_book(m["id"], "nope") and not lib.set_book("zzz", "voss")


def test_empty_file_rejected(lib):
    with pytest.raises(ParseError):
        lib.add_bytes("empty.txt", b"   \n\n ")


def test_settings_store(tmp_path):
    s = SettingsStore(tmp_path / "c.json")
    assert s.get().language == "ru"
    s.update({"vad_sensitivity": 9, "rag_top_k": "5", "use_mic": "false", "llm_model": "qwen2.5:7b", "bogus": 1})
    cur = s.get()
    assert cur.vad_sensitivity == 1.0 and cur.rag_top_k == 5 and cur.use_mic is False and cur.llm_model == "qwen2.5:7b"
    s.update({"llm_api_key": "secret"})
    s.update({"llm_api_key": ""})  # пустое поле формы не стирает ключ
    assert s.get().llm_api_key == "secret"
    pub = s.public()
    assert "llm_api_key" not in pub and pub["llm_api_key_set"] is True
    s.update({"llm_api_key": "__clear__"})
    assert s.get().llm_api_key == ""
    again = SettingsStore(tmp_path / "c.json")  # сохраняется на диск
    assert again.get().llm_model == "qwen2.5:7b"
    (tmp_path / "c.json").write_text("{broken", encoding="utf-8")
    assert SettingsStore(tmp_path / "c.json").get().language == "ru"
