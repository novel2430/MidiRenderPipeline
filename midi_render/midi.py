from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import mido

from .patches import PerformanceProfile


@dataclass(frozen=True)
class ProgramUsage:
    program: int
    note_count: int
    duration_ticks: int
    first_note_tick: int


@dataclass(frozen=True)
class TrackInfo:
    index: int
    name: str
    note_count: int
    note_channels: tuple[int, ...]
    programs: tuple[int, ...]
    program_by_channel: tuple[tuple[int, int], ...]
    # Programs that were actually active when sounding Note On events occurred.
    # This deliberately excludes harmless setup/overwritten Program Change
    # messages before the first note, which are common in real-world MIDI files.
    program_usage: tuple[ProgramUsage, ...] = ()
    # When a physical MIDI track carries notes for multiple MIDI channels, MRP
    # exposes one logical stem per channel. `index` remains the source/physical
    # track index so legacy --track semantics stay intact; logical_channel makes
    # the stem identity unique without changing well-formed Type-1/Jay inputs.
    logical_channel: int | None = None

    @property
    def primary_program(self) -> int | None:
        if len(self.note_channels) == 1 and len(self.program_usage) == 1:
            return self.program_usage[0].program
        # Keep TrackInfo useful for tests/callers that construct it directly
        # without the analyzer's richer sounding-program metadata.
        if not self.program_usage and len(self.programs) == 1:
            return self.programs[0]
        return None

    @property
    def has_dynamic_program(self) -> bool:
        # Program Change does not select the GM drum kit on channel 10, so even
        # noisy/redundant program messages there must not disable dedicated drums.
        return (
            self.note_channels != (9,)
            and len(self.note_channels) == 1
            and len(self.program_usage) > 1
        )

    @property
    def effective_programs(self) -> tuple[int, ...]:
        if self.program_usage:
            return tuple(usage.program for usage in self.program_usage)
        return self.programs

    def usage_for_program(self, program: int) -> ProgramUsage | None:
        for usage in self.program_usage:
            if usage.program == program:
                return usage
        return None

    def program_share(self, program: int) -> float | None:
        selected = self.usage_for_program(program)
        if selected is None:
            return 1.0 if not self.program_usage and self.primary_program == program else None
        duration_total = sum(usage.duration_ticks for usage in self.program_usage)
        if duration_total > 0:
            return selected.duration_ticks / duration_total
        note_total = sum(usage.note_count for usage in self.program_usage)
        return selected.note_count / note_total if note_total > 0 else None

    @property
    def stem_channel(self) -> int | None:
        return self.logical_channel

    @property
    def stem_label(self) -> str:
        if self.logical_channel is None:
            return f"track {self.index:02d}"
        return f"track {self.index:02d}/ch {self.logical_channel + 1:02d}"

    @property
    def has_notes(self) -> bool:
        return self.note_count > 0

    @property
    def is_channel_10_drum_hint(self) -> bool:
        return bool(self.note_channels) and set(self.note_channels) == {9}

    @property
    def warnings(self) -> tuple[str, ...]:
        warnings: list[str] = []
        if len(self.note_channels) > 1:
            warnings.append(
                "notes use multiple MIDI channels: "
                + ", ".join(str(x + 1) for x in self.note_channels)
            )
        return tuple(warnings)


@dataclass(frozen=True)
class VelocityPlan:
    profile: PerformanceProfile
    mode: str
    note_count: int
    source_low: float
    source_median: float
    source_high: float
    scale: float
    shift: float

    @property
    def source_spread(self) -> float:
        return self.source_high - self.source_low

    @property
    def cache_tag(self) -> str:
        return self.profile.cache_tag()

    def map_velocity(self, velocity: int) -> int:
        if velocity <= 0:
            return velocity
        if self.mode == "constant":
            return self.profile.velocity_nominal
        if self.mode == "identity":
            return velocity

        mapped = velocity * self.scale + self.shift
        return max(
            self.profile.velocity_min,
            min(self.profile.velocity_max, int(round(mapped))),
        )


