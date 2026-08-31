from pathlib import Path

import midi_render.renderer as renderer


def test_find_sfizz_render_from_path(monkeypatch, tmp_path: Path):
    binary = tmp_path / "sfizz_render"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)

    monkeypatch.setattr(renderer.shutil, "which", lambda name: str(binary))
    assert renderer.find_sfizz_render() == binary.resolve()


def test_find_sfizz_render_from_pysfizz_wheel(monkeypatch, tmp_path: Path):
    purelib = tmp_path / "lib" / "python3.14" / "site-packages"
    binary = purelib / "bin" / "sfizz_render"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)

    monkeypatch.setattr(renderer.shutil, "which", lambda name: None)
    monkeypatch.setattr(renderer.sysconfig, "get_paths", lambda: {"purelib": str(purelib)})
    assert renderer.find_sfizz_render() == binary.resolve()


def test_fluidsynth_job_uses_global_options_before_soundfont_and_midi(monkeypatch, tmp_path: Path):
    from midi_render.midi import TrackInfo
    from midi_render.patches import Patch

    binary = tmp_path / "fluidsynth"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    sf2 = tmp_path / "MuseScore.sf2"
    sf2.write_bytes(b"sf2")
    midi = tmp_path / "track.mid"
    midi.write_bytes(b"midi")
    out = tmp_path / "out.wav"
    patch = Patch("gm_fallback_p089", "<gm>", sf2)
    track = TrackInfo(1, "Pad", 1, (0,), (89,), ((0, 89),))
    job = renderer.FluidSynthJob(track, "synth_pad", patch, midi, sf2, str(binary), 0.2, out)

    calls = []

    class Result:
        returncode = 0

    def fake_run(cmd):
        calls.append(cmd)
        out.write_bytes(b"wav")
        return Result()

    monkeypatch.setattr(renderer.subprocess, "run", fake_run)
    result = renderer.render_fluidsynth_jobs([job], workers=1, samplerate=48_000)
    assert result[0].path == out
    cmd = calls[0]
    assert cmd[0] == str(binary.resolve())
    assert cmd.index("-F") < cmd.index(str(sf2)) < cmd.index(str(midi))
    assert cmd[cmd.index("-r") + 1] == "48000"
    assert cmd[cmd.index("-g") + 1] == "0.2"
