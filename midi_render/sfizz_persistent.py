from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import threading
import time
from typing import Callable, Sequence

import mido

from .renderer import RenderJob, RenderedStem


SFIZZ_WORKER_PROTOCOL = 3
SFIZZ_OFFLINE_API_VERSION = 1
SFIZZ_TASK_SEED = 0
SFIZZ_RENDERER_CONTRACT = "mrp-persistent-sfizz-v1"
_GIB = 1024 ** 3
_MIB = 1024 ** 2
_UNKNOWN_INSTRUMENT_RESERVATION = 4 * _GIB
_AUTO_TOTAL_FRACTION = 0.70
_AUTO_OS_RESERVE = 1 * _GIB


@dataclass(frozen=True)
class InstrumentKey:
    sfz_path: str
    sfz_size: int
    sfz_mtime_ns: int
    blocksize: int
    samplerate: int
    quality: int
    polyphony: int

    @classmethod
    def from_job(
        cls,
        job: RenderJob,
        *,
        blocksize: int,
        samplerate: int,
        quality: int,
        polyphony: int,
    ) -> "InstrumentKey":
        sfz = job.patch.sfz.resolve()
        stat = sfz.stat()
        return cls(
            sfz_path=str(sfz),
            sfz_size=stat.st_size,
            sfz_mtime_ns=stat.st_mtime_ns,
            blocksize=blocksize,
            samplerate=samplerate,
            quality=quality,
            polyphony=polyphony,
        )


@dataclass(frozen=True)
class MidiEventInfo:
    events: int
    end_frame: int
    seconds: float


@dataclass(frozen=True)
class WorkerLoadInfo:
    milliseconds: float
    regions: int
    preloaded_samples: int
    sfizz_bytes: int
    diagnostics: str = ""


@dataclass(frozen=True)
class WorkerRenderInfo:
    milliseconds: float
    frames: int
    active_after: int
    tail_limit: bool
    instrument_loads: int
    diagnostics: str = ""


@dataclass(frozen=True)
class PersistentSfizzExecution:
    stem: RenderedStem
    diagnostics: str = ""
    cold_load: bool = False
    resident_bytes: int = 0


@dataclass(frozen=True)
class PersistentSfizzStats:
    tasks: int
    cold_loads: int
    warm_renders: int
    evictions: int
    worker_failures: int
    current_resident_bytes: int
    peak_resident_bytes: int
    memory_budget_bytes: int


@dataclass
class _ResidentEntry:
    key: InstrumentKey
    label: str
    worker: "_WorkerProcess"
    resident_bytes: int
    busy: bool = False
    last_used: float = 0.0


