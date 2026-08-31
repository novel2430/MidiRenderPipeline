from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time

import numpy as np
import soundfile as sf

from .patches import PatchRegistry
from .renderer import RenderedStem


def _resolve_executable(registry: PatchRegistry, value: str) -> Path:
    """Resolve a helper from project tools first, then PATH."""
    path = Path(value).expanduser()

    if path.is_absolute() or path.parent != Path("."):
        if path.is_file():
            return path.resolve()
        found = shutil.which(str(path))
        if found:
            return Path(found)
        raise FileNotFoundError(f"executable not found: {value}")

    local = registry.tools_root / path
    if local.is_file():
        return local.resolve()

    found = shutil.which(value)
    if found:
        return Path(found)
    raise FileNotFoundError(f"executable not found: {value}")


def _ensure_channels(source: Path, output: Path, channels: int) -> Path:
    """Return audio with the requested channel count, writing only when needed."""
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

    output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output, converted, sr, subtype="FLOAT")
    return output


def _effect_paths(
    stem: RenderedStem,
    source: Path,
    effect_name: str,
    stage: int,
    input_channels: int,
    work_dir: Path,
) -> tuple[Path, Path]:
    fx_dir = work_dir / "fx"
    fx_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"track-{stem.track.index:02d}.{stage:02d}-{effect_name}"
    prepared = _ensure_channels(
        source,
        fx_dir / f"{prefix}.input-{input_channels}ch.wav",
        input_channels,
    )
    return prepared, fx_dir / f"{prefix}.wav"



def _format_lv2_control(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    raise TypeError(
        "lv2apply control values must be numeric; "
        f"got {value!r} ({type(value).__name__})"
    )


def _run_lv2apply_effect(
    stem: RenderedStem,
    source: Path,
    effect_name: str,
    stage: int,
    registry: PatchRegistry,
    work_dir: Path,
) -> Path:
    """Apply one project-local LV2 plugin through Lilv's native lv2apply tool."""
    cfg = registry.effect(effect_name).values
    tool = _resolve_executable(registry, str(cfg.get("tool", "lv2apply")))

    plugin_uri = cfg.get("plugin_uri")
    if not isinstance(plugin_uri, str) or not plugin_uri:
        raise KeyError(f"effect {effect_name!r} has no plugin_uri")

    bundle_value = cfg.get("bundle")
    if bundle_value is not None:
        bundle = registry.resolve_lv2(str(bundle_value))
        if not bundle.exists():
            raise FileNotFoundError(f"LV2 bundle not found for {effect_name}: {bundle}")

    input_channels = int(cfg.get("input_channels", 1))
    params = cfg.get("params", {})
    if not isinstance(params, dict):
        raise TypeError(f"effect {effect_name!r} params must be a table")

    prepared, output = _effect_paths(
        stem,
        source,
        effect_name,
        stage,
        input_channels,
        work_dir,
    )

    # lv2apply writes directly to the target path. Remove any artifact from a
    # previous run so a failed process can never leave a stale stem looking valid.
    output.unlink(missing_ok=True)

    cmd = [str(tool), "-i", str(prepared), "-o", str(output)]
    for name, value in params.items():
        cmd.extend(["-c", str(name), _format_lv2_control(value)])
    cmd.append(plugin_uri)

    env = os.environ.copy()
    env["LV2_PATH"] = str(registry.lv2_root)

    t0 = time.perf_counter()
    proc = subprocess.run(cmd, env=env)
    dt = time.perf_counter() - t0
    if proc.returncode != 0:
        raise RuntimeError(f"lv2apply returned {proc.returncode} for effect {effect_name}")
    if not output.is_file():
        raise RuntimeError(f"lv2apply produced no output for effect {effect_name}: {output}")

    print(f"  FX   track={stem.track.index:02d} {effect_name:18s} {dt:.2f}s")
    return output


def process_stem_effects(
    stem: RenderedStem,
    registry: PatchRegistry,
    work_dir: Path,
) -> Path:
    path = stem.path
    for stage, effect_name in enumerate(stem.patch.effects, start=1):
        cfg = registry.effect(effect_name).values
        backend = str(cfg.get("backend", "lv2apply")).lower()
        if backend != "lv2apply":
            raise ValueError(f"unknown effect backend {backend!r} for {effect_name}")
        path = _run_lv2apply_effect(
            stem,
            path,
            effect_name,
            stage,
            registry,
            work_dir,
        )
    return path
