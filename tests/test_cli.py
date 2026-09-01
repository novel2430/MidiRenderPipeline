from argparse import Namespace
from pathlib import Path

import mido
import numpy as np
import soundfile as sf

import midi_render.cli as cli


def _make_drive_midi(path: Path) -> None:
    mid = mido.MidiFile(type=1)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name="Conductor", time=0))
    mid.tracks.append(conductor)

    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name="Egt", time=0))
    track.append(mido.Message("program_change", channel=4, program=29, time=0))
    track.append(mido.Message("note_on", channel=4, note=60, velocity=100, time=0))
    track.append(mido.Message("note_off", channel=4, note=60, velocity=0, time=120))
    mid.tracks.append(track)
    mid.save(path)


def _make_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    library = tmp_path / "instruments" / "ui"
    config_dir.mkdir()
    library.mkdir(parents=True)
    (library / "guitar.sfz").write_text("// test\n")
    config = config_dir / "patches.toml"
    config.write_text(
        """
[paths]
instruments = "../instruments"

[libraries.ui]
root = "ui"

[patches.electric_guitar_overdrive]
library = "ui"
sfz = "guitar.sfz"
effects = []
""".strip()
        + "\n"
    )
    return config


def _args(midi: Path, config: Path, work_dir: Path) -> Namespace:
    return Namespace(
        midi=midi,
        config=config,
        output=None,
        work_dir=work_dir,
        track=1,
        jobs=2,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        include_melody=False,
        skip_unconfigured=False,
        keep_work=False,
    )


def _sfz_cached_path(
    midi: Path,
    config: Path,
    work_dir: Path,
    instrument: str,
    *,
    track_index: int = 1,
) -> Path:
    args = _args(midi, config, work_dir)
    registry = cli.PatchRegistry(config)
    patch, route = registry.resolve_dedicated(instrument)
    assert patch is not None
    assert route is not None
    tag = cli._sfz_render_cache_tag(
        midi,
        patch,
        blocksize=args.blocksize,
        samplerate=args.samplerate,
        quality=args.quality,
        polyphony=args.polyphony,
    )
    return cli._raw_stem_path(
        work_dir / "stems",
        track_index,
        instrument,
        route,
        patch,
        tag,
    )


def test_single_track_render_reuses_cached_raw_and_reapplies_effects(monkeypatch, tmp_path: Path):
    midi = tmp_path / "song.mid"
    _make_drive_midi(midi)
    config = _make_config(tmp_path)
    work_dir = tmp_path / "work"
    raw = _sfz_cached_path(midi, config, work_dir, "electric_guitar_overdrive")
    raw.parent.mkdir(parents=True)
    sf.write(raw, np.ones((64, 2), dtype=np.float32) * 0.1, 48_000, subtype="FLOAT")

    def fail_render_jobs(*args, **kwargs):
        raise AssertionError("cached single-track render must not call sfizz_render")

    calls = []

    def fake_effects(stem, registry, work):
        calls.append((stem.track.index, stem.path, stem.patch.name))
        out = work / "fx" / "track-01.processed.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        sf.write(out, np.ones((64, 2), dtype=np.float32) * 0.2, 48_000, subtype="FLOAT")
        return out

    monkeypatch.setattr(cli, "render_jobs", fail_render_jobs)
    monkeypatch.setattr(cli, "process_stem_effects", fake_effects)
    monkeypatch.chdir(tmp_path)

    assert cli.cmd_render(_args(midi, config, work_dir)) == 0
    assert calls == [(1, raw, "electric_guitar_overdrive")]
    assert (tmp_path / "renders/final/song.track-01.wav").is_file()


def test_track_selector_rejects_missing_or_empty_track(tmp_path: Path):
    midi = tmp_path / "song.mid"
    _make_drive_midi(midi)
    _, tracks = cli.analyze_midi(midi)

    selected = cli._select_render_tracks(tracks, 1)
    assert [track.index for track in selected] == [1]

    try:
        cli._select_render_tracks(tracks, 99)
    except SystemExit as exc:
        assert "does not exist or has no notes" in str(exc)
    else:
        raise AssertionError("missing track should fail")


