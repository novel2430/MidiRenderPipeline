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
    mid, _ = cli.analyze_midi(midi)
    prepared = cli.make_split_midi(mid, midi, track_index, work_dir / "midi")
    tag = cli._sfz_render_cache_tag(
        prepared,
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


def test_single_track_render_reuses_cached_raw(monkeypatch, tmp_path: Path):
    midi = tmp_path / "song.mid"
    _make_drive_midi(midi)
    config = _make_config(tmp_path)
    work_dir = tmp_path / "work"
    raw = _sfz_cached_path(midi, config, work_dir, "electric_guitar_overdrive")
    raw.parent.mkdir(parents=True)
    sf.write(raw, np.ones((64, 2), dtype=np.float32) * 0.1, 48_000, subtype="FLOAT")

    monkeypatch.chdir(tmp_path)

    assert cli.cmd_render(_args(midi, config, work_dir)) == 0
    out = tmp_path / "renders/final/song.track-01.wav"
    audio, _ = sf.read(out, dtype="float32", always_2d=True)
    assert np.allclose(audio, 0.1, atol=1e-5)
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
    mid, _ = cli.analyze_midi(midi)
    kick_midi = cli.make_note_filtered_midi(
        mid, midi, 1, work_dir / "midi", [35, 36], "kick-layer"
    )
    kick_tag = cli._sfz_render_cache_tag(
        kick_midi,
        kick_patch,
        blocksize=args.blocksize,
        samplerate=args.samplerate,
        quality=args.quality,
        polyphony=args.polyphony,
    )
    kick_raw = stems / f"track-01.drums.kick-layer.render-{kick_tag}.raw.wav"
    sf.write(main_raw, np.ones((64, 2), dtype=np.float32) * 0.2, 48_000, subtype="FLOAT")
    sf.write(kick_raw, np.ones((64, 2), dtype=np.float32) * 0.2, 48_000, subtype="FLOAT")

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


def _planned_raw_jobs(midi: Path, config: Path, work_dir: Path, *, include_melody: bool = False):
    args = _args(midi, config, work_dir)
    args.include_melody = include_melody
    settings = cli._render_settings_from_args(args, active_songs=1)
    plan = cli._build_song_plan(
        midi,
        registry=cli.PatchRegistry(config),
        output=(work_dir.parent / f"{midi.stem}.wav"),
        work_dir=work_dir,
        settings=settings,
        track_index=1,
        reuse_raw=False,
        verbose=False,
    )
    jobs = []
    for task in plan.raw_tasks:
        if task.backend == cli.Backend.FLUIDSYNTH:
            jobs.extend(task.payload)
        else:
            jobs.append(task.payload)
    return plan, jobs


def test_conflicting_track_name_preserves_source_program_for_gm_fallback(monkeypatch, tmp_path: Path):
    midi = tmp_path / "pad.mid"
    _make_named_program_midi(midi, "Pad", 49)
    config = _make_gm_fallback_config(tmp_path)
    _, jobs = _planned_raw_jobs(midi, config, tmp_path / "work-gm")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.instrument == "string_ensemble"
    assert job.patch.name == "gm_fallback_p049"
    split = mido.MidiFile(job.split_midi).tracks[-1]
    assert {msg.program for msg in split if msg.type == "program_change"} == {49}


def test_trusted_program_is_preserved_for_gm_fallback(monkeypatch, tmp_path: Path):
    midi = tmp_path / "lead.mid"
    _make_named_program_midi(midi, "Lead", 82)
    config = _make_gm_fallback_config(tmp_path)
    _, jobs = _planned_raw_jobs(midi, config, tmp_path / "work-lead")
    assert len(jobs) == 1
    job = jobs[0]
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

    _, jobs = _planned_raw_jobs(
        midi, config, tmp_path / "work-melody-sfz", include_melody=True
    )
    assert len(jobs) == 1
    job = jobs[0]
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
    _, jobs = _planned_raw_jobs(
        midi, config, tmp_path / "work-melody-gm", include_melody=True
    )
    assert len(jobs) == 1
    job = jobs[0]
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
    _, jobs = _planned_raw_jobs(
        midi, config, tmp_path / "work-melody-force-gm", include_melody=True
    )
    assert len(jobs) == 1
    job = jobs[0]
    assert job.instrument == "melody"
    assert job.patch.name == "gm_fallback_p073"
    split = mido.MidiFile(job.split_midi).tracks[-1]
    assert {msg.program for msg in split if msg.type == "program_change"} == {73}


def test_melody_gm_mode_can_force_explicit_program(monkeypatch, tmp_path: Path):
    midi = tmp_path / "melody-gm-override.mid"
    _make_named_program_midi(midi, "Melody", 80)
    config = _make_melody_policy_config(tmp_path, 'mode = "gm"\ngm_program = 22')
    _, jobs = _planned_raw_jobs(
        midi, config, tmp_path / "work-melody-force-gm22", include_melody=True
    )
    assert len(jobs) == 1
    job = jobs[0]
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
    _, jobs = _planned_raw_jobs(
        midi, config, tmp_path / "work-melody-force-flute", include_melody=True
    )
    assert len(jobs) == 1
    assert jobs[0].instrument == "flute"
    assert jobs[0].patch.name == "flute"


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
    _, jobs = _planned_raw_jobs(
        midi, config, tmp_path / "work-melody-force-harmonica", include_melody=True
    )
    assert len(jobs) == 1
    job = jobs[0]
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

    prepared_plain = cli.make_split_midi(midi, midi_path, 0, tmp_path / "plain")
    prepared_a = cli.make_split_midi(midi, midi_path, 0, tmp_path / "a", plan_a)
    prepared_b = cli.make_split_midi(midi, midi_path, 0, tmp_path / "b", plan_b)
    render_tag = cli._sfz_render_cache_tag(
        prepared_plain,
        patch,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
    )
    tag_a = cli._sfz_render_cache_tag(
        prepared_a, patch, blocksize=1024, samplerate=48_000, quality=2, polyphony=256
    )
    tag_b = cli._sfz_render_cache_tag(
        prepared_b, patch, blocksize=1024, samplerate=48_000, quality=2, polyphony=256
    )
    plain = cli._raw_stem_path(
        tmp_path, 4, "electric_bass", "exact", patch, render_tag
    )
    cached_a = cli._raw_stem_path(
        tmp_path, 4, "electric_bass", "exact", patch, tag_a,
        performance_plan=plan_a,
    )
    cached_b = cli._raw_stem_path(
        tmp_path, 4, "electric_bass", "exact", patch, tag_b,
        performance_plan=plan_b,
    )

    assert f".render-{render_tag}.raw.wav" in plain.name
    assert tag_a != tag_b
    assert ".perf-" in cached_a.name
    assert cached_a != cached_b


def test_sfz_raw_cache_tag_tracks_asset_renderer_and_prepared_midi(tmp_path: Path):
    from midi_render.patches import Patch

    source = tmp_path / "song.mid"
    _make_drive_midi(source)
    mid, _ = cli.analyze_midi(source)
    prepared = cli.make_split_midi(mid, source, 1, tmp_path / "prepared-a")
    same_bytes = tmp_path / "prepared-copy.mid"
    same_bytes.write_bytes(prepared.read_bytes())
    sfz_a = tmp_path / "a.sfz"
    sfz_b = tmp_path / "b.sfz"
    sfz_a.write_text("// a\n")
    sfz_b.write_text("// b\n")
    patch_a = Patch("guitar", "test", sfz_a, gain_db=-3.0, effects=("fx-a",))
    patch_a_mix_change = Patch("guitar", "test", sfz_a, gain_db=6.0, effects=("fx-b",))
    patch_b = Patch("guitar", "test", sfz_b)

    def tag(patch, *, midi=prepared, samplerate=48_000, quality=2):
        return cli._sfz_render_cache_tag(
            midi,
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
    # Cache identity is content-addressed, not tied to a temporary work path.
    assert tag(patch_a, midi=same_bytes) == baseline

    # Changing the actual bytes handed to sfizz must invalidate the raw stem.
    edited = mido.MidiFile(prepared)
    edited.tracks[-1].append(mido.Message("control_change", channel=4, control=64, value=127, time=0))
    changed = tmp_path / "prepared-changed.mid"
    edited.save(changed)
    assert tag(patch_a, midi=changed) != baseline


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


def test_prepared_midi_cache_identity_changes_for_controller_edits(tmp_path: Path):
    source = tmp_path / "piano.mid"
    mid = mido.MidiFile(type=1)
    conductor = mido.MidiTrack()
    mid.tracks.append(conductor)
    track = mido.MidiTrack()
    track.append(mido.Message("control_change", channel=0, control=64, value=127, time=0))
    track.append(mido.Message("note_on", channel=0, note=60, velocity=90, time=0))
    track.append(mido.Message("note_off", channel=0, note=60, velocity=0, time=120))
    mid.tracks.append(track)
    mid.save(source)

    prepared_a = cli.make_split_midi(mid, source, 1, tmp_path / "a")
    edited = mido.MidiFile(source)
    for msg in edited.tracks[1]:
        if msg.type == "control_change" and msg.control == 64:
            msg.value = 0
    prepared_b = cli.make_split_midi(edited, source, 1, tmp_path / "b")

    assert cli._prepared_midi_cache_identity(prepared_a) != cli._prepared_midi_cache_identity(prepared_b)


def test_rebuild_raw_parser_flag_is_available_for_render_and_batch():
    parser = cli.build_parser()
    render = parser.parse_args(["render", "song.mid", "--rebuild-raw"])
    batch = parser.parse_args(["batch", "dataset", "--rebuild-raw"])
    assert render.rebuild_raw is True
    assert batch.rebuild_raw is True
