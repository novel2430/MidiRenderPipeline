from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha1
import json
from pathlib import Path
import shutil
import time

from .effects import process_stem_effects
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
from .mixer import MixStem, export_stem, export_submix, mix_stems
from .patches import Patch, PatchRegistry
from .renderer import (
    FluidSynthJob,
    RenderJob,
    RenderedStem,
    find_fluidsynth,
    find_sfizz_render,
    render_fluidsynth_jobs,
    render_jobs,
)


DEFAULT_CONFIG = Path("config/patches.toml")


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
    sfizz = find_sfizz_render()
    print(f"command  {'OK' if sfizz else 'MISSING':7s}  sfizz_render  {sfizz or ''}")

    needs_lv2apply = any(
        str(cfg.get("backend", "lv2apply")).lower() == "lv2apply"
        for cfg in registry.data.get("effects", {}).values()
    )
    if needs_lv2apply:
        lv2apply = shutil.which("lv2apply")
        print(f"command  {'OK' if lv2apply else 'MISSING':7s}  lv2apply     {lv2apply or ''}")

    gm = registry.general_midi_fallback
    if gm is not None and gm.enabled:
        fluidsynth = find_fluidsynth(gm.tool)
        print(
            f"command  {'OK' if fluidsynth else 'MISSING':7s}  "
            f"{gm.tool:12s}  {fluidsynth or ''}"
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


def _midi_cache_identity(path: Path) -> dict[str, object]:
    """Hash the small source MIDI so an edited file cannot reuse an old stem."""
    resolved = path.resolve()
    digest = sha1(resolved.read_bytes()).hexdigest()
    return {"path": str(resolved), "sha1": digest}


def _render_cache_tag(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha1(encoded.encode("utf-8")).hexdigest()[:12]


def _sfz_render_cache_tag(
    midi_path: Path,
    patch: Patch,
    *,
    blocksize: int,
    samplerate: int,
    quality: int,
    polyphony: int,
    midi_transform: dict[str, object] | None = None,
) -> str:
    return _render_cache_tag(
        {
            "schema": "raw-sfz-v1",
            "midi": _midi_cache_identity(midi_path),
            "patch": {
                "name": patch.name,
                "library": patch.library,
                "asset": _asset_cache_identity(patch.sfz, hash_content=True),
            },
            "midi_transform": midi_transform,
            "renderer": {
                "kind": "sfizz_render",
                "blocksize": blocksize,
                "samplerate": samplerate,
                "quality": quality,
                "polyphony": polyphony,
            },
        }
    )


def _gm_render_cache_tag(
    midi_path: Path,
    patch: Patch,
    *,
    tool: str,
    synth_gain: float,
    samplerate: int,
) -> str:
    return _render_cache_tag(
        {
            "schema": "raw-gm-v1",
            "midi": _midi_cache_identity(midi_path),
            "soundfont": _asset_cache_identity(patch.sfz),
            "renderer": {
                "kind": "fluidsynth",
                "tool": tool,
                "synth_gain": synth_gain,
                "samplerate": samplerate,
            },
        }
    )


def _print_performance_plan(track, instrument: str, plan: VelocityPlan | None) -> None:
    if plan is None:
        return
    if plan.mode == "constant":
        print(
            f"    PERF {instrument:24s} constant-like "
            f"p10={plan.source_low:.1f} med={plan.source_median:.1f} "
            f"p90={plan.source_high:.1f} -> nominal={plan.profile.velocity_nominal}"
        )
        return
    detail = f"mode={plan.mode}"
    if plan.mode == "shift":
        detail += f" shift={plan.shift:+.1f}"
    elif plan.mode == "compress":
        detail += f" scale={plan.scale:.3f}x shift={plan.shift:+.1f}"
    print(
        f"    PERF {instrument:24s} dynamic "
        f"p10={plan.source_low:.1f} med={plan.source_median:.1f} "
        f"p90={plan.source_high:.1f} -> "
        f"range={plan.profile.velocity_min}..{plan.profile.velocity_max} "
        f"{detail}"
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


def cmd_render(args: argparse.Namespace) -> int:
    midi_path = args.midi.resolve()
    registry = _registry(args.config)
    mid, tracks = analyze_midi(midi_path)
    selected_tracks = _select_render_tracks(tracks, args.track)

    output = (
        args.output.resolve()
        if args.output
        else _default_render_output(midi_path, args.track).resolve()
    )
    work_dir = args.work_dir.resolve() if args.work_dir else (Path("renders/work") / midi_path.stem).resolve()
    split_dir = work_dir / "midi"
    stems_dir = work_dir / "stems"
    work_dir.mkdir(parents=True, exist_ok=True)

    sfz_jobs: list[RenderJob] = []
    gm_jobs: list[FluidSynthJob] = []
    cached: list[RenderedStem] = []
    skipped: list[tuple[int, str, str]] = []

    print(f"MIDI:   {midi_path}")
    if args.track is not None:
        print(f"Track:  {args.track:02d}")
    print(f"Output: {output}")
    print(f"Work:   {work_dir}\n")
    print("Instrument mapping:")

    for track in selected_tracks:
        resolution = resolve_track(track)
        instrument = resolution.instrument
        if instrument == "melody" and not args.include_melody:
            print(f"  SKIP track={track.index:02d} {track.name!r} -> melody")
            continue

        for warning in (*track.warnings, *resolution_warnings(track)):
            print(f"  WARN track={track.index:02d} {track.name!r}: {warning}")

        render_instrument, force_gm, gm_program_override, trust_source_program = _render_policy(
            track, resolution, registry
        )
        performance_plan = build_velocity_plan(
            mid.tracks[track.index],
            registry.performance_profile(render_instrument),
        )
        _print_performance_plan(track, render_instrument, performance_plan)
        if force_gm:
            patch, route = None, None
        else:
            patch, route = registry.resolve_dedicated(render_instrument)
        gm_program: int | None = None
        split: Path | None = None

        if patch is not None and route is not None:
            render_cache_tag = _sfz_render_cache_tag(
                midi_path,
                patch,
                blocksize=args.blocksize,
                samplerate=args.samplerate,
                quality=args.quality,
                polyphony=args.polyphony,
            )
            stem = _raw_stem_path(
                stems_dir, track.index, render_instrument, route, patch,
                render_cache_tag,
                performance_plan=performance_plan,
            )
            route_label = route if route == "family" else "exact"
            if instrument == render_instrument:
                mapping = f"{instrument:28s}"
            else:
                mapping = f"{instrument:12s} -> {render_instrument:13s}"
            print(
                f"  track={track.index:02d} {track.name!r:24s} -> {mapping} "
                f"-> {patch.name} [{route_label}]"
            )

            if args.track is not None and stem.is_file():
                cached.append(
                    RenderedStem(
                        track=track,
                        instrument=render_instrument,
                        patch=patch,
                        path=stem,
                        render_seconds=0.0,
                    )
                )
                print(f"  REUSE raw track={track.index:02d} {stem}")
            else:
                split = make_split_midi(
                    mid, midi_path, track.index, split_dir, performance_plan
                )
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
                elif (
                    trust_source_program
                    and resolution.program_trusted
                    and track.primary_program is not None
                ):
                    gm_program = track.primary_program
                else:
                    gm_program = gm.representative_program(gm_lookup_instrument)
                    if gm_program is None and instrument == "melody":
                        gm_lookup_instrument = "synth_lead"
                        gm_program = gm.representative_program(gm_lookup_instrument)
                    if gm_program is None:
                        reason = (
                            f"no trustworthy Program Change and no representative GM program "
                            f"configured for {gm_lookup_instrument}"
                        )

            if reason is not None:
                if args.skip_unconfigured:
                    print(f"  SKIP track={track.index:02d} {track.name!r} -> {instrument} ({reason})")
                    skipped.append((track.index, instrument, reason))
                    continue
                raise SystemExit(
                    f"FAIL: track {track.index} {track.name!r} resolved to {instrument}, but {reason}. "
                    "Run `midi-render doctor` or use --skip-unconfigured for exploratory renders."
                )

            assert gm is not None and gm_program is not None
            patch = _gm_patch(registry, gm_program)
            route = "gm"
            render_cache_tag = _gm_render_cache_tag(
                midi_path,
                patch,
                tool=gm.tool,
                synth_gain=gm.synth_gain,
                samplerate=args.samplerate,
            )
            stem = _raw_stem_path(
                stems_dir,
                track.index,
                render_instrument,
                route,
                patch,
                render_cache_tag,
                gm_program,
                performance_plan,
            )
            if gm_program_override is not None:
                program_source = "melody-config"
            elif trust_source_program and resolution.program_trusted:
                program_source = "source"
            else:
                program_source = "representative"
            if instrument == render_instrument:
                mapping = f"{instrument:28s}"
            else:
                mapping = f"{instrument:12s} -> {render_instrument:13s}"
            print(
                f"  track={track.index:02d} {track.name!r:24s} -> {mapping} "
                f"-> GM program={gm_program:03d} ({program_source}) [gm]"
            )

            if args.track is not None and stem.is_file():
                cached.append(
                    RenderedStem(
                        track=track,
                        instrument=render_instrument,
                        patch=patch,
                        path=stem,
                        render_seconds=0.0,
                    )
                )
                print(f"  REUSE raw track={track.index:02d} {stem}")
            else:
                preserve_source_program = (
                    gm_program_override is None
                    and trust_source_program
                    and resolution.program_trusted
                )
                if preserve_source_program:
                    split = make_split_midi(
                        mid, midi_path, track.index, split_dir, performance_plan
                    )
                else:
                    split = make_program_override_midi(
                        mid,
                        midi_path,
                        track.index,
                        split_dir,
                        gm_program,
                        velocity_plan=performance_plan,
                    )
                gm_jobs.append(
                    FluidSynthJob(
                        track=track,
                        instrument=render_instrument,
                        patch=patch,
                        split_midi=split,
                        soundfont=gm.soundfont,
                        tool=gm.tool,
                        synth_gain=gm.synth_gain,
                        output=stem,
                    )
                )

        if instrument == "drums":
            kick_patch = _drum_kick_patch(registry)
            if kick_patch is not None:
                layer = registry.drum_kick_layer
                assert layer is not None
                kick_cache_tag = _sfz_render_cache_tag(
                    midi_path,
                    kick_patch,
                    blocksize=args.blocksize,
                    samplerate=args.samplerate,
                    quality=args.quality,
                    polyphony=args.polyphony,
                    midi_transform={"kind": "note-filter", "notes": list(layer.notes)},
                )
                kick_stem = stems_dir / (
                    f"track-{track.index:02d}.drums.kick-layer"
                    f"{_performance_cache_suffix(performance_plan)}"
                    f".render-{kick_cache_tag}.raw.wav"
                )
                print(
                    f"    + kick layer notes={','.join(str(x) for x in layer.notes)} "
                    f"gain={layer.gain_db:+.2f} dB -> {kick_patch.sfz.name}"
                )
                if args.track is not None and kick_stem.is_file():
                    cached.append(
                        RenderedStem(
                            track=track,
                            instrument="drums_kick_layer",
                            patch=kick_patch,
                            path=kick_stem,
                            render_seconds=0.0,
                        )
                    )
                    print(f"  REUSE raw kick layer track={track.index:02d} {kick_stem}")
                else:
                    kick_midi = make_note_filtered_midi(
                        mid,
                        midi_path,
                        track.index,
                        split_dir,
                        layer.notes,
                        "kick-layer",
                        performance_plan,
                    )
                    sfz_jobs.append(
                        RenderJob(
                            track,
                            "drums_kick_layer",
                            kick_patch,
                            kick_midi,
                            kick_stem,
                        )
                    )

    if not sfz_jobs and not gm_jobs and not cached:
        raise SystemExit("FAIL: no renderable tracks")

    rendered: list[RenderedStem] = list(cached)
    if sfz_jobs:
        print(f"\nRaw SFZ render: {len(sfz_jobs)} job(s), workers={args.jobs}")
        t0 = time.perf_counter()
        rendered.extend(
            render_jobs(
                sfz_jobs,
                workers=args.jobs,
                blocksize=args.blocksize,
                samplerate=args.samplerate,
                quality=args.quality,
                polyphony=args.polyphony,
            )
        )
        print(f"Raw SFZ wall time: {time.perf_counter() - t0:.2f}s")
    else:
        print("\nRaw SFZ render: 0 job(s)")

    if gm_jobs:
        print(f"\nRaw GM render:  {len(gm_jobs)} job(s), workers={args.jobs}")
        t0 = time.perf_counter()
        rendered.extend(
            render_fluidsynth_jobs(
                gm_jobs,
                workers=args.jobs,
                samplerate=args.samplerate,
            )
        )
        print(f"Raw GM wall time:  {time.perf_counter() - t0:.2f}s")
    else:
        print("Raw GM render:  0 job(s)")

    if cached:
        print(f"Cached raw:     {len(cached)} stem(s) reused")

    rendered.sort(key=lambda x: (x.track.index, x.instrument))
    mix_inputs: list[MixStem] = []
    for stem in rendered:
        final_stem = process_stem_effects(stem, registry, work_dir)
        mix_inputs.append(
            MixStem(
                name=f"track-{stem.track.index:02d} {stem.instrument}",
                path=final_stem,
                gain_db=stem.patch.gain_db,
            )
        )

    if args.track is not None:
        # A selected-track render is an audition/export, not a one-stem final
        # mix. Avoid peak normalization so tone/level changes remain audible.
        if len(mix_inputs) == 1:
            stats = export_stem(mix_inputs[0], output)
        else:
            stats = export_submix(mix_inputs, output)
        print("\nTrack render:")
        for component in mix_inputs:
            print(f"  component:          {component.name:28s} {component.gain_db:+.2f} dB")
        print(f"  peak:               {stats['peak']:.4f}")
        print(f"  duration:           {stats['duration_seconds']:.2f}s")
        print(f"  PASS:               {output}")
        if cached:
            print("  NOTE:               reused cached raw stem(s); current config was reapplied")
    else:
        stats = mix_stems(
            mix_inputs,
            output,
            normalize_peak_db=registry.master.normalize_peak_db,
            master_gain_db=registry.master.gain_db,
        )
        print("\nFinal:")
        print(f"  pre-normalize peak: {stats['pre_peak']:.4f}")
        print(f"  normalize target:   {stats['normalize_peak_db']:+.2f} dBFS")
        print(f"  normalize gain:     {stats['normalize_gain']:.4f}")
        print(f"  master gain:        {stats['master_gain_db']:+.2f} dB  x{stats['master_gain']:.4f}")
        print(f"  final peak:         {stats['final_peak']:.4f}")
        print(f"  RMS:                {stats['rms']:.4f}")
        print(f"  duration:           {stats['duration_seconds']:.2f}s")
        print(f"  PASS:               {output}")
        if skipped:
            print(f"  NOTE:               skipped {len(skipped)} unconfigured track(s)")

    if not args.keep_work:
        shutil.rmtree(split_dir, ignore_errors=True)
    return 0


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

    p = sub.add_parser("render", help="render one MIDI file")
    p.add_argument("midi", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--track", type=int, help="render only this MIDI track; reuse its cached raw SFZ stem when available")
    p.add_argument("--work-dir", type=Path)
    p.add_argument("--jobs", type=int, default=5)
    p.add_argument("--blocksize", type=int, default=1024)
    p.add_argument("--samplerate", type=int, default=48_000)
    p.add_argument("--quality", type=int, default=2)
    p.add_argument("--polyphony", type=int, default=256)
    p.add_argument("--include-melody", action="store_true")
    p.add_argument("--skip-unconfigured", action="store_true")
    p.add_argument("--keep-work", action="store_true")
    p.set_defaults(func=cmd_render)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "jobs", 1) < 1:
        parser.error("--jobs must be >= 1")
    if getattr(args, "track", None) is not None and args.track < 0:
        parser.error("--track must be >= 0")
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
