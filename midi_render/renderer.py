from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import subprocess
import sysconfig
import time

from .midi import TrackInfo
from .patches import Patch


@dataclass(frozen=True)
class RenderJob:
    track: TrackInfo
    instrument: str
    patch: Patch
    split_midi: Path
    output: Path


@dataclass(frozen=True)
class FluidSynthJob:
    track: TrackInfo
    instrument: str
    patch: Patch
    split_midi: Path
    soundfont: Path
    tool: str
    synth_gain: float
    output: Path


@dataclass(frozen=True)
class RenderedStem:
    track: TrackInfo
    instrument: str
    patch: Patch
    path: Path
    render_seconds: float


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_sfizz_render() -> Path | None:
    """Find the sfizz-render helper.

    pysfizz wheels ship the binary under ``site-packages/bin/sfizz_render``
    instead of the virtualenv's normal ``bin`` directory. Prefer a normal
    PATH installation, then fall back to that wheel layout.
    """
    found = shutil.which("sfizz_render")
    if found:
        return Path(found).resolve()

    purelib = Path(sysconfig.get_paths()["purelib"])
    for name in ("sfizz_render", "sfizz-render"):
        candidate = purelib / "bin" / name
        if _is_executable(candidate):
            return candidate.resolve()

    return None


def require_sfizz_render() -> str:
    path = find_sfizz_render()
    if path is None:
        raise RuntimeError(
            "sfizz_render not found. Install the project dependencies (pysfizz) "
            "or put sfizz_render in PATH."
        )
    return str(path)


def find_fluidsynth(tool: str = "fluidsynth") -> Path | None:
    path = Path(tool).expanduser()
    if path.is_absolute() or path.parent != Path("."):
        return path.resolve() if _is_executable(path) else None
    found = shutil.which(tool)
    return Path(found).resolve() if found else None


def require_fluidsynth(tool: str = "fluidsynth") -> str:
    path = find_fluidsynth(tool)
    if path is None:
        raise RuntimeError(f"FluidSynth executable not found: {tool}")
    return str(path)


def _run_job(
    job: RenderJob,
    sfizz_render: str,
    blocksize: int,
    samplerate: int,
    quality: int,
    polyphony: int,
) -> RenderedStem:
    job.output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sfizz_render,
        "--sfz", str(job.patch.sfz),
        "--midi", str(job.split_midi),
        "--wav", str(job.output),
        "--blocksize", str(blocksize),
        "--samplerate", str(samplerate),
        "--quality", str(quality),
        "--polyphony", str(polyphony),
    ]

    t0 = time.perf_counter()
    proc = subprocess.run(cmd)
    dt = time.perf_counter() - t0
    if proc.returncode != 0:
        raise RuntimeError(
            f"track {job.track.index} {job.track.name!r}: "
            f"sfizz_render returned {proc.returncode}"
        )

    return RenderedStem(
        track=job.track,
        instrument=job.instrument,
        patch=job.patch,
        path=job.output,
        render_seconds=dt,
    )


def render_jobs(
    jobs: list[RenderJob],
    *,
    workers: int,
    blocksize: int,
    samplerate: int,
    quality: int,
    polyphony: int,
) -> list[RenderedStem]:
    if not jobs:
        return []

    sfizz_render = require_sfizz_render()
    results: list[RenderedStem] = []

    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        futures = {
            pool.submit(
                _run_job,
                job,
                sfizz_render,
                blocksize,
                samplerate,
                quality,
                polyphony,
            ): job
            for job in jobs
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                f"  DONE track={result.track.index:02d} "
                f"{result.instrument:24s} {result.render_seconds:7.2f}s"
            )

    results.sort(key=lambda x: x.track.index)
    return results


def _run_fluidsynth_job(
    job: FluidSynthJob,
    fluidsynth: str,
    samplerate: int,
) -> RenderedStem:
    job.output.parent.mkdir(parents=True, exist_ok=True)
    job.output.unlink(missing_ok=True)

    # FluidSynth global options must precede positional SoundFont/MIDI inputs.
    # The split MIDI retains the original Program Change unless the resolver
    # explicitly created a representative-program override for dirty metadata.
    cmd = [
        fluidsynth,
        "-ni",
        "-r", str(samplerate),
        "-g", str(job.synth_gain),
        "-F", str(job.output),
        str(job.soundfont),
        str(job.split_midi),
    ]

    t0 = time.perf_counter()
    proc = subprocess.run(cmd)
    dt = time.perf_counter() - t0
    if proc.returncode != 0:
        raise RuntimeError(
            f"track {job.track.index} {job.track.name!r}: "
            f"FluidSynth returned {proc.returncode}"
        )
    if not job.output.is_file():
        raise RuntimeError(
            f"track {job.track.index} {job.track.name!r}: "
            f"FluidSynth produced no output: {job.output}"
        )

    return RenderedStem(
        track=job.track,
        instrument=job.instrument,
        patch=job.patch,
        path=job.output,
        render_seconds=dt,
    )


def render_fluidsynth_jobs(
    jobs: list[FluidSynthJob],
    *,
    workers: int,
    samplerate: int,
) -> list[RenderedStem]:
    if not jobs:
        return []

    # All jobs in one render command use the same configured executable.
    tool = require_fluidsynth(jobs[0].tool)
    results: list[RenderedStem] = []

    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        futures = {
            pool.submit(_run_fluidsynth_job, job, tool, samplerate): job
            for job in jobs
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                f"  DONE track={result.track.index:02d} "
                f"{result.instrument:24s} {result.render_seconds:7.2f}s [GM]"
            )

    results.sort(key=lambda x: x.track.index)
    return results
