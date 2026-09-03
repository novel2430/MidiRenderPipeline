from __future__ import annotations

from pathlib import Path
import threading

import mido

from midi_render.midi import TrackInfo
from midi_render.patches import Patch
from midi_render.renderer import RenderJob
import midi_render.sfizz_persistent as persistent


def _midi(path: Path, *, program: int = 12) -> None:
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
    mid.tracks.append(conductor)
    track = mido.MidiTrack()
    track.append(mido.Message("program_change", channel=0, program=program, time=0))
    track.append(mido.Message("aftertouch", channel=0, value=45, time=0))
    track.append(mido.Message("polytouch", channel=0, note=60, value=67, time=0))
    track.append(mido.Message("note_on", channel=0, note=60, velocity=90, time=0))
    track.append(mido.Message("note_off", channel=0, note=60, velocity=0, time=480))
    mid.tracks.append(track)
    mid.save(path)


def _job(tmp_path: Path, name: str, sfz_name: str) -> RenderJob:
    sfz = tmp_path / sfz_name
    sfz.write_text("<region> sample=dummy.wav\n")
    midi = tmp_path / f"{name}.mid"
    _midi(midi)
    track = TrackInfo(1, name, 1, (0,), (0,), ((0, 0),))
    patch = Patch(sfz_name, "test", sfz)
    return RenderJob(track, name, patch, midi, tmp_path / f"{name}.wav")


def _load_info(
    working_set: int, *, sample: int | None = None, full_samples: int = 1
) -> persistent.WorkerLoadInfo:
    sample_bytes = working_set if sample is None else sample
    return persistent.WorkerLoadInfo(
        milliseconds=1.0,
        regions=1,
        preloaded_samples=1,
        sfizz_bytes=working_set,
        sample_resident_bytes=sample_bytes,
        sample_peak_bytes=sample_bytes,
        full_resident_samples=full_samples,
    )


def _render_info(
    loads: int,
    working_set: int,
    *,
    sample: int | None = None,
    sample_peak: int | None = None,
    full_samples: int = 1,
) -> persistent.WorkerRenderInfo:
    sample_bytes = working_set if sample is None else sample
    return persistent.WorkerRenderInfo(
        milliseconds=1.0,
        frames=64,
        active_after=0,
        tail_limit=False,
        instrument_loads=loads,
        sfizz_bytes=working_set,
        sample_resident_bytes=sample_bytes,
        sample_peak_bytes=sample_bytes if sample_peak is None else sample_peak,
        full_resident_samples=full_samples,
    )


def _runtime(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        persistent,
        "require_sfizz_runtime",
        lambda **kwargs: (tmp_path / "worker", tmp_path / "libsfizz.so"),
    )


def test_event_bridge_preserves_sfizz_channel_messages(tmp_path: Path):
    midi = tmp_path / "events.mid"
    _midi(midi, program=31)
    out = tmp_path / "events.mrpev"
    info = persistent.write_event_file(midi, out, 48_000)
    text = out.read_text()
    assert info.events == 5
    assert " program 31\n" in text
    assert " aftertouch 45\n" in text
    assert " polytouch 60 67\n" in text
    assert " note_on 60 90\n" in text


def test_pool_reuses_one_worker_and_reports_new_reuse_semantics(monkeypatch, tmp_path: Path):
    created = []

    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0
            self.closed = False
            created.append(self)

        def load(self, sfz):
            self.loads += 1
            return _load_info(80, sample=40)

        def render(self, events, output, *, seed, midi_seconds):
            output.write_bytes(b"wav")
            return _render_info(
                self.loads, 100, sample=60, sample_peak=60, full_samples=4
            )

        def close(self, *, graceful=True):
            self.closed = True

    _runtime(monkeypatch, tmp_path)
    job = _job(tmp_path, "piano", "piano.sfz")
    pool = persistent.PersistentSfizzPool(
        max_concurrency=2,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=1000,
        worker_factory=FakeWorker,
    )
    try:
        first = pool.submit(job).result(timeout=2)
        job.output.unlink()
        second = pool.submit(job).result(timeout=2)
        stats = pool.stats()
    finally:
        pool.close()

    assert first.worker_started is True
    assert second.worker_started is False
    assert first.working_set_bytes == 100
    assert first.working_set_growth_bytes == 20
    assert first.sample_resident_bytes == 60
    assert first.full_resident_samples == 4
    assert len(created) == 1
    assert stats.tasks == 2
    assert stats.worker_starts == 1
    assert stats.worker_reuses == 1
    assert stats.current_sample_resident_bytes == 60
    assert stats.peak_sample_resident_bytes == 60
    assert stats.full_resident_samples == 4
    assert stats.max_observed_task_growth_bytes == 20


