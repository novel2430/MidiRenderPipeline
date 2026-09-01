#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib

import numpy as np
import soundfile as sf


def ensure_channels(source: Path, output: Path, channels: int) -> Path:
    """Mirror midi_render.effects._ensure_channels exactly."""
    info = sf.info(source)
    if info.channels == channels:
        return source

    audio, sr = sf.read(source, dtype="float32", always_2d=True)
    if channels == 1:
        converted = np.mean(audio[:, : min(audio.shape[1], 2)], axis=1, dtype=np.float32)
    elif channels == 2:
        if audio.shape[1] == 1:
            converted = np.repeat(audio, 2, axis=1)
        else:
            converted = audio[:, :2]
    else:
        raise ValueError(f"unsupported effect input channel count: {channels}")

    sf.write(output, converted, sr, subtype="FLOAT")
    return output


def effect_cfg(config: dict, name: str) -> dict:
    try:
        return config["effects"][name]
    except KeyError as exc:
        raise SystemExit(f"effect {name!r} not found in config") from exc


def control_args(params: dict) -> list[str]:
    args: list[str] = []
    for name, value in params.items():
        if isinstance(value, bool):
            text = "1" if value else "0"
        elif isinstance(value, (int, float)):
            text = str(value)
        else:
            raise TypeError(f"control {name} must be numeric, got {value!r}")
        args += ["-c", str(name), text]
    return args


def run_reference(
    lv2apply: str,
    source: Path,
    output: Path,
    effects: list[tuple[str, dict]],
    env: dict[str, str],
    temp_dir: Path,
) -> float:
    t0 = time.perf_counter()
    current = source
    for index, (name, cfg) in enumerate(effects, start=1):
        requested = int(cfg.get("input_channels", 1))
        prepared = ensure_channels(current, temp_dir / f"ref-{index:02d}-{name}.input-{requested}ch.wav", requested)
        stage_out = output if index == len(effects) else temp_dir / f"ref-{index:02d}-{name}.wav"
        stage_out.unlink(missing_ok=True)
        cmd = [lv2apply, "-i", str(prepared), "-o", str(stage_out)]
        cmd += control_args(cfg.get("params", {}))
        cmd.append(str(cfg["plugin_uri"]))
        subprocess.run(cmd, env=env, check=True, stdout=subprocess.DEVNULL)
        current = stage_out
    return time.perf_counter() - t0


def native_command(
    binary: Path,
    source: Path,
    output: Path,
    lv2_path: Path,
    block: int,
    effects: list[tuple[str, dict]],
) -> list[str]:
    cmd = [
        str(binary),
        "-i", str(source),
        "-o", str(output),
        "--block", str(block),
        "--lv2-path", str(lv2_path),
    ]
    for _name, cfg in effects:
        cmd += ["--plugin", str(cfg["plugin_uri"])]
        cmd += ["--input-channels", str(int(cfg.get("input_channels", 1)))]
        for symbol, value in cfg.get("params", {}).items():
            cmd += ["--control", str(symbol), str(value)]
    return cmd


def run_native(cmd: list[str]) -> float:
    t0 = time.perf_counter()
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return time.perf_counter() - t0


def compare_wavs(reference: Path, candidate: Path) -> dict[str, float | int]:
    ref, ref_sr = sf.read(reference, dtype="float64", always_2d=True)
    got, got_sr = sf.read(candidate, dtype="float64", always_2d=True)
    if ref_sr != got_sr:
        raise RuntimeError(f"sample-rate mismatch: {ref_sr} != {got_sr}")
    if ref.shape != got.shape:
        raise RuntimeError(f"shape mismatch: {ref.shape} != {got.shape}")

    diff = got - ref
    max_abs = float(np.max(np.abs(diff))) if diff.size else 0.0
    rms_diff = float(np.sqrt(np.mean(diff * diff))) if diff.size else 0.0
    rms_ref = float(np.sqrt(np.mean(ref * ref))) if ref.size else 0.0
    peak_ref = float(np.max(np.abs(ref))) if ref.size else 0.0
    peak_got = float(np.max(np.abs(got))) if got.size else 0.0
    snr = math.inf if rms_diff == 0.0 else 20.0 * math.log10(max(rms_ref, 1e-30) / rms_diff)
    return {
        "frames": int(ref.shape[0]),
        "channels": int(ref.shape[1]),
        "max_abs": max_abs,
        "rms_diff": rms_diff,
        "snr_db": snr,
        "peak_ref": peak_ref,
        "peak_got": peak_got,
    }


