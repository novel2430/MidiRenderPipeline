from pathlib import Path

from midi_render.patches import PatchRegistry


def test_current_registry_selects_baked_guitars_and_existing_asset_families():
    config = Path(__file__).parents[1] / "config" / "patches.toml"
    registry = PatchRegistry(config)

    clean = registry.get("electric_guitar_clean")
    jazz = registry.get("electric_guitar_jazz")
    overdrive = registry.get("electric_guitar_overdrive")
    distortion = registry.get("electric_guitar_distortion")
    assert clean is not None
    assert jazz is not None
    assert overdrive is not None
    assert distortion is not None
    assert clean.effects == ()
    assert jazz.effects == ()
    assert overdrive.effects == ()
    assert distortion.effects == ()
    assert clean.sfz.name == "EGuitarFSBS-clean bridge 20260807.sfz"
    assert jazz.sfz.name == "EGuitarFSBS-jazz bridge 20260807.sfz"
    assert overdrive.sfz.name == "EGuitarFSBS-dist1 bridge 20220911.sfz"
    assert distortion.sfz.name == "EGuitarFSBS-dist2 bridge 20220911.sfz"

    assert registry.get("string_ensemble") is not None
    assert registry.get("choir") is not None
    assert registry.get("flute") is not None
    assert registry.get("solo_violin") is not None
    assert registry.get("solo_viola") is not None
    assert registry.get("harp") is not None
    assert registry.get("timpani") is not None
    assert registry.get("vibraphone") is not None
    assert registry.get("brass") is not None
    assert registry.get("horn") is not None
    assert registry.get("electric_guitar_jazz") is not None
    assert registry.get("electric_guitar_overdrive") is not None
    assert registry.get("electric_guitar_distortion") is not None
    assert registry.get("cello") is not None
    assert registry.get("contrabass") is not None
    assert registry.get("string_tremolo") is not None
    assert registry.get("string_pizzicato") is not None
    assert registry.get("trumpet") is not None
    assert registry.get("trombone") is not None
    assert registry.get("tuba") is not None
    assert registry.get("oboe") is not None
    assert registry.get("english_horn") is not None
    assert registry.get("bassoon") is not None
    assert registry.get("clarinet") is not None
    assert registry.get("piccolo") is not None
    assert registry.family_fallbacks["electric_guitar_muted"] == "electric_guitar_clean"
    assert registry.family_fallbacks["electric_guitar_harmonics"] == "electric_guitar_clean"
    assert registry.family_fallbacks["muted_trumpet"] == "trumpet"
    assert registry.melody.mode == "gm"
    assert registry.melody.instrument is None
    assert registry.melody.gm_program == 71
    gm = registry.general_midi_fallback
    assert gm is not None
    assert gm.soundfont.name == "MuseScore_General_Full.sf2"
    assert gm.representative_program("melody") == 80
    assert gm.representative_program("synth_pad") == 89
    assert registry.post_effects_for("drums") == ("dragonfly_room",)
    assert registry.post_effects_for("synth_lead") == ("dragonfly_plate",)
    assert registry.post_effects_for("melody") == ("dragonfly_plate",)
    assert registry.post_effects_for("drums_kick_layer") == ()
    bass = registry.get("electric_bass")
    drums = registry.get("drums")
    assert bass is not None and drums is not None
    assert registry.stem_effects("electric_bass", bass) == ("gxsvt",)
    assert registry.stem_effects("drums", drums) == ("dragonfly_room",)