def test_default_single_track_output_does_not_overwrite_full_mix(tmp_path: Path):
    midi = tmp_path / "song.mid"
    assert cli._default_render_output(midi, None) == Path("renders/final/song.wav")
    assert cli._default_render_output(midi, 6) == Path("renders/final/song.track-06.wav")


def _make_drum_midi(path: Path) -> None:
    mid = mido.MidiFile(type=1)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name="Conductor", time=0))
    mid.tracks.append(conductor)

    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name="Drum", time=0))
    track.append(mido.Message("note_on", channel=9, note=36, velocity=100, time=0))
    track.append(mido.Message("note_off", channel=9, note=36, velocity=0, time=120))
    track.append(mido.Message("note_on", channel=9, note=42, velocity=90, time=0))
    track.append(mido.Message("note_off", channel=9, note=42, velocity=0, time=120))
    mid.tracks.append(track)
    mid.save(path)


def _make_drum_layer_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    instruments = tmp_path / "instruments"
    main = instruments / "sm"
    layer = instruments / "muldjord"
    config_dir.mkdir()
    main.mkdir(parents=True)
    layer.mkdir(parents=True)
    (main / "drums.sfz").write_text("// main\n")
    (layer / "MuldjordKit GM.sfz").write_text("// layer\n")
    config = config_dir / "patches.toml"
    config.write_text(
        """
[paths]
instruments = "../instruments"

[libraries.sm]
root = "sm"

[patches.drums]
library = "sm"
sfz = "drums.sfz"
gain_db = 0.0

[drum_kick_layer]
sfz = "muldjord/MuldjordKit GM.sfz"
notes = [35, 36]
gain_db = -6.0
""".strip()
        + "\n"
    )
    return config


def test_single_drum_track_reuses_main_and_kick_cache_and_exports_submix(monkeypatch, tmp_path: Path):
    midi = tmp_path / "song.mid"
    _make_drum_midi(midi)
    config = _make_drum_layer_config(tmp_path)
    work_dir = tmp_path / "work"
    stems = work_dir / "stems"
    stems.mkdir(parents=True)
    main_raw = _sfz_cached_path(midi, config, work_dir, "drums")
    args = _args(midi, config, work_dir)
    registry = cli.PatchRegistry(config)
    kick_patch = cli._drum_kick_patch(registry)
    assert kick_patch is not None
    kick_tag = cli._sfz_render_cache_tag(
        midi,
        kick_patch,
        blocksize=args.blocksize,
        samplerate=args.samplerate,
        quality=args.quality,
        polyphony=args.polyphony,
        midi_transform={"kind": "note-filter", "notes": [35, 36]},
    )
    kick_raw = stems / f"track-01.drums.kick-layer.render-{kick_tag}.raw.wav"
    sf.write(main_raw, np.ones((64, 2), dtype=np.float32) * 0.2, 48_000, subtype="FLOAT")
    sf.write(kick_raw, np.ones((64, 2), dtype=np.float32) * 0.2, 48_000, subtype="FLOAT")

    def fail_render_jobs(*args, **kwargs):
        raise AssertionError("cached drum audition must not call sfizz_render")

    monkeypatch.setattr(cli, "render_jobs", fail_render_jobs)
    monkeypatch.chdir(tmp_path)
    args = _args(midi, config, work_dir)

    assert cli.cmd_render(args) == 0
    out = tmp_path / "renders/final/song.track-01.wav"
    audio, _ = sf.read(out, dtype="float32", always_2d=True)
    # main 0.2 + kick 0.2 * -6 dB (~0.5012)
    assert np.allclose(audio, 0.2 + 0.2 * (10 ** (-6.0 / 20.0)), atol=1e-5)


def _make_gm_fallback_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config-gm"
    instruments = tmp_path / "instruments-gm"
    config_dir.mkdir()
    instruments.mkdir()
    (instruments / "MuseScore_General_Full.sf2").write_bytes(b"sf2")
    config = config_dir / "patches.toml"
    config.write_text(
        """
[paths]
instruments = "../instruments-gm"

[general_midi_fallback]
soundfont = "MuseScore_General_Full.sf2"
synth_gain = 0.2
gain_db = 0.0

[general_midi_fallback.program_for_instrument]
melody = 80
synth_pad = 89
synth_lead = 80
""".strip()
        + "\n"
    )
    return config


