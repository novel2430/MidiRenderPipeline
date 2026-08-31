from midi_render.instruments import (
    melody_render_instrument,
    resolution_warnings,
    resolve_instrument,
    resolve_track,
)
from midi_render.midi import TrackInfo


def track(name: str, program: int | None = None, channels=(0,)) -> TrackInfo:
    programs = () if program is None else (program,)
    return TrackInfo(1, name, 10, tuple(channels), programs, ())


def test_program_is_primary_even_for_generic_names():
    assert resolve_instrument(track("Piano", 5)) == "electric_piano"
    assert resolve_instrument(track("String", 40)) == "solo_violin"
    assert resolve_instrument(track("String", 46)) == "harp"


def test_name_is_fallback_only_without_single_program():
    assert resolve_instrument(track("Piano")) == "acoustic_piano"
    assert resolve_instrument(track("String")) == "string_ensemble"
    assert resolve_instrument(track("Bass", 120)) == "sound_fx"

    ambiguous = TrackInfo(1, "Bass", 10, (0,), (0, 33), ())
    assert resolve_instrument(ambiguous) == "electric_bass"


def test_egt_program_decides_specific_guitar_source():
    assert resolve_instrument(track("Egt", 25)) == "acoustic_guitar"
    assert resolve_instrument(track("Egt", 26)) == "electric_guitar_jazz"
    assert resolve_instrument(track("Egt", 27)) == "electric_guitar_clean"
    assert resolve_instrument(track("Egt", 28)) == "electric_guitar_muted"
    assert resolve_instrument(track("Egt", 29)) == "electric_guitar_overdrive"
    assert resolve_instrument(track("Egt", 30)) == "electric_guitar_distortion"
    assert resolve_instrument(track("Egt", 31)) == "electric_guitar_harmonics"
    assert resolve_instrument(track("Egt")) == "electric_guitar_clean"


def test_program_wins_when_track_name_conflicts():
    assert resolve_instrument(track("Bass", 0)) == "acoustic_piano"
    assert resolve_instrument(track("Piano", 27)) == "electric_guitar_clean"
    assert resolve_instrument(track("Pad", 49)) == "string_ensemble"
    assert resolve_instrument(track("Flute", 119)) == "percussion"


def test_melody_role_and_channel_10_drums_remain_special_cases():
    assert resolve_instrument(track("Melody", 80)) == "melody"
    assert resolve_instrument(track("Drums", 0, channels=(9,))) == "drums"
    assert resolve_instrument(track("", 0, channels=(9,))) == "drums"
    # A textual drum label is not a hard override outside channel 10.
    assert resolve_instrument(track("Drums", 0, channels=(0,))) == "acoustic_piano"


def test_melody_render_instrument_uses_program_family_when_available():
    assert melody_render_instrument(track("Melody", 73)) == "flute"
    assert melody_render_instrument(track("Melody", 40)) == "solo_violin"
    assert melody_render_instrument(track("Melody")) is None


def test_program_mapping_keeps_specific_orchestral_programs():
    assert resolve_instrument(track("Violin", 40)) == "solo_violin"
    assert resolve_instrument(track("Viola", 41)) == "solo_viola"
    assert resolve_instrument(track("Harp", 46)) == "harp"
    assert resolve_instrument(track("Timpani", 47)) == "timpani"
    assert resolve_instrument(track("Horn", 60)) == "horn"


def test_name_program_conflicts_no_longer_emit_resolver_warnings():
    assert resolution_warnings(track("Egt", 25)) == ()
    assert resolution_warnings(track("Egt", 30)) == ()
    assert resolution_warnings(track("Bass", 0)) == ()
    assert resolution_warnings(track("Piano", 27)) == ()


def test_program_fallback_for_unnamed_track():
    assert resolve_instrument(track("", 33)) == "electric_bass"


def test_program_mapping_covers_specific_orchestral_and_gm_families():
    assert resolve_instrument(track("String", 42)) == "cello"
    assert resolve_instrument(track("String", 43)) == "contrabass"
    assert resolve_instrument(track("String", 44)) == "string_tremolo"
    assert resolve_instrument(track("String", 45)) == "string_pizzicato"
    assert resolve_instrument(track("", 56)) == "trumpet"
    assert resolve_instrument(track("", 64)) == "saxophone"
    assert resolve_instrument(track("", 68)) == "oboe"
    assert resolve_instrument(track("", 72)) == "piccolo"
    assert resolve_instrument(track("", 89)) == "synth_pad"
    assert resolve_instrument(track("", 120)) == "sound_fx"


def test_resolution_trusts_any_single_program_regardless_of_name():
    good = resolve_track(track("Egt", 29))
    assert good.instrument == "electric_guitar_overdrive"
    assert good.program_trusted is True

    conflicting = resolve_track(track("Pad", 49))
    assert conflicting.instrument == "string_ensemble"
    assert conflicting.program_trusted is True
    assert conflicting.warning is None

    missing = resolve_track(track("Organ"))
    assert missing.instrument == "organ"
    assert missing.program_trusted is False
