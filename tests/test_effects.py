from pathlib import Path
import shutil
from types import SimpleNamespace

import numpy as np
import soundfile as sf

import midi_render.effects as effects
from midi_render.midi import TrackInfo
from midi_render.patches import PatchRegistry
from midi_render.renderer import RenderedStem


def _make_registry(tmp_path: Path) -> PatchRegistry:
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
    for name in ("lv2apply",):
        tool = tools / name
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)

    (config_dir / "patches.toml").write_text(
        """
[paths]
instruments = "../instruments"
lv2 = "../lv2"
tools = "../tools"

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
backend = "lv2apply"
tool = "lv2apply"
bundle = "mono.lv2"
plugin_uri = "urn:test:mono"
input_channels = 1

[effects.mono_amp.params]
BYPASS = 1.0
GAIN = 0.35

[effects.stereo_cab]
backend = "lv2apply"
tool = "lv2apply"
bundle = "stereo.lv2"
plugin_uri = "urn:test:stereo"
input_channels = 2

[effects.stereo_cab.params]
BYPASS = 1.0
SIZE = 0.5

[effects.bass_amp]
backend = "lv2apply"
tool = "lv2apply"
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


def test_lv2apply_chain_uses_uri_controls_and_project_lv2_path(monkeypatch, tmp_path: Path):
    registry = _make_registry(tmp_path)
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
    assert ["-c", "BYPASS", "1.0"] == first_cmd[5:8]
    assert ["-c", "GAIN", "0.35"] == first_cmd[8:11]
    assert second_cmd[-1] == "urn:test:stereo"
    assert first_env["LV2_PATH"] == str(registry.lv2_root)
    assert second_env["LV2_PATH"] == str(registry.lv2_root)



def test_bass_lv2apply_backend_keeps_numeric_enum_controls(monkeypatch, tmp_path: Path):
    registry = _make_registry(tmp_path)
    raw = tmp_path / "raw.wav"
    sf.write(raw, np.ones((64, 2), dtype=np.float32) * 0.1, 48_000, subtype="FLOAT")
    stem = _stem(registry, "electric_bass", raw)

    calls: list[list[str]] = []

    def fake_run(cmd, *, env):
        source = Path(cmd[cmd.index("-i") + 1])
        output = Path(cmd[cmd.index("-o") + 1])
        shutil.copyfile(source, output)
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(effects.subprocess, "run", fake_run)

    final = effects.process_stem_effects(stem, registry, tmp_path / "work")

    assert len(calls) == 1
    assert sf.info(final).channels == 1
    assert calls[0][-1] == "urn:test:bass"
    assert ["-c", "BYPASS", "1.0"] in [calls[0][i:i+3] for i in range(len(calls[0]) - 2)]
    assert ["-c", "GAIN", "0.25"] in [calls[0][i:i+3] for i in range(len(calls[0]) - 2)]
    assert ["-c", "MODE", "1"] in [calls[0][i:i+3] for i in range(len(calls[0]) - 2)]