def _make_named_program_midi(path: Path, name: str, program: int) -> None:
    mid = mido.MidiFile(type=1)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name="Conductor", time=0))
    mid.tracks.append(conductor)
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name=name, time=0))
    track.append(mido.Message("program_change", channel=0, program=program, time=0))
    track.append(mido.Message("note_on", channel=0, note=60, velocity=100, time=10))
    track.append(mido.Message("note_off", channel=0, note=60, velocity=0, time=120))
    mid.tracks.append(track)
    mid.save(path)


def _fake_gm_renderer_with_capture(captured):
    def fake(jobs, *, workers, samplerate):
        rendered = []
        for job in jobs:
            captured.append(job)
            job.output.parent.mkdir(parents=True, exist_ok=True)
            sf.write(job.output, np.ones((64, 2), dtype=np.float32) * 0.1, samplerate, subtype="FLOAT")
            rendered.append(
                cli.RenderedStem(
                    track=job.track,
                    instrument=job.instrument,
                    patch=job.patch,
                    path=job.output,
                    render_seconds=0.01,
                )
            )
        return rendered
    return fake


def test_conflicting_track_name_preserves_source_program_for_gm_fallback(monkeypatch, tmp_path: Path):
    midi = tmp_path / "pad.mid"
    _make_named_program_midi(midi, "Pad", 49)
    config = _make_gm_fallback_config(tmp_path)
    captured = []
    monkeypatch.setattr(cli, "render_fluidsynth_jobs", _fake_gm_renderer_with_capture(captured))
    monkeypatch.chdir(tmp_path)

    args = _args(midi, config, tmp_path / "work-gm")
    args.keep_work = True
    assert cli.cmd_render(args) == 0
    assert len(captured) == 1
    job = captured[0]
    assert job.instrument == "string_ensemble"
    assert job.patch.name == "gm_fallback_p049"
    split = mido.MidiFile(job.split_midi).tracks[-1]
    assert {msg.program for msg in split if msg.type == "program_change"} == {49}


def test_trusted_program_is_preserved_for_gm_fallback(monkeypatch, tmp_path: Path):
    midi = tmp_path / "lead.mid"
    _make_named_program_midi(midi, "Lead", 82)
    config = _make_gm_fallback_config(tmp_path)
    captured = []
    monkeypatch.setattr(cli, "render_fluidsynth_jobs", _fake_gm_renderer_with_capture(captured))
    monkeypatch.chdir(tmp_path)

    args = _args(midi, config, tmp_path / "work-lead")
    args.keep_work = True
    assert cli.cmd_render(args) == 0
    assert len(captured) == 1
    job = captured[0]
    assert job.instrument == "synth_lead"
    assert job.patch.name == "gm_fallback_p082"
    split = mido.MidiFile(job.split_midi).tracks[-1]
    assert {msg.program for msg in split if msg.type == "program_change"} == {82}


def test_include_melody_prefers_dedicated_patch_before_gm(monkeypatch, tmp_path: Path):
    midi = tmp_path / "melody.mid"
    _make_named_program_midi(midi, "Melody", 73)

    config_dir = tmp_path / "config-melody"
    instruments = tmp_path / "instruments-melody"
    vpo = instruments / "vpo"
    config_dir.mkdir()
    vpo.mkdir(parents=True)
    (vpo / "flute.sfz").write_text("// flute\n")
    config = config_dir / "patches.toml"
    config.write_text(
        """
[paths]
instruments = "../instruments-melody"

[libraries.vpo]
root = "vpo"

[patches.flute]
library = "vpo"
sfz = "flute.sfz"
gain_db = 0.0

[general_midi_fallback]
soundfont = "missing.sf2"
synth_gain = 0.2
gain_db = 0.0
""".strip()
        + "\n"
    )

    captured = []

    def fake_render_jobs(jobs, **kwargs):
        rendered = []
        for job in jobs:
            captured.append(job)
            job.output.parent.mkdir(parents=True, exist_ok=True)
            sf.write(job.output, np.ones((64, 2), dtype=np.float32) * 0.1, 48_000, subtype="FLOAT")
            rendered.append(
                cli.RenderedStem(
                    track=job.track,
                    instrument=job.instrument,
                    patch=job.patch,
                    path=job.output,
                    render_seconds=0.01,
                )
            )
        return rendered

    def fail_gm(*args, **kwargs):
        raise AssertionError("dedicated melody render should not fall back to GM")

    monkeypatch.setattr(cli, "render_jobs", fake_render_jobs)
    monkeypatch.setattr(cli, "render_fluidsynth_jobs", fail_gm)
    monkeypatch.chdir(tmp_path)

    args = _args(midi, config, tmp_path / "work-melody-sfz")
    args.include_melody = True
    args.keep_work = True
    assert cli.cmd_render(args) == 0
    assert len(captured) == 1
    job = captured[0]
    assert job.instrument == "flute"
    assert job.patch.name == "flute"


