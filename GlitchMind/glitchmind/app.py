"""Точка входа: поднимает сервер и открывает окно приложения."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from . import APP_NAME, __version__, paths  # noqa: E402
from .config import SettingsStore  # noqa: E402
from .controller import Controller  # noqa: E402
from .events import EventBus  # noqa: E402
from .knowledge.library import Library  # noqa: E402
from .server import ApiServer  # noqa: E402


def _setup_logging() -> None:
    # В оконной сборке (без консоли) stdout/stderr = None — это ломает tqdm и print в библиотеках.
    logfile = paths.logs_dir() / "app.log"
    stream = open(logfile, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    logging.basicConfig(level=logging.INFO, stream=stream,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def build() -> tuple[Controller, ApiServer, threading.Event]:
    store = SettingsStore()
    bus = EventBus()
    library = Library(paths.library_dir(), paths.index_dir())
    try:
        library.rescan()
    except Exception:  # noqa: BLE001
        logging.getLogger("glitchmind").exception("library rescan failed")
    ctl = Controller(store, bus, library)
    quit_evt = threading.Event()
    srv = ApiServer(ctl, on_quit=quit_evt.set)
    return ctl, srv, quit_evt


def selftest() -> int:
    """Проверка сборки: все ли модули на месте, поднимается ли сервер. Результат — в selftest.txt."""
    frozen = bool(getattr(sys, "frozen", False))
    lines: list[str] = []
    failed = False

    def check(name: str, fn, hard: bool = True) -> None:
        nonlocal failed
        try:
            lines.append(f"OK    {name}: {fn()}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"{'FAIL' if hard else 'WARN'}  {name}: {e!r}")
            failed = failed or hard

    def imp(mod: str):
        def f():
            m = __import__(mod)
            return getattr(m, "__version__", "импортируется")
        return f

    for mod in ("numpy", "faster_whisper", "ctranslate2", "pyaudiowpatch", "webview", "pypdf"):
        check(f"import {mod}", imp(mod), hard=frozen)

    from .analysis.engine import Engine, parse_dialog
    from .knowledge import kb

    def eng():
        e = Engine()
        items = parse_dialog("Он: Если ты опять опоздаешь, то я больше не буду с тобой разговаривать.")
        for u in items:
            u.update(e.analyze_utterance(u))
        a = e.build_advice(items)
        assert a["replies"], "нет вариантов ответа"
        return f"{len(a['replies'])} вариантов ответа"

    check("книги (конспекты)", lambda: f"{len(kb.books())} книг, {kb.technique_count()} приёмов")
    check("движок анализа", eng)
    check("устройства звука", lambda: __import__("glitchmind.audio.capture", fromlist=["x"]).list_devices()["backend"],
          hard=False)

    ctl, srv, _ = build()

    def http():
        srv.start()
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/state", headers={"X-Token": srv.token})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
        return f"порт {srv.port}"

    check("локальный сервер", http)

    def ui():
        for name in ("index.html", "style.css", "app.js"):
            f = paths.resource_dir() / "ui" / name
            assert f.is_file(), f"нет {f}"
        return str(paths.resource_dir() / "ui")

    check("файлы интерфейса", ui)
    srv.stop()
    ctl.shutdown()
    lines.insert(0, f"{APP_NAME} {__version__} selftest ({'frozen' if frozen else 'source'})")
    out = "\n".join(lines)
    try:
        (paths.data_dir() / "selftest.txt").write_text(out, encoding="utf-8")
        if frozen:
            (Path(sys.executable).parent / "selftest.txt").write_text(out, encoding="utf-8")
    except OSError:
        pass
    print(out)
    return 1 if failed else 0


def _run_window(url: str) -> bool:
    try:
        import webview  # type: ignore
    except Exception:  # noqa: BLE001
        return False
    try:
        webview.create_window(APP_NAME, url, width=1380, height=900, min_size=(1100, 700),
                              background_color="#05070a")
        kwargs = {"gui": "edgechromium"} if sys.platform == "win32" else {}
        webview.start(**kwargs)
        return True
    except Exception:  # noqa: BLE001
        logging.getLogger("glitchmind").exception("webview failed, fallback to browser")
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=APP_NAME)
    ap.add_argument("--selftest", action="store_true", help="проверка сборки и выход")
    ap.add_argument("--browser", action="store_true", help="открыть в браузере вместо окна")
    ap.add_argument("--no-window", action="store_true", help="только сервер (печатает адрес)")
    args = ap.parse_args(argv)

    _setup_logging()
    if args.selftest:
        return selftest()

    ctl, srv, quit_evt = build()
    srv.start()
    print(f"{APP_NAME} {__version__}: {srv.url}")

    def finish() -> None:
        ctl.shutdown()
        srv.stop()

    try:
        if args.no_window:
            quit_evt.wait()
        elif args.browser or not _run_window(srv.url):
            webbrowser.open(srv.url)
            quit_evt.wait()
    except KeyboardInterrupt:
        pass
    finally:
        finish()
    time.sleep(0.1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
