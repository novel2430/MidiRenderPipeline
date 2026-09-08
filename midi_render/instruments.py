from __future__ import annotations

from dataclasses import dataclass
import re

from .midi import TrackInfo


@dataclass(frozen=True)
class InstrumentResolution:
    instrument: str
    program_trusted: bool
    warning: str | None = None
    representative_program: int | None = None
    representative_share: float | None = None


def _norm(value: str) -> str:
    value = value.strip().lower()
    value = value.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", value)


EXACT_ALIASES: dict[str, str] = {
    "melody": "melody",
    "piano": "acoustic_piano",
    "acoustic piano": "acoustic_piano",
    "e piano": "electric_piano",
    "epiano": "electric_piano",
    "electric piano": "electric_piano",
    "acoustic guitar": "acoustic_guitar",
    "nylon guitar": "acoustic_guitar",
    "bass": "electric_bass",
    "drum": "drums",
    "drums": "drums",
    "string": "string_ensemble",
    "strings": "string_ensemble",
    "string ensemble": "string_ensemble",
    "choir": "choir",
    "chorus": "choir",
    "voice": "choir",
    "harmonica": "harmonica",
    "pad": "synth_pad",
    "synth pad": "synth_pad",
    "synth": "synth_pad",
    "flute": "flute",
    "dizi": "flute",
    "harp": "harp",
    "organ": "organ",
    "horn": "horn",
    "violin": "solo_violin",
    "viola": "solo_viola",
    "cello": "cello",
    "vibraphone": "vibraphone",
    "dulcimer": "dulcimer",
    "music box": "music_box",
    "timpani": "timpani",
}


def _program_family(program: int | None) -> str | None:
    if program is None:
        return None

    # MIDI programs are 0-based here (mido representation). Keep the canonical
    # name as specific as the installed dedicated sources justify, while still
    # covering all 128 GM programs so the final SoundFont fallback can preserve
    # the original timbre when no dedicated/family patch exists.
    if 0 <= program <= 3:
        return "acoustic_piano"
    if 4 <= program <= 7:
        return "electric_piano"
    if 8 <= program <= 15:
        if program == 10:
            return "music_box"
        if program == 11:
            return "vibraphone"
        if program == 15:
            return "dulcimer"
        return "chromatic_percussion"
    if 16 <= program <= 20:
        return "organ"
    if program == 21 or program == 23:
        return "accordion"
    if program == 22:
        return "harmonica"
    if 24 <= program <= 25:
        return "acoustic_guitar"
    if program == 26:
        return "electric_guitar_jazz"
    if program == 27:
        return "electric_guitar_clean"
    if program == 28:
        return "electric_guitar_muted"
    if program == 29:
        return "electric_guitar_overdrive"
    if program == 30:
        return "electric_guitar_distortion"
    if program == 31:
        return "electric_guitar_harmonics"
    if 32 <= program <= 39:
        return "electric_bass"
    if program == 40:
        return "solo_violin"
    if program == 41:
        return "solo_viola"
    if program == 42:
        return "cello"
    if program == 43:
        return "contrabass"
    if program == 44:
        return "string_tremolo"
    if program == 45:
        return "string_pizzicato"
    if program == 46:
        return "harp"
    if program == 47:
        return "timpani"
    if 48 <= program <= 51:
        return "string_ensemble"
    if 52 <= program <= 54:
        return "choir"
    if program == 55:
        return "orchestra_hit"
    if program == 56:
        return "trumpet"
    if program == 57:
        return "trombone"
    if program == 58:
        return "tuba"
    if program == 59:
        return "muted_trumpet"
    if program == 60:
        return "horn"
    if 61 <= program <= 63:
        return "brass"
    if 64 <= program <= 67:
        return "saxophone"
    if program == 68:
        return "oboe"
    if program == 69:
        return "english_horn"
    if program == 70:
        return "bassoon"
    if program == 71:
        return "clarinet"
    if program == 72:
        return "piccolo"
    if program == 73:
        return "flute"
    if 74 <= program <= 79:
        return "wind"
    if 80 <= program <= 87:
        return "synth_lead"
    if 88 <= program <= 95:
        return "synth_pad"
    if 96 <= program <= 103:
        return "synth_fx"
    if 104 <= program <= 111:
        return "ethnic"
    if 112 <= program <= 119:
        return "percussion"
    if 120 <= program <= 127:
        return "sound_fx"
    return None


def _name_family(name: str) -> str | None:
    """Resolve only from textual metadata, without consulting Program Change."""
    if name in {"egt", "e gt", "electric guitar", "e guitar", "guitar"}:
        return "electric_guitar_clean"

    if "distortion" in name or "distorted" in name:
        return "electric_guitar_distortion"
    if "overdrive" in name or "drive guitar" in name or "driven guitar" in name:
        return "electric_guitar_overdrive"

    if name in EXACT_ALIASES:
        return EXACT_ALIASES[name]

    if "drum" in name:
        return "drums"
    if "string" in name:
        return "string_ensemble"
    if "choir" in name or "chorus" in name:
        return "choir"
    if "harmonica" in name:
        return "harmonica"
    if "piano" in name:
        return "acoustic_piano"
    if "organ" in name:
        return "organ"
    if "guitar" in name:
        return "electric_guitar_clean"
    if "bass" in name:
        return "electric_bass"
    if "pad" in name:
        return "synth_pad"
    return None


