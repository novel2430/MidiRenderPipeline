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


SFIZZ_WORKER_PROTOCOL = 5
SFIZZ_OFFLINE_API_VERSION = 3
SFIZZ_TASK_SEED = 0
SFIZZ_SAMPLE_LOADING = "deterministic-lazy"
SFIZZ_SAMPLE_LOADING_CODE = 2
SFIZZ_RENDERER_CONTRACT = "mrp-persistent-sfizz-v3"
_GIB = 1024 ** 3
_MIB = 1024 ** 2
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
    sample_resident_bytes: int
    sample_peak_bytes: int
    full_resident_samples: int
    diagnostics: str = ""


@dataclass(frozen=True)
class WorkerRenderInfo:
    milliseconds: float
    frames: int
    active_after: int
    tail_limit: bool
    instrument_loads: int
    sfizz_bytes: int
    sample_resident_bytes: int
    sample_peak_bytes: int
    full_resident_samples: int
    diagnostics: str = ""


@dataclass(frozen=True)
class PersistentSfizzExecution:
    stem: RenderedStem
    diagnostics: str = ""
    worker_started: bool = False
    working_set_bytes: int = 0
    working_set_growth_bytes: int = 0
    sample_resident_bytes: int = 0
    sample_peak_bytes: int = 0
    full_resident_samples: int = 0


@dataclass(frozen=True)
class PersistentSfizzStats:
    tasks: int
    worker_starts: int
    worker_reuses: int
    worker_scale_outs: int
    worker_evictions: int
    worker_failures: int
    current_resident_workers: int
    peak_resident_workers: int
    peak_active_workers: int
    peak_replicas_per_key: int
    replica_limit: int
    current_working_set_bytes: int
    peak_working_set_bytes: int
    current_sample_resident_bytes: int
    peak_sample_resident_bytes: int
    full_resident_samples: int
    memory_budget_bytes: int
    max_observed_task_growth_bytes: int


@dataclass
class _ResidentEntry:
    replica_id: int
    key: InstrumentKey
    label: str
    worker: "ResidentSfizzWorker"
    working_set_bytes: int
    sample_resident_bytes: int = 0
    full_resident_samples: int = 0
    busy: bool = False
    last_used: float = 0.0


@dataclass(frozen=True)
class _AdmissionPlan:
    entry_id: int | None
    victim_ids: tuple[int, ...]
    reservation: int
    scale_out: bool = False


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
        "sample_loading": SFIZZ_SAMPLE_LOADING,
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


def auto_sfizz_memory_budget() -> int:
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
            "--sample-loading", SFIZZ_SAMPLE_LOADING,
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
        if int(values.get("sample_loading", -1)) != SFIZZ_SAMPLE_LOADING_CODE:
            self.close(graceful=False)
            raise RuntimeError(f"unexpected sfizz sample-loading mode: {ready}")

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
            sample_resident_bytes=int(values["sample_bytes"]),
            sample_peak_bytes=int(values["sample_peak_bytes"]),
            full_resident_samples=int(values["full_resident_samples"]),
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
            sfizz_bytes=int(values["sfizz_bytes"]),
            sample_resident_bytes=int(values["sample_bytes"]),
            sample_peak_bytes=int(values["sample_peak_bytes"]),
            full_resident_samples=int(values["full_resident_samples"]),
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
        if int(values.get("sample_loading", -1)) != SFIZZ_SAMPLE_LOADING_CODE:
            raise RuntimeError(
                f"sfizz sample-loading mode {values.get('sample_loading')} != "
                f"{SFIZZ_SAMPLE_LOADING_CODE}"
            )
        return worker_path, library_path, offline_api
    finally:
        process.close()


