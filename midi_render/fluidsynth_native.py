from __future__ import annotations

import ctypes
from ctypes import POINTER, c_char_p, c_double, c_float, c_int, c_void_p
from ctypes.util import find_library
from pathlib import Path
import os


FLUID_OK = 0
FLUID_PLAYER_PLAYING = 1
CHANNEL_TYPE_MELODIC = 0
CHANNEL_TYPE_DRUM = 1


class FluidSynthNativeError(RuntimeError):
    pass


def _library_candidates() -> list[str]:
    candidates: list[str] = []
    override = os.environ.get("FLUIDSYNTH_LIBRARY")
    if override:
        candidates.append(override)
    discovered = find_library("fluidsynth")
    if discovered:
        candidates.append(discovered)
    candidates.extend(
        [
            "libfluidsynth.so.3",
            "libfluidsynth.so",
            "libfluidsynth.dylib",
            "libfluidsynth-3.dll",
            "libfluidsynth.dll",
        ]
    )
    return list(dict.fromkeys(candidates))


def find_fluidsynth_library() -> str | None:
    for candidate in _library_candidates():
        try:
            ctypes.CDLL(candidate)
        except OSError:
            continue
        return candidate
    return None


class FluidSynthLibrary:
    """Small ctypes binding for the libfluidsynth surface used by this project."""

    def __init__(self, path: str | None = None):
        selected = path or find_fluidsynth_library()
        if selected is None:
            raise FluidSynthNativeError(
                "libfluidsynth not found. Install the FluidSynth shared library "
                "or set FLUIDSYNTH_LIBRARY to its path."
            )
        try:
            self.lib = ctypes.CDLL(selected)
        except OSError as exc:
            raise FluidSynthNativeError(f"failed to load libfluidsynth: {selected}: {exc}") from exc
        self.path = selected
        self._bind()

    def _bind(self) -> None:
        lib = self.lib

        lib.fluid_version_str.argtypes = []
        lib.fluid_version_str.restype = c_char_p

        lib.new_fluid_settings.argtypes = []
        lib.new_fluid_settings.restype = c_void_p
        lib.delete_fluid_settings.argtypes = [c_void_p]
        lib.delete_fluid_settings.restype = None
        lib.fluid_settings_setint.argtypes = [c_void_p, c_char_p, c_int]
        lib.fluid_settings_setint.restype = c_int
        lib.fluid_settings_setnum.argtypes = [c_void_p, c_char_p, c_double]
        lib.fluid_settings_setnum.restype = c_int
        lib.fluid_settings_setstr.argtypes = [c_void_p, c_char_p, c_char_p]
        lib.fluid_settings_setstr.restype = c_int

        lib.new_fluid_synth.argtypes = [c_void_p]
        lib.new_fluid_synth.restype = c_void_p
        lib.delete_fluid_synth.argtypes = [c_void_p]
        lib.delete_fluid_synth.restype = None
        lib.fluid_synth_sfload.argtypes = [c_void_p, c_char_p, c_int]
        lib.fluid_synth_sfload.restype = c_int
        lib.fluid_synth_process.argtypes = [
            c_void_p,
            c_int,
            c_int,
            POINTER(POINTER(c_float)),
            c_int,
            POINTER(POINTER(c_float)),
        ]
        lib.fluid_synth_process.restype = c_int
        lib.fluid_synth_count_audio_channels.argtypes = [c_void_p]
        lib.fluid_synth_count_audio_channels.restype = c_int
        lib.fluid_synth_count_effects_channels.argtypes = [c_void_p]
        lib.fluid_synth_count_effects_channels.restype = c_int
        lib.fluid_synth_count_effects_groups.argtypes = [c_void_p]
        lib.fluid_synth_count_effects_groups.restype = c_int
        lib.fluid_synth_set_channel_type.argtypes = [c_void_p, c_int, c_int]
        lib.fluid_synth_set_channel_type.restype = c_int
        lib.fluid_synth_system_reset.argtypes = [c_void_p]
        lib.fluid_synth_system_reset.restype = c_int

        lib.new_fluid_player.argtypes = [c_void_p]
        lib.new_fluid_player.restype = c_void_p
        lib.delete_fluid_player.argtypes = [c_void_p]
        lib.delete_fluid_player.restype = None
        lib.fluid_player_add.argtypes = [c_void_p, c_char_p]
        lib.fluid_player_add.restype = c_int
        lib.fluid_player_play.argtypes = [c_void_p]
        lib.fluid_player_play.restype = c_int
        lib.fluid_player_get_status.argtypes = [c_void_p]
        lib.fluid_player_get_status.restype = c_int
        lib.fluid_player_stop.argtypes = [c_void_p]
        lib.fluid_player_stop.restype = c_int
        lib.fluid_player_join.argtypes = [c_void_p]
        lib.fluid_player_join.restype = c_int

        lib.new_fluid_file_renderer.argtypes = [c_void_p]
        lib.new_fluid_file_renderer.restype = c_void_p
        lib.delete_fluid_file_renderer.argtypes = [c_void_p]
        lib.delete_fluid_file_renderer.restype = None
        lib.fluid_file_renderer_process_block.argtypes = [c_void_p]
        lib.fluid_file_renderer_process_block.restype = c_int

    @property
    def version(self) -> str:
        raw = self.lib.fluid_version_str()
        return raw.decode("utf-8", errors="replace") if raw else "unknown"


