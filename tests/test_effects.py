from pathlib import Path
import shutil
from types import SimpleNamespace

import numpy as np
import soundfile as sf

import midi_render.effects as effects
from midi_render.midi import TrackInfo
from midi_render.patches import PatchRegistry
from midi_render.renderer import RenderedStem


def _make_registry(tmp_path: Path, *, backend: str = "native-lv2") -> PatchRegistry:
    config_dir = tmp_path / "config"
    instruments = tmp_path / "instruments" / "ui"
    lv2 = tmp_path / "lv2"
    tools = tmp_path / "tools"
    config_dir.mkdir()
    instruments.mkdir(parents=True)
    lv2.mkdir()
    tools.mkdir()

    (instruments / "guitar.sfz").write_text("// test\n")
    for name in ("mono.lv2", "stereo.lv2", "bass.lv2"):
        (lv2 / name).mkdir()
    for name in ("mrp-lv2-chain", "lv2apply"):
        tool = tools / name
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)

    tool_name = "mrp-lv2-chain" if backend == "native-lv2" else "lv2apply"
    (config_dir / "patches.toml").write_text(
        f"""
[paths]
instruments = "../instruments"
lv2 = "../lv2"
tools = "../tools"

[effect_renderer]
backend = "{backend}"
tool = "{tool_name}"
block_size = 1024

[libraries.ui]
root = "ui"

[patches.electric_guitar_clean]
library = "ui"
sfz = "guitar.sfz"
effects = ["mono_amp", "stereo_cab"]

[patches.electric_bass]
library = "ui"
sfz = "guitar.sfz"
effects = ["bass_amp"]

[effects.mono_amp]
bundle = "mono.lv2"
plugin_uri = "urn:test:mono"
input_channels = 1

[effects.mono_amp.params]
BYPASS = 1.0
GAIN = 0.35

[effects.stereo_cab]
bundle = "stereo.lv2"
plugin_uri = "urn:test:stereo"
input_channels = 2

[effects.stereo_cab.params]
BYPASS = 1.0
SIZE = 0.5

[effects.bass_amp]
bundle = "bass.lv2"
plugin_uri = "urn:test:bass"
input_channels = 1

[effects.bass_amp.params]
BYPASS = 1.0
GAIN = 0.25
MODE = 1
""".strip()
        + "\n"
    )
    return PatchRegistry(config_dir / "patches.toml")


def _stem(registry: PatchRegistry, instrument: str, raw: Path) -> RenderedStem:
    patch = registry.get(instrument)
    assert patch is not None
    return RenderedStem(
        track=TrackInfo(2, "Egt", 10, (0,), (27,), ((0, 27),)),
        instrument=instrument,
        patch=patch,
        path=raw,
        render_seconds=0.01,
    )


def _triples(cmd: list[str]) -> list[list[str]]:
    return [cmd[i:i + 3] for i in range(len(cmd) - 2)]


def test_native_lv2_chain_runs_full_chain_once_with_block_controls_and_project_path(
    monkeypatch, tmp_path: Path
):
    registry = _make_registry(tmp_path)
    raw = tmp_path / "raw.wav"
    sf.write(raw, np.ones((64, 2), dtype=np.float32) * 0.1, 48_000, subtype="FLOAT")
    stem = _stem(registry, "electric_guitar_clean", raw)

    calls: list[tuple[list[str], dict[str, str], dict[str, object]]] = []

    def fake_run(cmd, *, env, **kwargs):
        source = Path(cmd[cmd.index("-i") + 1])
        output = Path(cmd[cmd.index("-o") + 1])
        shutil.copyfile(source, output)
        calls.append((list(cmd), dict(env), dict(kwargs)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(effects.subprocess, "run", fake_run)

    final = effects.process_stem_effects(stem, registry, tmp_path / "work")

    assert len(calls) == 1
    cmd, env, kwargs = calls[0]
    assert Path(cmd[0]) == registry.tools_root / "mrp-lv2-chain"
    assert Path(cmd[cmd.index("-i") + 1]) == raw
    assert Path(cmd[cmd.index("-o") + 1]) == final
    assert cmd[cmd.index("--block") + 1] == "1024"
    assert Path(cmd[cmd.index("--lv2-path") + 1]) == registry.lv2_root
    assert env["LV2_PATH"] == str(registry.lv2_root)
    assert kwargs == {"capture_output": True, "text": True}

    plugins = [cmd[i + 1] for i, item in enumerate(cmd[:-1]) if item == "--plugin"]
    assert plugins == ["urn:test:mono", "urn:test:stereo"]
    assert ["--control", "BYPASS", "1.0"] in _triples(cmd)
    assert ["--control", "GAIN", "0.35"] in _triples(cmd)
    assert ["--control", "SIZE", "0.5"] in _triples(cmd)
    assert not list((tmp_path / "work" / "fx").glob("*.input-*ch.wav"))


def test_native_bass_chain_keeps_numeric_enum_controls(monkeypatch, tmp_path: Path):
    registry = _make_registry(tmp_path)
    raw = tmp_path / "raw.wav"
    sf.write(raw, np.ones((64, 2), dtype=np.float32) * 0.1, 48_000, subtype="FLOAT")
    stem = _stem(registry, "electric_bass", raw)

    calls: list[list[str]] = []

    def fake_run(cmd, *, env, **kwargs):
        output = Path(cmd[cmd.index("-o") + 1])
        shutil.copyfile(raw, output)
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(effects.subprocess, "run", fake_run)

    effects.process_stem_effects(stem, registry, tmp_path / "work")

    assert len(calls) == 1
    assert ["--control", "BYPASS", "1.0"] in _triples(calls[0])
    assert ["--control", "GAIN", "0.25"] in _triples(calls[0])
    assert ["--control", "MODE", "1"] in _triples(calls[0])


def test_explicit_legacy_lv2apply_backend_still_runs_stage_by_stage(monkeypatch, tmp_path: Path):
    registry = _make_registry(tmp_path, backend="lv2apply")
    raw = tmp_path / "raw.wav"
    sf.write(raw, np.ones((64, 2), dtype=np.float32) * 0.1, 48_000, subtype="FLOAT")
    stem = _stem(registry, "electric_guitar_clean", raw)

    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd, *, env):
        source = Path(cmd[cmd.index("-i") + 1])
        output = Path(cmd[cmd.index("-o") + 1])
        shutil.copyfile(source, output)
        calls.append((list(cmd), dict(env)))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(effects.subprocess, "run", fake_run)

    final = effects.process_stem_effects(stem, registry, tmp_path / "work")

    assert len(calls) == 2
    first_cmd, first_env = calls[0]
    second_cmd, second_env = calls[1]

    first_input = Path(first_cmd[first_cmd.index("-i") + 1])
    second_input = Path(second_cmd[second_cmd.index("-i") + 1])
    assert sf.info(first_input).channels == 1
    assert sf.info(second_input).channels == 2
    assert sf.info(final).channels == 2
    assert first_cmd[-1] == "urn:test:mono"
    assert second_cmd[-1] == "urn:test:stereo"
    assert first_env["LV2_PATH"] == str(registry.lv2_root)
    assert second_env["LV2_PATH"] == str(registry.lv2_root)
