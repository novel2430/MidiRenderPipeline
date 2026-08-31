from pathlib import Path

import numpy as np
import soundfile as sf

from midi_render.mixer import MixStem, export_stem, export_submix


def test_export_stem_applies_gain_without_normalizing(tmp_path: Path):
    source = tmp_path / "source.wav"
    output = tmp_path / "out.wav"
    sf.write(source, np.ones((16, 1), dtype=np.float32) * 0.25, 48_000, subtype="FLOAT")

    stats = export_stem(MixStem("test", source, gain_db=6.020599913), output)
    audio, _ = sf.read(output, dtype="float32", always_2d=True)

    assert audio.shape[1] == 2
    assert np.allclose(audio, 0.5, atol=1e-5)
    assert abs(stats["gain"] - 2.0) < 1e-5
    assert abs(stats["peak"] - 0.5) < 1e-5


def test_export_submix_applies_component_gains_without_normalizing(tmp_path: Path):
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    out = tmp_path / "out.wav"
    sf.write(a, np.ones((32, 2), dtype=np.float32) * 0.25, 48_000, subtype="FLOAT")
    sf.write(b, np.ones((32, 2), dtype=np.float32) * 0.25, 48_000, subtype="FLOAT")

    stats = export_submix(
        [MixStem("main", a, 0.0), MixStem("kick", b, -6.020599913)], out
    )
    audio, sr = sf.read(out, dtype="float32", always_2d=True)
    assert sr == 48_000
    assert np.allclose(audio, 0.375, atol=1e-5)
    assert np.isclose(stats["peak"], 0.375, atol=1e-5)


def test_mix_stems_normalizes_then_applies_master_gain(tmp_path: Path):
    source = tmp_path / "source.wav"
    output = tmp_path / "out.wav"
    sf.write(source, np.ones((64, 2), dtype=np.float32) * 0.25, 48_000, subtype="FLOAT")

    from midi_render.mixer import mix_stems

    stats = mix_stems(
        [MixStem("test", source, 0.0)],
        output,
        normalize_peak_db=-6.020599913,
        master_gain_db=-6.020599913,
    )
    audio, _ = sf.read(output, dtype="float32", always_2d=True)

    # 0.25 is first normalized to 0.5, then master gain halves it to 0.25.
    assert np.allclose(audio, 0.25, atol=4e-5)
    assert np.isclose(stats["normalize_gain"], 2.0, atol=1e-5)
    assert np.isclose(stats["master_gain"], 0.5, atol=1e-5)
    assert np.isclose(stats["final_peak"], 0.25, atol=1e-5)


def test_mix_stems_rejects_master_clipping(tmp_path: Path):
    source = tmp_path / "source.wav"
    output = tmp_path / "out.wav"
    sf.write(source, np.ones((64, 2), dtype=np.float32) * 0.25, 48_000, subtype="FLOAT")

    from midi_render.mixer import mix_stems

    try:
        mix_stems(
            [MixStem("test", source, 0.0)],
            output,
            normalize_peak_db=-1.0,
            master_gain_db=2.0,
        )
    except RuntimeError as exc:
        assert "master output would clip" in str(exc)
    else:
        raise AssertionError("positive master gain above 0 dBFS should not silently clip")
