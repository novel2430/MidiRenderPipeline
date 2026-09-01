from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha1, sha256
import json
from pathlib import Path
import shutil

from .effects import (
    LEGACY_LV2APPLY_BACKEND,
    NATIVE_LV2_BACKEND,
    find_effect_renderer_tool,
)
from .fluidsynth_native import fluidsynth_library_info
from .instruments import (
    melody_render_instrument,
    resolution_warnings,
    resolve_instrument,
    resolve_track,
)
from .midi import (
    VelocityPlan,
    analyze_midi,
    build_velocity_plan,
    make_note_filtered_midi,
    make_program_override_midi,
    make_split_midi,
    musical_tracks,
)
from .patches import Patch, PatchRegistry
from .render_log import LogOptions, RenderLogger
from .renderer import (
    FluidSynthJob,
    RenderJob,
    RenderedStem,
)
from .sfizz_persistent import (
    find_sfizz_library,
    find_sfizz_worker,
    format_bytes,
    probe_sfizz_runtime,
    sfizz_renderer_identity,
)
from .system import (
    Backend,
    RenderSettings,
    RenderingCoordinator,
    SongPlan,
    Stage,
    StateStore,
    StemPlan,
    make_song_id,
    make_stem_id,
    make_task,
)


DEFAULT_CONFIG = Path("config/patches.toml")


def _parse_byte_size(value: str) -> int | None:
    text = value.strip().lower()
    if text == "auto":
        return None
    units = {
        "b": 1,
        "kib": 1024,
        "mib": 1024 ** 2,
        "gib": 1024 ** 3,
        "kb": 1000,
        "mb": 1000 ** 2,
        "gb": 1000 ** 3,
    }
    for suffix in sorted(units, key=len, reverse=True):
        if text.endswith(suffix):
            number = text[:-len(suffix)].strip()
            break
    else:
        suffix = "b"
        number = text
    try:
        result = int(float(number) * units[suffix])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "memory size must look like auto, 12GiB, 4096MiB, or bytes"
        ) from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("memory size must be > 0")
    return result


def _registry(path: Path) -> PatchRegistry:
    if not path.is_file():
        raise SystemExit(f"FAIL: patch config not found: {path}")
    return PatchRegistry(path)


def cmd_doctor(args: argparse.Namespace) -> int:
    registry = _registry(args.config)
    print(f"Config: {registry.config_path}")
    print(f"Instruments: {registry.instruments_root}")
    print(f"FX:          {registry.fx_root}")
    print(f"LV2:         {registry.lv2_root}")
    print(f"Tools:       {registry.tools_root}\n")
    for line in registry.doctor_lines():
        print(line)
    print()
    sfizz_worker = find_sfizz_worker(registry.tools_root / "mrp-sfizz-worker")
    sfizz_library = find_sfizz_library(registry.sfizz_library)
    print(
        f"command  {'OK' if sfizz_worker else 'MISSING':7s}  "
        f"mrp-sfizz-worker  {sfizz_worker or ''}"
    )
    print(
        f"library  {'OK' if sfizz_library else 'MISSING':7s}  "
        f"MRP libsfizz      {sfizz_library or ''}"
    )
    if sfizz_worker is not None and sfizz_library is not None:
        try:
            _, _, offline_api = probe_sfizz_runtime(
                worker=sfizz_worker, library=sfizz_library
            )
            print(f"sfizz    OK       offline API v{offline_api}")
        except Exception as exc:
            print(f"sfizz    FAIL     {exc}")

    effect_backends = {
        str(cfg.get("backend", registry.effect_renderer.backend)).strip().lower()
        for cfg in registry.data.get("effects", {}).values()
    }
    if NATIVE_LV2_BACKEND in effect_backends:
        native_lv2 = find_effect_renderer_tool(registry)
        print(
            f"command  {'OK' if native_lv2 else 'MISSING':7s}  "
            f"mrp-lv2-chain  {native_lv2 or ''}"
        )
    if LEGACY_LV2APPLY_BACKEND in effect_backends:
        lv2apply = shutil.which("lv2apply")
        print(f"command  {'OK' if lv2apply else 'MISSING':7s}  lv2apply     {lv2apply or ''}")

    gm = registry.general_midi_fallback
    if gm is not None and gm.enabled:
        libfluidsynth, version = fluidsynth_library_info()
        detail = libfluidsynth or ""
        if version:
            detail = f"{detail} (FluidSynth {version})"
        print(
            f"library  {'OK' if libfluidsynth else 'MISSING':7s}  "
            f"libfluidsynth  {detail}"
        )
    return 0