def test_include_melody_without_program_uses_melody_gm_fallback(monkeypatch, tmp_path: Path):
    midi = tmp_path / "melody-no-program.mid"
    _make_named_program_midi(midi, "Melody", 0)
    # remove the explicit program change so Melody has role metadata only
    mid = mido.MidiFile(midi)
    mid.tracks[-1] = mido.MidiTrack([msg for msg in mid.tracks[-1] if msg.type != "program_change"])
    mid.save(midi)

    config = _make_gm_fallback_config(tmp_path)
    captured = []
    monkeypatch.setattr(cli, "render_fluidsynth_jobs", _fake_gm_renderer_with_capture(captured))
    monkeypatch.chdir(tmp_path)

    args = _args(midi, config, tmp_path / "work-melody-gm")
    args.include_melody = True
    args.keep_work = True
    assert cli.cmd_render(args) == 0
    assert len(captured) == 1
    job = captured[0]
    assert job.instrument == "melody"
    assert job.patch.name == "gm_fallback_p080"
    split = mido.MidiFile(job.split_midi).tracks[-1]
    assert {msg.program for msg in split if msg.type == "program_change"} == {80}


def _make_melody_policy_config(tmp_path: Path, melody_table: str) -> Path:
    config_dir = tmp_path / f"config-melody-policy-{len(list(tmp_path.iterdir()))}"
    instruments = tmp_path / f"instruments-melody-policy-{len(list(tmp_path.iterdir()))}"
    vpo = instruments / "vpo"
    config_dir.mkdir()
    vpo.mkdir(parents=True)
    (vpo / "flute.sfz").write_text("// flute\n")
    (instruments / "MuseScore_General_Full.sf2").write_bytes(b"sf2")
    config = config_dir / "patches.toml"
    config.write_text(
        f"""
[paths]
instruments = "../{instruments.name}"

[melody]
{melody_table}

[libraries.vpo]
root = "vpo"

[patches.flute]
library = "vpo"
sfz = "flute.sfz"
gain_db = 0.0

[general_midi_fallback]
soundfont = "MuseScore_General_Full.sf2"
synth_gain = 0.2
gain_db = 0.0

[general_midi_fallback.program_for_instrument]
melody = 80
flute = 73
synth_lead = 80
""".strip()
        + "\n"
    )
    return config


def test_melody_gm_mode_bypasses_available_dedicated_patch(monkeypatch, tmp_path: Path):
    midi = tmp_path / "melody-gm.mid"
    _make_named_program_midi(midi, "Melody", 73)
    config = _make_melody_policy_config(tmp_path, 'mode = "gm"')
    captured = []

    def fail_sfz(*args, **kwargs):
        raise AssertionError("melody gm mode must bypass dedicated SFZ patches")

    monkeypatch.setattr(cli, "render_jobs", fail_sfz)
    monkeypatch.setattr(cli, "render_fluidsynth_jobs", _fake_gm_renderer_with_capture(captured))
    monkeypatch.chdir(tmp_path)

    args = _args(midi, config, tmp_path / "work-melody-force-gm")
    args.include_melody = True
    args.keep_work = True
    assert cli.cmd_render(args) == 0
    assert len(captured) == 1
    job = captured[0]
    assert job.instrument == "melody"
    assert job.patch.name == "gm_fallback_p073"
    split = mido.MidiFile(job.split_midi).tracks[-1]
    assert {msg.program for msg in split if msg.type == "program_change"} == {73}