def test_current_active_bass_effect_baseline_uses_native_lv2_chain_uri():
    config = Path(__file__).parents[1] / "config" / "patches.toml"
    registry = PatchRegistry(config)

    assert registry.effect_renderer.backend == "native-lv2"
    assert registry.effect_renderer.tool == "mrp-lv2-chain"
    assert registry.effect_renderer.block_size == 1024
    bass = registry.effect("gxsvt").values
    assert "backend" not in bass
    assert "tool" not in bass
    assert bass["bundle"] == "gx_ampegsvt.lv2"
    assert bass["plugin_uri"].endswith("gx_ampegsvt_#_ampegsvt_")
    assert bass["params"] == {
        "BYPASS": 1.0,
        "BASS": 0.60,
        "MIDDLE": 0.30,
        "TREBLE": 0.40,
        "VOLUME": 0.20,
        "LOWSWITCH": 1,
        "MIDSWITCH": 1,
        "HIGHSWITCH": 0,
        "CABSWITCH": 1,
    }
    plate = registry.effect("dragonfly_plate").values
    assert plate["input_channels"] == 2
    assert plate["params"]["dry_level"] == 80.0
    assert plate["params"]["early_level"] == 20.0
    assert plate["params"]["decay"] == 0.6


def test_optional_drum_kick_layer_resolves_from_instruments_root(tmp_path: Path):
    config_dir = tmp_path / "config"
    instruments = tmp_path / "instruments"
    config_dir.mkdir()
    (instruments / "muldjord").mkdir(parents=True)
    sfz = instruments / "muldjord" / "MuldjordKit GM.sfz"
    sfz.write_text("// test\n")

    config = config_dir / "patches.toml"
    config.write_text(
        """
[paths]
instruments = "../instruments"

[drum_kick_layer]
sfz = "muldjord/MuldjordKit GM.sfz"
notes = [35, 36]
gain_db = -6.0
""".strip()
        + "\n"
    )

    registry = PatchRegistry(config)
    layer = registry.drum_kick_layer
    assert layer is not None
    assert layer.sfz == sfz.resolve()
    assert layer.notes == (35, 36)
    assert layer.gain_db == -6.0


def test_master_config_reads_normalize_target_and_post_normalize_gain(tmp_path: Path):
    config = tmp_path / "patches.toml"
    config.write_text(
        """
[master]
normalize_peak_db = -2.0
gain_db = -3.0
""".strip()
        + "\n"
    )

    registry = PatchRegistry(config)
    assert registry.master.normalize_peak_db == -2.0
    assert registry.master.gain_db == -3.0


def test_master_config_defaults_match_previous_output_behavior(tmp_path: Path):
    config = tmp_path / "patches.toml"
    config.write_text("# empty config\n")

    registry = PatchRegistry(config)
    assert registry.master.normalize_peak_db == -1.0
    assert registry.master.gain_db == 0.0


def test_family_then_general_midi_fallback_config(tmp_path: Path):
    config_dir = tmp_path / "config"
    instruments = tmp_path / "instruments"
    config_dir.mkdir()
    (instruments / "guitar").mkdir(parents=True)
    clean = instruments / "guitar" / "clean.sfz"
    clean.write_text("// clean\n")
    sf2 = instruments / "MuseScore_General_Full.sf2"
    sf2.write_bytes(b"sf2")

    config = config_dir / "patches.toml"
    config.write_text(
        """
[paths]
instruments = "../instruments"

[libraries.guitar]
root = "guitar"

[patches.electric_guitar_clean]
library = "guitar"
sfz = "clean.sfz"

[family_fallbacks]
electric_guitar_muted = "electric_guitar_clean"

[general_midi_fallback]
soundfont = "MuseScore_General_Full.sf2"
synth_gain = 0.25
gain_db = -1.0

[general_midi_fallback.program_for_instrument]
synth_pad = 89
""".strip()
        + "\n"
    )

    registry = PatchRegistry(config)
    patch, route = registry.resolve_dedicated("electric_guitar_muted")
    assert patch is not None
    assert patch.name == "electric_guitar_clean"
    assert route == "family"

    missing, route = registry.resolve_dedicated("synth_pad")
    assert missing is None
    assert route is None
    gm = registry.general_midi_fallback
    assert gm is not None
    assert gm.soundfont == sf2.resolve()
    assert gm.representative_program("synth_pad") == 89
    assert gm.synth_gain == 0.25
    assert gm.gain_db == -1.0


