from __future__ import annotations

from pathlib import Path

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


def test_resident_pool_reuses_one_worker_for_same_instrument(monkeypatch, tmp_path: Path):
    created = []

    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0
            self.closed = False
            created.append(self)

        def load(self, sfz):
            self.loads += 1
            return persistent.WorkerLoadInfo(1.0, 1, 1, 100)

        def render(self, events, output, *, seed, midi_seconds):
            output.write_bytes(b"wav")
            return persistent.WorkerRenderInfo(2.0, 64, 0, False, self.loads)

        def close(self, *, graceful=True):
            self.closed = True

    monkeypatch.setattr(
        persistent,
        "require_sfizz_runtime",
        lambda **kwargs: (tmp_path / "worker", tmp_path / "libsfizz.so"),
    )
    job = _job(tmp_path, "piano", "piano.sfz")
    pool = persistent.PersistentSfizzPool(
        max_workers=2,
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

    assert first.cold_load is True
    assert second.cold_load is False
    assert len(created) == 1
    assert created[0].loads == 1
    assert stats.tasks == 2
    assert stats.cold_loads == 1
    assert stats.warm_renders == 1


def test_resident_budget_evicts_idle_lru_before_new_instrument(monkeypatch, tmp_path: Path):
    created = []

    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0
            self.closed = False
            created.append(self)

        def load(self, sfz):
            self.loads += 1
            return persistent.WorkerLoadInfo(1.0, 1, 1, 100)

        def render(self, events, output, *, seed, midi_seconds):
            output.write_bytes(b"wav")
            return persistent.WorkerRenderInfo(1.0, 64, 0, False, self.loads)

        def close(self, *, graceful=True):
            self.closed = True

    monkeypatch.setattr(
        persistent,
        "require_sfizz_runtime",
        lambda **kwargs: (tmp_path / "worker", tmp_path / "libsfizz.so"),
    )
    first_job = _job(tmp_path, "piano", "piano.sfz")
    second_job = _job(tmp_path, "bass", "bass.sfz")
    pool = persistent.PersistentSfizzPool(
        max_workers=2,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=150,
        worker_factory=FakeWorker,
    )
    try:
        pool.submit(first_job).result(timeout=2)
        pool.submit(second_job).result(timeout=2)
        stats = pool.stats()
    finally:
        pool.close()

    assert len(created) == 2
    assert created[0].closed is True
    assert stats.evictions == 1
    assert stats.peak_resident_bytes == 100


def test_auto_budget_allows_post_load_estimate_overshoot_then_trims(monkeypatch, tmp_path: Path):
    import threading

    first_started = threading.Event()
    release_first = threading.Event()
    created = []

    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0
            self.closed = False
            self.name = ""
            created.append(self)

        def load(self, sfz):
            self.loads += 1
            self.name = Path(sfz).name
            resident = 80 if self.name == "piano.sfz" else 90
            return persistent.WorkerLoadInfo(1.0, 1, 1, resident)

        def render(self, events, output, *, seed, midi_seconds):
            if self.name == "piano.sfz":
                first_started.set()
                assert release_first.wait(timeout=2)
            output.write_bytes(b"wav")
            return persistent.WorkerRenderInfo(1.0, 64, 0, False, self.loads)

        def close(self, *, graceful=True):
            self.closed = True

    monkeypatch.setattr(persistent, "auto_resident_memory_budget", lambda: 150)
    monkeypatch.setattr(persistent, "_UNKNOWN_INSTRUMENT_RESERVATION", 60)
    monkeypatch.setattr(
        persistent,
        "require_sfizz_runtime",
        lambda **kwargs: (tmp_path / "worker", tmp_path / "libsfizz.so"),
    )
    first_job = _job(tmp_path, "piano", "piano.sfz")
    second_job = _job(tmp_path, "bass", "bass.sfz")
    pool = persistent.PersistentSfizzPool(
        max_workers=2,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=None,
        worker_factory=FakeWorker,
    )
    try:
        first = pool.submit(first_job)
        assert first_started.wait(timeout=2)
        # 80 resident + 60 reserved was a legal admission, but the second
        # instrument turns out to cost 90 after sfizz has already loaded it.
        assert pool.can_accept(second_job) is True
        second = pool.submit(second_job)
        result = second.result(timeout=2)
        stats = pool.stats()

        assert result.cold_load is True
        assert stats.peak_resident_bytes == 170
        assert stats.current_resident_bytes == 80
        assert stats.evictions == 1
        assert stats.worker_failures == 0
        assert created[1].closed is True

        release_first.set()
        first.result(timeout=2)
    finally:
        release_first.set()
        pool.close()


def test_explicit_budget_remains_hard_after_unknown_load(monkeypatch, tmp_path: Path):
    import threading

    first_started = threading.Event()
    release_first = threading.Event()

    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0
            self.name = ""

        def load(self, sfz):
            self.loads += 1
            self.name = Path(sfz).name
            resident = 80 if self.name == "piano.sfz" else 90
            return persistent.WorkerLoadInfo(1.0, 1, 1, resident)

        def render(self, events, output, *, seed, midi_seconds):
            if self.name == "piano.sfz":
                first_started.set()
                assert release_first.wait(timeout=2)
            output.write_bytes(b"wav")
            return persistent.WorkerRenderInfo(1.0, 64, 0, False, self.loads)

        def close(self, *, graceful=True):
            pass

    monkeypatch.setattr(persistent, "_UNKNOWN_INSTRUMENT_RESERVATION", 60)
    monkeypatch.setattr(
        persistent,
        "require_sfizz_runtime",
        lambda **kwargs: (tmp_path / "worker", tmp_path / "libsfizz.so"),
    )
    first_job = _job(tmp_path, "piano", "piano.sfz")
    second_job = _job(tmp_path, "bass", "bass.sfz")
    pool = persistent.PersistentSfizzPool(
        max_workers=2,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=150,
        worker_factory=FakeWorker,
    )
    try:
        first = pool.submit(first_job)
        assert first_started.wait(timeout=2)
        assert pool.can_accept(second_job) is True
        second = pool.submit(second_job)
        try:
            second.result(timeout=2)
        except RuntimeError as exc:
            assert "exceeds resident memory admission after load" in str(exc)
        else:
            raise AssertionError("explicit resident memory budget must remain hard")
        release_first.set()
        first.result(timeout=2)
    finally:
        release_first.set()
        pool.close()


def test_busy_instrument_has_no_replica_in_v1(monkeypatch, tmp_path: Path):
    started = __import__("threading").Event()
    release = __import__("threading").Event()

    class FakeWorker:
        def __init__(self, **kwargs):
            self.loads = 0

        def load(self, sfz):
            self.loads += 1
            return persistent.WorkerLoadInfo(1.0, 1, 1, 100)

        def render(self, events, output, *, seed, midi_seconds):
            started.set()
            assert release.wait(timeout=2)
            output.write_bytes(b"wav")
            return persistent.WorkerRenderInfo(1.0, 64, 0, False, self.loads)

        def close(self, *, graceful=True):
            pass

    monkeypatch.setattr(
        persistent,
        "require_sfizz_runtime",
        lambda **kwargs: (tmp_path / "worker", tmp_path / "libsfizz.so"),
    )
    job = _job(tmp_path, "piano", "piano.sfz")
    pool = persistent.PersistentSfizzPool(
        max_workers=2,
        blocksize=1024,
        samplerate=48_000,
        quality=2,
        polyphony=256,
        memory_budget_bytes=8 * 1024 ** 3,
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
            return persistent.WorkerLoadInfo(1.0, 1, 1, 100)

        def render(self, events, output, *, seed, midi_seconds):
            output.write_bytes(b"partial")
            raise RuntimeError("synthetic worker failure")

        def close(self, *, graceful=True):
            self.closed = True

    monkeypatch.setattr(
        persistent,
        "require_sfizz_runtime",
        lambda **kwargs: (tmp_path / "worker", tmp_path / "libsfizz.so"),
    )
    job = _job(tmp_path, "piano", "piano.sfz")
    pool = persistent.PersistentSfizzPool(
        max_workers=1,
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
        assert stats.current_resident_bytes == 0
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

    # Different metadata/path locations must not change cache identity when the
    # actual renderer bytes and renderer contract are identical.
    right_worker.touch()
    right_lib.touch()

    left_identity = persistent.sfizz_renderer_identity(
        worker=left_worker, library=left_lib
    )
    right_identity = persistent.sfizz_renderer_identity(
        worker=right_worker, library=right_lib
    )

    assert left_identity == right_identity
    assert "path" not in left_identity["worker"]
    assert "mtime_ns" not in left_identity["worker"]
