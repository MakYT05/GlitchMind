# -*- mode: python ; coding: utf-8 -*-
# Сборка: pyinstaller --noconfirm --clean glitchmind.spec   (или просто запусти build.bat)
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("glitchmind/ui", "glitchmind/ui")]
binaries = []
hiddenimports = collect_submodules("glitchmind")

# Пакеты с бинарниками/данными, которые PyInstaller сам не всегда находит.
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers",
            "huggingface_hub", "webview", "pyaudiowpatch", "pypdf"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:  # пакет не установлен — пропускаем (selftest покажет)
        print(f"[spec] пропускаю {pkg}: {e}")

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "pandas", "IPython", "pytest", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="GlitchMind",
    debug=False, strip=False, upx=False,
    console=False,              # без чёрного окна консоли
    icon="assets/icon.ico",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="GlitchMind")
