from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf


@dataclass(frozen=True)
class MixStem:
    name: str
    path: Path
    gain_db: float = 0.0


def db_to_gain(db: float) -> float:
    return 10.0 ** (db / 20.0)


def read_stereo(path: Path) -> tuple[np.ndarray, int]:
    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    elif audio.shape[1] > 2:
        audio = audio[:, :2]
    return audio, sr


def pad_to(audio: np.ndarray, frames: int) -> np.ndarray:
    if len(audio) == frames:
        return audio
    out = np.zeros((frames, 2), dtype=np.float32)
    n = min(len(audio), frames)
    out[:n] = audio[:n, :2]
    return out


def export_stem(stem: MixStem, output: Path) -> dict[str, float]:
    """Export one processed stem with its configured gain and no normalization."""
    audio, sr = read_stereo(stem.path)
    gain = db_to_gain(stem.gain_db)
    audio = audio * gain
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))) if audio.size else 0.0

    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep a floating-point audition file so positive configured gain is not
    # silently normalized or clipped by the exporter.
    sf.write(output, audio, sr, subtype="FLOAT")
    return {
        "gain": gain,
        "peak": peak,
        "rms": rms,
        "duration_seconds": len(audio) / sr,
        "sample_rate": float(sr),
    }


def export_submix(stems: list[MixStem], output: Path) -> dict[str, float]:
    """Sum configured stems to a float WAV without peak normalization."""
    if not stems:
        raise RuntimeError("no stems to export")

    loaded: list[tuple[MixStem, np.ndarray]] = []
    sample_rates: set[int] = set()
    max_frames = 0
    for stem in stems:
        audio, sr = read_stereo(stem.path)
        loaded.append((stem, audio))
        sample_rates.add(sr)
        max_frames = max(max_frames, len(audio))

    if len(sample_rates) != 1:
        raise RuntimeError(f"stem sample rates differ: {sorted(sample_rates)}")

    sr = next(iter(sample_rates))
    mix = np.zeros((max_frames, 2), dtype=np.float32)
    for stem, audio in loaded:
        mix += pad_to(audio, max_frames) * db_to_gain(stem.gain_db)

    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    rms = float(np.sqrt(np.mean(mix.astype(np.float64) ** 2))) if mix.size else 0.0
    output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output, mix, sr, subtype="FLOAT")
    return {
        "peak": peak,
        "rms": rms,
        "duration_seconds": len(mix) / sr,
        "sample_rate": float(sr),
    }


def mix_stems(
    stems: list[MixStem],
    output: Path,
    normalize_peak_db: float = -1.0,
    master_gain_db: float = 0.0,
) -> dict[str, float]:
    if not stems:
        raise RuntimeError("no stems to mix")

    loaded: list[tuple[MixStem, np.ndarray]] = []
    sample_rates: set[int] = set()
    max_frames = 0

    for stem in stems:
        audio, sr = read_stereo(stem.path)
        loaded.append((stem, audio))
        sample_rates.add(sr)
        max_frames = max(max_frames, len(audio))

    if len(sample_rates) != 1:
        raise RuntimeError(f"stem sample rates differ: {sorted(sample_rates)}")

    sr = next(iter(sample_rates))
    mix = np.zeros((max_frames, 2), dtype=np.float32)

    for stem, audio in loaded:
        gain = db_to_gain(stem.gain_db)
        mix += pad_to(audio, max_frames) * gain

    pre_peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    if pre_peak <= 1e-12:
        raise RuntimeError("final mix is silent")

    target_peak = db_to_gain(normalize_peak_db)
    normalize_gain = target_peak / pre_peak
    mix *= normalize_gain

    master_gain = db_to_gain(master_gain_db)
    mix *= master_gain
    final_peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    if final_peak > 1.0 + 1e-7:
        raise RuntimeError(
            f"master output would clip: peak={final_peak:.4f}; "
            "lower master.gain_db or master.normalize_peak_db"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output, mix, sr, subtype="PCM_16")
    rms = float(np.sqrt(np.mean(mix.astype(np.float64) ** 2)))

    return {
        "pre_peak": pre_peak,
        "normalize_peak_db": normalize_peak_db,
        "normalize_gain": normalize_gain,
        "master_gain_db": master_gain_db,
        "master_gain": master_gain,
        "final_peak": final_peak,
        "rms": rms,
        "duration_seconds": len(mix) / sr,
        "sample_rate": float(sr),
    }
