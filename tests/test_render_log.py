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