def test_unseen_worker_is_learned_then_idle_lru_is_trimmed(monkeypatch, tmp_path: Path):
    created = []

    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0
            self.closed = False
            created.append(self)

        def load(self, sfz):
            self.loads += 1
            return _load_info(100)

        def render(self, events, output, *, seed, midi_seconds):
            output.write_bytes(b"wav")
            return _render_info(self.loads, 100)

        def close(self, *, graceful=True):
            self.closed = True

    _runtime(monkeypatch, tmp_path)
    piano = _job(tmp_path, "piano", "piano.sfz")
    bass = _job(tmp_path, "bass", "bass.sfz")
    pool = persistent.PersistentSfizzPool(
        max_concurrency=2,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=150,
        worker_factory=FakeWorker,
    )
    try:
        pool.submit(piano).result(timeout=2)
        # Bass has never been observed, so it is admitted once with no invented
        # whole-instrument reservation. LOAD reveals the real footprint and the
        # idle piano worker is then trimmed.
        assert pool.can_accept(bass) is True
        pool.submit(bass).result(timeout=2)
        stats = pool.stats()
    finally:
        pool.close()

    assert created[0].closed is True
    assert stats.worker_evictions == 1
    assert stats.current_working_set_bytes == 100
    assert stats.peak_working_set_bytes == 200


def test_explicit_budget_is_a_working_set_target_not_a_hard_unknown_load_limit(monkeypatch, tmp_path: Path):
    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0

        def load(self, sfz):
            self.loads += 1
            return _load_info(100)

        def render(self, events, output, *, seed, midi_seconds):
            output.write_bytes(b"wav")
            return _render_info(self.loads, 120)

        def close(self, *, graceful=True):
            pass

    _runtime(monkeypatch, tmp_path)
    job = _job(tmp_path, "piano", "piano.sfz")
    pool = persistent.PersistentSfizzPool(
        max_concurrency=1,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=50,
        worker_factory=FakeWorker,
    )
    try:
        result = pool.submit(job).result(timeout=2)
        stats = pool.stats()
    finally:
        pool.close()

    assert result.worker_started is True
    assert result.working_set_bytes == 120
    assert stats.current_working_set_bytes == 120
    assert stats.worker_failures == 0


def test_observed_worker_peak_guides_recreated_worker_admission(monkeypatch, tmp_path: Path):
    created = []

    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0
            self.closed = False
            created.append(self)

        def load(self, sfz):
            self.loads += 1
            return _load_info(100)

        def render(self, events, output, *, seed, midi_seconds):
            output.write_bytes(b"wav")
            return _render_info(self.loads, 100)

        def close(self, *, graceful=True):
            self.closed = True

    _runtime(monkeypatch, tmp_path)
    piano = _job(tmp_path, "piano", "piano.sfz")
    bass = _job(tmp_path, "bass", "bass.sfz")
    pool = persistent.PersistentSfizzPool(
        max_concurrency=2,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=150,
        worker_factory=FakeWorker,
    )
    try:
        pool.submit(piano).result(timeout=2)
        pool.submit(bass).result(timeout=2)  # evicts piano after learning bass
        assert created[0].closed is True
        pool.submit(piano).result(timeout=2)  # known 100-byte peak evicts bass before restart
        stats = pool.stats()
    finally:
        pool.close()

    assert created[1].closed is True
    assert stats.worker_starts == 3
    assert stats.worker_evictions == 2
    assert stats.current_working_set_bytes == 100


def test_observed_task_growth_can_throttle_while_another_worker_is_busy(monkeypatch, tmp_path: Path):
    bass_started = threading.Event()
    release_bass = threading.Event()

    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0
            self.name = ""
            self.renders = 0

        def load(self, sfz):
            self.loads += 1
            self.name = Path(sfz).name
            return _load_info(50 if self.name == "piano.sfz" else 100)

        def render(self, events, output, *, seed, midi_seconds):
            self.renders += 1
            if self.name == "bass.sfz" and self.renders == 2:
                bass_started.set()
                assert release_bass.wait(timeout=2)
            output.write_bytes(b"wav")
            if self.name == "piano.sfz":
                return _render_info(self.loads, 100)  # learns +50 task growth
            return _render_info(self.loads, 100)

        def close(self, *, graceful=True):
            pass

    _runtime(monkeypatch, tmp_path)
    piano = _job(tmp_path, "piano", "piano.sfz")
    bass = _job(tmp_path, "bass", "bass.sfz")
    pool = persistent.PersistentSfizzPool(
        max_concurrency=2,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=220,
        worker_factory=FakeWorker,
    )
    try:
        pool.submit(piano).result(timeout=2)
        pool.submit(bass).result(timeout=2)
        bass_future = pool.submit(bass)
        assert bass_started.wait(timeout=2)
        # Current observed workers are 200 and piano previously grew by 50.
        # With bass busy there is no safe idle victim, so the known growth
        # estimate delays piano instead of pretending memory is free.
        assert pool.can_accept(piano) is False
        release_bass.set()
        bass_future.result(timeout=2)
        assert pool.can_accept(piano) is True
    finally:
        release_bass.set()
        pool.close()