@dataclass(frozen=True)
class _MemorySnapshot:
    total: int
    available: int


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _existing_executable(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    candidate = Path(value).expanduser()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate.resolve()
    return None


def _existing_file(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    return None


def find_sfizz_worker(configured: Path | None = None) -> Path | None:
    for candidate in (
        configured,
        os.environ.get("MRP_SFIZZ_WORKER"),
        _project_root() / "resources/tools/mrp-sfizz-worker",
    ):
        found = _existing_executable(candidate)
        if found is not None:
            return found
    found = shutil.which("mrp-sfizz-worker")
    return Path(found).resolve() if found else None


def find_sfizz_library(configured: Path | None = None) -> Path | None:
    for candidate in (
        configured,
        os.environ.get("MRP_LIBSFIZZ"),
        os.environ.get("LIBSFIZZ"),
        _project_root() / "resources/lib/libsfizz.so.1.2.3",
        _project_root() / "resources/lib/libsfizz.so",
    ):
        found = _existing_file(candidate)
        if found is not None:
            return found

    patterns = (
        "/usr/local/lib*/libsfizz.so*",
        "/usr/lib*/libsfizz.so*",
        "/lib*/libsfizz.so*",
    )
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(Path("/").glob(pattern.lstrip("/")))
    for candidate in sorted(candidates, reverse=True):
        if candidate.is_file():
            return candidate.resolve()
    return None


def require_sfizz_runtime(
    *, worker: Path | None = None, library: Path | None = None
) -> tuple[Path, Path]:
    worker_path = find_sfizz_worker(worker)
    if worker_path is None:
        raise RuntimeError(
            "mrp-sfizz-worker not found. Build it with `make native-sfizz-worker` "
            "or set MRP_SFIZZ_WORKER."
        )
    library_path = find_sfizz_library(library)
    if library_path is None:
        raise RuntimeError(
            "MRP sfizz fork library not found. Set [paths].sfizz_library in "
            "config/patches.toml or MRP_LIBSFIZZ to the patched libsfizz.so."
        )
    return worker_path, library_path


@lru_cache(maxsize=16)
def _binary_sha256(path_text: str, size: int, mtime_ns: int) -> str:
    """Hash a renderer binary while using stat data only as a cache invalidator.

    The returned renderer identity intentionally excludes installation paths and
    mtimes so identical binaries installed on different machines share the same
    raw-cache identity.
    """
    path = Path(path_text)
    return sha256(path.read_bytes()).hexdigest()


def sfizz_renderer_identity(
    *, worker: Path | None = None, library: Path | None = None
) -> dict[str, object]:
    worker_path = find_sfizz_worker(worker)
    library_path = find_sfizz_library(library)
    identity: dict[str, object] = {
        "kind": "mrp-persistent-sfizz",
        "contract": SFIZZ_RENDERER_CONTRACT,
        "worker_protocol": SFIZZ_WORKER_PROTOCOL,
        "offline_api": SFIZZ_OFFLINE_API_VERSION,
        "task_seed": SFIZZ_TASK_SEED,
    }
    for name, path in (("worker", worker_path), ("libsfizz", library_path)):
        if path is None:
            identity[name] = None
            continue
        stat = path.stat()
        identity[name] = {
            "size": stat.st_size,
            "sha256": _binary_sha256(str(path), stat.st_size, stat.st_mtime_ns),
        }
    return identity


def _memory_snapshot() -> _MemorySnapshot:
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        values: dict[str, int] = {}
        for line in meminfo.read_text().splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            fields = raw.strip().split()
            if not fields:
                continue
            multiplier = 1024 if len(fields) > 1 and fields[1].lower() == "kb" else 1
            try:
                values[key] = int(fields[0]) * multiplier
            except ValueError:
                pass
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", values.get("MemFree", total))
        if total > 0:
            return _MemorySnapshot(total, max(available, 0))

    try:
        page = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_PHYS_PAGES")
        total = int(page) * int(pages)
    except (ValueError, OSError, AttributeError):
        total = 8 * _GIB
    return _MemorySnapshot(total, total)


def auto_resident_memory_budget() -> int:
    memory = _memory_snapshot()
    by_total = int(memory.total * _AUTO_TOTAL_FRACTION)
    by_available = max(memory.available - _AUTO_OS_RESERVE, 512 * _MIB)
    return max(512 * _MIB, min(by_total, by_available))


def format_bytes(value: int) -> str:
    if value >= _GIB:
        return f"{value / _GIB:.2f} GiB"
    if value >= _MIB:
        return f"{value / _MIB:.1f} MiB"
    return f"{value} B"


def write_event_file(midi_path: Path, out_path: Path, sample_rate: int) -> MidiEventInfo:
    """Flatten the prepared MIDI into the worker's frame-event protocol.

    The dedicated SFZ path is channel-agnostic, matching sfizz's synth-level
    event API. Preserve every channel message sfizz can consume directly.
    """
    mid = mido.MidiFile(midi_path)
    merged = mido.merge_tracks(mid.tracks)
    tempo = 500_000
    seconds = 0.0
    rows: list[tuple[int, str, int, int | None]] = []

    for msg in merged:
        seconds += mido.tick2second(msg.time, mid.ticks_per_beat, tempo)
        frame = int(seconds * sample_rate)
        if msg.is_meta:
            if msg.type == "set_tempo":
                tempo = int(msg.tempo)
            continue
        if msg.type == "note_on":
            rows.append((frame, "note_off" if msg.velocity == 0 else "note_on", msg.note, msg.velocity))
        elif msg.type == "note_off":
            rows.append((frame, "note_off", msg.note, msg.velocity))
        elif msg.type == "control_change":
            rows.append((frame, "cc", msg.control, msg.value))
        elif msg.type == "pitchwheel":
            rows.append((frame, "pitch", msg.pitch, None))
        elif msg.type == "program_change":
            rows.append((frame, "program", msg.program, None))
        elif msg.type == "aftertouch":
            rows.append((frame, "aftertouch", msg.value, None))
        elif msg.type == "polytouch":
            rows.append((frame, "polytouch", msg.note, msg.value))

    end_frame = int(seconds * sample_rate)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as stream:
        stream.write(f"MRPEV1 {sample_rate}\n")
        stream.write(f"# source={midi_path}\n")
        for frame, kind, a, b in rows:
            if b is None:
                stream.write(f"{frame} {kind} {a}\n")
            else:
                stream.write(f"{frame} {kind} {a} {b}\n")
        stream.write(f"END {end_frame}\n")
    return MidiEventInfo(events=len(rows), end_frame=end_frame, seconds=seconds)


class _WorkerProcess:
    """Persistent subprocess with continuously drained stderr."""

    def __init__(self, command: Sequence[str], *, diagnostic_lines: int = 200):
        self._diagnostics: deque[tuple[int, str]] = deque(maxlen=diagnostic_lines)
        self._diagnostics_lock = threading.Lock()
        self._diagnostic_seq = 0
        self.proc = subprocess.Popen(
            [str(part) for part in command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if self.proc.stdin is None or self.proc.stdout is None or self.proc.stderr is None:
            self._terminate_best_effort()
            raise RuntimeError("failed to create sfizz worker stdio pipes")
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name=f"mrp-sfizz-stderr-{self.proc.pid}",
            daemon=True,
        )
        self._stderr_thread.start()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        try:
            for line in self.proc.stderr:
                text = line.rstrip("\r\n")
                if not text:
                    continue
                with self._diagnostics_lock:
                    self._diagnostic_seq += 1
                    self._diagnostics.append((self._diagnostic_seq, text))
        except (OSError, ValueError):
            pass

    def diagnostic_cursor(self) -> int:
        with self._diagnostics_lock:
            return self._diagnostic_seq

    def diagnostics_since(self, cursor: int) -> tuple[int, str]:
        with self._diagnostics_lock:
            current = self._diagnostic_seq
            lines = [text for seq, text in self._diagnostics if seq > cursor]
        return current, "\n".join(lines)

    def recent_diagnostics(self) -> str:
        with self._diagnostics_lock:
            return "\n".join(text for _, text in self._diagnostics)

    def _failure_context(self) -> str:
        rc = self.proc.poll()
        diagnostics = self.recent_diagnostics()
        status = f"returncode={rc}" if rc is not None else "returncode=unknown"
        if diagnostics:
            return f"{status}\nrecent worker stderr:\n{diagnostics}"
        return f"{status}\nrecent worker stderr: <empty>"

    def send(self, command: str) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(command.rstrip("\n") + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise RuntimeError(f"failed to send sfizz worker command: {exc}\n{self._failure_context()}") from exc

    def read_reply(self, *, timeout: float) -> str:
        assert self.proc.stdout is not None
        selector = selectors.DefaultSelector()
        try:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            if not selector.select(timeout):
                self._terminate_best_effort()
                raise RuntimeError(
                    f"sfizz worker timed out after {timeout:.1f}s\n{self._failure_context()}"
                )
        finally:
            selector.close()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"sfizz worker exited unexpectedly\n{self._failure_context()}")
        line = line.rstrip("\r\n")
        if line.startswith("ERR\t"):
            raise RuntimeError(f"{line}\n{self._failure_context()}")
        return line

    def _terminate_best_effort(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
        except Exception:
            pass

    def close(self, *, graceful: bool = True) -> None:
        if graceful and self.proc.poll() is None:
            try:
                self.send("QUIT")
                self.read_reply(timeout=2.0)
            except Exception:
                pass
        self._terminate_best_effort()
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        self._stderr_thread.join(timeout=1.0)
        try:
            if self.proc.stderr is not None:
                self.proc.stderr.close()
        except Exception:
            pass


class ResidentSfizzWorker:
    def __init__(
        self,
        *,
        worker_path: Path,
        library_path: Path,
        blocksize: int,
        samplerate: int,
        quality: int,
        polyphony: int,
    ):
        command = [
            worker_path,
            "--libsfizz", library_path,
            "--block-size", str(blocksize),
            "--sample-rate", str(samplerate),
            "--quality", str(quality),
            "--polyphony", str(polyphony),
        ]
        self.process = _WorkerProcess(command)
        self._diag_cursor = self.process.diagnostic_cursor()
        ready = self.process.read_reply(timeout=15.0)
        fields = _parse_reply(ready)
        if fields[0] != "READY":
            self.close(graceful=False)
            raise RuntimeError(f"unexpected sfizz worker greeting: {ready}")
        values = fields[1]
        if int(values.get("protocol", -1)) != SFIZZ_WORKER_PROTOCOL:
            self.close(graceful=False)
            raise RuntimeError(f"unsupported sfizz worker protocol: {ready}")
        if int(values.get("offline_api", 0)) < SFIZZ_OFFLINE_API_VERSION:
            self.close(graceful=False)
            raise RuntimeError(f"unsupported sfizz offline API: {ready}")

    @property
    def pid(self) -> int:
        return self.process.pid

    def _new_diagnostics(self) -> str:
        self._diag_cursor, text = self.process.diagnostics_since(self._diag_cursor)
        return text

    def load(self, sfz: Path) -> WorkerLoadInfo:
        self.process.send(f"LOAD\t{sfz.resolve()}")
        reply = self.process.read_reply(timeout=600.0)
        tag, values = _parse_reply(reply)
        if tag != "OK LOAD":
            raise RuntimeError(f"unexpected sfizz LOAD reply: {reply}")
        return WorkerLoadInfo(
            milliseconds=float(values["ms"]),
            regions=int(values["regions"]),
            preloaded_samples=int(values["preloaded_samples"]),
            sfizz_bytes=int(values["sfizz_bytes"]),
            diagnostics=self._new_diagnostics(),
        )

    def render(
        self,
        events: Path,
        output: Path,
        *,
        seed: int,
        midi_seconds: float,
    ) -> WorkerRenderInfo:
        self.process.send(f"RENDER\t{events}\t{output}\t{seed}")
        timeout = max(300.0, midi_seconds * 20.0 + 60.0)
        reply = self.process.read_reply(timeout=timeout)
        tag, values = _parse_reply(reply)
        if tag != "OK RENDER":
            raise RuntimeError(f"unexpected sfizz RENDER reply: {reply}")
        return WorkerRenderInfo(
            milliseconds=float(values["ms"]),
            frames=int(values["frames"]),
            active_after=int(values["active_after"]),
            tail_limit=bool(int(values["tail_limit"])),
            instrument_loads=int(values["instrument_loads"]),
            diagnostics=self._new_diagnostics(),
        )

    def close(self, *, graceful: bool = True) -> None:
        self.process.close(graceful=graceful)


def _parse_reply(line: str) -> tuple[str, dict[str, str]]:
    parts = line.split("\t")
    if not parts:
        raise RuntimeError("empty sfizz worker reply")
    if parts[0] == "READY":
        tag = "READY"
        raw_fields = parts[1:]
    elif len(parts) >= 2 and parts[0] == "OK":
        tag = f"OK {parts[1]}"
        raw_fields = parts[2:]
    else:
        tag = parts[0]
        raw_fields = parts[1:]
    values: dict[str, str] = {}
    for field in raw_fields:
        if "=" in field:
            key, value = field.split("=", 1)
            values[key] = value
    return tag, values


def probe_sfizz_runtime(
    *, worker: Path | None = None, library: Path | None = None
) -> tuple[Path, Path, int]:
    worker_path, library_path = require_sfizz_runtime(worker=worker, library=library)
    process = _WorkerProcess([worker_path, "--libsfizz", library_path])
    try:
        ready = process.read_reply(timeout=15.0)
        tag, values = _parse_reply(ready)
        if tag != "READY":
            raise RuntimeError(f"unexpected sfizz worker greeting: {ready}")
        protocol = int(values.get("protocol", -1))
        offline_api = int(values.get("offline_api", 0))
        if protocol != SFIZZ_WORKER_PROTOCOL:
            raise RuntimeError(f"sfizz worker protocol {protocol} != {SFIZZ_WORKER_PROTOCOL}")
        if offline_api < SFIZZ_OFFLINE_API_VERSION:
            raise RuntimeError(
                f"sfizz offline API {offline_api} < {SFIZZ_OFFLINE_API_VERSION}"
            )
        return worker_path, library_path, offline_api
    finally:
        process.close()


class PersistentSfizzPool:
    """Instrument-affine resident sfizz process pool.

    One resident process owns one Synth/instrument for its lifetime and executes
    tasks serially. V1 intentionally permits at most one resident replica per
    InstrumentKey. Multiple different resident processes may render concurrently.
    """

    def __init__(
        self,
        *,
        max_workers: int,
        blocksize: int,
        samplerate: int,
        quality: int,
        polyphony: int,
        memory_budget_bytes: int | None = None,
        worker_path: Path | None = None,
        library_path: Path | None = None,
        worker_factory: Callable[..., ResidentSfizzWorker] = ResidentSfizzWorker,
    ):
        self.max_workers = max_workers
        self.blocksize = blocksize
        self.samplerate = samplerate
        self.quality = quality
        self.polyphony = polyphony
        self.worker_path = worker_path
        self.library_path = library_path
        self._worker_factory = worker_factory
        self._auto_budget = memory_budget_bytes is None
        self.memory_budget_bytes = (
            auto_resident_memory_budget() if memory_budget_bytes is None else memory_budget_bytes
        )
        if self.memory_budget_bytes <= 0:
            raise ValueError("SFZ resident memory budget must be > 0")

        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mrp-sfizz")
        self._lock = threading.Lock()
        self._entries: dict[InstrumentKey, _ResidentEntry] = {}
        self._known_costs: dict[InstrumentKey, int] = {}
        self._loading_keys: set[InstrumentKey] = set()
        self._reserved_cold_bytes = 0
        self._closed = False
        self._tasks = 0
        self._cold_loads = 0
        self._warm_renders = 0
        self._evictions = 0
        self._worker_failures = 0
        self._peak_resident_bytes = 0

    def _key(self, job: RenderJob) -> InstrumentKey:
        return InstrumentKey.from_job(
            job,
            blocksize=self.blocksize,
            samplerate=self.samplerate,
            quality=self.quality,
            polyphony=self.polyphony,
        )

    def _resident_bytes_locked(self) -> int:
        return sum(entry.resident_bytes for entry in self._entries.values())

    def _estimated_cost_locked(self, key: InstrumentKey) -> int:
        known = self._known_costs.get(key)
        if known is not None:
            return known
        return min(_UNKNOWN_INSTRUMENT_RESERVATION, self.memory_budget_bytes)

    def _eviction_plan_locked(self, key: InstrumentKey) -> list[InstrumentKey] | None:
        entry = self._entries.get(key)
        if entry is not None:
            return [] if not entry.busy else None
        if key in self._loading_keys:
            return None

        required = self._estimated_cost_locked(key)
        current = self._resident_bytes_locked() + self._reserved_cold_bytes
        if current + required <= self.memory_budget_bytes:
            return []

        evictable = sorted(
            (entry for entry in self._entries.values() if not entry.busy),
            key=lambda item: item.last_used,
        )
        plan: list[InstrumentKey] = []
        remaining = current
        for candidate in evictable:
            plan.append(candidate.key)
            remaining -= candidate.resident_bytes
            if remaining + required <= self.memory_budget_bytes:
                return plan

        # Auto mode may discover that one legitimate instrument is larger than
        # the conservative budget. Allow it only as the sole resident entry.
        if self._auto_budget and remaining == 0:
            return plan
        return None

    def can_accept(self, job: RenderJob) -> bool:
        key = self._key(job)
        with self._lock:
            if self._closed:
                return False
            return self._eviction_plan_locked(key) is not None

    def submit(self, job: RenderJob, *, seed: int = SFIZZ_TASK_SEED) -> Future:
        key = self._key(job)
        to_close: list[_ResidentEntry] = []
        with self._lock:
            if self._closed:
                raise RuntimeError("persistent sfizz pool is closed")
            entry = self._entries.get(key)
            if entry is not None:
                if entry.busy:
                    raise RuntimeError("resident SFZ worker was acquired twice")
                entry.busy = True
                self._tasks += 1
                self._warm_renders += 1
                return self._executor.submit(self._run_existing, entry, job, seed)

            plan = self._eviction_plan_locked(key)
            if plan is None:
                raise RuntimeError("SFZ task submitted without resident-memory admission")
            for victim_key in plan:
                victim = self._entries.pop(victim_key)
                to_close.append(victim)
                self._evictions += 1
            reservation = self._estimated_cost_locked(key)
            self._loading_keys.add(key)
            self._reserved_cold_bytes += reservation
            self._tasks += 1
            self._cold_loads += 1

        for victim in to_close:
            victim.worker.close()
        return self._executor.submit(self._run_new, key, job, seed, reservation)

    def _event_path(self, job: RenderJob) -> Path:
        return job.split_midi.with_suffix(job.split_midi.suffix + f".sr{self.samplerate}.mrpev")

    def _prepare_events(self, job: RenderJob) -> tuple[Path, MidiEventInfo]:
        path = self._event_path(job)
        info = write_event_file(job.split_midi, path, self.samplerate)
        return path, info

    def _make_stem(self, job: RenderJob, render: WorkerRenderInfo) -> RenderedStem:
        return RenderedStem(
            track=job.track,
            instrument=job.instrument,
            patch=job.patch,
            path=job.output,
            render_seconds=render.milliseconds / 1000.0,
            backend_log=render.diagnostics,
        )

    def _run_existing(
        self, entry: _ResidentEntry, job: RenderJob, seed: int
    ) -> PersistentSfizzExecution:
        try:
            events_path, event_info = self._prepare_events(job)
            job.output.parent.mkdir(parents=True, exist_ok=True)
            job.output.unlink(missing_ok=True)
            render = entry.worker.render(
                events_path, job.output, seed=seed, midi_seconds=event_info.seconds
            )
            if render.instrument_loads != 1:
                raise RuntimeError(
                    f"resident worker reloaded instrument unexpectedly: {render.instrument_loads} loads"
                )
            if not job.output.is_file():
                raise RuntimeError(f"sfizz worker produced no output: {job.output}")
            execution = PersistentSfizzExecution(
                stem=self._make_stem(job, render),
                diagnostics=render.diagnostics,
                cold_load=False,
                resident_bytes=entry.resident_bytes,
            )
        except Exception:
            job.output.unlink(missing_ok=True)
            self._invalidate_entry(entry)
            raise
        with self._lock:
            current = self._entries.get(entry.key)
            if current is entry:
                entry.busy = False
                entry.last_used = time.monotonic()
        return execution

    def _run_new(
        self, key: InstrumentKey, job: RenderJob, seed: int, reservation: int
    ) -> PersistentSfizzExecution:
        worker: ResidentSfizzWorker | None = None
        entry: _ResidentEntry | None = None
        extra_evictions: list[_ResidentEntry] = []
        reservation_released = False
        try:
            worker_path, library_path = require_sfizz_runtime(
                worker=self.worker_path, library=self.library_path
            )
            events_path, event_info = self._prepare_events(job)
            worker = self._worker_factory(
                worker_path=worker_path,
                library_path=library_path,
                blocksize=self.blocksize,
                samplerate=self.samplerate,
                quality=self.quality,
                polyphony=self.polyphony,
            )
            load = worker.load(job.patch.sfz)
            if load.sfizz_bytes <= 0:
                raise RuntimeError(f"sfizz reported invalid resident size: {load.sfizz_bytes}")

            with self._lock:
                self._loading_keys.discard(key)
                self._reserved_cold_bytes = max(0, self._reserved_cold_bytes - reservation)
                reservation_released = True
                self._known_costs[key] = load.sfizz_bytes
                current_resident = self._resident_bytes_locked()
                current = current_resident + self._reserved_cold_bytes
                if current + load.sfizz_bytes > self.memory_budget_bytes:
                    for candidate in sorted(
                        (item for item in self._entries.values() if not item.busy),
                        key=lambda item: item.last_used,
                    ):
                        self._entries.pop(candidate.key, None)
                        extra_evictions.append(candidate)
                        self._evictions += 1
                        current -= candidate.resident_bytes
                        if current + load.sfizz_bytes <= self.memory_budget_bytes:
                            break
                over_budget = current + load.sfizz_bytes > self.memory_budget_bytes
                if over_budget and not (self._auto_budget and current == 0):
                    raise RuntimeError(
                        "SFZ instrument exceeds resident memory admission after load: "
                        f"instrument={format_bytes(load.sfizz_bytes)} "
                        f"resident={format_bytes(current)} "
                        f"budget={format_bytes(self.memory_budget_bytes)}"
                    )
                entry = _ResidentEntry(
                    key=key,
                    label=job.patch.name,
                    worker=worker,
                    resident_bytes=load.sfizz_bytes,
                    busy=True,
                    last_used=time.monotonic(),
                )
                self._entries[key] = entry
                self._peak_resident_bytes = max(
                    self._peak_resident_bytes, self._resident_bytes_locked()
                )

            for victim in extra_evictions:
                victim.worker.close()
            extra_evictions.clear()

            job.output.parent.mkdir(parents=True, exist_ok=True)
            job.output.unlink(missing_ok=True)
            render = worker.render(
                events_path, job.output, seed=seed, midi_seconds=event_info.seconds
            )
            if render.instrument_loads != 1:
                raise RuntimeError(
                    f"new resident worker reloaded instrument unexpectedly: {render.instrument_loads} loads"
                )
            if not job.output.is_file():
                raise RuntimeError(f"sfizz worker produced no output: {job.output}")
            diagnostics = "\n".join(
                part for part in (load.diagnostics, render.diagnostics) if part
            )
            execution = PersistentSfizzExecution(
                stem=self._make_stem(job, render),
                diagnostics=diagnostics,
                cold_load=True,
                resident_bytes=load.sfizz_bytes,
            )
        except Exception:
            job.output.unlink(missing_ok=True)
            with self._lock:
                self._loading_keys.discard(key)
                if not reservation_released:
                    self._reserved_cold_bytes = max(0, self._reserved_cold_bytes - reservation)
                if entry is not None and self._entries.get(key) is entry:
                    self._entries.pop(key, None)
                self._worker_failures += 1
            if worker is not None:
                worker.close(graceful=False)
            for victim in extra_evictions:
                victim.worker.close()
            raise

        assert entry is not None
        with self._lock:
            current = self._entries.get(key)
            if current is entry:
                entry.busy = False
                entry.last_used = time.monotonic()
        return execution

    def _invalidate_entry(self, entry: _ResidentEntry) -> None:
        with self._lock:
            if self._entries.get(entry.key) is entry:
                self._entries.pop(entry.key, None)
            self._worker_failures += 1
        entry.worker.close(graceful=False)

    def stats(self) -> PersistentSfizzStats:
        with self._lock:
            return PersistentSfizzStats(
                tasks=self._tasks,
                cold_loads=self._cold_loads,
                warm_renders=self._warm_renders,
                evictions=self._evictions,
                worker_failures=self._worker_failures,
                current_resident_bytes=self._resident_bytes_locked(),
                peak_resident_bytes=self._peak_resident_bytes,
                memory_budget_bytes=self.memory_budget_bytes,
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry.worker.close()
