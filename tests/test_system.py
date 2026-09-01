from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from midi_render.midi import TrackInfo
from midi_render.patches import MasterConfig, Patch
from midi_render.renderer import RenderedStem
from midi_render.system import (
    Backend,
    RenderSettings,
    RenderingCoordinator,
    SongPlan,
    Stage,
    StateStore,
    StemPlan,
    make_song_id,
    make_stem_id,
    make_task,
)


def _cached_plan(tmp_path: Path, name: str, value: float = 0.1) -> SongPlan:
    midi = tmp_path / f"{name}.mid"
    midi.write_bytes(b"midi-placeholder")
    raw = tmp_path / name / "stems" / "track-01.raw.wav"
    raw.parent.mkdir(parents=True)
    sf.write(raw, np.ones((64, 2), dtype=np.float32) * value, 48_000, subtype="FLOAT")
    output = tmp_path / "out" / f"{name}.wav"
    track = TrackInfo(1, "Piano", 1, (0,), (0,), ((0, 0),))
    patch = Patch("piano", "test", tmp_path / "unused.sfz", gain_db=0.0, effects=())
    rendered = RenderedStem(track, "piano", patch, raw, 0.0)
    stem_id = make_stem_id(1, "piano")
    settings = RenderSettings(workers=2, active_songs=1, keep_work=True).normalized()
    return SongPlan(
        song_id=make_song_id(midi, output),
        midi_path=midi,
        output=output,
        work_dir=tmp_path / name,
        split_dir=tmp_path / name / "midi",
        config_path=tmp_path / "unused.toml",
        settings=settings,
        stems=(StemPlan(stem_id, 1, "piano", Backend.SFZ, raw, ()),),
        raw_tasks=(),
        cached_stems=(rendered,),
        track_index=None,
        master=MasterConfig(normalize_peak_db=-1.0, gain_db=0.0),
    )


def test_coordinator_uses_cached_raw_and_persists_done_state(tmp_path: Path):
    plan = _cached_plan(tmp_path, "song")
    state = StateStore(tmp_path / "state.sqlite3")
    try:
        with RenderingCoordinator(plan.settings, state=state) as coordinator:
            results = coordinator.run([plan])
        assert len(results) == 1
        assert results[0].status == "DONE"
        assert plan.output.is_file()
        assert state.song_status(plan.song_id) == "DONE"
    finally:
        state.close()


def test_active_window_admits_next_song_after_completion(tmp_path: Path):
    first = _cached_plan(tmp_path, "first", 0.1)
    second = _cached_plan(tmp_path, "second", 0.2)
    settings = RenderSettings(workers=2, active_songs=1, keep_work=True).normalized()
    first.settings = settings
    second.settings = settings
    with RenderingCoordinator(settings) as coordinator:
        results = coordinator.run([first, second])
    assert [r.status for r in results] == ["DONE", "DONE"]
    assert {r.midi_path.name for r in results} == {"first.mid", "second.mid"}


def test_task_model_allows_one_physical_task_to_cover_multiple_stems():
    task = make_task(
        "song",
        Stage.RAW,
        Backend.FLUIDSYNTH,
        ("track-01:organ", "track-02:harmonica"),
        payload=("job-a", "job-b"),
    )
    assert task.stage == Stage.RAW
    assert task.backend == Backend.FLUIDSYNTH
    assert task.stem_ids == ("track-01:organ", "track-02:harmonica")


def test_fx_backpressure_blocks_new_raw_dispatch_when_fx_pool_is_saturated(tmp_path: Path):
    settings = RenderSettings(
        workers=2,
        sfz_workers=2,
        gm_workers=1,
        fx_workers=1,
        mix_workers=1,
        active_songs=1,
        max_fx_backlog=1,
    ).normalized()
    coordinator = RenderingCoordinator(settings)
    try:
        coordinator.inflight_by_backend[Backend.FX] = 1
        coordinator.pending_raw.append(
            make_task("song", Stage.RAW, Backend.SFZ, ("track-01:piano",), payload="raw")
        )
        assert coordinator._next_dispatchable_task() is None
    finally:
        coordinator.inflight_by_backend[Backend.FX] = 0
        coordinator.close()


def test_song_id_changes_when_source_midi_is_edited_in_place(tmp_path: Path):
    midi = tmp_path / "song.mid"
    output = tmp_path / "song.wav"
    midi.write_bytes(b"midi-v1")
    first = make_song_id(midi, output, "run")
    midi.write_bytes(b"midi-v2")
    second = make_song_id(midi, output, "run")
    assert second != first


def test_gm_worker_captures_native_stderr(monkeypatch):
    import os
    import midi_render.system as system

    def fake_render(jobs, *, workers, samplerate):
        os.write(2, b"ALSA/JACK warning from native library\n")
        return []

    monkeypatch.setattr(system, "render_fluidsynth_jobs", fake_render)
    result = system._execute_gm(system._GMPayload((), 48_000))
    assert result.value == []
    assert "ALSA/JACK warning" in result.diagnostics
