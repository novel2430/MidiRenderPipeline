from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import ctypes
from pathlib import Path
import os
import shutil
import subprocess
import sysconfig
import tempfile
import time

import mido
import numpy as np
import soundfile as sf

from .fluidsynth_native import (
    FLUID_OK,
    FLUID_PLAYER_PLAYING,
    FluidSynthLibrary,
    FluidSynthNativeError,
    FluidSynthSession,
)
from .midi import TrackInfo
from .patches import Patch


GM_BATCH_BLOCKSIZE = 1024
GM_CPU_CORES = 1
GM_BATCH_CAPACITY = 16


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
    synth_gain: float
    output: Path


@dataclass(frozen=True)
class RenderedStem:
    track: TrackInfo
    instrument: str
    patch: Patch
    path: Path
    render_seconds: float
    backend_log: str = ""


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


_FLUIDSYNTH_LIBRARY_CACHE: FluidSynthLibrary | None = None


def require_fluidsynth_library() -> FluidSynthLibrary:
    global _FLUIDSYNTH_LIBRARY_CACHE
    if _FLUIDSYNTH_LIBRARY_CACHE is not None:
        return _FLUIDSYNTH_LIBRARY_CACHE
    try:
        _FLUIDSYNTH_LIBRARY_CACHE = FluidSynthLibrary()
        return _FLUIDSYNTH_LIBRARY_CACHE
    except FluidSynthNativeError as exc:
        raise RuntimeError(str(exc)) from exc


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
    proc = subprocess.run(cmd, capture_output=True, text=True)
    dt = time.perf_counter() - t0
    diagnostic = "\n".join(x.strip() for x in (proc.stdout, proc.stderr) if x and x.strip())
    if proc.returncode != 0:
        suffix = f": {diagnostic}" if diagnostic else ""
        raise RuntimeError(
            f"track {job.track.index} {job.track.name!r}: "
            f"sfizz_render returned {proc.returncode}{suffix}"
        )

    return RenderedStem(
        track=job.track,
        instrument=job.instrument,
        patch=job.patch,
        path=job.output,
        render_seconds=dt,
        backend_log=diagnostic,
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

    results.sort(key=lambda x: x.track.index)
    return results


def _copy_track(track: mido.MidiTrack) -> mido.MidiTrack:
    out = mido.MidiTrack()
    for msg in track:
        out.append(msg.copy())
    return out


def _gm_job_channels(job: FluidSynthJob) -> tuple[int, ...]:
    mid = mido.MidiFile(job.split_midi)
    if not mid.tracks:
        return ()
    channels = sorted(
        {
            int(msg.channel)
            for msg in mid.tracks[-1]
            if hasattr(msg, "channel")
        }
    )
    return tuple(channels)


def _remap_track_to_channel(track: mido.MidiTrack, channel: int) -> mido.MidiTrack:
    out = mido.MidiTrack()
    for msg in track:
        if hasattr(msg, "channel"):
            out.append(msg.copy(channel=channel))
        else:
            out.append(msg.copy())
    return out


def _build_gm_batch_midi(jobs: list[FluidSynthJob], output: Path) -> None:
    """Combine one-channel GM jobs into one SMF with one channel per stem.

    Every input is already a conductor+single-track split MIDI with performance
    and optional Program override applied. Assigning job N to MIDI channel N
    lets FluidSynth route that track to audio/effects group N.
    """
    if not jobs:
        raise ValueError("cannot build an empty GM batch")
    if len(jobs) > 16:
        raise ValueError("one FluidSynth MIDI batch can contain at most 16 stems")

    loaded = [mido.MidiFile(job.split_midi) for job in jobs]
    ticks_per_beat = loaded[0].ticks_per_beat
    if any(mid.ticks_per_beat != ticks_per_beat for mid in loaded[1:]):
        raise RuntimeError("GM batch MIDI files have different ticks_per_beat values")

    out = mido.MidiFile(type=1, ticks_per_beat=ticks_per_beat)

    # Split MIDI files created by midi.py contain conductor/meta track(s) first
    # and the selected musical track last. Keep the first job's conductor data
    # once; duplicating it per stem would duplicate tempo/meta events.
    first = loaded[0]
    if len(first.tracks) > 1:
        for conductor in first.tracks[:-1]:
            out.tracks.append(_copy_track(conductor))

    for slot, (job, mid) in enumerate(zip(jobs, loaded, strict=True)):
        if not mid.tracks:
            raise RuntimeError(f"track {job.track.index}: split MIDI contains no tracks")
        channels = {
            int(msg.channel)
            for msg in mid.tracks[-1]
            if hasattr(msg, "channel")
        }
        if len(channels) > 1:
            raise RuntimeError(
                f"track {job.track.index}: multi-channel MIDI cannot use GM batch fast-path"
            )
        out.tracks.append(_remap_track_to_channel(mid.tracks[-1], slot))

    output.parent.mkdir(parents=True, exist_ok=True)
    out.save(output)


def _render_native_file_job(
    job: FluidSynthJob,
    *,
    binding: FluidSynthLibrary,
    samplerate: int,
    cpu_cores: int,
) -> RenderedStem:
    """Compatibility path for a track that itself uses multiple MIDI channels."""
    job.output.parent.mkdir(parents=True, exist_ok=True)
    job.output.unlink(missing_ok=True)
    t0 = time.perf_counter()

    with FluidSynthSession(
        binding,
        soundfont=job.soundfont,
        samplerate=samplerate,
        synth_gain=job.synth_gain,
        audio_groups=1,
        effects_groups=1,
        cpu_cores=cpu_cores,
        output_file=job.output,
    ) as session:
        session.reset()
        player = session.new_player(job.split_midi)
        renderer = binding.lib.new_fluid_file_renderer(session.synth)
        if not renderer:
            session.finish_player(player)
            raise RuntimeError(
                f"track {job.track.index} {job.track.name!r}: "
                "new_fluid_file_renderer() failed"
            )
        try:
            while binding.lib.fluid_player_get_status(player) == FLUID_PLAYER_PLAYING:
                if binding.lib.fluid_file_renderer_process_block(renderer) != FLUID_OK:
                    raise RuntimeError(
                        f"track {job.track.index} {job.track.name!r}: "
                        "FluidSynth file renderer failed"
                    )
        finally:
            binding.lib.delete_fluid_file_renderer(renderer)
            session.finish_player(player)

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
        render_seconds=time.perf_counter() - t0,
    )