def median(values: list[float]) -> float:
    return statistics.median(values)


def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark the MRP native LV2 chain host against legacy lv2apply semantics")
    ap.add_argument("input", type=Path, help="raw sampler stem WAV")
    ap.add_argument("--project", type=Path, default=Path.cwd(), help="MidiRenderPipeline root")
    ap.add_argument("--binary", type=Path, default=Path("resources/tools/mrp-lv2-chain"), help="native LV2 chain binary")
    ap.add_argument("--effects", nargs="+", required=True, help="effect names from config/patches.toml")
    ap.add_argument("--blocks", nargs="+", type=int, default=[64, 256, 1024, 4096])
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--output-dir", type=Path, default=Path("renders/bench-lv2"))
    args = ap.parse_args()

    project = args.project.resolve()
    input_path = args.input.resolve()
    binary = args.binary if args.binary.is_absolute() else (project / args.binary)
    binary = binary.resolve()
    output_dir = args.output_dir if args.output_dir.is_absolute() else project / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_path.is_file():
        raise SystemExit(f"input does not exist: {input_path}")
    if not binary.is_file():
        raise SystemExit(f"native LV2 binary does not exist: {binary}; run `make native-lv2`")
    lv2apply = shutil.which("lv2apply")
    if not lv2apply:
        raise SystemExit("lv2apply not found in PATH (needed as reference)")

    config_path = project / "config/patches.toml"
    with config_path.open("rb") as f:
        config = tomllib.load(f)

    lv2_rel = Path(config["paths"]["lv2"])
    lv2_path = (config_path.parent / lv2_rel).resolve()
    effects = [(name, effect_cfg(config, name)) for name in args.effects]

    env = os.environ.copy()
    env["LV2_PATH"] = str(lv2_path)

    chain_name = "__".join(args.effects)
    reference = output_dir / f"{chain_name}.reference.wav"

    print(f"input:      {input_path}")
    print(f"LV2_PATH:   {lv2_path}")
    print(f"effects:    {' -> '.join(args.effects)}")
    print(f"repeat:     {args.repeat}")
    print()

    reference_times: list[float] = []
    with tempfile.TemporaryDirectory(prefix="mrp-lv2-bench-") as td:
        temp_dir = Path(td)
        for _ in range(args.repeat):
            reference_times.append(run_reference(lv2apply, input_path, reference, effects, env, temp_dir))

    ref_med = median(reference_times)
    print(f"lv2apply reference: median {ref_med:.4f}s  runs={', '.join(f'{x:.4f}' for x in reference_times)}")

    print("\nblock    native(s)   speedup    max_abs_diff      rms_diff        SNR(dB)")
    print("-----    ---------   -------    ------------      --------        -------")
    for block in args.blocks:
        candidate = output_dir / f"{chain_name}.native-b{block}.wav"
        times: list[float] = []
        cmd = native_command(binary, input_path, candidate, lv2_path, block, effects)
        for _ in range(args.repeat):
            candidate.unlink(missing_ok=True)
            times.append(run_native(cmd))
        native_med = median(times)
        metrics = compare_wavs(reference, candidate)
        speedup = ref_med / native_med if native_med > 0 else math.inf
        snr = metrics["snr_db"]
        snr_text = "inf" if math.isinf(float(snr)) else f"{float(snr):.2f}"
        print(
            f"{block:5d}    {native_med:9.4f}   {speedup:7.2f}x   "
            f"{float(metrics['max_abs']):12.6g}      {float(metrics['rms_diff']):.6g}    {snr_text:>9s}"
        )

    print(f"\noutputs: {output_dir}")
    print("The production renderer defaults to block 1024; this tool keeps lv2apply only as a reference benchmark.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
