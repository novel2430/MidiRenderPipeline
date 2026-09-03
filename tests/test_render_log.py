from __future__ import annotations

from io import StringIO
import json
from pathlib import Path

from midi_render.render_log import LogOptions, RenderLogger


class _TTY(StringIO):
    def isatty(self) -> bool:
        return True


class _Pipe(StringIO):
    def isatty(self) -> bool:
        return False


def test_color_auto_uses_ansi_only_for_tty(tmp_path: Path):
    tty = _TTY()
    logger = RenderLogger(LogOptions(color="auto"), stream=tty)
    logger.single_header(Path("song.mid"), tmp_path / "song.wav")
    logger.close()
    assert "\x1b[" in tty.getvalue()

    pipe = _Pipe()
    logger = RenderLogger(LogOptions(color="auto"), stream=pipe)
    logger.single_header(Path("song.mid"), tmp_path / "song.wav")
    logger.close()
    assert "\x1b[" not in pipe.getvalue()


def test_no_color_environment_disables_forced_auto(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("NO_COLOR", "1")
    tty = _TTY()
    logger = RenderLogger(LogOptions(color="auto"), stream=tty)
    logger.single_header(Path("song.mid"), tmp_path / "song.wav")
    logger.close()
    assert "\x1b[" not in tty.getvalue()


def test_json_log_records_structured_task_event(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    logger = RenderLogger(LogOptions(json_log=path, color="never"), stream=_Pipe())
    logger.task_done(
        song=Path("song.mid"),
        stage="fx",
        backend="fx",
        label="04 electric_bass · gxsvt",
        seconds=1.25,
        stem_ids=("track-04:electric_bass",),
        diagnostics="hidden warning",
    )
    logger.close()
    event = json.loads(path.read_text().splitlines()[0])
    assert event["event"] == "task_done"
    assert event["stage"] == "fx"
    assert event["seconds"] == 1.25
    assert event["diagnostics"] is None


def test_debug_shows_backend_diagnostics_but_normal_hides_them(tmp_path: Path):
    normal = _Pipe()
    logger = RenderLogger(LogOptions(verbosity="normal", color="never"), stream=normal)
    logger.task_done(
        song=Path("song.mid"), stage="raw", backend="fluidsynth",
        label="06 harmonica · fluidsynth", seconds=1.0,
        stem_ids=("track-06:harmonica",), diagnostics="ALSA warning",
    )
    logger.close()
    assert "ALSA warning" not in normal.getvalue()

    debug = _Pipe()
    logger = RenderLogger(LogOptions(verbosity="debug", color="never"), stream=debug)
    logger.task_done(
        song=Path("song.mid"), stage="raw", backend="fluidsynth",
        label="06 harmonica · fluidsynth", seconds=1.0,
        stem_ids=("track-06:harmonica",), diagnostics="ALSA warning",
    )
    logger.close()
    assert "ALSA warning" in debug.getvalue()


def test_batch_summary_reports_normalized_performance_and_peak_memory_only():
    from types import SimpleNamespace

    stream = _Pipe()
    logger = RenderLogger(LogOptions(mode="batch", color="never"), stream=stream)
    logger._started = 100.0

    import midi_render.render_log as render_log
    original = render_log.time.perf_counter
    render_log.time.perf_counter = lambda: 110.0
    try:
        stats = SimpleNamespace(
            tasks=10,
            worker_starts=2,
            worker_reuses=8,
            worker_scale_outs=1,
            worker_evictions=1,
            worker_failures=0,
            current_resident_workers=2,
            peak_resident_workers=3,
            peak_active_workers=2,
            peak_replicas_per_key=2,
            replica_limit=2,
            current_working_set_bytes=0,
            peak_working_set_bytes=2 * 1024 ** 3,
            current_sample_resident_bytes=0,
            peak_sample_resident_bytes=int(1.8 * 1024 ** 3),
            full_resident_samples=0,
            memory_budget_bytes=4 * 1024 ** 3,
            max_observed_task_growth_bytes=256 * 1024 ** 2,
        )
        logger.batch_summary(
            total=2,
            completed_now=2,
            failed_now=0,
            skipped_done=0,
            skipped_failed=0,
            track_seconds=100.0,
            track_bars=500.0,
            sfz_stats=stats,
        )
    finally:
        render_log.time.perf_counter = original
        logger.close()

    text = stream.getvalue()
    assert "songs/min          12.00" in text
    assert "track× realtime    10.0×" in text
    assert "ms / track-bar     20.0 ms" in text
    assert "peak working set" in text
    assert "peak sample payload" in text
    assert "scale-outs        1" in text
    assert "peak replicas/key 2 / 2" in text
    assert "current working set" not in text
    assert "full samples" not in text


def test_batch_header_uses_concurrency_and_marks_auto_backend_policy(tmp_path: Path):
    stream = _Pipe()
    logger = RenderLogger(LogOptions(mode="batch", verbosity="verbose", color="never"), stream=stream)
    logger.batch_header(
        total=59,
        active_songs=24,
        concurrency=24,
        backend_concurrency={"sfz": 24, "gm": 4, "fx": 24, "mix": 24},
        backend_auto={"sfz": True, "gm": True, "fx": True, "mix": True},
        sfz_max_replicas=2,
        sfz_memory_budget="auto",
        state_db=tmp_path / "state.sqlite3",
        run_identity="deadbeef",
    )
    logger.close()
    text = stream.getvalue()
    assert "59 MIDI · concurrency 24 · active songs 24" in text
    assert "SFZ auto→24" in text
    assert "GM auto→4" in text
    assert "MIX auto→24" in text
    assert "workers 24" not in text