def _percentile(values: list[int], quantile: float) -> float:
    if not values:
        raise ValueError("cannot compute percentile of empty values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def build_velocity_plan(
    track: mido.MidiTrack,
    profile: PerformanceProfile | None,
    channel: int | None = None,
) -> VelocityPlan | None:
    if profile is None:
        return None

    velocities = [
        msg.velocity
        for msg in track
        if msg.type == "note_on"
        and msg.velocity > 0
        and (channel is None or msg.channel == channel)
    ]
    if not velocities:
        return None

    low = _percentile(velocities, profile.low_percentile)
    median = _percentile(velocities, 0.50)
    high = _percentile(velocities, profile.high_percentile)
    spread = high - low

    if spread <= profile.constant_spread_max:
        return VelocityPlan(
            profile=profile,
            mode="constant",
            note_count=len(velocities),
            source_low=low,
            source_median=median,
            source_high=high,
            scale=0.0,
            shift=0.0,
        )

    target_low = float(profile.velocity_min)
    target_high = float(profile.velocity_max)
    target_span = target_high - target_low

    # Minimal intervention for genuinely dynamic tracks:
    #   1. preserve an already-safe robust range exactly;
    #   2. otherwise shift the contour intact if it fits;
    #   3. only compress when the source robust range is wider than the target.
    # Dynamic velocity is never expanded.
    if target_low <= low and high <= target_high:
        mode = "identity"
        scale = 1.0
        shift = 0.0
    elif spread <= target_span:
        mode = "shift"
        scale = 1.0
        if low < target_low:
            shift = target_low - low
        else:
            shift = target_high - high
    else:
        mode = "compress"
        scale = target_span / spread
        shift = target_low - low * scale

    return VelocityPlan(
        profile=profile,
        mode=mode,
        note_count=len(velocities),
        source_low=low,
        source_median=median,
        source_high=high,
        scale=scale,
        shift=shift,
    )


def _copy_with_velocity(msg: mido.Message | mido.MetaMessage, plan: VelocityPlan | None):
    if plan is not None and msg.type == "note_on" and msg.velocity > 0:
        return msg.copy(velocity=plan.map_velocity(msg.velocity))
    return msg.copy()


def clone_track_with_velocity_plan(
    track: mido.MidiTrack,
    plan: VelocityPlan | None,
    *,
    strip_program_changes: bool = False,
) -> mido.MidiTrack:
    out = mido.MidiTrack()
    pending_time = 0
    for msg in track:
        if strip_program_changes and msg.type == "program_change":
            pending_time += msg.time
            continue
        copied = _copy_with_velocity(msg, plan)
        out.append(copied.copy(time=msg.time + pending_time))
        pending_time = 0
    return out


def get_track_name(track: mido.MidiTrack) -> str:
    for msg in track:
        if msg.type == "track_name":
            return msg.name.strip()
    return ""


def _program_usage_by_channel(track: mido.MidiTrack) -> dict[int, tuple[ProgramUsage, ...]]:
    """Measure the GM programs that actually sound on each MIDI channel.

    Program Change is channel state, and many exported/internet MIDI files emit
    one or more setup programs that are overwritten before any note sounds. A
    renderer should not treat those as real dynamic timbre changes. We therefore
    attribute each Note On to the program active at that instant (GM defaults to
    program 0), then use paired note duration as the primary usage signal.
    """
    current_program = {channel: 0 for channel in range(16)}
    absolute_tick = 0
    # Repeated note-ons of the same pitch before a note-off are legal enough in
    # wild MIDI that a small FIFO is safer than storing only one active note.
    active: dict[tuple[int, int], list[tuple[int, int]]] = {}
    counts: dict[tuple[int, int], int] = {}
    durations: dict[tuple[int, int], int] = {}
    first_ticks: dict[tuple[int, int], int] = {}

    for msg in track:
        absolute_tick += msg.time
        if not hasattr(msg, "channel"):
            continue
        channel = msg.channel
        if msg.type == "program_change":
            current_program[channel] = msg.program
            continue

        is_note_on = msg.type == "note_on" and msg.velocity > 0
        is_note_off = msg.type == "note_off" or (
            msg.type == "note_on" and msg.velocity == 0
        )
        if is_note_on:
            program = current_program[channel]
            key = (channel, program)
            counts[key] = counts.get(key, 0) + 1
            first_ticks.setdefault(key, absolute_tick)
            active.setdefault((channel, msg.note), []).append((program, absolute_tick))
        elif is_note_off:
            stack = active.get((channel, msg.note))
            if not stack:
                continue
            program, start_tick = stack.pop(0)
            key = (channel, program)
            durations[key] = durations.get(key, 0) + max(0, absolute_tick - start_tick)
            if not stack:
                active.pop((channel, msg.note), None)

    # Malformed files sometimes omit final note-offs. Count the remaining note
    # duration up to the physical track end rather than throwing the evidence
    # away entirely.
    for (channel, _note), stack in active.items():
        for program, start_tick in stack:
            key = (channel, program)
            durations[key] = durations.get(key, 0) + max(0, absolute_tick - start_tick)

    grouped: dict[int, list[ProgramUsage]] = {}
    for (channel, program), note_count in counts.items():
        grouped.setdefault(channel, []).append(
            ProgramUsage(
                program=program,
                note_count=note_count,
                duration_ticks=durations.get((channel, program), 0),
                first_note_tick=first_ticks[(channel, program)],
            )
        )
    return {
        channel: tuple(sorted(usages, key=lambda usage: usage.program))
        for channel, usages in grouped.items()
    }