def _print_track(track) -> None:
    instrument = resolve_instrument(track)
    programs = ",".join(str(x) for x in track.programs) if track.programs else "-"
    channels = ",".join(str(x + 1) for x in track.note_channels) if track.note_channels else "-"
    print(
        f"[{track.index:02d}] {track.name or '<unnamed>':24.24s} "
        f"notes={track.note_count:5d} ch={channels:8s} prog={programs:10s} "
        f"=> {instrument}"
    )
    for warning in (*track.warnings, *resolution_warnings(track)):
        print(f"     WARN: {warning}")


def cmd_inspect(args: argparse.Namespace) -> int:
    _, tracks = analyze_midi(args.midi)
    print(f"MIDI: {args.midi.resolve()}\n")
    for track in tracks:
        if args.all_tracks or track.has_notes:
            _print_track(track)
    return 0


def _midi_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted({*root.rglob("*.mid"), *root.rglob("*.midi")})


def cmd_scan(args: argparse.Namespace) -> int:
    files = _midi_files(args.path)
    if not files:
        raise SystemExit(f"FAIL: no MIDI files found under {args.path}")

    counts: Counter[str] = Counter()
    notes: Counter[str] = Counter()
    warning_count = 0
    track_count = 0

    for path in files:
        _, tracks = analyze_midi(path)
        for track in musical_tracks(tracks):
            instrument = resolve_instrument(track)
            if instrument == "melody" and not args.include_melody:
                continue
            counts[instrument] += 1
            notes[instrument] += track.note_count
            track_count += 1
            warning_count += len(track.warnings) + len(resolution_warnings(track))

    total_notes = sum(notes.values())
    print(f"MIDI files: {len(files)}")
    print(f"Musical tracks: {track_count}")
    print(f"Note-on events: {total_notes}")
    print(f"Track warnings: {warning_count}\n")
    print(f"{'Instrument':28s} {'Tracks':>8s} {'Track %':>8s} {'Notes':>10s} {'Note %':>8s}")
    print("-" * 68)
    for instrument, ntracks in counts.most_common():
        nn = notes[instrument]
        print(
            f"{instrument:28s} {ntracks:8d} {100*ntracks/track_count:7.1f}% "
            f"{nn:10d} {100*nn/total_notes:7.1f}%"
        )
    return 0


def _select_render_tracks(tracks, track_index: int | None):
    musical = musical_tracks(tracks)
    if track_index is None:
        return musical

    for track in musical:
        if track.index == track_index:
            return [track]
    raise SystemExit(f"FAIL: track {track_index} does not exist or has no notes")


def _default_render_output(midi_path: Path, track_index: int | None) -> Path:
    if track_index is None:
        name = f"{midi_path.stem}.wav"
    else:
        name = f"{midi_path.stem}.track-{track_index:02d}.wav"
    return Path("renders/final") / name


def _drum_kick_patch(registry: PatchRegistry) -> Patch | None:
    layer = registry.drum_kick_layer
    if layer is None or not layer.enabled:
        return None
    if not layer.sfz.is_file():
        raise SystemExit(f"FAIL: drum kick layer SFZ not found: {layer.sfz}")
    return Patch(
        name="drum_kick_layer",
        library="<drum_kick_layer>",
        sfz=layer.sfz,
        gain_db=layer.gain_db,
        effects=(),
        enabled=True,
    )


def _safe_cache_component(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in value)


def _performance_cache_suffix(plan: VelocityPlan | None) -> str:
    return f".perf-{plan.cache_tag}" if plan is not None else ""


