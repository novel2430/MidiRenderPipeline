from __future__ import annotations

from dataclasses import dataclass
import re

from .midi import TrackInfo


@dataclass(frozen=True)
class InstrumentResolution:
    instrument: str
    program_trusted: bool
    warning: str | None = None


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
    if "guitar" in name:
        return "electric_guitar_clean"
    if "bass" in name:
        return "electric_bass"
    if "pad" in name:
        return "synth_pad"
    return None


def resolve_track(track: TrackInfo) -> InstrumentResolution:
    """Resolve one logical track with a strict Program-first policy.

    A single GM Program Change is the primary timbre signal. Track names are
    fallback metadata only and never veto a clear Program. The only higher-level
    exceptions are the dataset `Melody` role and GM percussion on channel 10.
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
        return InstrumentResolution("melody", track.primary_program is not None)

    # The corpus' track names are often coarse or misleading. If one clear GM
    # Program exists, trust it unconditionally and use the name only as metadata.
    program_family = _program_family(track.primary_program)
    if program_family is not None:
        return InstrumentResolution(program_family, True)

    # No single usable Program: fall back to textual metadata. This also covers
    # multi-program tracks, which the analyzer already reports as unusual.
    name_family = _name_family(name)
    return InstrumentResolution(name_family or "unknown", False)

def resolution_warnings(track: TrackInfo) -> tuple[str, ...]:
    result = resolve_track(track)
    return (result.warning,) if result.warning else ()


def melody_render_instrument(track: TrackInfo) -> str | None:
    """Return the best timbre candidate when a Melody role is explicitly rendered.

    `Melody` is kept as a logical role during normal resolution so callers may
    skip guide-melody tracks by default. When the caller *does* want the melody
    rendered, we still want to exploit any real timbre hint already present in
    the MIDI file before falling back to a GM SoundFont.

    Today the only safe portable hint is the primary GM Program Change. When it
    exists, map it to the same canonical instrument family table used by normal
    resolution.
    """
    result = resolve_track(track)
    if result.instrument != "melody":
        return result.instrument
    return _program_family(track.primary_program)


def resolve_instrument(track: TrackInfo) -> str:
    """Resolve one logical MIDI track to a canonical instrument name.

    Policy:
    1. channel-10 percussion is always drums;
    2. preserve the explicit `Melody` dataset role while retaining its Program
       as timbre information for Melody rendering policy;
    3. otherwise trust one clear GM Program Change unconditionally;
    4. consult track name only when no single usable Program exists.
    """
    return resolve_track(track).instrument