def test_melody_config_modes_and_validation(tmp_path: Path):
    config = tmp_path / "patches.toml"
    config.write_text(
        """
[melody]
mode = "gm"
gm_program = 73
instrument = "flute"
""".strip()
        + "\n"
    )
    registry = PatchRegistry(config)
    assert registry.melody.mode == "gm"
    assert registry.melody.gm_program == 73
    assert registry.melody.instrument == "flute"

    config.write_text('[melody]\nmode = "instrument"\ninstrument = "harmonica"\n')
    registry = PatchRegistry(config)
    assert registry.melody.mode == "instrument"
    assert registry.melody.instrument == "harmonica"

    config.write_text('[melody]\nmode = "instrument"\n')
    try:
        PatchRegistry(config)
    except ValueError as exc:
        assert "melody.instrument is required" in str(exc)
    else:
        raise AssertionError("instrument mode without instrument should fail")

    config.write_text('[melody]\nmode = "wat"\n')
    try:
        PatchRegistry(config)
    except ValueError as exc:
        assert "melody.mode" in str(exc)
    else:
        raise AssertionError("unknown melody mode should fail")

    config.write_text('[melody]\nmode = "gm"\ngm_program = 128\n')
    try:
        PatchRegistry(config)
    except ValueError as exc:
        assert "melody.gm_program" in str(exc)
    else:
        raise AssertionError("out-of-range melody GM program should fail")


def test_performance_profiles_read_global_defaults_and_per_instrument_ranges(tmp_path: Path):
    config = tmp_path / "patches.toml"
    config.write_text(
        """
[performance]
enabled = true
constant_spread_max = 4
low_percentile = 0.10
high_percentile = 0.90
[performance.instruments.electric_bass]
velocity_min = 50
velocity_nominal = 72
velocity_max = 85

[performance.instruments.string_ensemble]
velocity_min = 40
velocity_nominal = 62
velocity_max = 78
""".strip()
        + "\n"
    )

    registry = PatchRegistry(config)
    bass = registry.performance_profile("electric_bass")
    strings = registry.performance_profile("string_ensemble")
    assert bass is not None
    assert strings is not None
    assert (bass.velocity_min, bass.velocity_nominal, bass.velocity_max) == (50, 72, 85)
    assert bass.constant_spread_max == 4.0
    assert registry.performance_profile("flute") is None


def test_performance_profile_validation_rejects_invalid_range(tmp_path: Path):
    config = tmp_path / "patches.toml"
    config.write_text(
        """
[performance.instruments.electric_bass]
velocity_min = 80
velocity_nominal = 72
velocity_max = 85
""".strip()
        + "\n"
    )
    try:
        PatchRegistry(config)
    except ValueError as exc:
        assert "velocity_min" in str(exc)
    else:
        raise AssertionError("invalid performance range should fail")


def test_post_effects_append_after_patch_effects_and_validate_names(tmp_path: Path):
    config_dir = tmp_path / "config-post-fx"
    instruments = tmp_path / "instruments-post-fx" / "lib"
    config_dir.mkdir()
    instruments.mkdir(parents=True)
    (instruments / "bass.sfz").write_text("// bass\n")
    config = config_dir / "patches.toml"
    config.write_text(
        """
[paths]
instruments = "../instruments-post-fx"

[libraries.test]
root = "lib"

[patches.electric_bass]
library = "test"
sfz = "bass.sfz"
effects = ["amp"]

[post_effects]
electric_bass = ["room", "limiter"]

[effects.amp]
plugin_uri = "urn:test:amp"

[effects.room]
plugin_uri = "urn:test:room"

[effects.limiter]
plugin_uri = "urn:test:limiter"
""".strip()
        + "\n"
    )
    registry = PatchRegistry(config)
    patch = registry.get("electric_bass")
    assert patch is not None
    assert registry.stem_effects("electric_bass", patch) == ("amp", "room", "limiter")

    config.write_text(
        """
[post_effects]
synth_lead = ["missing"]

[effects.plate]
plugin_uri = "urn:test:plate"
""".strip()
        + "\n"
    )
    try:
        PatchRegistry(config)
    except ValueError as exc:
        assert "unknown effect 'missing'" in str(exc)
    else:
        raise AssertionError("unknown post effect should fail during config load")
