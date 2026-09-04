from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha1, sha256
import json
import multiprocessing as mp
from pathlib import Path
import os
import shutil
import sqlite3
import tempfile
import time
from typing import Iterable

from .effects import process_stem_effects
from .mixer import MixStem, export_stem, export_submix, mix_stems
from .patches import MasterConfig, PatchRegistry
from .render_log import RenderLogger
from .renderer import (
    FluidSynthJob,
    RenderJob,
    RenderedStem,
    render_fluidsynth_jobs,
)
from .sfizz_persistent import (
    PersistentSfizzExecution,
    PersistentSfizzPool,
    PersistentSfizzStats,
    SFIZZ_TASK_SEED,
)


class Stage(str, Enum):
    RAW = "raw"
    FX = "fx"
    MIX = "mix"


class Backend(str, Enum):
    SFZ = "sfz"
    FLUIDSYNTH = "fluidsynth"
    FX = "fx"
    MIXER = "mixer"


@dataclass(frozen=True)
class RenderSettings:
    """User-facing render policy.

    ``concurrency`` is the only global execution budget. Backend concurrency
    values are optional advanced overrides; ``None`` means AUTO. The resolved
    backend capacities are derived once by :func:`resolve_resource_policy`.
    """

    concurrency: int = 5
    sfz_concurrency: int | None = None
    sfz_max_replicas: int = 1
    gm_concurrency: int | None = None
    fx_concurrency: int | None = None
    mix_concurrency: int | None = None
    blocksize: int = 1024
    samplerate: int = 48_000
    quality: int = 2
    polyphony: int = 256
    include_melody: bool = False
    skip_unconfigured: bool = False
    keep_work: bool = False
    active_songs: int = 32
    max_fx_backlog: int | None = None
    sfz_memory_budget: int | None = None
    sfizz_worker: Path | None = None
    sfizz_library: Path | None = None

    def normalized(self) -> "RenderSettings":
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if self.active_songs < 1:
            raise ValueError("active_songs must be >= 1")
        for name, value in (
            ("sfz_concurrency", self.sfz_concurrency),
            ("gm_concurrency", self.gm_concurrency),
            ("fx_concurrency", self.fx_concurrency),
            ("mix_concurrency", self.mix_concurrency),
        ):
            if value is not None and value < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.sfz_max_replicas < 1:
            raise ValueError("sfz_max_replicas must be >= 1")
        if self.max_fx_backlog is not None and self.max_fx_backlog < 1:
            raise ValueError("max_fx_backlog must be >= 1")
        if self.sfz_memory_budget is not None and self.sfz_memory_budget < 1:
            raise ValueError("sfz_memory_budget must be >= 1")
        return self


@dataclass(frozen=True)
class ResourcePolicy:
    """Concrete capacities used by the coordinator after AUTO resolution."""

    concurrency: int
    sfz_concurrency: int
    gm_concurrency: int
    fx_concurrency: int
    mix_concurrency: int
    max_fx_backlog: int