def analyze_track(index: int, track: mido.MidiTrack) -> TrackInfo:
    note_channels: set[int] = set()
    programs: set[int] = set()
    program_by_channel: dict[int, int] = {}
    note_count = 0
    usage_by_channel = _program_usage_by_channel(track)

    for msg in track:
        if msg.type == "program_change":
            programs.add(msg.program)
            program_by_channel[msg.channel] = msg.program
        elif msg.type == "note_on" and msg.velocity > 0:
            note_count += 1
            note_channels.add(msg.channel)

    return TrackInfo(
        index=index,
        name=get_track_name(track),
        note_count=note_count,
        note_channels=tuple(sorted(note_channels)),
        programs=tuple(sorted(programs)),
        program_by_channel=tuple(sorted(program_by_channel.items())),
        program_usage=tuple(
            usage
            for channel in sorted(usage_by_channel)
            for usage in usage_by_channel[channel]
        ),
    )


def analyze_midi(path: Path) -> tuple[mido.MidiFile, list[TrackInfo]]:
    mid = mido.MidiFile(path)
    tracks = [analyze_track(i, track) for i, track in enumerate(mid.tracks)]
    return mid, tracks


def _analyze_track_channel(
    index: int,
    track: mido.MidiTrack,
    channel: int,
    usage_by_channel: dict[int, tuple[ProgramUsage, ...]] | None = None,
) -> TrackInfo:
    programs: set[int] = set()
    last_program: int | None = None
    note_count = 0
    if usage_by_channel is None:
        usage_by_channel = _program_usage_by_channel(track)
    for msg in track:
        if not hasattr(msg, "channel") or msg.channel != channel:
            continue
        if msg.type == "program_change":
            programs.add(msg.program)
            last_program = msg.program
        elif msg.type == "note_on" and msg.velocity > 0:
            note_count += 1

    program_by_channel = () if last_program is None else ((channel, last_program),)
    return TrackInfo(
        index=index,
        name=get_track_name(track),
        note_count=note_count,
        note_channels=(channel,),
        programs=tuple(sorted(programs)),
        program_by_channel=program_by_channel,
        program_usage=usage_by_channel.get(channel, ()),
        logical_channel=channel,
    )


def logical_tracks(mid: mido.MidiFile, tracks: Iterable[TrackInfo]) -> list[TrackInfo]:
    """Expand multi-channel physical tracks into renderable logical stems.

    Conventional Type-1 files with one instrument/channel per physical track
    are returned unchanged, preserving the exact legacy/Jay behavior. A track
    that actually carries note events on multiple MIDI channels is exposed as
    one TrackInfo per note-bearing channel, while retaining the source track
    index for diagnostics and --track selection.
    """
    out: list[TrackInfo] = []
    for info in tracks:
        if len(info.note_channels) <= 1:
            out.append(info)
            continue
        source_track = mid.tracks[info.index]
        usage_by_channel = _program_usage_by_channel(source_track)
        out.extend(
            _analyze_track_channel(
                info.index,
                source_track,
                channel,
                usage_by_channel,
            )
            for channel in info.note_channels
        )
    return out