def _asset_cache_identity(path: Path, *, hash_content: bool = False) -> dict[str, object]:
    """Return an identity for a renderer asset without hashing big libraries.

    The resolved path catches patch/SoundFont swaps. Size and nanosecond mtime
    invalidate the cache when the asset at that path is replaced or edited.
    Small textual SFZ entry points are additionally content-hashed; large
    SoundFonts intentionally use the cheaper stat identity.
    """
    resolved = path.resolve()
    stat = resolved.stat()
    identity: dict[str, object] = {
        "path": str(resolved),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if hash_content:
        identity["sha1"] = sha1(resolved.read_bytes()).hexdigest()
    return identity


def _prepared_midi_cache_identity(path: Path) -> dict[str, object]:
    """Identify the exact MIDI bytes handed to a renderer.

    The prepared MIDI is the authoritative symbolic input to SFZ/FluidSynth.
    Hashing its bytes means any future change to velocity, controller, timing,
    split, note-filter, or program-remap logic invalidates the raw cache without
    requiring a separate transform-version bump.
    """
    data = path.read_bytes()
    return {"size": len(data), "sha256": sha256(data).hexdigest()}


def _render_cache_tag(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha1(encoded.encode("utf-8")).hexdigest()[:12]


def _sfz_render_cache_tag(
    prepared_midi: Path,
    patch: Patch,
    *,
    blocksize: int,
    samplerate: int,
    quality: int,
    polyphony: int,
    renderer_identity: dict[str, object] | None = None,
) -> str:
    identity = renderer_identity or sfizz_renderer_identity()
    return _render_cache_tag(
        {
            "schema": "raw-sfz-v3",
            "prepared_midi": _prepared_midi_cache_identity(prepared_midi),
            "patch": {
                "name": patch.name,
                "library": patch.library,
                "asset": _asset_cache_identity(patch.sfz, hash_content=True),
            },
            "renderer": {
                **identity,
                "blocksize": blocksize,
                "samplerate": samplerate,
                "quality": quality,
                "polyphony": polyphony,
            },
        }
    )

def _gm_render_cache_tag(
    prepared_midi: Path,
    patch: Patch,
    *,
    synth_gain: float,
    samplerate: int,
    render_mode: str = "full-render",
) -> str:
    return _render_cache_tag(
        {
            "schema": "raw-gm-v4",
            "prepared_midi": _prepared_midi_cache_identity(prepared_midi),
            "soundfont": _asset_cache_identity(patch.sfz),
            "renderer": {
                "kind": "libfluidsynth-fastpath",
                "render_mode": render_mode,
                "synth_gain": synth_gain,
                "samplerate": samplerate,
                "cpu_cores": 1,
                "batch_blocksize": 1024,
            },
        }
    )


def _performance_plan_text(instrument: str, plan: VelocityPlan | None) -> str | None:
    if plan is None:
        return None
    if plan.mode == "constant":
        return (
            f"PERF {instrument} constant-like p10={plan.source_low:.1f} "
            f"med={plan.source_median:.1f} p90={plan.source_high:.1f} "
            f"-> nominal={plan.profile.velocity_nominal}"
        )
    detail = f"mode={plan.mode}"
    if plan.mode == "shift":
        detail += f" shift={plan.shift:+.1f}"
    elif plan.mode == "compress":
        detail += f" scale={plan.scale:.3f}x shift={plan.shift:+.1f}"
    return (
        f"PERF {instrument} dynamic p10={plan.source_low:.1f} "
        f"med={plan.source_median:.1f} p90={plan.source_high:.1f} -> "
        f"range={plan.profile.velocity_min}..{plan.profile.velocity_max} {detail}"
    )

def _raw_stem_path(
    stems_dir: Path,
    track_index: int,
    instrument: str,
    route: str,
    patch: Patch,
    render_cache_tag: str,
    gm_program: int | None = None,
    performance_plan: VelocityPlan | None = None,
) -> Path:
    base = (
        f"track-{track_index:02d}.{instrument}"
        f"{_performance_cache_suffix(performance_plan)}.render-{render_cache_tag}"
    )
    if route == "exact":
        return stems_dir / f"{base}.raw.wav"
    if route == "family":
        return stems_dir / f"{base}.family-{_safe_cache_component(patch.name)}.raw.wav"
    if route == "gm":
        if gm_program is None:
            raise ValueError("GM cache path requires gm_program")
        return stems_dir / f"{base}.gm-p{gm_program:03d}.raw.wav"
    raise ValueError(f"unknown render route: {route}")


def _gm_patch(registry: PatchRegistry, program: int) -> Patch:
    gm = registry.general_midi_fallback
    assert gm is not None
    return Patch(
        name=f"gm_fallback_p{program:03d}",
        library="<general_midi_fallback>",
        sfz=gm.soundfont,
        gain_db=gm.gain_db,
        effects=(),
        enabled=True,
    )


def _render_policy(track, resolution, registry: PatchRegistry) -> tuple[str, bool, int | None, bool]:
    """Return (target instrument, force GM, GM override, trust source program).

    Melody is a logical role, so its rendering source is configurable without
    changing normal instrument resolution:

    - auto: use the Melody track's GM Program to choose a dedicated/family patch
      first, then GM;
    - gm: bypass dedicated/family patches and use the configured or source GM
      Program directly;
    - instrument: force one canonical instrument, preferring its dedicated/family
      patch and using that instrument's representative GM Program as fallback.
    """
    if resolution.instrument != "melody":
        return resolution.instrument, False, None, True

    melody = registry.melody
    if melody.mode == "gm":
        return "melody", True, melody.gm_program, melody.gm_program is None
    if melody.mode == "instrument":
        assert melody.instrument is not None
        return melody.instrument, False, None, False
    return melody_render_instrument(track) or "melody", False, None, True


def _render_settings_from_args(
    args: argparse.Namespace, *, active_songs: int = 1, registry: PatchRegistry | None = None
) -> RenderSettings:
    workers = int(getattr(args, "workers", getattr(args, "jobs", 5)))
    sfizz_worker = None if registry is None else registry.tools_root / "mrp-sfizz-worker"
    sfizz_library = None if registry is None else registry.sfizz_library
    return RenderSettings(
        workers=workers,
        sfz_workers=getattr(args, "sfz_workers", None),
        gm_workers=int(getattr(args, "gm_workers", 1)),
        fx_workers=getattr(args, "fx_workers", None),
        mix_workers=int(getattr(args, "mix_workers", 1)),
        blocksize=int(args.blocksize),
        samplerate=int(args.samplerate),
        quality=int(args.quality),
        polyphony=int(args.polyphony),
        include_melody=bool(args.include_melody),
        skip_unconfigured=bool(args.skip_unconfigured),
        keep_work=bool(args.keep_work),
        active_songs=active_songs,
        max_fx_backlog=getattr(args, "max_fx_backlog", None),
        sfz_resident_memory=getattr(args, "sfz_resident_memory", None),
        sfizz_worker=sfizz_worker,
        sfizz_library=sfizz_library,
    ).normalized()


def _batch_run_identity(registry: PatchRegistry, settings: RenderSettings) -> str:
    assets = []
    for name, patch in sorted(registry.patches.items()):
        if patch.sfz.exists():
            stat = patch.sfz.stat()
            assets.append((name, str(patch.sfz.resolve()), stat.st_size, stat.st_mtime_ns))
    gm = registry.general_midi_fallback
    gm_asset = None
    if gm is not None and gm.soundfont.exists():
        stat = gm.soundfont.stat()
        gm_asset = (str(gm.soundfont.resolve()), stat.st_size, stat.st_mtime_ns)
    tool = registry.tools_root / registry.effect_renderer.tool
    tool_asset = None
    if tool.exists():
        stat = tool.stat()
        tool_asset = (str(tool.resolve()), stat.st_size, stat.st_mtime_ns)
    payload = {
        "schema": "render-system-v2",
        "prepared_midi_contract": "artifact-addressed-v1",
        "config_sha1": sha1(registry.config_path.read_bytes()).hexdigest(),
        "assets": assets,
        "gm": gm_asset,
        "fx_tool": tool_asset,
        "sfizz_renderer": sfizz_renderer_identity(
            worker=settings.sfizz_worker, library=settings.sfizz_library
        ),
        "settings": {
            "blocksize": settings.blocksize,
            "samplerate": settings.samplerate,
            "quality": settings.quality,
            "polyphony": settings.polyphony,
            "include_melody": settings.include_melody,
            "skip_unconfigured": settings.skip_unconfigured,
        },
    }
    return sha1(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _build_song_plan(
    midi_path: Path,
    *,
    registry: PatchRegistry,
    output: Path,
    work_dir: Path,
    settings: RenderSettings,
    track_index: int | None = None,
    reuse_raw: bool = False,
    verbose: bool = True,
    run_identity: str = "",
    logger: RenderLogger | None = None,
) -> SongPlan:
    midi_path = midi_path.resolve()
    output = output.resolve()
    work_dir = work_dir.resolve()
    mid, tracks = analyze_midi(midi_path)
    selected_tracks = _select_render_tracks(tracks, track_index)
    split_dir = work_dir / "midi"
    stems_dir = work_dir / "stems"
    work_dir.mkdir(parents=True, exist_ok=True)

    sfz_jobs: list[RenderJob] = []
    gm_jobs: list[FluidSynthJob] = []
    cached: list[RenderedStem] = []
    skipped: list[tuple[int, str, str]] = []
    stem_plans: list[StemPlan] = []

    if logger is not None and logger.verbose:
        logger.plan_detail(f"MIDI {midi_path}")
        if track_index is not None:
            logger.plan_detail(f"track {track_index:02d}")
        logger.plan_detail(f"output {output}")
        logger.plan_detail(f"work {work_dir}")

    for track in selected_tracks:
        resolution = resolve_track(track)
        instrument = resolution.instrument
        if instrument == "melody" and not settings.include_melody:
            if logger is not None and logger.verbose:
                logger.plan_detail(f"SKIP track={track.index:02d} {track.name!r} -> melody")
            continue

        if logger is not None:
            for warning in (*track.warnings, *resolution_warnings(track)):
                logger.warning(f"track {track.index:02d} {track.name!r}: {warning}", song=midi_path)

        render_instrument, force_gm, gm_program_override, trust_source_program = _render_policy(
            track, resolution, registry
        )
        performance_plan = build_velocity_plan(
            mid.tracks[track.index],
            registry.performance_profile(render_instrument),
        )
        if logger is not None and logger.verbose:
            perf_text = _performance_plan_text(render_instrument, performance_plan)
            if perf_text:
                logger.plan_detail(perf_text, track_index=track.index)
        if force_gm:
            patch, route = None, None
        else:
            patch, route = registry.resolve_dedicated(render_instrument)
        gm_program: int | None = None

        if patch is not None and route is not None:
            split = make_split_midi(mid, midi_path, track.index, split_dir, performance_plan)
            render_cache_tag = _sfz_render_cache_tag(
                split,
                patch,
                blocksize=settings.blocksize,
                samplerate=settings.samplerate,
                quality=settings.quality,
                polyphony=settings.polyphony,
                renderer_identity=sfizz_renderer_identity(
                    worker=settings.sfizz_worker, library=settings.sfizz_library
                ),
            )
            stem = _raw_stem_path(
                stems_dir,
                track.index,
                render_instrument,
                route,
                patch,
                render_cache_tag,
                performance_plan=performance_plan,
            )
            if logger is not None and logger.verbose:
                route_label = route if route == "family" else "exact"
                mapping = instrument if instrument == render_instrument else f"{instrument} -> {render_instrument}"
                logger.plan_detail(
                    f"track={track.index:02d} {track.name!r} -> {mapping} -> {patch.name} [{route_label}]"
                )

            stem_plans.append(
                StemPlan(
                    stem_id=make_stem_id(track.index, render_instrument),
                    track_index=track.index,
                    instrument=render_instrument,
                    raw_backend=Backend.SFZ,
                    raw_output=stem,
                    effects=patch.effects,
                )
            )
            if reuse_raw and stem.is_file():
                cached.append(RenderedStem(track, render_instrument, patch, stem, 0.0))
            else:
                sfz_jobs.append(RenderJob(track, render_instrument, patch, split, stem))
        else:
            gm = registry.general_midi_fallback
            reason: str | None = None
            gm_lookup_instrument = render_instrument
            if gm is None or not gm.enabled:
                reason = "no dedicated/family patch and GM fallback is disabled"
            elif not gm.soundfont.is_file():
                reason = f"GM fallback SoundFont missing: {gm.soundfont}"
            else:
                if gm_program_override is not None:
                    gm_program = gm_program_override
                elif trust_source_program and resolution.program_trusted and track.primary_program is not None:
                    gm_program = track.primary_program
                else:
                    gm_program = gm.representative_program(gm_lookup_instrument)
                    if gm_program is None and instrument == "melody":
                        gm_lookup_instrument = "synth_lead"
                        gm_program = gm.representative_program(gm_lookup_instrument)
                    if gm_program is None:
                        reason = (
                            "no trustworthy Program Change and no representative GM program "
                            f"configured for {gm_lookup_instrument}"
                        )

            if reason is not None:
                if settings.skip_unconfigured:
                    if logger is not None and logger.verbose:
                        logger.plan_detail(
                            f"SKIP track={track.index:02d} {track.name!r} -> {instrument} ({reason})"
                        )
                    skipped.append((track.index, instrument, reason))
                    continue
                raise RuntimeError(
                    f"track {track.index} {track.name!r} resolved to {instrument}, but {reason}"
                )

            assert gm is not None and gm_program is not None
            patch = _gm_patch(registry, gm_program)
            preserve_source_program = (
                gm_program_override is None
                and trust_source_program
                and resolution.program_trusted
            )
            if preserve_source_program:
                split = make_split_midi(mid, midi_path, track.index, split_dir, performance_plan)
            else:
                split = make_program_override_midi(
                    mid,
                    midi_path,
                    track.index,
                    split_dir,
                    gm_program,
                    velocity_plan=performance_plan,
                )
            render_mode = "single-native" if track_index is not None else "full-render"
            render_cache_tag = _gm_render_cache_tag(
                split,
                patch,
                synth_gain=gm.synth_gain,
                samplerate=settings.samplerate,
                render_mode=render_mode,
            )
            stem = _raw_stem_path(
                stems_dir,
                track.index,
                render_instrument,
                "gm",
                patch,
                render_cache_tag,
                gm_program,
                performance_plan,
            )
            if logger is not None and logger.verbose:
                if gm_program_override is not None:
                    program_source = "melody-config"
                elif trust_source_program and resolution.program_trusted:
                    program_source = "source"
                else:
                    program_source = "representative"
                mapping = instrument if instrument == render_instrument else f"{instrument} -> {render_instrument}"
                logger.plan_detail(
                    f"track={track.index:02d} {track.name!r} -> {mapping} -> "
                    f"GM program={gm_program:03d} ({program_source}) [gm]"
                )

            stem_plans.append(
                StemPlan(
                    stem_id=make_stem_id(track.index, render_instrument),
                    track_index=track.index,
                    instrument=render_instrument,
                    raw_backend=Backend.FLUIDSYNTH,
                    raw_output=stem,
                    effects=patch.effects,
                )
            )
            if reuse_raw and stem.is_file():
                cached.append(RenderedStem(track, render_instrument, patch, stem, 0.0))
            else:
                gm_jobs.append(
                    FluidSynthJob(
                        track=track,
                        instrument=render_instrument,
                        patch=patch,
                        split_midi=split,
                        soundfont=gm.soundfont,
                        synth_gain=gm.synth_gain,
                        output=stem,
                    )
                )

        if instrument == "drums":
            kick_patch = _drum_kick_patch(registry)
            if kick_patch is not None:
                layer = registry.drum_kick_layer
                assert layer is not None
                kick_midi = make_note_filtered_midi(
                    mid,
                    midi_path,
                    track.index,
                    split_dir,
                    layer.notes,
                    "kick-layer",
                    performance_plan,
                )
                kick_cache_tag = _sfz_render_cache_tag(
                    kick_midi,
                    kick_patch,
                    blocksize=settings.blocksize,
                    samplerate=settings.samplerate,
                    quality=settings.quality,
                    polyphony=settings.polyphony,
                    renderer_identity=sfizz_renderer_identity(
                        worker=settings.sfizz_worker, library=settings.sfizz_library
                    ),
                )
                kick_stem = stems_dir / (
                    f"track-{track.index:02d}.drums.kick-layer"
                    f"{_performance_cache_suffix(performance_plan)}"
                    f".render-{kick_cache_tag}.raw.wav"
                )
                if logger is not None and logger.verbose:
                    logger.plan_detail(
                        f"kick layer track={track.index:02d} notes={','.join(str(x) for x in layer.notes)} "
                        f"gain={layer.gain_db:+.2f} dB -> {kick_patch.sfz.name}"
                    )
                stem_plans.append(
                    StemPlan(
                        stem_id=make_stem_id(track.index, "drums_kick_layer"),
                        track_index=track.index,
                        instrument="drums_kick_layer",
                        raw_backend=Backend.SFZ,
                        raw_output=kick_stem,
                        effects=(),
                    )
                )
                if reuse_raw and kick_stem.is_file():
                    cached.append(RenderedStem(track, "drums_kick_layer", kick_patch, kick_stem, 0.0))
                else:
                    sfz_jobs.append(RenderJob(track, "drums_kick_layer", kick_patch, kick_midi, kick_stem))

    if not sfz_jobs and not gm_jobs and not cached:
        raise RuntimeError("no renderable tracks")

    song_id = make_song_id(midi_path, output, run_identity)
    raw_tasks = []
    for job in sfz_jobs:
        stem_id = make_stem_id(job.track.index, job.instrument)
        raw_tasks.append(make_task(song_id, Stage.RAW, Backend.SFZ, (stem_id,), job))
    if gm_jobs:
        stem_ids = tuple(make_stem_id(job.track.index, job.instrument) for job in gm_jobs)
        raw_tasks.append(
            make_task(song_id, Stage.RAW, Backend.FLUIDSYNTH, stem_ids, tuple(gm_jobs))
        )

    return SongPlan(
        song_id=song_id,
        midi_path=midi_path,
        output=output,
        work_dir=work_dir,
        split_dir=split_dir,
        config_path=registry.config_path,
        settings=settings,
        stems=tuple(stem_plans),
        raw_tasks=tuple(raw_tasks),
        cached_stems=tuple(cached),
        skipped=tuple(skipped),
        track_index=track_index,
        master=registry.master,
    )


def _render_logger(args: argparse.Namespace, *, mode: str) -> RenderLogger:
    verbosity = "debug" if bool(getattr(args, "debug", False)) else (
        "verbose" if bool(getattr(args, "verbose", False)) else "normal"
    )
    return RenderLogger(
        LogOptions(
            mode=mode,
            verbosity=verbosity,
            color=str(getattr(args, "color", "auto")),
            log_file=getattr(args, "log_file", None),
            json_log=getattr(args, "json_log", None),
            heartbeat_seconds=float(getattr(args, "heartbeat", 5.0)),
        )
    )


def cmd_render(args: argparse.Namespace) -> int:
    midi_path = args.midi.resolve()
    registry = _registry(args.config)
    settings = _render_settings_from_args(args, active_songs=1, registry=registry)
    output = args.output.resolve() if args.output else _default_render_output(midi_path, args.track).resolve()
    work_dir = args.work_dir.resolve() if args.work_dir else (Path("renders/work") / midi_path.stem).resolve()
    with _render_logger(args, mode="single") as logger:
        logger.single_header(midi_path, output, track_index=args.track)
        plan = _build_song_plan(
            midi_path,
            registry=registry,
            output=output,
            work_dir=work_dir,
            settings=settings,
            track_index=args.track,
            reuse_raw=args.track is not None and not bool(getattr(args, "rebuild_raw", False)),
            verbose=logger.verbose,
            logger=logger,
        )
        if logger.verbose:
            logger.plan_detail(f"{len(plan.stems)} planned stem{'s' if len(plan.stems) != 1 else ''}")
        with RenderingCoordinator(settings, logger=logger, total_songs=1) as coordinator:
            results = coordinator.run([plan])
        result = results[0]
    return 0 if result.status == "DONE" else 1

def _batch_output_path(root: Path, source_root: Path, midi_path: Path) -> Path:
    if source_root.is_file():
        relative = Path(midi_path.name)
    else:
        relative = midi_path.relative_to(source_root)
    return (root / relative).with_suffix(".wav")


def _batch_work_path(root: Path, source_root: Path, midi_path: Path) -> Path:
    if source_root.is_file():
        relative = Path(midi_path.stem)
    else:
        relative = midi_path.relative_to(source_root).with_suffix("")
    return root / relative


def cmd_batch(args: argparse.Namespace) -> int:
    source_root = args.path.resolve()
    files = _midi_files(source_root)
    if not files:
        raise SystemExit(f"FAIL: no MIDI files found under {source_root}")

    registry = _registry(args.config)
    settings = _render_settings_from_args(args, active_songs=args.active_songs, registry=registry)
    output_root = args.output_dir.resolve()
    work_root = args.work_root.resolve()
    state = StateStore(args.state_db)
    run_identity = _batch_run_identity(registry, settings)
    planning_failures: list[tuple[Path, str]] = []
    skipped_done = 0
    skipped_failed = 0

    with _render_logger(args, mode="batch") as logger:
        logger.batch_header(
            total=len(files),
            active_songs=settings.active_songs,
            workers=settings.workers,
            sfz_workers=int(settings.sfz_workers or 1),
            gm_workers=settings.gm_workers,
            fx_workers=int(settings.fx_workers or 1),
            mix_workers=settings.mix_workers,
            sfz_resident_memory=(
                "auto" if settings.sfz_resident_memory is None
                else format_bytes(settings.sfz_resident_memory)
            ),
            state_db=state.path,
            run_identity=run_identity,
        )

        def plans():
            nonlocal skipped_done, skipped_failed
            for index, midi_path in enumerate(files, start=1):
                output = _batch_output_path(output_root, source_root, midi_path)
                work_dir = _batch_work_path(work_root, source_root, midi_path)
                song_id = make_song_id(midi_path, output, run_identity)
                status = state.song_status(song_id)
                if not args.force and status == "DONE":
                    skipped_done += 1
                    logger.batch_skip("DONE", midi_path)
                    continue
                if not args.force and status == "FAILED" and not args.retry_failed:
                    skipped_failed += 1
                    logger.batch_skip("FAILED", midi_path)
                    continue
                try:
                    if logger.verbose:
                        logger.plan_detail(f"[{index}/{len(files)}] {midi_path}")
                    yield _build_song_plan(
                        midi_path,
                        registry=registry,
                        output=output,
                        work_dir=work_dir,
                        settings=settings,
                        track_index=None,
                        reuse_raw=not bool(args.rebuild_raw),
                        verbose=logger.verbose,
                        run_identity=run_identity,
                        logger=logger,
                    )
                except BaseException as exc:
                    if isinstance(exc, KeyboardInterrupt):
                        raise
                    error = f"{type(exc).__name__}: {exc}"
                    planning_failures.append((midi_path, error))
                    state.record_failure(song_id, midi_path, output, error)
                    logger.failure(song=midi_path, stage="plan", backend=None, message=error)

        try:
            with RenderingCoordinator(
                settings, state=state, logger=logger, total_songs=len(files)
            ) as coordinator:
                results = coordinator.run(plans())
        finally:
            state.close()

        failed = [result for result in results if result.status != "DONE"]
        done = [result for result in results if result.status == "DONE"]
        logger.batch_progress(
            total=len(files),
            done=len(done),
            failed=len(failed) + len(planning_failures),
            active=0,
            pending_raw=0,
            pending_fx=0,
            pending_mix=0,
            inflight=0,
            cache_hits=getattr(coordinator, "cache_hits", 0),
            force=True,
        )
        logger.batch_summary(
            total=len(files),
            completed_now=len(done),
            failed_now=len(failed) + len(planning_failures),
            skipped_done=skipped_done,
            skipped_failed=skipped_failed,
            sfz_stats=coordinator.sfz_stats(),
        )
        for result in failed[:20]:
            logger.failure(song=result.midi_path, stage=None, backend=None, message=result.error or "render failed")
        for path, error in planning_failures[:20]:
            logger.failure(song=path, stage="plan", backend=None, message=error)
        return 1 if failed or planning_failures else 0

def _add_render_engine_args(p: argparse.ArgumentParser, *, batch: bool = False) -> None:
    # --jobs remains a compatibility alias for the global worker budget on the
    # single-file command. Batch uses the clearer --workers spelling.
    if batch:
        p.add_argument("--workers", type=int, default=5, help="global concurrent task budget")
    else:
        p.add_argument("--jobs", type=int, default=5, help="global concurrent task budget")
    p.add_argument("--sfz-workers", type=int, help="maximum simultaneous SFZ tasks")
    p.add_argument(
        "--sfz-resident-memory",
        type=_parse_byte_size,
        default=None,
        metavar="SIZE",
        help="resident SFZ RAM budget (e.g. 12GiB; default: auto)",
    )
    p.add_argument("--gm-workers", type=int, default=1, help="persistent FluidSynth worker processes")
    p.add_argument("--fx-workers", type=int, help="maximum simultaneous FX-chain tasks")
    p.add_argument("--mix-workers", type=int, default=1, help="maximum simultaneous mix/export tasks")
    p.add_argument("--max-fx-backlog", type=int, help="pause new raw work when queued/running FX reaches this count")
    p.add_argument("--blocksize", type=int, default=1024)
    p.add_argument("--samplerate", type=int, default=48_000)
    p.add_argument("--quality", type=int, default=2)
    p.add_argument("--polyphony", type=int, default=256)
    p.add_argument("--include-melody", action="store_true")
    p.add_argument("--skip-unconfigured", action="store_true")
    p.add_argument("--keep-work", action="store_true")
    p.add_argument(
        "--rebuild-raw",
        action="store_true",
        help="ignore existing raw-stem cache files; for batch DONE songs also pass --force",
    )


def _add_logging_args(p: argparse.ArgumentParser, *, batch: bool = False) -> None:
    p.add_argument("--verbose", action="store_true", help="show task/planning details")
    p.add_argument("--debug", action="store_true", help="show captured backend diagnostics")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    p.add_argument("--log-file", type=Path, help="append a plain-text rendering log")
    p.add_argument("--json-log", type=Path, help="append structured JSONL rendering events")
    if batch:
        p.add_argument("--heartbeat", type=float, default=5.0, help="batch dashboard refresh interval in seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="midi-render", description="Midi Render Pipeline")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="patch registry TOML")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="check resource and patch paths")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("inspect", help="inspect one MIDI and resolve track instruments")
    p.add_argument("midi", type=Path)
    p.add_argument("--all-tracks", action="store_true")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("scan", help="scan a MIDI set and summarize required instruments")
    p.add_argument("path", type=Path)
    p.add_argument("--include-melody", action="store_true")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("render", help="render one MIDI file through the rendering coordinator")
    p.add_argument("midi", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--track", type=int, help="render only this MIDI track; reuse its cached raw stem when available")
    p.add_argument("--work-dir", type=Path)
    _add_render_engine_args(p, batch=False)
    _add_logging_args(p, batch=False)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("batch", help="long-running resumable renderer for a MIDI file or directory tree")
    p.add_argument("path", type=Path)
    p.add_argument("--output-dir", type=Path, default=Path("renders/final-batch"))
    p.add_argument("--work-root", type=Path, default=Path("renders/work-batch"))
    p.add_argument("--state-db", type=Path, default=Path("renders/render-state.sqlite3"))
    p.add_argument("--active-songs", type=int, default=32, help="maximum planned songs resident in the scheduler")
    p.add_argument("--retry-failed", action="store_true", help="retry songs recorded as FAILED")
    p.add_argument("--force", action="store_true", help="ignore DONE/FAILED state and re-plan every input")
    _add_render_engine_args(p, batch=True)
    _add_logging_args(p, batch=True)
    p.set_defaults(func=cmd_batch)
    return parser

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for attr, option in (
        ("jobs", "--jobs"),
        ("workers", "--workers"),
        ("sfz_workers", "--sfz-workers"),
        ("gm_workers", "--gm-workers"),
        ("fx_workers", "--fx-workers"),
        ("mix_workers", "--mix-workers"),
        ("active_songs", "--active-songs"),
        ("max_fx_backlog", "--max-fx-backlog"),
    ):
        value = getattr(args, attr, None)
        if value is not None and value < 1:
            parser.error(f"{option} must be >= 1")
    if getattr(args, "track", None) is not None and args.track < 0:
        parser.error("--track must be >= 0")
    if getattr(args, "heartbeat", None) is not None and args.heartbeat <= 0:
        parser.error("--heartbeat must be > 0")
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