def _auto_gm_concurrency(concurrency: int) -> int:
    """Conservative GM process fan-out until FluidSynth has RAM admission.

    A GM physical task owns a process-local FluidSynth session and SoundFont.
    Scale slowly on small hosts and stop at four processes; advanced users can
    override this explicitly.
    """

    return min(concurrency, min(4, max(1, concurrency // 4)))


def resolve_resource_policy(settings: RenderSettings) -> ResourcePolicy:
    settings = settings.normalized()
    concurrency = settings.concurrency

    def resolved(value: int | None) -> int:
        return concurrency if value is None else min(value, concurrency)

    return ResourcePolicy(
        concurrency=concurrency,
        sfz_concurrency=resolved(settings.sfz_concurrency),
        gm_concurrency=(
            _auto_gm_concurrency(concurrency)
            if settings.gm_concurrency is None
            else min(settings.gm_concurrency, concurrency)
        ),
        fx_concurrency=resolved(settings.fx_concurrency),
        mix_concurrency=resolved(settings.mix_concurrency),
        max_fx_backlog=(
            max(concurrency * 2, 4)
            if settings.max_fx_backlog is None
            else settings.max_fx_backlog
        ),
    )


@dataclass(frozen=True)
class StemPlan:
    stem_id: str
    track_index: int
    instrument: str
    raw_backend: Backend
    raw_output: Path
    effects: tuple[str, ...]


@dataclass(frozen=True)
class RenderTask:
    task_id: str
    song_id: str
    stage: Stage
    backend: Backend
    stem_ids: tuple[str, ...]
    payload: object


@dataclass
class SongPlan:
    song_id: str
    midi_path: Path
    output: Path
    work_dir: Path
    split_dir: Path
    config_path: Path
    settings: RenderSettings
    stems: tuple[StemPlan, ...]
    raw_tasks: tuple[RenderTask, ...]
    cached_stems: tuple[RenderedStem, ...] = ()
    skipped: tuple[tuple[int, str, str], ...] = ()
    track_index: int | None = None
    master: MasterConfig = field(default_factory=MasterConfig)
    music_duration_seconds: float = 0.0
    music_bars: float = 0.0
    rendered_track_count: int = 0


@dataclass(frozen=True)
class SongResult:
    song_id: str
    midi_path: Path
    output: Path
    status: str
    stats: dict[str, float] | None = None
    error: str | None = None
    elapsed_seconds: float = 0.0
    music_duration_seconds: float = 0.0
    music_bars: float = 0.0
    rendered_track_count: int = 0


def make_song_id(midi_path: Path, output: Path, run_identity: str = "") -> str:
    """Identify one resumable song render, including the source MIDI contents."""
    midi_path = midi_path.resolve()
    midi_digest = sha256(midi_path.read_bytes()).hexdigest()
    payload = f"{midi_path}\0{midi_digest}\0{output.resolve()}\0{run_identity}"
    return sha1(payload.encode("utf-8")).hexdigest()[:20]


def make_stem_id(track_index: int, instrument: str) -> str:
    return f"track-{track_index:02d}:{instrument}"


def _task_id(song_id: str, stage: Stage, backend: Backend, stem_ids: tuple[str, ...]) -> str:
    joined = ",".join(stem_ids)
    digest = sha1(joined.encode("utf-8")).hexdigest()[:10]
    return f"{song_id}:{stage.value}:{backend.value}:{digest}"


def make_task(song_id: str, stage: Stage, backend: Backend, stem_ids: tuple[str, ...], payload: object) -> RenderTask:
    return RenderTask(
        task_id=_task_id(song_id, stage, backend, stem_ids),
        song_id=song_id,
        stage=stage,
        backend=backend,
        stem_ids=stem_ids,
        payload=payload,
    )


class StateStore:
    """Small coordinator-owned SQLite journal for long batch runs.

    Raw audio cache keys stay authoritative for expensive sampler reuse. The DB
    records scheduling state and completed songs so a restarted batch can skip
    final outputs and re-plan interrupted songs from their deterministic raw
    cache files.
    """

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS songs (
                song_id TEXT PRIMARY KEY,
                midi_path TEXT NOT NULL,
                output_path TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                started_at REAL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                song_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                backend TEXT NOT NULL,
                stem_ids TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_song ON tasks(song_id);
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def song_status(self, song_id: str) -> str | None:
        row = self.db.execute("SELECT status FROM songs WHERE song_id = ?", (song_id,)).fetchone()
        return None if row is None else str(row[0])

    def record_failure(self, song_id: str, midi_path: Path, output: Path, error: str) -> None:
        now = time.time()
        self.db.execute(
            """
            INSERT INTO songs(song_id, midi_path, output_path, status, error, started_at, updated_at)
            VALUES (?, ?, ?, 'FAILED', ?, ?, ?)
            ON CONFLICT(song_id) DO UPDATE SET
                midi_path=excluded.midi_path,
                output_path=excluded.output_path,
                status='FAILED',
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (song_id, str(midi_path), str(output), error, now, now),
        )
        self.db.commit()

    def register_song(self, plan: SongPlan) -> None:
        now = time.time()
        self.db.execute(
            """
            INSERT INTO songs(song_id, midi_path, output_path, status, error, started_at, updated_at)
            VALUES (?, ?, ?, 'RUNNING', NULL, ?, ?)
            ON CONFLICT(song_id) DO UPDATE SET
                midi_path=excluded.midi_path,
                output_path=excluded.output_path,
                status='RUNNING',
                error=NULL,
                started_at=COALESCE(songs.started_at, excluded.started_at),
                updated_at=excluded.updated_at
            """,
            (plan.song_id, str(plan.midi_path), str(plan.output), now, now),
        )
        self.db.commit()

    def task_started(self, task: RenderTask) -> None:
        now = time.time()
        self.db.execute(
            """
            INSERT INTO tasks(task_id, song_id, stage, backend, stem_ids, status, attempts, error, updated_at)
            VALUES (?, ?, ?, ?, ?, 'RUNNING', 1, NULL, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                status='RUNNING',
                attempts=tasks.attempts + 1,
                error=NULL,
                updated_at=excluded.updated_at
            """,
            (
                task.task_id,
                task.song_id,
                task.stage.value,
                task.backend.value,
                json.dumps(task.stem_ids),
                now,
            ),
        )
        self.db.commit()

    def task_done(self, task: RenderTask) -> None:
        self.db.execute(
            "UPDATE tasks SET status='DONE', error=NULL, updated_at=? WHERE task_id=?",
            (time.time(), task.task_id),
        )
        self.db.commit()

    def task_failed(self, task: RenderTask, error: str) -> None:
        self.db.execute(
            "UPDATE tasks SET status='FAILED', error=?, updated_at=? WHERE task_id=?",
            (error, time.time(), task.task_id),
        )
        self.db.commit()

    def song_done(self, plan: SongPlan) -> None:
        self.db.execute(
            "UPDATE songs SET status='DONE', error=NULL, updated_at=? WHERE song_id=?",
            (time.time(), plan.song_id),
        )
        self.db.commit()

    def song_failed(self, plan: SongPlan, error: str) -> None:
        self.db.execute(
            "UPDATE songs SET status='FAILED', error=?, updated_at=? WHERE song_id=?",
            (error, time.time(), plan.song_id),
        )
        self.db.commit()


@dataclass
class _Runtime:
    plan: SongPlan
    started_at: float = field(default_factory=time.perf_counter)
    processed: dict[str, MixStem] = field(default_factory=dict)
    failed: bool = False
    error: str | None = None
    mix_queued: bool = False
    stem_plans: dict[str, StemPlan] = field(init=False)

    def __post_init__(self) -> None:
        self.stem_plans = {stem.stem_id: stem for stem in self.plan.stems}
        if len(self.stem_plans) != len(self.plan.stems):
            raise ValueError(f"duplicate stem_id in song plan: {self.plan.song_id}")


@dataclass(frozen=True)
class _GMPayload:
    jobs: tuple[FluidSynthJob, ...]
    samplerate: int


@dataclass(frozen=True)
class _FXPayload:
    stem: RenderedStem
    effects: tuple[str, ...]
    config_path: Path
    work_dir: Path


@dataclass(frozen=True)
class _MixPayload:
    stems: tuple[MixStem, ...]
    output: Path
    track_index: int | None
    normalize_peak_db: float
    master_gain_db: float


@dataclass(frozen=True)
class _TaskExecution:
    value: object
    diagnostics: str = ""


def _capture_fd2(callable_):
    """Run native code with process stderr captured, including C libraries."""
    with tempfile.TemporaryFile(mode="w+b") as tmp:
        saved = os.dup(2)
        try:
            os.dup2(tmp.fileno(), 2)
            try:
                value = callable_()
                error = None
            except BaseException as exc:
                value = None
                error = exc
        finally:
            os.dup2(saved, 2)
            os.close(saved)
        tmp.seek(0)
        diagnostics = tmp.read().decode("utf-8", errors="replace").strip()
    if error is not None:
        suffix = f"\n{diagnostics}" if diagnostics else ""
        raise RuntimeError(f"{error}{suffix}") from error
    return value, diagnostics


def _execute_gm(payload: _GMPayload) -> _TaskExecution:
    value, diagnostics = _capture_fd2(
        lambda: render_fluidsynth_jobs(
            list(payload.jobs),
            workers=1,
            samplerate=payload.samplerate,
        )
    )
    return _TaskExecution(value, diagnostics)


_FX_REGISTRY_CACHE: dict[tuple[str, int], PatchRegistry] = {}


def _worker_registry(path: Path) -> PatchRegistry:
    resolved = path.resolve()
    key = (str(resolved), resolved.stat().st_mtime_ns)
    registry = _FX_REGISTRY_CACHE.get(key)
    if registry is None:
        registry = PatchRegistry(resolved)
        _FX_REGISTRY_CACHE.clear()
        _FX_REGISTRY_CACHE[key] = registry
    return registry


def _execute_fx(payload: _FXPayload) -> _TaskExecution:
    registry = _worker_registry(payload.config_path)
    diagnostics: list[str] = []
    final = process_stem_effects(
        payload.stem,
        registry,
        payload.work_dir,
        effect_names=payload.effects,
        diagnostics=diagnostics,
    )
    value = MixStem(
        name=f"track-{payload.stem.track.index:02d} {payload.stem.instrument}",
        path=final,
        gain_db=payload.stem.patch.gain_db,
    )
    return _TaskExecution(value, "\n".join(diagnostics))


def _execute_mix(payload: _MixPayload) -> _TaskExecution:
    stems = list(payload.stems)
    if payload.track_index is not None:
        if len(stems) == 1:
            value = export_stem(stems[0], payload.output)
        else:
            value = export_submix(stems, payload.output)
    else:
        value = mix_stems(
            stems,
            payload.output,
            normalize_peak_db=payload.normalize_peak_db,
            master_gain_db=payload.master_gain_db,
        )
    return _TaskExecution(value)


class RenderingCoordinator:
    """Bounded, long-running stem scheduler shared by single and batch renders."""

    def __init__(
        self,
        settings: RenderSettings,
        state: StateStore | None = None,
        *,
        logger: RenderLogger | None = None,
        total_songs: int | None = None,
    ):
        self.settings = settings.normalized()
        self.resources = resolve_resource_policy(self.settings)
        self.state = state
        self.logger = logger
        self.total_songs = total_songs

        self.sfz_pool = PersistentSfizzPool(
            max_concurrency=self.resources.sfz_concurrency,
            max_replicas_per_key=self.settings.sfz_max_replicas,
            blocksize=self.settings.blocksize,
            samplerate=self.settings.samplerate,
            quality=self.settings.quality,
            polyphony=self.settings.polyphony,
            memory_budget_bytes=self.settings.sfz_memory_budget,
            worker_path=self.settings.sfizz_worker,
            library_path=self.settings.sfizz_library,
        )
        # Embedded FluidSynth gets process isolation. The pool is persistent for
        # the coordinator lifetime, so interpreter/native-library startup is not
        # paid once per song even though synth/SoundFont session state is still
        # scoped to each GM batch.
        self.gm_pool = ProcessPoolExecutor(
            max_workers=self.resources.gm_concurrency,
            mp_context=mp.get_context("spawn"),
        )
        self.fx_pool = ThreadPoolExecutor(
            max_workers=self.resources.fx_concurrency, thread_name_prefix="mrp-fx"
        )
        self.mix_pool = ThreadPoolExecutor(
            max_workers=self.resources.mix_concurrency, thread_name_prefix="mrp-mix"
        )

        self.pending_mix: deque[RenderTask] = deque()
        self.pending_fx: deque[RenderTask] = deque()
        self.pending_raw: deque[RenderTask] = deque()
        self.inflight: dict[Future, RenderTask] = {}
        self.inflight_started: dict[Future, float] = {}
        self.inflight_by_backend: dict[Backend, int] = {backend: 0 for backend in Backend}
        self.active: dict[str, _Runtime] = {}
        self.results: list[SongResult] = []
        self.cache_hits = 0
        self._backpressure_active = False

    def close(self) -> None:
        self.sfz_pool.close()
        self.gm_pool.shutdown(wait=True, cancel_futures=True)
        self.fx_pool.shutdown(wait=True, cancel_futures=True)
        self.mix_pool.shutdown(wait=True, cancel_futures=True)

    def __enter__(self) -> "RenderingCoordinator":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def run(self, plans: Iterable[SongPlan]) -> list[SongResult]:
        source = iter(plans)
        exhausted = False

        while not exhausted or self.active or self.inflight or self._has_pending():
            while not exhausted and len(self.active) < self.settings.active_songs:
                try:
                    plan = next(source)
                except StopIteration:
                    exhausted = True
                    break
                self._admit(plan)

            self._dispatch_ready()

            if self.inflight:
                timeout = 1.0 if self.logger is not None and self.logger.batch_mode else None
                done, _ = wait(
                    tuple(self.inflight), timeout=timeout, return_when=FIRST_COMPLETED
                )
                for future in done:
                    self._complete_future(future)
                self._emit_progress()
            elif self._has_pending():
                # Pending work should be dispatchable unless all tasks belong to
                # songs that failed while siblings were still queued.
                self._drop_failed_pending()
                self._dispatch_ready()
                if not self.inflight and self._has_pending():
                    raise RuntimeError("scheduler deadlock: pending tasks cannot be dispatched")
            elif self.active and exhausted:
                # A song with only cached raw stems may become mix-ready without
                # a future completing; queue checks normally handle this.
                for runtime in list(self.active.values()):
                    self._maybe_queue_mix(runtime)
                self._dispatch_ready()
                if not self.inflight and not self._has_pending():
                    raise RuntimeError("scheduler deadlock: active songs have no work")

        return list(self.results)

    def _has_pending(self) -> bool:
        return bool(self.pending_mix or self.pending_fx or self.pending_raw)

    def _admit(self, plan: SongPlan) -> None:
        runtime = _Runtime(plan=plan)
        self.active[plan.song_id] = runtime
        if self.logger is not None:
            self.logger.scheduler(
                f"admit {plan.midi_path.name} · active={len(self.active)}",
                song=str(plan.midi_path),
                active=len(self.active),
            )
        if self.state is not None:
            self.state.register_song(plan)

        for stem in plan.cached_stems:
            self.cache_hits += 1
            if self.logger is not None:
                self.logger.cache_hit(
                    song=plan.midi_path,
                    track_index=stem.track.index,
                    instrument=stem.instrument,
                    path=stem.path,
                )
            self._raw_ready(runtime, stem)
        for task in plan.raw_tasks:
            self.pending_raw.append(task)
        self._maybe_queue_mix(runtime)

    def _raw_ready(self, runtime: _Runtime, stem: RenderedStem) -> None:
        stem_id = make_stem_id(stem.track.index, stem.instrument)
        stem_plan = runtime.stem_plans.get(stem_id)
        if stem_plan is None:
            raise RuntimeError(
                f"rendered stem {stem_id!r} is not present in song plan {runtime.plan.song_id!r}"
            )
        if stem_plan.effects:
            task = make_task(
                runtime.plan.song_id,
                Stage.FX,
                Backend.FX,
                (stem_id,),
                _FXPayload(
                    stem,
                    stem_plan.effects,
                    runtime.plan.config_path,
                    runtime.plan.work_dir,
                ),
            )
            self.pending_fx.append(task)
        else:
            runtime.processed[stem_id] = MixStem(
                name=f"track-{stem.track.index:02d} {stem.instrument}",
                path=stem.path,
                gain_db=stem.patch.gain_db,
            )
            self._maybe_queue_mix(runtime)

    def _maybe_queue_mix(self, runtime: _Runtime) -> None:
        if runtime.failed or runtime.mix_queued:
            return
        if len(runtime.processed) != len(runtime.plan.stems):
            return
        runtime.mix_queued = True
        stems = tuple(runtime.processed[key] for key in sorted(runtime.processed))
        task = make_task(
            runtime.plan.song_id,
            Stage.MIX,
            Backend.MIXER,
            tuple(sorted(runtime.processed)),
            _MixPayload(
                stems=stems,
                output=runtime.plan.output,
                track_index=runtime.plan.track_index,
                normalize_peak_db=runtime.plan.master.normalize_peak_db,
                master_gain_db=runtime.plan.master.gain_db,
            ),
        )
        self.pending_mix.append(task)

    def _backend_cap(self, backend: Backend) -> int:
        if backend == Backend.SFZ:
            return self.resources.sfz_concurrency
        if backend == Backend.FLUIDSYNTH:
            return self.resources.gm_concurrency
        if backend == Backend.FX:
            return self.resources.fx_concurrency
        return self.resources.mix_concurrency

    def _dispatch_ready(self) -> None:
        while len(self.inflight) < self.resources.concurrency:
            task = self._next_dispatchable_task()
            if task is None:
                break
            if task.song_id not in self.active or self.active[task.song_id].failed:
                continue
            self._submit(task)

    def _next_dispatchable_task(self) -> RenderTask | None:
        for queue in (self.pending_mix, self.pending_fx):
            task = self._take_for_available_backend(queue)
            if task is not None:
                return task

        fx_backlog = len(self.pending_fx) + self.inflight_by_backend[Backend.FX]
        blocked = fx_backlog >= self.resources.max_fx_backlog
        if blocked:
            if not self._backpressure_active and self.logger is not None:
                self.logger.scheduler(
                    f"raw paused · fx backlog={fx_backlog}",
                    fx_backlog=fx_backlog,
                )
            self._backpressure_active = True
            return None
        if self._backpressure_active and self.logger is not None:
            self.logger.scheduler(
                f"raw resumed · fx backlog={fx_backlog}",
                fx_backlog=fx_backlog,
            )
        self._backpressure_active = False
        return self._take_for_available_backend(self.pending_raw)

    def _take_for_available_backend(self, queue: deque[RenderTask]) -> RenderTask | None:
        for _ in range(len(queue)):
            task = queue.popleft()
            if task.song_id not in self.active or self.active[task.song_id].failed:
                continue
            if self._task_dispatchable(task):
                return task
            queue.append(task)
        return None

    def _task_dispatchable(self, task: RenderTask) -> bool:
        if self.inflight_by_backend[task.backend] >= self._backend_cap(task.backend):
            return False
        if task.backend == Backend.SFZ:
            if not isinstance(task.payload, RenderJob):
                raise RuntimeError("SFZ task payload must be a RenderJob")
            return self.sfz_pool.can_accept(task.payload)
        return True

    def _submit(self, task: RenderTask) -> None:
        if self.state is not None:
            self.state.task_started(task)
        if task.backend == Backend.SFZ:
            if not isinstance(task.payload, RenderJob):
                raise RuntimeError("SFZ task payload must be a RenderJob")
            future = self.sfz_pool.submit(task.payload, seed=SFIZZ_TASK_SEED)
        elif task.backend == Backend.FLUIDSYNTH:
            payload = task.payload
            if isinstance(payload, tuple):
                payload = _GMPayload(payload, self.settings.samplerate)
            future = self.gm_pool.submit(_execute_gm, payload)
        elif task.backend == Backend.FX:
            future = self.fx_pool.submit(_execute_fx, task.payload)
        elif task.backend == Backend.MIXER:
            future = self.mix_pool.submit(_execute_mix, task.payload)
        else:
            raise RuntimeError(f"unsupported backend: {task.backend}")
        self.inflight[future] = task
        self.inflight_started[future] = time.perf_counter()
        self.inflight_by_backend[task.backend] += 1
        if self.logger is not None:
            self.logger.task_start(
                song=self.active[task.song_id].plan.midi_path,
                stage=task.stage.value,
                backend=task.backend.value,
                label=self._task_label(task),
                stem_ids=task.stem_ids,
            )

    def _complete_future(self, future: Future) -> None:
        task = self.inflight.pop(future)
        started = self.inflight_started.pop(future, time.perf_counter())
        elapsed_task = time.perf_counter() - started
        self.inflight_by_backend[task.backend] -= 1
        runtime = self.active.get(task.song_id)

        try:
            execution = future.result()
            if isinstance(execution, _TaskExecution):
                value = execution.value
                diagnostics = execution.diagnostics
            elif isinstance(execution, PersistentSfizzExecution):
                value = [execution.stem]
                diagnostics = execution.diagnostics
                if self.logger is not None and self.logger.verbose:
                    state = "NEW" if execution.worker_started else "REUSE"
                    growth = execution.working_set_growth_bytes / (1024 ** 2)
                    self.logger.scheduler(
                        f"sfz {state} · {execution.stem.patch.name} · "
                        f"working-set={execution.working_set_bytes / (1024 ** 3):.2f}GiB · "
                        f"growth=+{growth:.0f}MiB",
                        backend="sfz",
                        working_set_bytes=execution.working_set_bytes,
                        working_set_growth_bytes=execution.working_set_growth_bytes,
                        sample_resident_bytes=execution.sample_resident_bytes,
                        worker_started=execution.worker_started,
                    )
            else:
                value = execution
                diagnostics = ""
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if self.state is not None:
                self.state.task_failed(task, error)
            if self.logger is not None:
                self.logger.failure(
                    song=runtime.plan.midi_path if runtime is not None else None,
                    stage=task.stage.value,
                    backend=task.backend.value,
                    message=error,
                )
            if runtime is not None:
                self._fail_song(runtime, error)
            return

        if self.state is not None:
            self.state.task_done(task)
        if self.logger is not None and runtime is not None:
            self.logger.task_done(
                song=runtime.plan.midi_path,
                stage=task.stage.value,
                backend=task.backend.value,
                label=self._task_label(task),
                seconds=elapsed_task,
                stem_ids=task.stem_ids,
                diagnostics=diagnostics,
            )
        if runtime is None or runtime.failed:
            return

        if task.stage == Stage.RAW:
            assert isinstance(value, list)
            for stem in value:
                self._raw_ready(runtime, stem)
            self._maybe_queue_mix(runtime)
            return

        if task.stage == Stage.FX:
            assert isinstance(value, MixStem)
            runtime.processed[task.stem_ids[0]] = value
            self._maybe_queue_mix(runtime)
            return

        if task.stage == Stage.MIX:
            assert isinstance(value, dict)
            elapsed = time.perf_counter() - runtime.started_at
            result = SongResult(
                song_id=runtime.plan.song_id,
                midi_path=runtime.plan.midi_path,
                output=runtime.plan.output,
                status="DONE",
                stats=value,
                elapsed_seconds=elapsed,
                music_duration_seconds=runtime.plan.music_duration_seconds,
                music_bars=runtime.plan.music_bars,
                rendered_track_count=runtime.plan.rendered_track_count,
            )
            self.results.append(result)
            if self.state is not None:
                self.state.song_done(runtime.plan)
            if self.logger is not None:
                self.logger.song_done(
                    song=runtime.plan.midi_path,
                    output=runtime.plan.output,
                    seconds=elapsed,
                    stats=value,
                )
            if not runtime.plan.settings.keep_work:
                shutil.rmtree(runtime.plan.split_dir, ignore_errors=True)
            del self.active[runtime.plan.song_id]
            return

        raise RuntimeError(f"unknown task stage: {task.stage}")

    def _task_label(self, task: RenderTask) -> str:
        payload = task.payload
        if task.backend == Backend.SFZ and isinstance(payload, RenderJob):
            return f"{payload.track.index:02d} {payload.instrument} · sfz"
        if task.backend == Backend.FLUIDSYNTH:
            jobs = payload.jobs if isinstance(payload, _GMPayload) else payload
            if isinstance(jobs, tuple):
                if len(jobs) == 1:
                    job = jobs[0]
                    return f"{job.track.index:02d} {job.instrument} · fluidsynth"
                tracks = ",".join(f"{job.track.index:02d}" for job in jobs)
                return f"tracks {tracks} · fluidsynth batch"
        if task.backend == Backend.FX and isinstance(payload, _FXPayload):
            chain = " → ".join(payload.effects)
            return f"{payload.stem.track.index:02d} {payload.stem.instrument} · {chain}"
        if task.backend == Backend.MIXER and isinstance(payload, _MixPayload):
            return f"{len(payload.stems)} stem{'s' if len(payload.stems) != 1 else ''}"
        return ",".join(task.stem_ids)

    def sfz_stats(self) -> PersistentSfizzStats:
        return self.sfz_pool.stats()

    def _emit_progress(self, *, force: bool = False) -> None:
        if self.logger is None or not self.logger.batch_mode or self.total_songs is None:
            return
        completed = [result for result in self.results if result.status == "DONE"]
        done = len(completed)
        failed = sum(1 for result in self.results if result.status != "DONE")
        track_seconds = sum(
            result.music_duration_seconds * result.rendered_track_count for result in completed
        )
        track_bars = sum(
            result.music_bars * result.rendered_track_count for result in completed
        )
        self.logger.batch_progress(
            total=self.total_songs,
            done=done,
            failed=failed,
            active=len(self.active),
            pending_raw=len(self.pending_raw),
            pending_fx=len(self.pending_fx),
            pending_mix=len(self.pending_mix),
            inflight=len(self.inflight),
            cache_hits=self.cache_hits,
            track_seconds=track_seconds,
            track_bars=track_bars,
            force=force,
        )

    def _fail_song(self, runtime: _Runtime, error: str) -> None:
        if runtime.failed:
            return
        runtime.failed = True
        runtime.error = error
        self.results.append(
            SongResult(
                song_id=runtime.plan.song_id,
                midi_path=runtime.plan.midi_path,
                output=runtime.plan.output,
                status="FAILED",
                error=error,
                elapsed_seconds=time.perf_counter() - runtime.started_at,
                music_duration_seconds=runtime.plan.music_duration_seconds,
                music_bars=runtime.plan.music_bars,
                rendered_track_count=runtime.plan.rendered_track_count,
            )
        )
        if self.state is not None:
            self.state.song_failed(runtime.plan, error)
        del self.active[runtime.plan.song_id]

    def _drop_failed_pending(self) -> None:
        active_ids = set(self.active)
        for queue in (self.pending_mix, self.pending_fx, self.pending_raw):
            kept = [task for task in queue if task.song_id in active_ids]
            queue.clear()
            queue.extend(kept)