def test_busy_instrument_has_no_replica(monkeypatch, tmp_path: Path):
    started = threading.Event()
    release = threading.Event()

    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0

        def load(self, sfz):
            self.loads += 1
            return _load_info(100)

        def render(self, events, output, *, seed, midi_seconds):
            started.set()
            assert release.wait(timeout=2)
            output.write_bytes(b"wav")
            return _render_info(self.loads, 100)

        def close(self, *, graceful=True):
            pass

    _runtime(monkeypatch, tmp_path)
    job = _job(tmp_path, "piano", "piano.sfz")
    pool = persistent.PersistentSfizzPool(
        max_concurrency=2,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=8 * 1024**3,
        worker_factory=FakeWorker,
    )
    try:
        future = pool.submit(job)
        assert started.wait(timeout=2)
        assert pool.can_accept(job) is False
        release.set()
        future.result(timeout=2)
    finally:
        release.set()
        pool.close()


def test_worker_failure_invalidates_entry_and_removes_partial_output(monkeypatch, tmp_path: Path):
    created = []

    class FailingWorker:
        def __init__(self, **kwargs):
            self.loads = 0
            self.closed = False
            created.append(self)

        def load(self, sfz):
            self.loads += 1
            return _load_info(100)

        def render(self, events, output, *, seed, midi_seconds):
            output.write_bytes(b"partial")
            raise RuntimeError("synthetic worker failure")

        def close(self, *, graceful=True):
            self.closed = True

    _runtime(monkeypatch, tmp_path)
    job = _job(tmp_path, "piano", "piano.sfz")
    pool = persistent.PersistentSfizzPool(
        max_concurrency=1,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=1000,
        worker_factory=FailingWorker,
    )
    try:
        try:
            pool.submit(job).result(timeout=2)
        except RuntimeError as exc:
            assert "synthetic worker failure" in str(exc)
        else:
            raise AssertionError("worker failure should propagate")
        stats = pool.stats()
        assert job.output.exists() is False
        assert stats.worker_failures == 1
        assert stats.current_working_set_bytes == 0
        assert created[0].closed is True
    finally:
        pool.close()