def midi_timeline_metrics(mid: mido.MidiFile) -> tuple[float, float]:
    """Return source MIDI duration in seconds and fractional musical bars.

    Bars are integrated over the global time-signature timeline, so changing
    meter is handled without coupling the structural metric to tempo. The
    duration follows the global tempo map. These are normalization metrics for
    renderer throughput; they deliberately describe the input MIDI timeline,
    not backend-specific audio tails.
    """
    if mid.ticks_per_beat <= 0:
        return 0.0, 0.0

    tempo = 500_000
    numerator = 4
    denominator = 4
    seconds = 0.0
    bars = 0.0

    for msg in mido.merge_tracks(mid.tracks):
        delta_ticks = int(msg.time)
        if delta_ticks:
            seconds += mido.tick2second(delta_ticks, mid.ticks_per_beat, tempo)
            ticks_per_bar = mid.ticks_per_beat * numerator * 4.0 / denominator
            if ticks_per_bar > 0:
                bars += delta_ticks / ticks_per_bar
        if msg.type == "set_tempo":
            tempo = msg.tempo
        elif msg.type == "time_signature":
            numerator = msg.numerator
            denominator = msg.denominator

    return seconds, bars


def musical_tracks(tracks: Iterable[TrackInfo]) -> list[TrackInfo]:
    return [track for track in tracks if track.has_notes]


def clone_track(track: mido.MidiTrack) -> mido.MidiTrack:
    out = mido.MidiTrack()
    for msg in track:
        out.append(msg.copy())
    return out


def clone_conductor_track(track: mido.MidiTrack) -> mido.MidiTrack:
    """Clone global/meta events from a would-be conductor track only.

    Most Type-1 MIDI files use track 0 purely as a conductor, so this is byte-
    semantically equivalent for normal inputs. If a real-world file also puts
    channel messages in track 0, those notes/controllers must not leak into
    every split stem rendered from another physical track.
    """
    out = mido.MidiTrack()
    pending_time = 0
    for msg in track:
        if hasattr(msg, "channel"):
            pending_time += msg.time
            continue
        out.append(msg.copy(time=msg.time + pending_time))
        pending_time = 0
    return out


def clone_track_for_channel(
    track: mido.MidiTrack,
    channel: int,
    velocity_plan: VelocityPlan | None = None,
    *,
    strip_program_changes: bool = False,
) -> mido.MidiTrack:
    """Clone one logical MIDI channel while preserving absolute event times.

    Meta/system events are retained because tempo, meter and other global
    timeline information may live in the same physical track (notably Type-0
    files). Channel messages for other channels are removed and their delta
    time is carried forward to the next retained event.
    """
    out = mido.MidiTrack()
    pending_time = 0
    for msg in track:
        keep = not hasattr(msg, "channel") or msg.channel == channel
        if keep and strip_program_changes and msg.type == "program_change":
            keep = False
        if not keep:
            pending_time += msg.time
            continue
        copied = _copy_with_velocity(msg, velocity_plan)
        out.append(copied.copy(time=msg.time + pending_time))
        pending_time = 0
    return out


def make_split_midi(
    source: mido.MidiFile,
    source_path: Path,
    track_index: int,
    split_dir: Path,
    velocity_plan: VelocityPlan | None = None,
    channel: int | None = None,
    strip_program_changes: bool = False,
) -> Path:
    """Keep conductor/meta track 0 plus exactly one logical musical stem."""
    split_dir.mkdir(parents=True, exist_ok=True)
    out = mido.MidiFile(type=1, ticks_per_beat=source.ticks_per_beat)

    if len(source.tracks) > 0 and track_index != 0:
        out.tracks.append(clone_conductor_track(source.tracks[0]))
    if channel is None:
        musical = clone_track_with_velocity_plan(
            source.tracks[track_index],
            velocity_plan,
            strip_program_changes=strip_program_changes,
        )
    else:
        musical = clone_track_for_channel(
            source.tracks[track_index],
            channel,
            velocity_plan,
            strip_program_changes=strip_program_changes,
        )
    out.tracks.append(musical)

    safe = source_path.stem.replace("/", "_")
    channel_suffix = "" if channel is None else f".ch-{channel + 1:02d}"
    output = split_dir / f"{safe}.track-{track_index:02d}{channel_suffix}.mid"
    out.save(output)
    return output