def fluidsynth_library_info() -> tuple[str | None, str | None]:
    path = find_fluidsynth_library()
    if path is None:
        return None, None
    try:
        binding = FluidSynthLibrary(path)
    except FluidSynthNativeError:
        return path, None
    return binding.path, binding.version


def _b(value: str | Path) -> bytes:
    return os.fsencode(os.fspath(value))


class FluidSynthSession:
    """Own one synth and one loaded SoundFont for offline rendering."""

    def __init__(
        self,
        binding: FluidSynthLibrary,
        *,
        soundfont: Path,
        samplerate: int,
        synth_gain: float,
        audio_groups: int,
        effects_groups: int,
        cpu_cores: int,
        output_file: Path | None = None,
    ):
        if audio_groups < 1 or effects_groups < 1:
            raise ValueError("FluidSynth audio/effects groups must be >= 1")
        self.binding = binding
        self.settings = binding.lib.new_fluid_settings()
        if not self.settings:
            raise FluidSynthNativeError("new_fluid_settings() failed")
        self.synth: int | None = None
        try:
            self._setnum("synth.sample-rate", float(samplerate))
            self._setnum("synth.gain", float(synth_gain))
            self._setstr("player.timing-source", "sample")
            self._setint("player.reset-synth", 0)
            self._setint("synth.lock-memory", 0)
            self._setint("audio.realtime-prio", 0)
            self._setint("synth.midi-channels", 16)
            self._setint("synth.audio-channels", int(audio_groups))
            self._setint("synth.audio-groups", int(audio_groups))
            self._setint("synth.effects-groups", int(effects_groups))
            self._setint("synth.cpu-cores", max(1, int(cpu_cores)))
            if output_file is not None:
                self._setstr("audio.file.name", os.fspath(output_file))
                self._setstr("audio.file.type", "auto")
                # Match the fluidsynth CLI/file-renderer default used before this refactor.
                self._setstr("audio.file.format", "s16")

            self.synth = binding.lib.new_fluid_synth(self.settings)
            if not self.synth:
                raise FluidSynthNativeError("new_fluid_synth() failed")
            sfid = binding.lib.fluid_synth_sfload(self.synth, _b(soundfont), 1)
            if sfid < 0:
                raise FluidSynthNativeError(f"failed to load SoundFont: {soundfont}")
        except Exception:
            self.close()
            raise

    def _setint(self, name: str, value: int) -> None:
        # FluidSynth 1.x returned boolean-like values while newer documentation
        # describes FLUID_OK/FAILED. Do not key correctness off the numeric return;
        # synth creation/count queries below validate the structural settings.
        self.binding.lib.fluid_settings_setint(self.settings, name.encode(), value)

    def _setnum(self, name: str, value: float) -> None:
        self.binding.lib.fluid_settings_setnum(self.settings, name.encode(), value)

    def _setstr(self, name: str, value: str) -> None:
        self.binding.lib.fluid_settings_setstr(self.settings, name.encode(), _b(value))

    def set_all_channels_melodic(self) -> None:
        assert self.synth is not None
        for channel in range(16):
            self.binding.lib.fluid_synth_set_channel_type(
                self.synth, channel, CHANNEL_TYPE_MELODIC
            )

    def set_channel_drum(self, channel: int) -> None:
        assert self.synth is not None
        self.binding.lib.fluid_synth_set_channel_type(
            self.synth, int(channel), CHANNEL_TYPE_DRUM
        )

    def reset(self) -> None:
        assert self.synth is not None
        if self.binding.lib.fluid_synth_system_reset(self.synth) != FLUID_OK:
            raise FluidSynthNativeError("fluid_synth_system_reset() failed")

    def new_player(self, midi_path: Path) -> int:
        assert self.synth is not None
        player = self.binding.lib.new_fluid_player(self.synth)
        if not player:
            raise FluidSynthNativeError("new_fluid_player() failed")
        if self.binding.lib.fluid_player_add(player, _b(midi_path)) != FLUID_OK:
            self.binding.lib.delete_fluid_player(player)
            raise FluidSynthNativeError(f"FluidSynth could not read MIDI: {midi_path}")
        if self.binding.lib.fluid_player_play(player) != FLUID_OK:
            self.binding.lib.delete_fluid_player(player)
            raise FluidSynthNativeError(f"FluidSynth could not play MIDI: {midi_path}")
        return player

    def finish_player(self, player: int) -> None:
        self.binding.lib.fluid_player_stop(player)
        self.binding.lib.fluid_player_join(player)
        self.binding.lib.delete_fluid_player(player)

    def close(self) -> None:
        if self.synth:
            self.binding.lib.delete_fluid_synth(self.synth)
            self.synth = None
        if self.settings:
            self.binding.lib.delete_fluid_settings(self.settings)
            self.settings = None

    def __enter__(self) -> "FluidSynthSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