def test_melody_gm_mode_can_force_explicit_program(monkeypatch, tmp_path: Path):
    midi = tmp_path / "melody-gm-override.mid"
    _make_named_program_midi(midi, "Melody", 80)
    config = _make_melody_policy_config(tmp_path, 'mode = "gm"\ngm_program = 22')
    captured = []

    monkeypatch.setattr(cli, "render_fluidsynth_jobs", _fake_gm_renderer_with_capture(captured))
    monkeypatch.chdir(tmp_path)

    args = _args(midi, config, tmp_path / "work-melody-force-gm22")
    args.include_melody = True
    args.keep_work = True
    assert cli.cmd_render(args) == 0
    assert len(captured) == 1
    job = captured[0]
    assert job.instrument == "melody"
    assert job.patch.name == "gm_fallback_p022"
    split = mido.MidiFile(job.split_midi).tracks[-1]
    assert {msg.program for msg in split if msg.type == "program_change"} == {22}


def test_melody_instrument_mode_forces_dedicated_instrument(monkeypatch, tmp_path: Path):
    midi = tmp_path / "melody-force-flute.mid"
    _make_named_program_midi(midi, "Melody", 80)
    config = _make_melody_policy_config(
        tmp_path,
        'mode = "instrument"\ninstrument = "flute"',
    )
    captured = []

    def fake_render_jobs(jobs, **kwargs):
        rendered = []
        for job in jobs:
            captured.append(job)
            job.output.parent.mkdir(parents=True, exist_ok=True)
            sf.write(job.output, np.ones((64, 2), dtype=np.float32) * 0.1, 48_000, subtype="FLOAT")
            rendered.append(
                cli.RenderedStem(
                    track=job.track,
                    instrument=job.instrument,
                    patch=job.patch,
                    path=job.output,
                    render_seconds=0.01,
                )
            )
        return rendered

    def fail_gm(*args, **kwargs):
        raise AssertionError("forced flute should use its available dedicated patch")

    monkeypatch.setattr(cli, "render_jobs", fake_render_jobs)
    monkeypatch.setattr(cli, "render_fluidsynth_jobs", fail_gm)
    monkeypatch.chdir(tmp_path)

    args = _args(midi, config, tmp_path / "work-melody-force-flute")
    args.include_melody = True
    args.keep_work = True
    assert cli.cmd_render(args) == 0
    assert len(captured) == 1
    assert captured[0].instrument == "flute"
    assert captured[0].patch.name == "flute"


def test_melody_instrument_mode_falls_back_to_target_instrument_gm(monkeypatch, tmp_path: Path):
    midi = tmp_path / "melody-force-harmonica.mid"
    _make_named_program_midi(midi, "Melody", 80)
    config = _make_melody_policy_config(
        tmp_path,
        'mode = "instrument"\ninstrument = "harmonica"',
    )
    # Append a representative GM mapping for the forced instrument.
    with config.open("a") as f:
        f.write("harmonica = 22\n")
    captured = []

    monkeypatch.setattr(cli, "render_fluidsynth_jobs", _fake_gm_renderer_with_capture(captured))
    monkeypatch.chdir(tmp_path)

    args = _args(midi, config, tmp_path / "work-melody-force-harmonica")
    args.include_melody = True
    args.keep_work = True
    assert cli.cmd_render(args) == 0
    assert len(captured) == 1
    job = captured[0]
    assert job.instrument == "harmonica"
    assert job.patch.name == "gm_fallback_p022"
    split = mido.MidiFile(job.split_midi).tracks[-1]
    assert {msg.program for msg in split if msg.type == "program_change"} == {22}


