from pathlib import Path

import mido

from midi_render.midi import analyze_midi, make_note_filtered_midi, make_program_override_midi, midi_timeline_metrics


def test_analyzer_reports_multi_program(tmp_path: Path):
    mid = mido.MidiFile(type=1)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name="Conductor", time=0))
    mid.tracks.append(conductor)

    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name="Egt", time=0))
    track.append(mido.Message("program_change", channel=0, program=27, time=0))
    track.append(mido.Message("note_on", channel=0, note=60, velocity=90, time=0))
    track.append(mido.Message("note_off", channel=0, note=60, velocity=0, time=120))
    track.append(mido.Message("program_change", channel=0, program=30, time=0))
    mid.tracks.append(track)

    path = tmp_path / "x.mid"
    mid.save(path)
    _, tracks = analyze_midi(path)
    assert tracks[1].programs == (27, 30)
    assert any("multiple programs" in x for x in tracks[1].warnings)


def test_make_note_filtered_midi_keeps_only_selected_notes_and_preserves_time(tmp_path: Path):
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name="Conductor", time=0))
    mid.tracks.append(conductor)

    drums = mido.MidiTrack()
    drums.append(mido.MetaMessage("track_name", name="Drum", time=0))
    drums.append(mido.Message("note_on", channel=9, note=42, velocity=90, time=10))
    drums.append(mido.Message("note_off", channel=9, note=42, velocity=0, time=20))
    drums.append(mido.Message("note_on", channel=9, note=36, velocity=100, time=30))
    drums.append(mido.Message("note_off", channel=9, note=36, velocity=0, time=40))
    mid.tracks.append(drums)

    source = tmp_path / "song.mid"
    mid.save(source)
    loaded = mido.MidiFile(source)

    out = make_note_filtered_midi(
        loaded, source, 1, tmp_path / "split", [35, 36], "kick-layer"
    )
    filtered = mido.MidiFile(out).tracks[-1]

    note_events = [msg for msg in filtered if msg.type in {"note_on", "note_off"}]
    assert [msg.note for msg in note_events] == [36, 36]

    absolute = 0
    note_times = []
    for msg in filtered:
        absolute += msg.time
        if msg.type in {"note_on", "note_off"}:
            note_times.append(absolute)
    assert note_times == [60, 100]


def test_make_program_override_midi_forces_program_without_shifting_notes(tmp_path: Path):
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name="Conductor", time=0))
    mid.tracks.append(conductor)

    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name="Pad", time=0))
    track.append(mido.Message("program_change", channel=2, program=49, time=0))
    track.append(mido.Message("note_on", channel=2, note=60, velocity=90, time=120))
    track.append(mido.Message("note_off", channel=2, note=60, velocity=0, time=240))
    mid.tracks.append(track)

    source = tmp_path / "song.mid"
    mid.save(source)
    loaded = mido.MidiFile(source)
    out = make_program_override_midi(loaded, source, 1, tmp_path / "split", 89)
    forced = mido.MidiFile(out).tracks[-1]

    assert {msg.program for msg in forced if msg.type == "program_change"} == {89}
    absolute = 0
    note_times = []
    for msg in forced:
        absolute += msg.time
        if msg.type in {"note_on", "note_off"}:
            note_times.append(absolute)
    assert note_times == [120, 360]


def _velocity_track(values: list[int]) -> mido.MidiTrack:
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name="Bass", time=0))
    for value in values:
        track.append(mido.Message("note_on", channel=0, note=48, velocity=value, time=10))
        track.append(mido.Message("note_off", channel=0, note=48, velocity=0, time=10))
    return track


def test_velocity_plan_constant_like_uses_nominal_velocity():
    from midi_render.midi import build_velocity_plan, clone_track_with_velocity_plan
    from midi_render.patches import PerformanceProfile

    profile = PerformanceProfile(
        instrument="electric_bass",
        velocity_min=50,
        velocity_nominal=72,
        velocity_max=85,
        constant_spread_max=4.0,
    )
    track = _velocity_track([100, 100, 99, 101, 100])
    plan = build_velocity_plan(track, profile)
    assert plan is not None
    assert plan.mode == "constant"

    adapted = clone_track_with_velocity_plan(track, plan)
    velocities = [
        msg.velocity for msg in adapted if msg.type == "note_on" and msg.velocity > 0
    ]
    assert velocities == [72, 72, 72, 72, 72]