def clone_track_with_notes(
    track: mido.MidiTrack,
    notes: Iterable[int],
    velocity_plan: VelocityPlan | None = None,
) -> mido.MidiTrack:
    """Clone a track while keeping only note events for the selected keys.

    Non-note events are preserved. Delta time from removed note events is carried
    forward so the retained notes stay at their original absolute times.
    """
    allowed = {int(note) for note in notes}
    out = mido.MidiTrack()
    pending_time = 0

    for msg in track:
        is_note = msg.type in {"note_on", "note_off"}
        if is_note and msg.note not in allowed:
            pending_time += msg.time
            continue

        copied = _copy_with_velocity(msg, velocity_plan)
        out.append(copied.copy(time=msg.time + pending_time))
        pending_time = 0

    return out


def make_note_filtered_midi(
    source: mido.MidiFile,
    source_path: Path,
    track_index: int,
    split_dir: Path,
    notes: Iterable[int],
    suffix: str,
    velocity_plan: VelocityPlan | None = None,
    channel: int | None = None,
) -> Path:
    """Keep conductor/meta track 0 plus selected notes from one musical track."""
    split_dir.mkdir(parents=True, exist_ok=True)
    out = mido.MidiFile(type=1, ticks_per_beat=source.ticks_per_beat)

    if len(source.tracks) > 0 and track_index != 0:
        out.tracks.append(clone_conductor_track(source.tracks[0]))
    source_track = source.tracks[track_index]
    if channel is not None:
        source_track = clone_track_for_channel(source_track, channel)
    out.tracks.append(clone_track_with_notes(source_track, notes, velocity_plan))

    safe = source_path.stem.replace("/", "_")
    channel_suffix = "" if channel is None else f".ch-{channel + 1:02d}"
    output = split_dir / f"{safe}.track-{track_index:02d}{channel_suffix}.{suffix}.mid"
    out.save(output)
    return output

def clone_track_with_program(
    track: mido.MidiTrack,
    program: int,
    velocity_plan: VelocityPlan | None = None,
) -> mido.MidiTrack:
    """Clone a musical track while forcing one GM program on every note channel.

    A program change is injected at time zero for each note-bearing channel and
    later program changes on those channels are rewritten to the same program.
    This is used only when the resolver rejected/missed the source Program Change
    and the GM fallback needs a safe representative timbre.
    """
    program = int(program)
    if program < 0 or program > 127:
        raise ValueError("program must be 0..127")

    note_channels = sorted(
        {msg.channel for msg in track if msg.type in {"note_on", "note_off"}}
    )
    if not note_channels:
        note_channels = [0]
    target_channels = set(note_channels)

    out = mido.MidiTrack()
    for channel in note_channels:
        # Force General MIDI bank 0 as well as the representative program; a
        # stale bank-select CC paired with a corrected Program Change can still
        # select the wrong SoundFont bank.
        out.append(mido.Message("control_change", channel=channel, control=0, value=0, time=0))
        out.append(mido.Message("control_change", channel=channel, control=32, value=0, time=0))
        out.append(mido.Message("program_change", channel=channel, program=program, time=0))

    for msg in track:
        if msg.type == "program_change" and msg.channel in target_channels:
            out.append(msg.copy(program=program))
        elif (
            msg.type == "control_change"
            and msg.channel in target_channels
            and msg.control in {0, 32}
        ):
            out.append(msg.copy(value=0))
        else:
            out.append(_copy_with_velocity(msg, velocity_plan))
    return out


def make_program_override_midi(
    source: mido.MidiFile,
    source_path: Path,
    track_index: int,
    split_dir: Path,
    program: int,
    suffix: str = "gm",
    velocity_plan: VelocityPlan | None = None,
    channel: int | None = None,
) -> Path:
    """Keep conductor + one track, forcing a representative GM program."""
    split_dir.mkdir(parents=True, exist_ok=True)
    out = mido.MidiFile(type=1, ticks_per_beat=source.ticks_per_beat)

    if len(source.tracks) > 0 and track_index != 0:
        out.tracks.append(clone_conductor_track(source.tracks[0]))
    source_track = source.tracks[track_index]
    if channel is not None:
        source_track = clone_track_for_channel(source_track, channel)
    out.tracks.append(clone_track_with_program(source_track, program, velocity_plan))

    safe = source_path.stem.replace("/", "_")
    channel_suffix = "" if channel is None else f".ch-{channel + 1:02d}"
    output = split_dir / f"{safe}.track-{track_index:02d}{channel_suffix}.{suffix}-p{program:03d}.mid"
    out.save(output)
    return output