def _render_gm_batch(
    jobs: list[FluidSynthJob],
    *,
    binding: FluidSynthLibrary,
    session: FluidSynthSession,
    samplerate: int,
    blocksize: int = GM_BATCH_BLOCKSIZE,
) -> list[RenderedStem]:
    if not jobs:
        return []

    for job in jobs:
        job.output.parent.mkdir(parents=True, exist_ok=True)
        job.output.unlink(missing_ok=True)

    with tempfile.NamedTemporaryFile(
        prefix="midi-render-gm-batch-",
        suffix=".mid",
        dir=jobs[0].output.parent,
        delete=False,
    ) as handle:
        batch_midi = Path(handle.name)
    _build_gm_batch_midi(jobs, batch_midi)

    session.reset()
    # Channel numbers in the batch are synthetic stem slots. Neutralize the
    # General MIDI channel-10 drum special case, then opt a real drum job back in.
    session.set_all_channels_melodic()
    for slot, job in enumerate(jobs):
        if job.instrument == "drums":
            session.set_channel_drum(slot)

    player = session.new_player(batch_midi)
    group_count = binding.lib.fluid_synth_count_audio_channels(session.synth)
    effect_channels = binding.lib.fluid_synth_count_effects_channels(session.synth)
    effect_groups = binding.lib.fluid_synth_count_effects_groups(session.synth)
    if group_count < len(jobs) or effect_groups < len(jobs):
        session.finish_player(player)
        batch_midi.unlink(missing_ok=True)
        raise RuntimeError(
            "libfluidsynth did not allocate the requested independent GM output groups"
        )

    # fluid_synth_process() uses planar mono buffers: L/R for audio group 0,
    # then L/R for group 1, etc. Effects may alias dry buffers. Point both
    # reverb and chorus of effects unit K at dry output K so each rendered stem
    # receives exactly its own default FluidSynth reverb/chorus, without bleed.
    dry = [np.zeros(blocksize, dtype=np.float32) for _ in range(group_count * 2)]
    float_ptr = ctypes.POINTER(ctypes.c_float)
    dry_ptr_values = [buf.ctypes.data_as(float_ptr) for buf in dry]
    dry_ptrs = (float_ptr * len(dry_ptr_values))(*dry_ptr_values)

    fx_ptr_values = []
    for group in range(effect_groups):
        pair = group % group_count
        for _effect in range(effect_channels):
            fx_ptr_values.append(dry_ptr_values[pair * 2])
            fx_ptr_values.append(dry_ptr_values[pair * 2 + 1])
    fx_ptrs = (float_ptr * len(fx_ptr_values))(*fx_ptr_values)

    writers: list[sf.SoundFile] = []
    interleaved = [np.empty((blocksize, 2), dtype=np.float32) for _ in jobs]
    t0 = time.perf_counter()
    try:
        for job in jobs:
            writers.append(
                sf.SoundFile(
                    job.output,
                    mode="w",
                    samplerate=samplerate,
                    channels=2,
                    format="WAV",
                    subtype="PCM_16",
                )
            )

        while binding.lib.fluid_player_get_status(player) == FLUID_PLAYER_PLAYING:
            for buf in dry:
                buf.fill(0.0)
            result = binding.lib.fluid_synth_process(
                session.synth,
                blocksize,
                len(fx_ptr_values),
                fx_ptrs,
                len(dry_ptr_values),
                dry_ptrs,
            )
            if result != FLUID_OK:
                raise RuntimeError("fluid_synth_process() failed during GM batch render")

            for slot, writer in enumerate(writers):
                block = interleaved[slot]
                block[:, 0] = dry[slot * 2]
                block[:, 1] = dry[slot * 2 + 1]
                writer.write(block)
    finally:
        for writer in writers:
            writer.close()
        session.finish_player(player)
        batch_midi.unlink(missing_ok=True)

    dt = time.perf_counter() - t0
    return [
        RenderedStem(
            track=job.track,
            instrument=job.instrument,
            patch=job.patch,
            path=job.output,
            render_seconds=dt,
        )
        for job in jobs
    ]


