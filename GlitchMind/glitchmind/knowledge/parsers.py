"""Извлечение текста из книг: txt/md, fb2, epub, docx, pdf, html."""
from __future__ import annotations

import html
import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from urllib.parse import unquote

SUPPORTED = {".txt", ".md", ".fb2", ".epub", ".docx", ".pdf", ".html", ".htm"}
MAX_BYTES = 200 * 1024 * 1024


class ParseError(Exception):
    pass


def decode_bytes(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "blockquote"}
    SKIP = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(src: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(src)
        p.close()
    except Exception:  # noqa: BLE001 - кривой HTML не должен ронять импорт
        pass
    return "".join(p.parts)


def parse_fb2(data: bytes) -> str:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return html_to_text(decode_bytes(data))
    out: list[str] = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            out.append("".join(el.itertext()).strip())
        elif tag in ("title", "subtitle", "section", "empty-line"):
            out.append("")
    return "\n".join(out)


def parse_epub(data: bytes) -> str:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ParseError("Файл EPUB повреждён") from e
    names = set(z.namelist())
    order: list[str] = []
    try:
        container = z.read("META-INF/container.xml").decode("utf-8", "replace")
        opf = re.search(r'full-path="([^"]+)"', container).group(1)
        root = ET.fromstring(z.read(opf))
        base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
        manifest, spine = {}, []
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "item":
                manifest[el.get("id")] = el.get("href")
            elif tag == "itemref":
                spine.append(el.get("idref"))
        for idref in spine:
            href = manifest.get(idref)
            if href:
                order.append(posixpath.normpath(base + unquote(href)))
    except Exception:  # noqa: BLE001
        order = []
    if not order:
        order = sorted(n for n in names if n.lower().endswith((".xhtml", ".html", ".htm")))
    texts = [html_to_text(decode_bytes(z.read(n))) for n in order if n in names]
    return "\n\n".join(texts)


def parse_docx(data: bytes) -> str:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError) as e:
        raise ParseError("Файл DOCX повреждён") from e
    out = []
    for para in xml.split("</w:p>"):
        texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S)
        if texts:
            out.append(html.unescape("".join(texts)))
    return "\n".join(out)


def parse_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise ParseError("Для PDF нужен пакет pypdf") from e
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages]
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"Не удалось прочитать PDF: {e}") from e
    text = "\n\n".join(pages)
    if len(text.strip()) < 200:
        raise ParseError("В PDF нет текстового слоя (похоже на скан). Нужна версия с текстом или OCR.")
    return text


def extract_text(name: str, data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise ParseError("Файл слишком большой (лимит 200 МБ)")
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext not in SUPPORTED:
        raise ParseError(f"Формат {ext or '?'} не поддерживается. Подойдут: {', '.join(sorted(SUPPORTED))}")
    if ext in (".txt", ".md"):
        return decode_bytes(data)
    if ext == ".fb2":
        return parse_fb2(data)
    if ext == ".epub":
        return parse_epub(data)
    if ext == ".docx":
        return parse_docx(data)
    if ext == ".pdf":
        return parse_pdf(data)
    return html_to_text(decode_bytes(data))
