from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Patch:
    name: str
    library: str
    sfz: Path
    gain_db: float = 0.0
    effects: tuple[str, ...] = ()
    enabled: bool = True


@dataclass(frozen=True)
class EffectConfig:
    name: str
    values: dict[str, object]


@dataclass(frozen=True)
class EffectRendererConfig:
    backend: str = "native-lv2"
    tool: str = "mrp-lv2-chain"
    block_size: int = 1024


@dataclass(frozen=True)
class DrumKickLayer:
    sfz: Path
    notes: tuple[int, ...] = (35, 36)
    gain_db: float = -6.0
    enabled: bool = True


@dataclass(frozen=True)
class MasterConfig:
    normalize_peak_db: float = -1.0
    gain_db: float = 0.0


@dataclass(frozen=True)
class MelodyConfig:
    mode: str = "auto"
    instrument: str | None = None
    gm_program: int | None = None


@dataclass(frozen=True)
class PerformanceProfile:
    instrument: str
    velocity_min: int
    velocity_nominal: int
    velocity_max: int
    constant_spread_max: float = 4.0
    low_percentile: float = 0.10
    high_percentile: float = 0.90

    def cache_tag(self) -> str:
        payload = (
            "velocity-adapter-v2|"
            f"{self.instrument}|{self.velocity_min}|{self.velocity_nominal}|{self.velocity_max}|"
            f"{self.constant_spread_max:.6g}|{self.low_percentile:.6g}|"
            f"{self.high_percentile:.6g}"
        )
        return sha1(payload.encode("utf-8")).hexdigest()[:10]


@dataclass(frozen=True)
class GeneralMidiFallback:
    soundfont: Path
    synth_gain: float = 0.2
    gain_db: float = 0.0
    enabled: bool = True
    program_for_instrument: dict[str, int] | None = None

    def representative_program(self, instrument: str) -> int | None:
        if self.program_for_instrument is None:
            return None
        return self.program_for_instrument.get(instrument)