def _chunk_simple_gm_jobs(jobs: list[FluidSynthJob]) -> list[list[FluidSynthJob]]:
    """Split simple GM jobs into <=16-stem batches without singleton tails."""
    if not jobs:
        return []
    if len(jobs) == 1:
        return [jobs]

    batches: list[list[FluidSynthJob]] = []
    start = 0
    while len(jobs) - start > GM_BATCH_CAPACITY:
        remaining = len(jobs) - start
        take = GM_BATCH_CAPACITY
        if remaining - take == 1:
            take -= 1
        batches.append(jobs[start : start + take])
        start += take
    batches.append(jobs[start:])
    return batches


def render_fluidsynth_jobs(
    jobs: list[FluidSynthJob],
    *,
    workers: int,
    samplerate: int,
) -> list[RenderedStem]:
    """Render GM fallback stems through embedded libfluidsynth.

    A singleton stem uses FluidSynth's native file renderer directly. Two or
    more ordinary one-channel stems share one SoundFont-backed synth and render
    through independent audio/effects groups in batches of at most 16. Rare
    tracks that themselves contain multiple MIDI channels always use the native
    single-stem file renderer so their channel/controller semantics are kept.

    ``workers`` is intentionally *not* mapped to ``synth.cpu-cores``. It still
    controls SFZ job scheduling at the CLI level, while the embedded GM backend
    currently uses one FluidSynth synthesis core; internal FluidSynth threading
    is a separate tuning knob and should be benchmarked independently.
    """
    if not jobs:
        return []

    soundfonts = {job.soundfont.resolve() for job in jobs}
    gains = {float(job.synth_gain) for job in jobs}
    if len(soundfonts) != 1 or len(gains) != 1:
        raise RuntimeError("one GM render call requires one SoundFont and one synth_gain")

    # Keep the public call signature stable while deliberately decoupling the
    # SFZ outer-job count from FluidSynth's internal synthesis thread count.
    _ = workers
    binding = require_fluidsynth_library()
    cpu_cores = GM_CPU_CORES

    simple: list[FluidSynthJob] = []
    compatibility: list[FluidSynthJob] = []
    for job in jobs:
        if len(_gm_job_channels(job)) <= 1:
            simple.append(job)
        else:
            compatibility.append(job)

    results: list[RenderedStem] = []

    # Native FluidSynth's file renderer is the fast path for a single stem.
    # Only use the Python multi-output block loop when there are actually
    # multiple stems to separate.
    if len(simple) == 1:
        result = _render_native_file_job(
            simple[0],
            binding=binding,
            samplerate=samplerate,
            cpu_cores=cpu_cores,
        )
        results.append(result)
    elif simple:
        batches = _chunk_simple_gm_jobs(simple)
        capacity = max(len(batch) for batch in batches)
        with FluidSynthSession(
            binding,
            soundfont=simple[0].soundfont,
            samplerate=samplerate,
            synth_gain=simple[0].synth_gain,
            audio_groups=capacity,
            effects_groups=capacity,
            cpu_cores=cpu_cores,
        ) as session:
            for batch in batches:
                batch_results = _render_gm_batch(
                    batch,
                    binding=binding,
                    session=session,
                    samplerate=samplerate,
                    blocksize=GM_BATCH_BLOCKSIZE,
                )
                results.extend(batch_results)

    for job in compatibility:
        result = _render_native_file_job(
            job,
            binding=binding,
            samplerate=samplerate,
            cpu_cores=cpu_cores,
        )
        results.append(result)

    results.sort(key=lambda x: x.track.index)
    return results
