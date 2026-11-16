import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

# изолированная папка данных — до импорта пакета
_HOME = tempfile.mkdtemp(prefix="glitchmind-test-")
os.environ["GLITCHMIND_HOME"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glitchmind.audio.dsp import SR  # noqa: E402


def speech(sec: float, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(sec * SR)) / SR
    return (0.15 * np.sin(2 * np.pi * 180 * t) * (1 + 0.5 * np.sin(2 * np.pi * 3 * t))
            + 0.02 * rng.standard_normal(t.size)).astype(np.float32)


def silence(sec: float, seed: int = 2) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (0.002 * rng.standard_normal(int(sec * SR))).astype(np.float32)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GLITCHMIND_HOME", str(tmp_path))
    return tmp_path
