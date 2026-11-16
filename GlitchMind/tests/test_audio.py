import numpy as np

from glitchmind.audio.asr import FakeASR, clean_text
from glitchmind.audio.dsp import SR, StreamResampler, estimate_f0, to_mono, utterance_features
from glitchmind.audio.vad import Segmenter

from conftest import silence, speech


def peak_hz(x, sr=SR):
    return np.argmax(np.abs(np.fft.rfft(x))) * sr / len(x)


def test_resampler_48k_preserves_tone_and_length():
    sr = 48000
    x = (0.5 * np.sin(2 * np.pi * 440 * np.arange(sr * 2) / sr)).astype(np.float32)
    rs = StreamResampler(sr)
    out = np.concatenate([rs.process(x[i:i + 1440]) for i in range(0, len(x), 1440)])
    assert abs(len(out) - 2 * SR) <= 2
    assert abs(peak_hz(out) - 440) < 2


def test_resampler_44k_odd_chunks():
    x = (0.5 * np.sin(2 * np.pi * 300 * np.arange(44100) / 44100)).astype(np.float32)
    rs = StreamResampler(44100)
    out = np.concatenate([rs.process(x[i:i + 1323]) for i in range(0, len(x), 1323)])
    assert abs(len(out) - SR) <= 2
    assert abs(peak_hz(out) - 300) < 2


def test_resampler_passthrough_and_empty():
    rs = StreamResampler(SR)
    a = np.ones(10, dtype=np.float32)
    assert np.array_equal(rs.process(a), a)
    assert StreamResampler(48000).process(np.zeros(0, dtype=np.float32)).size == 0


def test_to_mono():
    st = np.array([1, 3, 2, 4, 5], dtype=np.float32)
    assert list(to_mono(st, 2)) == [2.0, 3.0]  # хвостовой неполный кадр отбрасывается
    assert to_mono(st, 1) is st


def test_f0_estimate():
    t = np.arange(SR) / SR
    v = (0.3 * np.sin(2 * np.pi * 200 * t) + 0.1 * np.sin(2 * np.pi * 400 * t)).astype(np.float32)
    f0 = estimate_f0(v)
    assert f0.size > 10 and abs(f0.mean() - 200) < 6
    assert estimate_f0(silence(1.0)).size == 0


def test_utterance_features_keys():
    f = utterance_features(speech(2.0), 6)
    assert {"duration", "wps", "f0_mean", "f0_std", "pauses", "pause_ratio"} <= f.keys()
    assert 150 < f["f0_mean"] < 220
    assert abs(f["wps"] - 3.0) < 0.6


def feed_all(seg, audio, t0=1000.0, chunk=480):
    now = t0
    for i in range(0, len(audio), chunk):
        c = audio[i:i + chunk]
        now += len(c) / SR
        seg.feed(c, now)
    return now


def test_vad_splits_two_phrases():
    got = []
    seg = Segmenter(got.append)
    feed_all(seg, np.concatenate([silence(1), speech(1.5), silence(1.5), speech(0.8), silence(2)]))
    assert len(got) == 2
    assert 1.5 < got[0].audio.size / SR < 2.3
    assert got[0].t_voice_start < got[0].t_end < got[1].t_voice_start


def test_vad_ignores_noise_and_blips():
    got = []
    seg = Segmenter(got.append)
    feed_all(seg, np.concatenate([silence(2), speech(0.1), silence(2)]))
    assert got == []


def test_vad_tick_finalizes_when_stream_stops():
    """WASAPI loopback молчит, когда ничего не играет: конец фразы определяется по часам."""
    got = []
    seg = Segmenter(got.append)
    now = feed_all(seg, speech(1.0), t0=500.0)
    assert got == []
    seg.tick(now + 1.2)
    assert len(got) == 1


def test_vad_flush_and_max_len():
    got = []
    seg = Segmenter(got.append, max_len=2.0)
    feed_all(seg, speech(5.0))
    assert len(got) >= 2  # длинную речь режет по max_len
    seg2 = Segmenter(got.append)
    feed_all(seg2, speech(1.0))
    n = len(got)
    seg2.flush()
    assert len(got) == n + 1


def test_vad_sensitivity_clamped():
    seg = Segmenter(lambda s: None)
    seg.set_sensitivity(5)
    assert seg.sens == 1.0
    seg.set_sensitivity(-1)
    assert seg.sens == 0.0


def test_asr_cleaning_removes_hallucinations_and_loops():
    assert clean_text("Субтитры сделал DimaTorzok") == ""
    assert clean_text("  привет   мир ") == "привет мир"
    assert clean_text("да да да да да да да да да") == "да"
    long_real = "Продолжение следует " + "слово " * 30
    assert clean_text(long_real).startswith("Продолжение")  # длинный текст не режем


def test_fake_asr_sequence():
    a = FakeASR(["раз", "два"])
    assert a.transcribe(None) == "раз" and a.transcribe(None) == "два" and a.transcribe(None) == ""