class PersistentSfizzPool:
    """Instrument-affine persistent sfizz worker pool with elastic replicas.

    Each InstrumentKey owns one or more resident worker replicas up to
    ``max_replicas_per_key``. Idle replicas are reused preferentially; when all
    replicas for a warmed instrument are busy, pressure may start another
    replica if the per-key cap and observed working-set budget allow it.

    Memory accounting is based on workers' observed working sets, not the
    theoretical decoded size of the SFZ. The configured memory budget is a
    steady-state target: known growth is reserved before a task, idle workers
    are evicted globally by LRU when needed, and previously unseen workloads
    are admitted once so their real footprint can be learned. A key must finish
    at least one render before it can scale out, preventing multiple unknown
    first-touch working sets from being admitted concurrently.
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
        max_replicas_per_key: int = 1,
        worker_path: Path | None = None,
        library_path: Path | None = None,
        worker_factory: Callable[..., ResidentSfizzWorker] = ResidentSfizzWorker,
    ):
        if max_workers < 1:
            raise ValueError("SFZ max workers must be >= 1")
        if max_replicas_per_key < 1:
            raise ValueError("SFZ max replicas per key must be >= 1")
        self.max_workers = max_workers
        self.max_replicas_per_key = max_replicas_per_key
        self.blocksize = blocksize
        self.samplerate = samplerate
        self.quality = quality
        self.polyphony = polyphony
        self.worker_path = worker_path
        self.library_path = library_path
        self._worker_factory = worker_factory
        self.memory_budget_bytes = (
            auto_sfizz_memory_budget() if memory_budget_bytes is None else memory_budget_bytes
        )
        if self.memory_budget_bytes <= 0:
            raise ValueError("SFZ memory budget must be > 0")

        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="mrp-sfizz"
        )
        self._lock = threading.Lock()
        # Worker instances, not InstrumentKeys, are the eviction/lifecycle unit.
        self._entries: dict[int, _ResidentEntry] = {}
        self._by_key: dict[InstrumentKey, set[int]] = {}
        self._starting_counts: dict[InstrumentKey, int] = {}
        self._completed_keys: set[InstrumentKey] = set()
        self._active_outputs: set[Path] = set()
        self._next_replica_id = 1
        # Historical observations survive worker eviction and are estimates only.
        self._observed_worker_peak: dict[InstrumentKey, int] = {}
        self._observed_task_growth: dict[InstrumentKey, int] = {}
        # Reservations cover only growth which has actually been observed before.
        self._reserved_growth_bytes = 0
        self._closed = False
        self._tasks = 0
        self._worker_starts = 0
        self._worker_reuses = 0
        self._worker_scale_outs = 0
        self._worker_evictions = 0
        self._worker_failures = 0
        self._peak_resident_workers = 0
        self._peak_active_workers = 0
        self._peak_replicas_per_key = 0
        self._peak_working_set_bytes = 0
        self._peak_sample_resident_bytes = 0
        self._max_observed_task_growth_bytes = 0

    def _key(self, job: RenderJob) -> InstrumentKey:
        return InstrumentKey.from_job(
            job,
            blocksize=self.blocksize,
            samplerate=self.samplerate,
            quality=self.quality,
            polyphony=self.polyphony,
        )

    @staticmethod
    def _output_key(job: RenderJob) -> Path:
        return job.output.resolve()

    def _entries_for_key_locked(self, key: InstrumentKey) -> list[_ResidentEntry]:
        return [
            self._entries[replica_id]
            for replica_id in self._by_key.get(key, ())
            if replica_id in self._entries
        ]

    def _starting_count_locked(self, key: InstrumentKey) -> int:
        return self._starting_counts.get(key, 0)

    def _set_starting_delta_locked(self, key: InstrumentKey, delta: int) -> None:
        value = self._starting_counts.get(key, 0) + delta
        if value < 0:
            raise RuntimeError("SFZ starting replica count became negative")
        if value:
            self._starting_counts[key] = value
        else:
            self._starting_counts.pop(key, None)
        self._update_parallelism_peaks_locked()

    def _add_entry_locked(self, entry: _ResidentEntry) -> None:
        self._entries[entry.replica_id] = entry
        self._by_key.setdefault(entry.key, set()).add(entry.replica_id)
        self._update_parallelism_peaks_locked()

    def _remove_entry_locked(self, entry: _ResidentEntry) -> bool:
        current = self._entries.get(entry.replica_id)
        if current is not entry:
            return False
        self._entries.pop(entry.replica_id, None)
        replica_ids = self._by_key.get(entry.key)
        if replica_ids is not None:
            replica_ids.discard(entry.replica_id)
            if not replica_ids:
                self._by_key.pop(entry.key, None)
        return True

    def _active_workers_locked(self) -> int:
        return sum(1 for entry in self._entries.values() if entry.busy) + sum(
            self._starting_counts.values()
        )

    def _update_parallelism_peaks_locked(self) -> None:
        self._peak_resident_workers = max(
            self._peak_resident_workers, len(self._entries)
        )
        self._peak_active_workers = max(
            self._peak_active_workers, self._active_workers_locked()
        )
        keys = set(self._by_key) | set(self._starting_counts)
        if keys:
            current_max = max(
                len(self._by_key.get(key, ())) + self._starting_counts.get(key, 0)
                for key in keys
            )
            self._peak_replicas_per_key = max(
                self._peak_replicas_per_key, current_max
            )

    def _working_set_bytes_locked(self) -> int:
        return sum(entry.working_set_bytes for entry in self._entries.values())

    def _sample_resident_bytes_locked(self) -> int:
        return sum(entry.sample_resident_bytes for entry in self._entries.values())

    def _record_working_set_locked(
        self,
        entry: _ResidentEntry,
        *,
        sfizz_bytes: int,
        sample_resident_bytes: int,
        full_resident_samples: int,
        task_growth_bytes: int = 0,
    ) -> None:
        if sfizz_bytes <= 0:
            raise RuntimeError(f"sfizz reported invalid working-set size: {sfizz_bytes}")
        if sample_resident_bytes < 0:
            raise RuntimeError(
                f"sfizz reported invalid sample-resident size: {sample_resident_bytes}"
            )
        entry.working_set_bytes = sfizz_bytes
        entry.sample_resident_bytes = sample_resident_bytes
        entry.full_resident_samples = full_resident_samples
        self._observed_worker_peak[entry.key] = max(
            self._observed_worker_peak.get(entry.key, 0), sfizz_bytes
        )
        if task_growth_bytes > 0:
            self._observed_task_growth[entry.key] = max(
                self._observed_task_growth.get(entry.key, 0), task_growth_bytes
            )
            self._max_observed_task_growth_bytes = max(
                self._max_observed_task_growth_bytes, task_growth_bytes
            )
        self._peak_working_set_bytes = max(
            self._peak_working_set_bytes, self._working_set_bytes_locked()
        )
        self._peak_sample_resident_bytes = max(
            self._peak_sample_resident_bytes, self._sample_resident_bytes_locked()
        )

    def _select_idle_entry_locked(self, key: InstrumentKey) -> _ResidentEntry | None:
        idle = [entry for entry in self._entries_for_key_locked(key) if not entry.busy]
        if not idle:
            return None
        # Prefer the hottest replica for reuse. A larger decoded sample payload is
        # more likely to contain corpus-common samples; recency breaks ties.
        return max(
            idle,
            key=lambda entry: (
                entry.sample_resident_bytes,
                entry.last_used,
                entry.working_set_bytes,
            ),
        )

    def _estimated_reservation_locked(
        self, key: InstrumentKey, entry: _ResidentEntry | None
    ) -> int:
        if entry is None:
            # A recreated/replica worker can use its previously observed
            # high-water mark. A never-seen instrument has no honest estimate
            # and is admitted once with zero reservation so the pool can learn.
            return self._observed_worker_peak.get(key, 0)
        return self._observed_task_growth.get(key, 0)

    def _admission_plan_locked(
        self, key: InstrumentKey, output_key: Path
    ) -> _AdmissionPlan | None:
        if output_key in self._active_outputs:
            return None

        entry = self._select_idle_entry_locked(key)
        resident = self._entries_for_key_locked(key)
        starting = self._starting_count_locked(key)
        scale_out = False

        if entry is None:
            replicas = len(resident) + starting
            if replicas >= self.max_replicas_per_key:
                return None
            # Never admit multiple unknown first-touch replicas. Once one task has
            # completed, historical high-water data can safely guide scale-out.
            if replicas > 0 and key not in self._completed_keys:
                return None
            scale_out = replicas > 0

        reservation = self._estimated_reservation_locked(key, entry)
        current = self._working_set_bytes_locked() + self._reserved_growth_bytes
        if current + reservation <= self.memory_budget_bytes:
            return _AdmissionPlan(
                entry_id=None if entry is None else entry.replica_id,
                victim_ids=(),
                reservation=reservation,
                scale_out=scale_out,
            )

        # Keep the selected target worker if it already exists; evict only other
        # idle LRU workers. Busy workers are never killed for memory admission.
        evictable = sorted(
            (
                candidate
                for candidate in self._entries.values()
                if not candidate.busy and candidate is not entry
            ),
            key=lambda item: item.last_used,
        )
        victim_ids: list[int] = []
        remaining = current
        for candidate in evictable:
            victim_ids.append(candidate.replica_id)
            remaining -= candidate.working_set_bytes
            if remaining + reservation <= self.memory_budget_bytes:
                return _AdmissionPlan(
                    entry_id=None if entry is None else entry.replica_id,
                    victim_ids=tuple(victim_ids),
                    reservation=reservation,
                    scale_out=scale_out,
                )

        # The budget is a steady-state target, not a hard allocation limit. If
        # no other busy worker is causing pressure, let a sole/reused target run
        # and learn/refresh its real footprint. Scale-out always has another
        # busy/starting replica and therefore cannot use this escape hatch.
        other_busy = any(
            candidate.busy and candidate is not entry
            for candidate in self._entries.values()
        ) or any(self._starting_counts.values())
        if not other_busy:
            return _AdmissionPlan(
                entry_id=None if entry is None else entry.replica_id,
                victim_ids=tuple(victim_ids),
                reservation=reservation,
                scale_out=scale_out,
            )
        return None

    def _trim_idle_to_budget_locked(self) -> list[_ResidentEntry]:
        """Evict idle LRU worker replicas until the steady-state target is met."""
        current = self._working_set_bytes_locked()
        if current <= self.memory_budget_bytes:
            return []

        victims: list[_ResidentEntry] = []
        for candidate in sorted(
            (entry for entry in self._entries.values() if not entry.busy),
            key=lambda item: item.last_used,
        ):
            # Keep a sole worker even when its own working set exceeds the target;
            # evicting it cannot make an in-flight task safer and would destroy
            # useful residency after every render.
            if len(self._entries) == 1:
                break
            if not self._remove_entry_locked(candidate):
                continue
            victims.append(candidate)
            self._worker_evictions += 1
            current -= candidate.working_set_bytes
            if current <= self.memory_budget_bytes:
                break
        return victims

    def can_accept(self, job: RenderJob) -> bool:
        key = self._key(job)
        output_key = self._output_key(job)
        with self._lock:
            if self._closed:
                return False
            return self._admission_plan_locked(key, output_key) is not None

    def submit(self, job: RenderJob, *, seed: int = SFIZZ_TASK_SEED) -> Future:
        key = self._key(job)
        output_key = self._output_key(job)
        to_close: list[_ResidentEntry] = []
        entry: _ResidentEntry | None = None
        before = 0
        before_sample = 0
        reservation = 0
        worker_started = False
        scale_out = False
        with self._lock:
            if self._closed:
                raise RuntimeError("persistent sfizz pool is closed")
            if output_key in self._active_outputs:
                raise RuntimeError(f"SFZ output is already active: {job.output}")
            plan = self._admission_plan_locked(key, output_key)
            if plan is None:
                raise RuntimeError("SFZ task submitted without working-set admission")
            reservation = plan.reservation
            scale_out = plan.scale_out
            for victim_id in plan.victim_ids:
                victim = self._entries.get(victim_id)
                if victim is None or victim.busy:
                    raise RuntimeError("SFZ admission victim changed while pool lock was held")
                self._remove_entry_locked(victim)
                to_close.append(victim)
                self._worker_evictions += 1

            if plan.entry_id is not None:
                entry = self._entries.get(plan.entry_id)
                if entry is None or entry.busy:
                    raise RuntimeError("resident SFZ replica was acquired twice")

            self._active_outputs.add(output_key)
            self._reserved_growth_bytes += reservation
            self._tasks += 1
            if entry is not None:
                entry.busy = True
                self._worker_reuses += 1
                before = entry.working_set_bytes
                before_sample = entry.sample_resident_bytes
                self._update_parallelism_peaks_locked()
            else:
                self._set_starting_delta_locked(key, +1)
                self._worker_starts += 1
                if scale_out:
                    self._worker_scale_outs += 1
                worker_started = True

        # Close planned victims before a new task can begin allocating sample
        # memory. This makes LRU admission a real memory handoff.
        for victim in to_close:
            victim.worker.close()

        try:
            if worker_started:
                return self._executor.submit(
                    self._run_new, key, job, output_key, seed, reservation
                )
            assert entry is not None
            return self._executor.submit(
                self._run_existing,
                entry,
                job,
                output_key,
                seed,
                reservation,
                before,
                before_sample,
            )
        except Exception:
            with self._lock:
                self._active_outputs.discard(output_key)
                self._reserved_growth_bytes = max(
                    0, self._reserved_growth_bytes - reservation
                )
                if worker_started:
                    self._set_starting_delta_locked(key, -1)
                    self._worker_starts = max(0, self._worker_starts - 1)
                    if scale_out:
                        self._worker_scale_outs = max(0, self._worker_scale_outs - 1)
                elif entry is not None and self._entries.get(entry.replica_id) is entry:
                    entry.busy = False
                    self._worker_reuses = max(0, self._worker_reuses - 1)
                self._tasks = max(0, self._tasks - 1)
            raise

    def _event_path(self, job: RenderJob) -> Path:
        return job.split_midi.with_suffix(
            job.split_midi.suffix + f".sr{self.samplerate}.mrpev"
        )

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

    def _execution(
        self,
        job: RenderJob,
        render: WorkerRenderInfo,
        *,
        diagnostics: str,
        worker_started: bool,
        before_working_set: int,
        before_sample_resident: int,
    ) -> PersistentSfizzExecution:
        growth = max(
            0,
            render.sfizz_bytes - before_working_set,
            render.sample_resident_bytes - before_sample_resident,
        )
        return PersistentSfizzExecution(
            stem=self._make_stem(job, render),
            diagnostics=diagnostics,
            worker_started=worker_started,
            working_set_bytes=render.sfizz_bytes,
            working_set_growth_bytes=growth,
            sample_resident_bytes=render.sample_resident_bytes,
            sample_peak_bytes=render.sample_peak_bytes,
            full_resident_samples=render.full_resident_samples,
        )

    def _run_existing(
        self,
        entry: _ResidentEntry,
        job: RenderJob,
        output_key: Path,
        seed: int,
        reservation: int,
        before_working_set: int,
        before_sample_resident: int,
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
            if render.sfizz_bytes <= 0:
                raise RuntimeError(
                    f"sfizz reported invalid working-set size: {render.sfizz_bytes}"
                )
            if not job.output.is_file():
                raise RuntimeError(f"sfizz worker produced no output: {job.output}")
            execution = self._execution(
                job,
                render,
                diagnostics=render.diagnostics,
                worker_started=False,
                before_working_set=before_working_set,
                before_sample_resident=before_sample_resident,
            )
        except Exception:
            job.output.unlink(missing_ok=True)
            with self._lock:
                self._active_outputs.discard(output_key)
                self._reserved_growth_bytes = max(
                    0, self._reserved_growth_bytes - reservation
                )
            self._invalidate_entry(entry)
            raise

        to_close: list[_ResidentEntry] = []
        with self._lock:
            self._active_outputs.discard(output_key)
            self._reserved_growth_bytes = max(
                0, self._reserved_growth_bytes - reservation
            )
            current = self._entries.get(entry.replica_id)
            if current is entry:
                self._record_working_set_locked(
                    entry,
                    sfizz_bytes=render.sfizz_bytes,
                    sample_resident_bytes=render.sample_resident_bytes,
                    full_resident_samples=render.full_resident_samples,
                    task_growth_bytes=execution.working_set_growth_bytes,
                )
                self._completed_keys.add(entry.key)
                entry.busy = False
                entry.last_used = time.monotonic()
                to_close = self._trim_idle_to_budget_locked()
        for victim in to_close:
            victim.worker.close()
        return execution

    def _run_new(
        self,
        key: InstrumentKey,
        job: RenderJob,
        output_key: Path,
        seed: int,
        reservation: int,
    ) -> PersistentSfizzExecution:
        worker: ResidentSfizzWorker | None = None
        entry: _ResidentEntry | None = None
        extra_evictions: list[_ResidentEntry] = []
        active_reservation = reservation
        starting_registered = True
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
                raise RuntimeError(
                    f"sfizz reported invalid working-set size: {load.sfizz_bytes}"
                )

            with self._lock:
                self._set_starting_delta_locked(key, -1)
                starting_registered = False
                # A new-worker reservation represents the previously observed
                # whole-worker high-water mark. Once LOAD reveals the baseline,
                # reserve only the still-unrealized portion before rendering.
                self._reserved_growth_bytes = max(
                    0, self._reserved_growth_bytes - active_reservation
                )
                active_reservation = max(0, reservation - load.sfizz_bytes)
                self._reserved_growth_bytes += active_reservation
                replica_id = self._next_replica_id
                self._next_replica_id += 1
                entry = _ResidentEntry(
                    replica_id=replica_id,
                    key=key,
                    label=job.patch.name,
                    worker=worker,
                    working_set_bytes=load.sfizz_bytes,
                    sample_resident_bytes=load.sample_resident_bytes,
                    full_resident_samples=load.full_resident_samples,
                    busy=True,
                    last_used=time.monotonic(),
                )
                self._add_entry_locked(entry)
                self._record_working_set_locked(
                    entry,
                    sfizz_bytes=load.sfizz_bytes,
                    sample_resident_bytes=load.sample_resident_bytes,
                    full_resident_samples=load.full_resident_samples,
                )
                # An unseen instrument can be larger than expected at LOAD.
                # Reclaim other idle workers immediately, but never fail merely
                # because the estimate was unknowable before first observation.
                extra_evictions = self._trim_idle_to_budget_locked()

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
            if render.sfizz_bytes <= 0:
                raise RuntimeError(
                    f"sfizz reported invalid working-set size: {render.sfizz_bytes}"
                )
            if not job.output.is_file():
                raise RuntimeError(f"sfizz worker produced no output: {job.output}")
            diagnostics = "\n".join(
                part for part in (load.diagnostics, render.diagnostics) if part
            )
            execution = self._execution(
                job,
                render,
                diagnostics=diagnostics,
                worker_started=True,
                before_working_set=load.sfizz_bytes,
                before_sample_resident=load.sample_resident_bytes,
            )
        except Exception:
            job.output.unlink(missing_ok=True)
            with self._lock:
                self._active_outputs.discard(output_key)
                if starting_registered:
                    self._set_starting_delta_locked(key, -1)
                    starting_registered = False
                self._reserved_growth_bytes = max(
                    0, self._reserved_growth_bytes - active_reservation
                )
                if entry is not None:
                    self._remove_entry_locked(entry)
                self._worker_failures += 1
            if worker is not None:
                worker.close(graceful=False)
            for victim in extra_evictions:
                victim.worker.close()
            raise

        assert entry is not None
        to_close: list[_ResidentEntry] = []
        with self._lock:
            self._active_outputs.discard(output_key)
            self._reserved_growth_bytes = max(
                0, self._reserved_growth_bytes - active_reservation
            )
            current = self._entries.get(entry.replica_id)
            if current is entry:
                self._record_working_set_locked(
                    entry,
                    sfizz_bytes=render.sfizz_bytes,
                    sample_resident_bytes=render.sample_resident_bytes,
                    full_resident_samples=render.full_resident_samples,
                    task_growth_bytes=execution.working_set_growth_bytes,
                )
                self._completed_keys.add(key)
                entry.busy = False
                entry.last_used = time.monotonic()
                to_close = self._trim_idle_to_budget_locked()
        for victim in to_close:
            victim.worker.close()
        return execution

    def _invalidate_entry(self, entry: _ResidentEntry) -> None:
        with self._lock:
            self._remove_entry_locked(entry)
            self._worker_failures += 1
        entry.worker.close(graceful=False)

    def stats(self) -> PersistentSfizzStats:
        with self._lock:
            return PersistentSfizzStats(
                tasks=self._tasks,
                worker_starts=self._worker_starts,
                worker_reuses=self._worker_reuses,
                worker_scale_outs=self._worker_scale_outs,
                worker_evictions=self._worker_evictions,
                worker_failures=self._worker_failures,
                current_resident_workers=len(self._entries),
                peak_resident_workers=self._peak_resident_workers,
                peak_active_workers=self._peak_active_workers,
                peak_replicas_per_key=self._peak_replicas_per_key,
                replica_limit=self.max_replicas_per_key,
                current_working_set_bytes=self._working_set_bytes_locked(),
                peak_working_set_bytes=self._peak_working_set_bytes,
                current_sample_resident_bytes=self._sample_resident_bytes_locked(),
                peak_sample_resident_bytes=self._peak_sample_resident_bytes,
                full_resident_samples=sum(
                    entry.full_resident_samples for entry in self._entries.values()
                ),
                memory_budget_bytes=self.memory_budget_bytes,
                max_observed_task_growth_bytes=self._max_observed_task_growth_bytes,
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
            self._by_key.clear()
            self._starting_counts.clear()
            self._active_outputs.clear()
            self._reserved_growth_bytes = 0
        for entry in entries:
            entry.worker.close()
