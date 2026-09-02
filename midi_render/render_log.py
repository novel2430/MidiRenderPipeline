from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, TextIO


_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_COLORS = {
    "raw": "\x1b[34m",      # blue
    "gm": "\x1b[36m",       # cyan
    "fx": "\x1b[35m",       # magenta
    "mix": "\x1b[33m",      # yellow
    "done": "\x1b[32m",     # green
    "cache": "\x1b[32m",    # green
    "warn": "\x1b[33m",     # yellow
    "fail": "\x1b[31m",     # red
    "muted": "\x1b[90m",    # gray
}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class LogOptions:
    mode: str = "single"  # single | batch
    verbosity: str = "normal"  # normal | verbose | debug
    color: str = "auto"  # auto | always | never
    log_file: Path | None = None
    json_log: Path | None = None
    heartbeat_seconds: float = 5.0


class RenderLogger:
    """Single owner of rendering-system console and persistent logs.

    Backend workers never write user-facing progress directly. They return
    diagnostics to the coordinator, which emits structured events here.
    """

    def __init__(self, options: LogOptions, stream: TextIO | None = None):
        self.options = options
        self.stream = stream or sys.stdout
        self._use_color = self._resolve_color(options.color)
        self._log_fp: TextIO | None = None
        self._json_fp: TextIO | None = None
        self._status_open = False
        self._last_heartbeat = 0.0
        self._started = time.perf_counter()
        self._skipped_done = 0
        self._skipped_failed = 0
        if options.log_file is not None:
            path = options.log_file.resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fp = path.open("a", encoding="utf-8", buffering=1)
        if options.json_log is not None:
            path = options.json_log.resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._json_fp = path.open("a", encoding="utf-8", buffering=1)

    @property
    def verbose(self) -> bool:
        return self.options.verbosity in {"verbose", "debug"}

    @property
    def debug_enabled(self) -> bool:
        return self.options.verbosity == "debug"

    @property
    def batch_mode(self) -> bool:
        return self.options.mode == "batch"

    def close(self) -> None:
        self.finish_status()
        if self._log_fp is not None:
            self._log_fp.close()
            self._log_fp = None
        if self._json_fp is not None:
            self._json_fp.close()
            self._json_fp = None

    def __enter__(self) -> "RenderLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _resolve_color(self, mode: str) -> bool:
        if mode == "always":
            return True
        if mode == "never" or os.environ.get("NO_COLOR") is not None:
            return False
        return bool(getattr(self.stream, "isatty", lambda: False)())

    def _style(self, text: str, key: str, *, bold: bool = False, dim: bool = False) -> str:
        if not self._use_color:
            return text
        prefix = _COLORS.get(key, "")
        if bold:
            prefix += _BOLD
        if dim:
            prefix += _DIM
        return f"{prefix}{text}{_RESET}"

    def _event(self, event: str, **fields: Any) -> None:
        if self._json_fp is None:
            return
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        self._json_fp.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def _plain_record(self, line: str) -> None:
        if self._log_fp is not None:
            self._log_fp.write(_ANSI_RE.sub("", line) + "\n")

    def _clear_status(self) -> None:
        if not self._status_open:
            return
        if getattr(self.stream, "isatty", lambda: False)():
            self.stream.write("\r\x1b[2K")
        else:
            self.stream.write("\n")
        self.stream.flush()
        self._status_open = False

    def _line(self, line: str = "") -> None:
        self._clear_status()
        print(line, file=self.stream, flush=True)
        self._plain_record(line)

    def finish_status(self) -> None:
        if self._status_open:
            self.stream.write("\n")
            self.stream.flush()
            self._status_open = False

    def _batch_rates(
        self,
        *,
        done: int,
        track_seconds: float,
        track_bars: float,
    ) -> tuple[float, float, float]:
        elapsed = max(time.perf_counter() - self._started, 1e-9)
        songs_per_minute = done / elapsed * 60.0
        track_realtime = track_seconds / elapsed if track_seconds > 0 else 0.0
        ms_per_track_bar = elapsed * 1000.0 / track_bars if track_bars > 0 else 0.0
        return songs_per_minute, track_realtime, ms_per_track_bar

    def single_header(self, midi: Path, output: Path, *, track_index: int | None = None, stems: int | None = None) -> None:
        title = midi.stem
        suffix = f" · track {track_index:02d}" if track_index is not None else ""
        self._line(f"{self._style('◆', 'raw', bold=True)} {self._style(title, 'raw', bold=True)}{suffix}")
        if stems is not None:
            self._line(self._style(f"  {stems} stem{'s' if stems != 1 else ''} · {output}", "muted", dim=True))
        self._event("song_start", midi=str(midi), output=str(output), track_index=track_index, stems=stems)

    def batch_header(
        self,
        *,
        total: int,
        active_songs: int,
        workers: int,
        sfz_workers: int,
        sfz_max_replicas: int,
        gm_workers: int,
        fx_workers: int,
        mix_workers: int,
        sfz_memory_budget: str,
        state_db: Path,
        run_identity: str,
    ) -> None:
        self._line(f"{self._style('◆ MRP Batch', 'raw', bold=True)}")
        self._line(f"  {total:,} MIDI · workers {workers} · active {active_songs}")
        if self.verbose:
            self._line(
                self._style(
                    f"  caps: SFZ {sfz_workers} · GM {gm_workers} · FX {fx_workers} · MIX {mix_workers}",
                    "muted",
                )
            )
            self._line(
                self._style(
                    f"  SFZ replicas/key: {sfz_max_replicas}",
                    "muted",
                )
            )
            self._line(self._style(f"  SFZ memory budget: {sfz_memory_budget}", "muted"))
            self._line(self._style(f"  state: {state_db}", "muted"))
            self._line(self._style(f"  run:   {run_identity}", "muted"))
        self._event(
            "batch_start",
            total=total,
            active_songs=active_songs,
            workers=workers,
            backend_caps={"sfz": sfz_workers, "gm": gm_workers, "fx": fx_workers, "mix": mix_workers},
            sfz_max_replicas=sfz_max_replicas,
            sfz_memory_budget=sfz_memory_budget,
            state_db=str(state_db),
            run_identity=run_identity,
        )

    def plan_detail(self, message: str, **fields: Any) -> None:
        self._event("plan_detail", message=message, **fields)
        if self.verbose:
            self._line(self._style(f"  PLAN  {message}", "muted"))

    def scheduler(self, message: str, **fields: Any) -> None:
        self._event("scheduler", message=message, **fields)
        if self.verbose:
            self._line(self._style(f"  SCHED {message}", "muted"))

    def batch_skip(self, status: str, song: Path) -> None:
        if status == "DONE":
            self._skipped_done += 1
        elif status == "FAILED":
            self._skipped_failed += 1
        self._event("song_skip", status=status, song=str(song))
        if self.verbose:
            self._line(self._style(f"  SKIP  {status:6s} {song}", "muted"))

    def warning(self, message: str, **fields: Any) -> None:
        self._event("warning", message=message, **fields)
        self._line(f"{self._style('⚠ WARN', 'warn', bold=True)}  {message}")

    def cache_hit(self, *, song: Path, track_index: int, instrument: str, path: Path) -> None:
        self._event(
            "cache_hit",
            song=str(song),
            track_index=track_index,
            instrument=instrument,
            path=str(path),
        )
        if not self.batch_mode or self.verbose:
            self._line(
                f"  {self._style('RAW', 'raw', bold=True)}  "
                f"{track_index:02d} {instrument:28.28s} "
                f"{self._style('cache', 'cache')}"
            )

    def task_start(self, *, song: Path, stage: str, backend: str, label: str, stem_ids: tuple[str, ...]) -> None:
        self._event("task_start", song=str(song), stage=stage, backend=backend, label=label, stem_ids=stem_ids)
        if self.verbose:
            key = self._stage_key(stage, backend)
            stage_label = f"{stage.upper():>3}"
            self._line(f"  {self._style(stage_label, key, bold=True)}  {label}  {self._style('start', 'muted')}")

    def task_done(
        self,
        *,
        song: Path,
        stage: str,
        backend: str,
        label: str,
        seconds: float,
        stem_ids: tuple[str, ...],
        diagnostics: str = "",
    ) -> None:
        self._event(
            "task_done",
            song=str(song),
            stage=stage,
            backend=backend,
            label=label,
            seconds=seconds,
            stem_ids=stem_ids,
            diagnostics=diagnostics if self.debug_enabled else None,
        )
        if not self.batch_mode or self.verbose:
            key = self._stage_key(stage, backend)
            stage_label = f"{stage.upper():>3}"
            self._line(
                f"  {self._style(stage_label, key, bold=True)}  "
                f"{label:42.42s} {seconds:7.2f}s"
            )
        if diagnostics and self.debug_enabled:
            for line in diagnostics.rstrip().splitlines():
                self._line(self._style(f"       │ {line}", "muted", dim=True))

    def song_done(self, *, song: Path, output: Path, seconds: float, stats: dict[str, float] | None = None) -> None:
        self._event("song_done", song=str(song), output=str(output), seconds=seconds, stats=stats or {})
        if self.batch_mode:
            if self.verbose:
                self._line(f"{self._style('✓ DONE', 'done', bold=True)}  {song.name}  {seconds:.2f}s")
            return
        duration = float((stats or {}).get("duration_seconds", 0.0))
        speed = duration / seconds if seconds > 1e-9 and duration > 0 else 0.0
        speed_text = f" · {speed:.1f}× realtime" if speed else ""
        self._line()
        self._line(f"{self._style('✓ Done', 'done', bold=True)} · {seconds:.2f}s{speed_text}")
        self._line(self._style(f"  {output}", "muted"))

    def failure(self, *, song: Path | None, stage: str | None, backend: str | None, message: str, diagnostics: str = "") -> None:
        self._event(
            "failure",
            song=str(song) if song else None,
            stage=stage,
            backend=backend,
            message=message,
            diagnostics=diagnostics,
        )
        prefix = "✗ FAIL"
        if stage:
            prefix += f" {stage.upper()}"
        self._line(f"{self._style(prefix, 'fail', bold=True)}  {message}")
        if diagnostics:
            for line in diagnostics.rstrip().splitlines()[:30]:
                self._line(self._style(f"       │ {line}", "muted"))

    def backend_debug(self, backend: str, diagnostics: str, **fields: Any) -> None:
        if not diagnostics:
            return
        self._event("backend_diagnostics", backend=backend, diagnostics=diagnostics, **fields)
        if self.debug_enabled:
            self._line(self._style(f"  DEBUG {backend}", "muted"))
            for line in diagnostics.rstrip().splitlines():
                self._line(self._style(f"       │ {line}", "muted", dim=True))

    def batch_progress(
        self,
        *,
        total: int,
        done: int,
        failed: int,
        active: int,
        pending_raw: int,
        pending_fx: int,
        pending_mix: int,
        inflight: int,
        cache_hits: int,
        track_seconds: float = 0.0,
        track_bars: float = 0.0,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if not force and now - self._last_heartbeat < self.options.heartbeat_seconds:
            return
        self._last_heartbeat = now
        display_done = done + self._skipped_done
        display_failed = failed + self._skipped_failed
        completed = display_done + display_failed
        rate, track_realtime, ms_per_track_bar = self._batch_rates(
            done=done, track_seconds=track_seconds, track_bars=track_bars
        )
        pct = (100.0 * completed / total) if total else 100.0
        performance = f"{rate:.2f} songs/min"
        if track_realtime > 0:
            performance += f" · {track_realtime:.1f}× track-RT"
        if ms_per_track_bar > 0:
            performance += f" · {ms_per_track_bar:.1f} ms/trk-bar"
        line = (
            f"{completed:,}/{total:,} {pct:5.1f}% · active {active} · "
            f"raw {pending_raw} · fx {pending_fx} · mix {pending_mix} · run {inflight} · "
            f"cache {cache_hits} · fail {display_failed} · {performance}"
        )
        self._event(
            "batch_progress",
            total=total,
            done=done,
            failed=failed,
            active=active,
            pending_raw=pending_raw,
            pending_fx=pending_fx,
            pending_mix=pending_mix,
            inflight=inflight,
            cache_hits=cache_hits,
            songs_per_minute=rate,
            track_realtime=track_realtime,
            ms_per_track_bar=ms_per_track_bar,
            track_seconds=track_seconds,
            track_bars=track_bars,
        )
        if getattr(self.stream, "isatty", lambda: False)():
            self.stream.write("\r\x1b[2K" + self._style(line, "muted"))
            self.stream.flush()
            self._status_open = True
            self._plain_record(line)
        else:
            self._line(line)

    def batch_summary(
        self,
        *,
        total: int,
        completed_now: int,
        failed_now: int,
        skipped_done: int,
        skipped_failed: int,
        track_seconds: float = 0.0,
        track_bars: float = 0.0,
        sfz_stats: Any | None = None,
    ) -> None:
        self.finish_status()
        songs_per_minute, track_realtime, ms_per_track_bar = self._batch_rates(
            done=completed_now, track_seconds=track_seconds, track_bars=track_bars
        )
        self._event(
            "batch_summary",
            total=total,
            completed_now=completed_now,
            failed_now=failed_now,
            skipped_done=skipped_done,
            skipped_failed=skipped_failed,
            songs_per_minute=songs_per_minute,
            track_realtime=track_realtime,
            ms_per_track_bar=ms_per_track_bar,
            track_seconds=track_seconds,
            track_bars=track_bars,
            sfz_stats=(
                None if sfz_stats is None else {
                    "tasks": sfz_stats.tasks,
                    "worker_starts": sfz_stats.worker_starts,
                    "worker_reuses": sfz_stats.worker_reuses,
                    "worker_scale_outs": sfz_stats.worker_scale_outs,
                    "worker_evictions": sfz_stats.worker_evictions,
                    "worker_failures": sfz_stats.worker_failures,
                    "current_resident_workers": sfz_stats.current_resident_workers,
                    "peak_resident_workers": sfz_stats.peak_resident_workers,
                    "peak_active_workers": sfz_stats.peak_active_workers,
                    "peak_replicas_per_key": sfz_stats.peak_replicas_per_key,
                    "replica_limit": sfz_stats.replica_limit,
                    "current_working_set_bytes": sfz_stats.current_working_set_bytes,
                    "peak_working_set_bytes": sfz_stats.peak_working_set_bytes,
                    "current_sample_resident_bytes": sfz_stats.current_sample_resident_bytes,
                    "peak_sample_resident_bytes": sfz_stats.peak_sample_resident_bytes,
                    "full_resident_samples": sfz_stats.full_resident_samples,
                    "memory_budget_bytes": sfz_stats.memory_budget_bytes,
                    "max_observed_task_growth_bytes": sfz_stats.max_observed_task_growth_bytes,
                }
            ),
        )
        self._line()
        status_key = "done" if failed_now == 0 else "warn"
        self._line(self._style("Batch summary", status_key, bold=True))
        self._line(f"  completed now   {completed_now:,}")
        self._line(f"  failed now      {failed_now:,}")
        self._line(f"  skipped DONE    {skipped_done:,}")
        self._line(f"  skipped FAILED  {skipped_failed:,}")
        if completed_now:
            self._line("  Performance")
            self._line(f"    songs/min          {songs_per_minute:.2f}")
            if track_realtime > 0:
                self._line(f"    track× realtime    {track_realtime:.1f}×")
            if ms_per_track_bar > 0:
                self._line(f"    ms / track-bar     {ms_per_track_bar:.1f} ms")
        if sfz_stats is not None and sfz_stats.tasks:
            reuse_rate = 100.0 * sfz_stats.worker_reuses / sfz_stats.tasks
            self._line("  SFZ workers")
            self._line(f"    tasks             {sfz_stats.tasks:,}")
            self._line(f"    starts            {sfz_stats.worker_starts:,}")
            self._line(f"    reuses            {sfz_stats.worker_reuses:,}")
            self._line(f"    reuse rate        {reuse_rate:.1f}%")
            self._line(f"    scale-outs        {sfz_stats.worker_scale_outs:,}")
            self._line(f"    peak residents    {sfz_stats.peak_resident_workers:,}")
            self._line(f"    peak active       {sfz_stats.peak_active_workers:,}")
            self._line(
                f"    peak replicas/key {sfz_stats.peak_replicas_per_key:,} / {sfz_stats.replica_limit:,}"
            )
            self._line(f"    worker evictions  {sfz_stats.worker_evictions:,}")
            self._line(f"    worker failures   {sfz_stats.worker_failures:,}")
            self._line("  SFZ memory")
            self._line(f"    peak working set    {sfz_stats.peak_working_set_bytes / (1024 ** 3):.2f} GiB")
            self._line(f"    peak sample payload {sfz_stats.peak_sample_resident_bytes / (1024 ** 3):.2f} GiB")
            self._line(f"    budget              {sfz_stats.memory_budget_bytes / (1024 ** 3):.2f} GiB")
            self._line(f"    max task growth      {sfz_stats.max_observed_task_growth_bytes / (1024 ** 2):.0f} MiB")

    def _stage_key(self, stage: str, backend: str) -> str:
        if stage == "fx":
            return "fx"
        if stage == "mix":
            return "mix"
        if backend == "fluidsynth":
            return "gm"
        return "raw"