def _usage_rank(track: TrackInfo, program: int) -> tuple[int, int, int, int]:
    usage = track.usage_for_program(program)
    assert usage is not None
    # Longer sounding duration is the strongest signal, note count breaks ties,
    # then prefer the program that started earlier and finally the lower GM id
    # for deterministic behavior on pathological equal-use files.
    return (
        usage.duration_ticks,
        usage.note_count,
        -usage.first_note_tick,
        -usage.program,
    )


def _representative_program(track: TrackInfo, name: str) -> int | None:
    if len(track.note_channels) != 1:
        return None
    if not track.program_usage:
        return track.primary_program
    programs = [usage.program for usage in track.program_usage]
    if len(programs) == 1:
        return programs[0]

    # Track names are unreliable as a primary signal, but when a dynamic stem's
    # name clearly names one of the timbre families that actually sounded, use
    # it to disambiguate e.g. "piano" program 0+91 or "organ" 19+48+52.
    name_family = _name_family(name)
    if name_family is not None:
        matching = [program for program in programs if _program_family(program) == name_family]
        if matching:
            return max(matching, key=lambda program: _usage_rank(track, program))

    return max(programs, key=lambda program: _usage_rank(track, program))


def _dynamic_lock_warning(track: TrackInfo, program: int, family: str) -> str:
    share = track.program_share(program)
    effective = ", ".join(str(value) for value in track.effective_programs)
    if share is None:
        return f"sounding notes use programs {effective}; locked to program {program} ({family})"
    return (
        f"sounding notes use programs {effective}; locked to program {program} "
        f"({family}, {share * 100.0:.1f}% of sounding duration)"
    )


def resolve_track(track: TrackInfo) -> InstrumentResolution:
    """Resolve one logical track with a strict Program-first policy.

    Programs that actually carry sounding notes are the primary timbre signal.
    A genuinely dynamic channel is approximated by one representative program
    so the normal SFZ-first route stays available. Track names are fallback
    metadata and, for dynamic channels only, a disambiguation hint among program
    families that truly sounded. The higher-level exceptions are the dataset
    `Melody` role and GM percussion on channel 10.
    """
    name = _norm(track.name)

    # GM percussion is selected by channel 10 rather than pitched-instrument
    # Program Change, so channel identity outranks both Program and track name.
    if track.is_channel_10_drum_hint:
        return InstrumentResolution("drums", False)

    # `Melody` is a dataset role, not a timbre. Keep the role so the CLI can
    # skip/configure it independently, while retaining the single source Program
    # as trustworthy timbre information when Melody is explicitly rendered.
    if name == "melody":
        # Preserve the legacy meaning of a Melody track with no explicit Program
        # Change: it carries role metadata only, so melody policy/config chooses
        # the timbre. GM's implicit program-0 default is only used to rescue
        # otherwise-unidentified pitched channels, not to reinterpret Melody.
        representative = _representative_program(track, name) if track.programs else None
        return InstrumentResolution(
            "melody",
            representative is not None,
            representative_program=representative,
            representative_share=(
                track.program_share(representative) if representative is not None else None
            ),
        )

    # Trust the program that actually carries the stem's musical content. For a
    # truly dynamic channel this is a deliberate SFZ-first approximation: name
    # is used only to disambiguate among programs that really sounded, otherwise
    # sounding duration/note count chooses the representative timbre.
    representative = _representative_program(track, name)
    program_family = _program_family(representative)
    if program_family is not None:
        warning = (
            _dynamic_lock_warning(track, representative, program_family)
            if track.has_dynamic_program
            else None
        )
        return InstrumentResolution(
            program_family,
            True,
            warning=warning,
            representative_program=representative,
            representative_share=track.program_share(representative),
        )

    # No single usable Program: fall back to textual metadata. This also covers
    # multi-program tracks, which the analyzer already reports as unusual.
    name_family = _name_family(name)
    if name_family is not None:
        return InstrumentResolution(name_family, False)

    # For a single pitched MIDI channel with no Program Change at all, General
    # MIDI starts on program 0. Keep named legacy/Jay tracks on the name fallback
    # above, but let otherwise-unresolvable real-world files render correctly.
    if len(track.note_channels) == 1 and not track.program_usage and track.note_channels != (9,):
        return InstrumentResolution("acoustic_piano", True, representative_program=0)

    return InstrumentResolution("unknown", False)

def resolution_warnings(track: TrackInfo) -> tuple[str, ...]:
    result = resolve_track(track)
    return (result.warning,) if result.warning else ()


def melody_render_instrument(track: TrackInfo) -> str | None:
    """Return the best timbre candidate when a Melody role is explicitly rendered.

    `Melody` is kept as a logical role during normal resolution so callers may
    skip guide-melody tracks by default. When the caller *does* want the melody
    rendered, we still want to exploit any real timbre hint already present in
    the MIDI file before falling back to a GM SoundFont.

    The safe portable hint is the representative sounding GM Program. When it
    exists, map it to the same canonical instrument family table used by normal
    resolution.
    """
    result = resolve_track(track)
    if result.instrument != "melody":
        return result.instrument
    return _program_family(result.representative_program)


def resolve_instrument(track: TrackInfo) -> str:
    """Resolve one logical MIDI track to a canonical instrument name.

    Policy:
    1. channel-10 percussion is always drums;
    2. preserve the explicit `Melody` dataset role while retaining its Program
       as timbre information for Melody rendering policy;
    3. otherwise resolve from the representative sounding GM Program (including
       the dynamic-program locking policy);
    4. consult track name only when no usable sounding Program exists.
    """
    return resolve_track(track).instrument