def test_velocity_plan_dynamic_inside_range_is_identity():
    from midi_render.midi import build_velocity_plan, clone_track_with_velocity_plan
    from midi_render.patches import PerformanceProfile

    profile = PerformanceProfile(
        instrument="string_ensemble",
        velocity_min=30,
        velocity_nominal=52,
        velocity_max=68,
        constant_spread_max=4.0,
    )
    source = [33, 40, 48, 55, 55, 55]
    track = _velocity_track(source)
    plan = build_velocity_plan(track, profile)
    assert plan is not None
    assert plan.mode == "identity"
    assert plan.scale == 1.0
    assert plan.shift == 0.0

    adapted = clone_track_with_velocity_plan(track, plan)
    velocities = [
        msg.velocity for msg in adapted if msg.type == "note_on" and msg.velocity > 0
    ]
    assert velocities == source


def test_velocity_plan_dynamic_above_range_shifts_without_rescaling():
    from midi_render.midi import build_velocity_plan, clone_track_with_velocity_plan
    from midi_render.patches import PerformanceProfile

    profile = PerformanceProfile(
        instrument="string_ensemble",
        velocity_min=30,
        velocity_nominal=52,
        velocity_max=68,
        constant_spread_max=4.0,
    )
    source = [75, 80, 85, 90, 95, 100]
    track = _velocity_track(source)
    plan = build_velocity_plan(track, profile)
    assert plan is not None
    assert plan.mode == "shift"
    assert plan.scale == 1.0
    assert plan.shift < 0.0

    adapted = clone_track_with_velocity_plan(track, plan)
    velocities = [
        msg.velocity for msg in adapted if msg.type == "note_on" and msg.velocity > 0
    ]
    assert velocities == sorted(velocities)
    assert max(velocities) <= 68
    # The source contour is translated, not stretched or compressed.
    assert velocities[3] - velocities[1] == source[3] - source[1]


def test_velocity_plan_dynamic_below_range_shifts_without_rescaling():
    from midi_render.midi import build_velocity_plan
    from midi_render.patches import PerformanceProfile

    profile = PerformanceProfile(
        instrument="electric_bass",
        velocity_min=42,
        velocity_nominal=62,
        velocity_max=78,
        constant_spread_max=2.0,
    )
    track = _velocity_track([20, 24, 28, 32, 36, 40])
    plan = build_velocity_plan(track, profile)
    assert plan is not None
    assert plan.mode == "shift"
    assert plan.scale == 1.0
    assert plan.shift > 0.0


def test_velocity_plan_dynamic_wider_than_target_compresses_but_never_expands():
    from midi_render.midi import build_velocity_plan
    from midi_render.patches import PerformanceProfile

    profile = PerformanceProfile(
        instrument="electric_bass",
        velocity_min=42,
        velocity_nominal=62,
        velocity_max=78,
        constant_spread_max=2.0,
    )
    track = _velocity_track([10, 20, 40, 60, 90, 110, 120])
    plan = build_velocity_plan(track, profile)
    assert plan is not None
    assert plan.mode == "compress"
    assert 0.0 < plan.scale < 1.0
    mapped = [plan.map_velocity(v) for v in [10, 20, 40, 60, 90, 110, 120]]
    assert mapped == sorted(mapped)
    assert min(mapped) >= 42
    assert max(mapped) <= 78


def test_program_override_applies_velocity_plan_without_shifting_notes(tmp_path: Path):
    from midi_render.midi import build_velocity_plan
    from midi_render.patches import PerformanceProfile

    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name="Conductor", time=0))
    mid.tracks.append(conductor)
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name="Bass", time=0))
    track.append(mido.Message("program_change", channel=0, program=33, time=0))
    track.append(mido.Message("note_on", channel=0, note=45, velocity=100, time=120))
    track.append(mido.Message("note_off", channel=0, note=45, velocity=0, time=240))
    mid.tracks.append(track)

    source = tmp_path / "bass.mid"
    mid.save(source)
    loaded = mido.MidiFile(source)
    profile = PerformanceProfile("electric_bass", 50, 72, 85)
    plan = build_velocity_plan(loaded.tracks[1], profile)
    out = make_program_override_midi(
        loaded,
        source,
        1,
        tmp_path / "split",
        33,
        velocity_plan=plan,
    )
    forced = mido.MidiFile(out).tracks[-1]
    note_on = next(msg for msg in forced if msg.type == "note_on" and msg.velocity > 0)
    assert note_on.velocity == 72

    absolute = 0
    note_times = []
    for msg in forced:
        absolute += msg.time
        if msg.type in {"note_on", "note_off"}:
            note_times.append(absolute)
    assert note_times == [120, 360]


def test_midi_timeline_metrics_follow_tempo_and_time_signature():
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    conductor.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
    conductor.append(mido.MetaMessage("time_signature", numerator=3, denominator=4, time=1920))
    conductor.append(mido.MetaMessage("set_tempo", tempo=1_000_000, time=0))
    conductor.append(mido.MetaMessage("end_of_track", time=1440))
    mid.tracks.append(conductor)

    seconds, bars = midi_timeline_metrics(mid)

    assert seconds == 5.0
    assert bars == 2.0