def test_renderer_identity_is_portable_across_install_paths(tmp_path: Path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()

    worker_bytes = b"same-worker-binary"
    library_bytes = b"same-libsfizz-binary"
    left_worker = left / "mrp-sfizz-worker"
    right_worker = right / "renamed-worker"
    left_lib = left / "libsfizz.so"
    right_lib = right / "libsfizz.so.1.2.3"
    for path, data in (
        (left_worker, worker_bytes),
        (right_worker, worker_bytes),
        (left_lib, library_bytes),
        (right_lib, library_bytes),
    ):
        path.write_bytes(data)
    left_worker.chmod(0o755)
    right_worker.chmod(0o755)
    right_worker.touch()
    right_lib.touch()

    left_identity = persistent.sfizz_renderer_identity(worker=left_worker, library=left_lib)
    right_identity = persistent.sfizz_renderer_identity(worker=right_worker, library=right_lib)

    assert left_identity == right_identity
    assert left_identity["worker_protocol"] == 5
    assert left_identity["offline_api"] == 3
    assert left_identity["sample_loading"] == "deterministic-lazy"
    assert "path" not in left_identity["worker"]
    assert "mtime_ns" not in left_identity["worker"]


def test_warm_busy_instrument_scales_out_to_second_replica(monkeypatch, tmp_path: Path):
    created = []
    second_render_started = threading.Event()
    release_second_render = threading.Event()

    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0
            self.renders = 0
            self.closed = False
            created.append(self)

        def load(self, sfz):
            self.loads += 1
            return _load_info(100)

        def render(self, events, output, *, seed, midi_seconds):
            self.renders += 1
            if self is created[0] and self.renders == 2:
                second_render_started.set()
                assert release_second_render.wait(timeout=2)
            output.write_bytes(b"wav")
            return _render_info(self.loads, 100)

        def close(self, *, graceful=True):
            self.closed = True

    _runtime(monkeypatch, tmp_path)
    warm = _job(tmp_path, "warm", "piano.sfz")
    first = _job(tmp_path, "first", "piano.sfz")
    second = _job(tmp_path, "second", "piano.sfz")
    pool = persistent.PersistentSfizzPool(
        max_concurrency=2,
        max_replicas_per_key=2,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=1000,
        worker_factory=FakeWorker,
    )
    try:
        pool.submit(warm).result(timeout=2)
        first_future = pool.submit(first)
        assert second_render_started.wait(timeout=2)
        assert pool.can_accept(second) is True
        second_result = pool.submit(second).result(timeout=2)
        stats = pool.stats()
        assert second_result.worker_started is True
        assert len(created) == 2
        assert stats.worker_scale_outs == 1
        assert stats.peak_replicas_per_key == 2
        assert stats.peak_active_workers == 2
        assert stats.peak_resident_workers == 2
        release_second_render.set()
        first_future.result(timeout=2)
    finally:
        release_second_render.set()
        pool.close()


def test_cold_instrument_does_not_scale_out_before_first_completed_render(monkeypatch, tmp_path: Path):
    started = threading.Event()
    release = threading.Event()

    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0

        def load(self, sfz):
            self.loads += 1
            return _load_info(100)

        def render(self, events, output, *, seed, midi_seconds):
            started.set()
            assert release.wait(timeout=2)
            output.write_bytes(b"wav")
            return _render_info(self.loads, 100)

        def close(self, *, graceful=True):
            pass

    _runtime(monkeypatch, tmp_path)
    first = _job(tmp_path, "first", "piano.sfz")
    second = _job(tmp_path, "second", "piano.sfz")
    pool = persistent.PersistentSfizzPool(
        max_concurrency=2,
        max_replicas_per_key=2,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=1000,
        worker_factory=FakeWorker,
    )
    try:
        future = pool.submit(first)
        assert started.wait(timeout=2)
        assert pool.can_accept(second) is False
        assert pool.stats().worker_scale_outs == 0
        release.set()
        future.result(timeout=2)
        assert pool.can_accept(second) is True
    finally:
        release.set()
        pool.close()


def test_memory_budget_can_block_warm_scale_out(monkeypatch, tmp_path: Path):
    busy_started = threading.Event()
    release_busy = threading.Event()

    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0
            self.renders = 0

        def load(self, sfz):
            self.loads += 1
            return _load_info(100)

        def render(self, events, output, *, seed, midi_seconds):
            self.renders += 1
            if self.renders == 2:
                busy_started.set()
                assert release_busy.wait(timeout=2)
            output.write_bytes(b"wav")
            return _render_info(self.loads, 100)

        def close(self, *, graceful=True):
            pass

    _runtime(monkeypatch, tmp_path)
    warm = _job(tmp_path, "warm", "piano.sfz")
    first = _job(tmp_path, "first", "piano.sfz")
    second = _job(tmp_path, "second", "piano.sfz")
    pool = persistent.PersistentSfizzPool(
        max_concurrency=2,
        max_replicas_per_key=2,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=150,
        worker_factory=FakeWorker,
    )
    try:
        pool.submit(warm).result(timeout=2)
        future = pool.submit(first)
        assert busy_started.wait(timeout=2)
        assert pool.can_accept(second) is False
        assert pool.stats().worker_scale_outs == 0
        release_busy.set()
        future.result(timeout=2)
    finally:
        release_busy.set()
        pool.close()


def test_active_output_path_cannot_be_rendered_by_two_replicas(monkeypatch, tmp_path: Path):
    busy_started = threading.Event()
    release_busy = threading.Event()

    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0
            self.renders = 0

        def load(self, sfz):
            self.loads += 1
            return _load_info(100)

        def render(self, events, output, *, seed, midi_seconds):
            self.renders += 1
            if self.renders == 2:
                busy_started.set()
                assert release_busy.wait(timeout=2)
            output.write_bytes(b"wav")
            return _render_info(self.loads, 100)

        def close(self, *, graceful=True):
            pass

    _runtime(monkeypatch, tmp_path)
    job = _job(tmp_path, "piano", "piano.sfz")
    pool = persistent.PersistentSfizzPool(
        max_concurrency=2,
        max_replicas_per_key=2,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=1000,
        worker_factory=FakeWorker,
    )
    try:
        pool.submit(job).result(timeout=2)
        future = pool.submit(job)
        assert busy_started.wait(timeout=2)
        assert pool.can_accept(job) is False
        try:
            pool.submit(job)
        except RuntimeError as exc:
            assert "output is already active" in str(exc)
        else:
            raise AssertionError("duplicate active output should be rejected")
        release_busy.set()
        future.result(timeout=2)
    finally:
        release_busy.set()
        pool.close()