class PatchRegistry:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        with self.config_path.open("rb") as f:
            self.data = tomllib.load(f)

        base = self.config_path.parent
        paths = self.data.get("paths", {})
        self.instruments_root = (base / str(paths.get("instruments", "../resources/instruments"))).resolve()
        self.fx_root = (base / str(paths.get("fx", "../resources/fx"))).resolve()
        self.lv2_root = (base / str(paths.get("lv2", "../resources/fx/lv2"))).resolve()
        self.tools_root = (base / str(paths.get("tools", "../resources/tools"))).resolve()

        effect_renderer_cfg = self.data.get("effect_renderer", {})
        if not isinstance(effect_renderer_cfg, dict):
            raise ValueError("effect_renderer must be a table")
        effect_backend = str(effect_renderer_cfg.get("backend", "native-lv2")).strip().lower()
        if effect_backend not in {"native-lv2", "lv2apply"}:
            raise ValueError("effect_renderer.backend must be one of: native-lv2, lv2apply")
        effect_block_size = int(effect_renderer_cfg.get("block_size", 1024))
        if effect_block_size < 1:
            raise ValueError("effect_renderer.block_size must be >= 1")
        self.effect_renderer = EffectRendererConfig(
            backend=effect_backend,
            tool=str(effect_renderer_cfg.get(
                "tool",
                "mrp-lv2-chain" if effect_backend == "native-lv2" else "lv2apply",
            )),
            block_size=effect_block_size,
        )

        master_cfg = self.data.get("master", {})
        self.master = MasterConfig(
            normalize_peak_db=float(master_cfg.get("normalize_peak_db", -1.0)),
            gain_db=float(master_cfg.get("gain_db", 0.0)),
        )
        if self.master.normalize_peak_db > 0.0:
            raise ValueError("master.normalize_peak_db must be <= 0 dBFS")

        melody_cfg = self.data.get("melody", {})
        mode = str(melody_cfg.get("mode", "auto")).strip().lower()
        if mode not in {"auto", "gm", "instrument"}:
            raise ValueError("melody.mode must be one of: auto, gm, instrument")
        melody_instrument = melody_cfg.get("instrument")
        if melody_instrument is not None:
            melody_instrument = str(melody_instrument).strip() or None
        gm_program = melody_cfg.get("gm_program")
        if gm_program is not None:
            gm_program = int(gm_program)
            if gm_program < 0 or gm_program > 127:
                raise ValueError("melody.gm_program must be 0..127")
        if mode == "instrument" and melody_instrument is None:
            raise ValueError("melody.instrument is required when melody.mode = 'instrument'")
        self.melody = MelodyConfig(
            mode=mode,
            instrument=melody_instrument,
            gm_program=gm_program,
        )

        performance_cfg = self.data.get("performance", {})
        self.performance_enabled = bool(performance_cfg.get("enabled", True))
        default_constant_spread = float(performance_cfg.get("constant_spread_max", 4.0))
        default_low_percentile = float(performance_cfg.get("low_percentile", 0.10))
        default_high_percentile = float(performance_cfg.get("high_percentile", 0.90))
        if default_constant_spread < 0.0:
            raise ValueError("performance.constant_spread_max must be >= 0")
        if not 0.0 <= default_low_percentile < default_high_percentile <= 1.0:
            raise ValueError("performance percentiles must satisfy 0 <= low < high <= 1")

        self.performance_profiles: dict[str, PerformanceProfile] = {}
        profile_cfg = performance_cfg.get("instruments", {})
        if not isinstance(profile_cfg, dict):
            raise ValueError("performance.instruments must be a table")
        for instrument, cfg in profile_cfg.items():
            if not isinstance(cfg, dict):
                raise ValueError(f"performance profile for {instrument!r} must be a table")
            velocity_min = int(cfg["velocity_min"])
            velocity_nominal = int(cfg["velocity_nominal"])
            velocity_max = int(cfg["velocity_max"])
            if not 1 <= velocity_min <= velocity_nominal <= velocity_max <= 127:
                raise ValueError(
                    f"performance profile {instrument!r} must satisfy "
                    "1 <= velocity_min <= velocity_nominal <= velocity_max <= 127"
                )
            if velocity_min == velocity_max:
                raise ValueError(f"performance profile {instrument!r} must have a non-zero velocity range")
            constant_spread = float(cfg.get("constant_spread_max", default_constant_spread))
            low_percentile = float(cfg.get("low_percentile", default_low_percentile))
            high_percentile = float(cfg.get("high_percentile", default_high_percentile))
            if constant_spread < 0.0:
                raise ValueError(
                    f"performance profile {instrument!r} constant_spread_max must be >= 0"
                )
            if not 0.0 <= low_percentile < high_percentile <= 1.0:
                raise ValueError(
                    f"performance profile {instrument!r} percentiles must satisfy "
                    "0 <= low < high <= 1"
                )
            name = str(instrument)
            self.performance_profiles[name] = PerformanceProfile(
                instrument=name,
                velocity_min=velocity_min,
                velocity_nominal=velocity_nominal,
                velocity_max=velocity_max,
                constant_spread_max=constant_spread,
                low_percentile=low_percentile,
                high_percentile=high_percentile,
            )

        self.libraries: dict[str, Path] = {}
        for name, cfg in self.data.get("libraries", {}).items():
            root = Path(str(cfg["root"])).expanduser()
            if not root.is_absolute():
                root = self.instruments_root / root
            self.libraries[name] = root.resolve()

        self.patches: dict[str, Patch] = {}
        for name, cfg in self.data.get("patches", {}).items():
            library = str(cfg["library"])
            if library not in self.libraries:
                raise ValueError(f"patch {name!r} refers to unknown library {library!r}")
            sfz = self.libraries[library] / str(cfg["sfz"])
            self.patches[name] = Patch(
                name=name,
                library=library,
                sfz=sfz.resolve(),
                gain_db=float(cfg.get("gain_db", 0.0)),
                effects=tuple(str(x) for x in cfg.get("effects", [])),
                enabled=bool(cfg.get("enabled", True)),
            )

        self.family_fallbacks: dict[str, str] = {
            str(instrument): str(patch_name)
            for instrument, patch_name in self.data.get("family_fallbacks", {}).items()
        }
        for instrument, patch_name in self.family_fallbacks.items():
            if patch_name not in self.patches:
                raise ValueError(
                    f"family fallback {instrument!r} refers to unknown patch {patch_name!r}"
                )

        self.general_midi_fallback: GeneralMidiFallback | None = None
        gm_cfg = self.data.get("general_midi_fallback")
        if gm_cfg is not None:
            soundfont = Path(str(gm_cfg["soundfont"])).expanduser()
            if not soundfont.is_absolute():
                soundfont = self.instruments_root / soundfont
            program_cfg = gm_cfg.get("program_for_instrument", {})
            if not isinstance(program_cfg, dict):
                raise ValueError("general_midi_fallback.program_for_instrument must be a table")
            programs: dict[str, int] = {}
            for instrument, program in program_cfg.items():
                value = int(program)
                if value < 0 or value > 127:
                    raise ValueError(
                        f"general_midi_fallback program for {instrument!r} must be 0..127"
                    )
                programs[str(instrument)] = value
            self.general_midi_fallback = GeneralMidiFallback(
                soundfont=soundfont.resolve(),
                synth_gain=float(gm_cfg.get("synth_gain", 0.2)),
                gain_db=float(gm_cfg.get("gain_db", 0.0)),
                enabled=bool(gm_cfg.get("enabled", True)),
                program_for_instrument=programs,
            )
            if self.general_midi_fallback.synth_gain <= 0.0:
                raise ValueError("general_midi_fallback.synth_gain must be > 0")

        self.drum_kick_layer: DrumKickLayer | None = None
        layer_cfg = self.data.get("drum_kick_layer")
        if layer_cfg is not None:
            layer_path = Path(str(layer_cfg["sfz"])).expanduser()
            if not layer_path.is_absolute():
                layer_path = self.instruments_root / layer_path
            notes = tuple(int(x) for x in layer_cfg.get("notes", [35, 36]))
            if not notes or any(note < 0 or note > 127 for note in notes):
                raise ValueError("drum_kick_layer.notes must contain MIDI notes 0..127")
            self.drum_kick_layer = DrumKickLayer(
                sfz=layer_path.resolve(),
                notes=notes,
                gain_db=float(layer_cfg.get("gain_db", -6.0)),
                enabled=bool(layer_cfg.get("enabled", True)),
            )

    def performance_profile(self, instrument: str) -> PerformanceProfile | None:
        if not self.performance_enabled:
            return None
        return self.performance_profiles.get(instrument)

    def get(self, instrument: str) -> Patch | None:
        patch = self.patches.get(instrument)
        if patch is None or not patch.enabled:
            return None
        return patch

    def get_available(self, instrument: str) -> Patch | None:
        patch = self.get(instrument)
        if patch is None or not patch.sfz.is_file():
            return None
        return patch

    def resolve_dedicated(self, instrument: str) -> tuple[Patch | None, str | None]:
        """Resolve exact then configured family/shared SFZ patch."""
        exact = self.get_available(instrument)
        if exact is not None:
            return exact, "exact"

        family_patch_name = self.family_fallbacks.get(instrument)
        if family_patch_name is not None:
            family = self.get_available(family_patch_name)
            if family is not None:
                return family, "family"

        return None, None

    def require(self, instrument: str) -> Patch:
        patch = self.get(instrument)
        if patch is None:
            raise KeyError(f"no enabled patch configured for instrument: {instrument}")
        if not patch.sfz.is_file():
            raise FileNotFoundError(f"SFZ not found for {instrument}: {patch.sfz}")
        return patch

    def effect(self, name: str) -> EffectConfig:
        cfg = self.data.get("effects", {}).get(name)
        if cfg is None:
            raise KeyError(f"effect not configured: {name}")
        return EffectConfig(name=name, values=dict(cfg))

    def resolve_tool(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.tools_root / path
        return path.resolve()

    def resolve_fx(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.fx_root / path
        return path.resolve()

    def resolve_lv2(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.lv2_root / path
        return path.resolve()

    def doctor_lines(self) -> list[str]:
        melody_detail = f"mode={self.melody.mode}"
        if self.melody.instrument is not None:
            melody_detail += f" instrument={self.melody.instrument}"
        if self.melody.gm_program is not None:
            melody_detail += f" gm_program={self.melody.gm_program}"
        lines: list[str] = [
            f"master   OK       normalize_peak={self.master.normalize_peak_db:+.2f}dBFS "
            f"gain={self.master.gain_db:+.2f}dB",
            f"melody   OK       {melody_detail}",
            f"perform  OK       enabled={self.performance_enabled} "
            f"profiles={len(self.performance_profiles)}",
            f"fx-host  OK       backend={self.effect_renderer.backend} "
            f"tool={self.effect_renderer.tool} block={self.effect_renderer.block_size}",
        ]
        for name, profile in sorted(self.performance_profiles.items()):
            lines.append(
                f"profile  OK       {name:24s}  "
                f"vel={profile.velocity_min}/{profile.velocity_nominal}/{profile.velocity_max} "
                f"constant_spread<={profile.constant_spread_max:g} "
                f"p={profile.low_percentile:g}..{profile.high_percentile:g}"
            )
        for name, root in sorted(self.libraries.items()):
            status = "OK" if root.exists() else "MISSING"
            lines.append(f"library  {status:7s}  {name:24s}  {root}")
        for name, patch in sorted(self.patches.items()):
            if not patch.enabled:
                status = "DISABLED"
            else:
                status = "OK" if patch.sfz.is_file() else "MISSING"
            lines.append(f"patch    {status:7s}  {name:24s}  {patch.sfz}")
        for instrument, patch_name in sorted(self.family_fallbacks.items()):
            patch = self.get_available(patch_name)
            status = "OK" if patch is not None else "MISSING"
            lines.append(
                f"family   {status:7s}  {instrument:24s}  -> {patch_name}"
            )
        if self.general_midi_fallback is not None:
            gm = self.general_midi_fallback
            if not gm.enabled:
                status = "DISABLED"
            else:
                status = "OK" if gm.soundfont.is_file() else "MISSING"
            lines.append(
                f"gm       {status:7s}  {'MuseScore/GM fallback':24s}  {gm.soundfont}  "
                f"[synth_gain={gm.synth_gain:g} gain={gm.gain_db:+.2f}dB]"
            )
        if self.drum_kick_layer is not None:
            layer = self.drum_kick_layer
            if not layer.enabled:
                status = "DISABLED"
            else:
                status = "OK" if layer.sfz.is_file() else "MISSING"
            notes = ",".join(str(x) for x in layer.notes)
            lines.append(
                f"layer    {status:7s}  {'drum_kick':24s}  {layer.sfz}  "
                f"[notes={notes} gain={layer.gain_db:+.2f}dB]"
            )
        for name, cfg in sorted(self.data.get("effects", {}).items()):
            bundle = cfg.get("bundle")
            if bundle is None:
                lines.append(f"effect   MISSING  {name:24s}  <bundle not configured>")
                continue
            path = self.resolve_lv2(str(bundle))
            status = "OK" if path.exists() else "MISSING"
            uri = str(cfg.get("plugin_uri", "<plugin_uri missing>"))
            lines.append(f"effect   {status:7s}  {name:24s}  {path}  [{uri}]")
        return lines
