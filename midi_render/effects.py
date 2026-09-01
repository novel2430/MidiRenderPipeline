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


NATIVE_LV2_BACKEND = "native-lv2"
LEGACY_LV2APPLY_BACKEND = "lv2apply"


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


def find_effect_renderer_tool(registry: PatchRegistry) -> Path | None:
    """Find the configured native effect-chain helper without raising."""
    try:
        return _resolve_executable(registry, registry.effect_renderer.tool)
    except FileNotFoundError:
        return None


def _ensure_channels(source: Path, output: Path, channels: int) -> Path:
    """Legacy lv2apply channel conversion; native chains convert in-memory."""
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
        "LV2 control values must be numeric; "
        f"got {value!r} ({type(value).__name__})"
    )


def _effect_backend(registry: PatchRegistry, effect_name: str) -> str:
    cfg = registry.effect(effect_name).values
    return str(cfg.get("backend", registry.effect_renderer.backend)).strip().lower()


def _run_lv2apply_effect(
    stem: RenderedStem,
    source: Path,
    effect_name: str,
    stage: int,
    registry: PatchRegistry,
    work_dir: Path,
) -> Path:
    """Legacy compatibility path for explicitly configured lv2apply effects."""
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


def _run_native_lv2_chain(
    stem: RenderedStem,
    registry: PatchRegistry,
    work_dir: Path,
) -> Path:
    """Process one complete LV2 chain in a single block-based native helper."""
    effect_names = stem.patch.effects
    if not effect_names:
        return stem.path

    tool_values: list[str] = []
    stages: list[tuple[str, dict[str, object]]] = []
    for effect_name in effect_names:
        cfg = registry.effect(effect_name).values
        tool_values.append(str(cfg.get("tool", registry.effect_renderer.tool)))

        plugin_uri = cfg.get("plugin_uri")
        if not isinstance(plugin_uri, str) or not plugin_uri:
            raise KeyError(f"effect {effect_name!r} has no plugin_uri")

        bundle_value = cfg.get("bundle")
        if bundle_value is not None:
            bundle = registry.resolve_lv2(str(bundle_value))
            if not bundle.exists():
                raise FileNotFoundError(f"LV2 bundle not found for {effect_name}: {bundle}")

        params = cfg.get("params", {})
        if not isinstance(params, dict):
            raise TypeError(f"effect {effect_name!r} params must be a table")
        stages.append((effect_name, cfg))

    if len(set(tool_values)) != 1:
        raise ValueError(
            "all native LV2 effects in one stem must use the same chain tool; "
            f"got {tool_values}"
        )
    tool = _resolve_executable(registry, tool_values[0])

    fx_dir = work_dir / "fx"
    fx_dir.mkdir(parents=True, exist_ok=True)
    output = fx_dir / f"track-{stem.track.index:02d}.lv2-chain.wav"
    output.unlink(missing_ok=True)

    cmd = [
        str(tool),
        "-i",
        str(stem.path),
        "-o",
        str(output),
        "--block",
        str(registry.effect_renderer.block_size),
        "--lv2-path",
        str(registry.lv2_root),
    ]
    for effect_name, cfg in stages:
        plugin_uri = str(cfg["plugin_uri"])
        input_channels = int(cfg.get("input_channels", 1))
        if input_channels < 1:
            raise ValueError(f"effect {effect_name!r} input_channels must be >= 1")

        cmd.extend(["--plugin", plugin_uri, "--input-channels", str(input_channels)])
        params = cfg.get("params", {})
        assert isinstance(params, dict)
        for name, value in params.items():
            cmd.extend(["--control", str(name), _format_lv2_control(value)])

    env = os.environ.copy()
    env["LV2_PATH"] = str(registry.lv2_root)

    t0 = time.perf_counter()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    dt = time.perf_counter() - t0
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"native LV2 chain returned {proc.returncode} for track {stem.track.index}{suffix}"
        )
    if not output.is_file():
        raise RuntimeError(f"native LV2 chain produced no output: {output}")

    chain = " -> ".join(effect_names)
    print(f"  FX   track={stem.track.index:02d} {chain} {dt:.2f}s")
    return output


def process_stem_effects(
    stem: RenderedStem,
    registry: PatchRegistry,
    work_dir: Path,
) -> Path:
    if not stem.patch.effects:
        return stem.path

    backends = tuple(_effect_backend(registry, name) for name in stem.patch.effects)
    unique_backends = set(backends)
    if len(unique_backends) != 1:
        raise ValueError(
            "mixed effect backends in one stem are not supported; "
            f"got {dict(zip(stem.patch.effects, backends))}"
        )

    backend = backends[0]
    if backend == NATIVE_LV2_BACKEND:
        return _run_native_lv2_chain(stem, registry, work_dir)

    if backend == LEGACY_LV2APPLY_BACKEND:
        path = stem.path
        for stage, effect_name in enumerate(stem.patch.effects, start=1):
            path = _run_lv2apply_effect(
                stem,
                path,
                effect_name,
                stage,
                registry,
                work_dir,
            )
        return path

    raise ValueError(f"unknown effect backend {backend!r}")