def test_performance_profile_changes_raw_cache_key(tmp_path: Path):
    from midi_render.midi import build_velocity_plan
    from midi_render.patches import Patch, PerformanceProfile

    track = mido.MidiTrack()
    track.append(mido.Message("note_on", channel=0, note=45, velocity=100, time=0))
    track.append(mido.Message("note_off", channel=0, note=45, velocity=0, time=120))
    sfz_path = tmp_path / "bass.sfz"
    sfz_path.write_text("// bass\n")
    midi_path = tmp_path / "song.mid"
    midi = mido.MidiFile(type=1)
    midi.tracks.append(track)
    midi.save(midi_path)
    patch = Patch("electric_bass", "test", sfz_path)

    plan_a = build_velocity_plan(track, PerformanceProfile("electric_bass", 50, 72, 85))
    plan_b = build_velocity_plan(track, PerformanceProfile("electric_bass", 50, 68, 85))
    assert plan_a is not None
    assert plan_b is not None

    render_tag = cli._sfz_render_cache_tag(
        midi_path,
        patch,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
    )
    plain = cli._raw_stem_path(
        tmp_path, 4, "electric_bass", "exact", patch, render_tag
    )
    cached_a = cli._raw_stem_path(
        tmp_path, 4, "electric_bass", "exact", patch, render_tag,
        performance_plan=plan_a,
    )
    cached_b = cli._raw_stem_path(
        tmp_path, 4, "electric_bass", "exact", patch, render_tag,
        performance_plan=plan_b,
    )

    assert f".render-{render_tag}.raw.wav" in plain.name
    assert ".perf-" in cached_a.name
    assert cached_a != cached_b


def test_sfz_raw_cache_tag_tracks_asset_renderer_and_midi_inputs(tmp_path: Path):
    from midi_render.patches import Patch

    midi_path = tmp_path / "song.mid"
    _make_drive_midi(midi_path)
    sfz_a = tmp_path / "a.sfz"
    sfz_b = tmp_path / "b.sfz"
    sfz_a.write_text("// a\n")
    sfz_b.write_text("// b\n")
    patch_a = Patch("guitar", "test", sfz_a, gain_db=-3.0, effects=("fx-a",))
    patch_a_mix_change = Patch("guitar", "test", sfz_a, gain_db=6.0, effects=("fx-b",))
    patch_b = Patch("guitar", "test", sfz_b)

    def tag(patch, *, samplerate=48_000, quality=2):
        return cli._sfz_render_cache_tag(
            midi_path,
            patch,
            blocksize=1024,
            samplerate=samplerate,
            quality=quality,
            polyphony=256,
        )

    baseline = tag(patch_a)
    assert tag(patch_a_mix_change) == baseline
    assert tag(patch_b) != baseline
    assert tag(patch_a, samplerate=44_100) != baseline
    assert tag(patch_a, quality=1) != baseline
    assert cli._sfz_render_cache_tag(
        midi_path,
        patch_a,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        midi_transform={"kind": "note-filter", "notes": [36]},
    ) != baseline

    # Editing the source MIDI in place must not reuse the previous raw stem.
    mid = mido.MidiFile(midi_path)
    mid.tracks[1].append(mido.Message("note_on", channel=4, note=64, velocity=90, time=0))
    mid.tracks[1].append(mido.Message("note_off", channel=4, note=64, velocity=0, time=120))
    mid.save(midi_path)
    assert tag(patch_a) != baseline


def test_gm_raw_cache_tag_tracks_soundfont_and_fluidsynth_settings(tmp_path: Path):
    from midi_render.patches import Patch

    midi_path = tmp_path / "song.mid"
    _make_drive_midi(midi_path)
    sf2_a = tmp_path / "a.sf2"
    sf2_b = tmp_path / "b.sf2"
    sf2_a.write_bytes(b"sf2-a")
    sf2_b.write_bytes(b"sf2-b")
    patch_a = Patch("gm_fallback_p029", "<general_midi_fallback>", sf2_a)
    patch_b = Patch("gm_fallback_p029", "<general_midi_fallback>", sf2_b)

    baseline = cli._gm_render_cache_tag(
        midi_path,
        patch_a,
        synth_gain=0.2,
        samplerate=48_000,
    )
    assert cli._gm_render_cache_tag(
        midi_path,
        patch_b,
        synth_gain=0.2,
        samplerate=48_000,
    ) != baseline
    assert cli._gm_render_cache_tag(
        midi_path,
        patch_a,
        synth_gain=0.3,
        samplerate=48_000,
    ) != baseline
    assert cli._gm_render_cache_tag(
        midi_path,
        patch_a,
        synth_gain=0.2,
        samplerate=44_100,
    ) != baseline
    assert cli._gm_render_cache_tag(
        midi_path,
        patch_a,
        synth_gain=0.2,
        samplerate=48_000,
        render_mode="single-native",
    ) != baseline
