# Moving the current resources into MidiRenderPipeline

From the old `MidiRenderTest/Res` directory, move sample libraries intact into
`resources/instruments/`:

```bash
mv AccurateSalamenderGrandPiano  /path/to/MidiRenderPipeline/resources/instruments/
mv GeneralUser-GS                /path/to/MidiRenderPipeline/resources/instruments/
mv GiantSoundFonts               /path/to/MidiRenderPipeline/resources/instruments/
mv Growlybass                    /path/to/MidiRenderPipeline/resources/instruments/
mv 'MDM Acoustic Guitar v1.0 F'  /path/to/MidiRenderPipeline/resources/instruments/
mv 'MDM Acoustic Guitar v1.0 WAV' /path/to/MidiRenderPipeline/resources/instruments/
mv SM_Drums                      /path/to/MidiRenderPipeline/resources/instruments/
mv UI_Standard_Guitar            /path/to/MidiRenderPipeline/resources/instruments/
mv Virtual-Playing-Orchestra3    /path/to/MidiRenderPipeline/resources/instruments/
mv MuseScore_General_Full.sf2    /path/to/MidiRenderPipeline/resources/instruments/
```

Move the Guitarix plugin source tree separately:

```bash
mv GxPlugins.lv2  /path/to/MidiRenderPipeline/resources/fx/
```

`Plugalyzer` is no longer required for the current runtime path.

Do **not** move these old project/output folders into `resources/`:

- `renders/`
- `scripts/`
- `smoke/`
- `test/`
- `C_RENDER_TEST_STATUS.md`

The starter `config/patches.toml` expects the library folder names above. After
moving, run:

```bash
midi-render doctor
```

`pysfizz` is installed with the Python project and its bundled `sfizz_render`
is discovered automatically even though the executable lives under
`site-packages/bin/`.

Bass and electric-guitar effects now use project-local builds. Compile the
selected plugins into `resources/fx/lv2/`:

```bash
cd /path/to/MidiRenderPipeline/resources/fx/GxPlugins.lv2
DEST="$PWD/../lv2"
mkdir -p "$DEST"

for p in GxSVT.lv2 GxBlueAmp.lv2 GxClubDrive.lv2 GxPlexi.lv2 GxUltraCab.lv2; do
    make -C "$p" clean
    make -C "$p" -j"$(nproc)"
    make -C "$p" install INSTALL_DIR="$DEST"
done
```

Install Lilv's command-line tools (`lv2apply`, `lv2ls`, `lv2info`). On Void
Linux:

```bash
sudo xbps-install -S lilv
```

At runtime, both Bass and electric-guitar chains use `lv2apply` with
`LV2_PATH=resources/fx/lv2` and stable LV2 plugin URIs. This avoids relying on
Plugalyzer/JUCE bundle-path scanning and keeps discovery tied to the
project-local Guitarix builds even if older duplicates remain under `~/.lv2`.
GxSVT's built-in cabinet remains enabled, matching the old tested Bass tone.

## Resolver policy update (v0.3.4)

Instrument resolution now prefers a single recognized GM Program Change when
it is compatible with the track's semantic label. Generic `Egt` labels allow
Program Change to select acoustic / clean electric / driven electric guitar.
Clearly contradictory or stale Program values fall back to the explicit track
name and are reported as resolver warnings. `Melody` and drums remain special
cases.

## v0.3.6 master output

Final full-song output is now controlled by `[master]` in `config/patches.toml`:

```toml
[master]
normalize_peak_db = -1.0
gain_db = 0.0
```

Normalization happens first and master gain second. The old `render --headroom-db` option was removed so output-level policy has one configuration source. Single-track audition renders do not use the master stage.

## v0.3.7 dedicated / family / GM fallback

Rendering coverage is now resolved in this order:

```text
exact dedicated SFZ -> family/shared SFZ -> MuseScore General SF2
```

Install/keep the FluidSynth shared library (`libfluidsynth`) available to the
dynamic loader and keep `MuseScore_General_Full.sf2` under
`resources/instruments/`. The registry keeps
the original GM Program Change when the resolver trusts it. For stale or absent
Program Change data, `[general_midi_fallback.program_for_instrument]` supplies a
representative program, e.g. `synth_pad = 89`.

The guitar resolver is now program-specific across the GM guitar block. In the
starter registry, GM 26 uses FSBS Jazz, GM 29 uses FSBS Distorted #1, and GM 30
uses FSBS Distorted #2. Muted/harmonics guitar may share the configured clean
guitar patch through `[family_fallbacks]`. Additional VPO cello, contrabass,
tremolo/pizzicato strings, trumpet/trombone/tuba, oboe/English horn/bassoon/
clarinet/piccolo patches are used before the GM SoundFont fallback.

Run:

```bash
midi-render doctor
```

to check `sfizz_render`, `libfluidsynth`, the MuseScore SoundFont, and all
configured dedicated/family sources.

## v0.3.8 melody patch-first fallback

When `--include-melody` is used, Melody no longer jumps straight to the GM
SoundFont. Its Program Change is now first translated to the same canonical
instrument families used elsewhere (for example flute / violin / piano), so the
renderer can reuse any installed dedicated or family patch before falling back
to MuseScore GM. If a Melody track has no usable Program Change, the configured
representative GM program for `melody` is used (the starter config sets this to
80 / synth lead).

## v0.3.9 configurable Melody source

`[melody]` now selects how an included Melody role is rendered. `mode = "auto"`
preserves v0.3.8 behavior: derive a canonical instrument from the source GM
Program and try dedicated/family patches before GM. `mode = "gm"` bypasses all
dedicated patches and optionally accepts `gm_program = 0..127`.
`mode = "instrument"` requires `instrument = "<canonical>"` and forces that
instrument through the normal dedicated -> family -> representative-GM chain.
This keeps Melody role semantics separate from timbre selection and makes
dedicated-vs-GM A/B rendering a config-only change.

## v0.3.10 strict Program-first resolver

Corpus inspection showed that conflicting track names are less reliable than the
MIDI Program Change. Resolution therefore no longer performs name/Program
compatibility checks. Channel-10 percussion is still forced to `drums`, and
`Melody` remains a dataset role, but every other track with one clear GM Program
uses that Program unconditionally. Track name is consulted only when no single
Program is available (including multi-program tracks). GM fallback now preserves
the source Program even when the textual track label disagrees with it.

## v0.3.11 - Performance Adapter

Velocity is now treated as performance control before it is treated as mix level.
The renderer can define a safe velocity operating range per canonical instrument
under `[performance.instruments.*]`.

- Constant-like tracks (`p90 - p10 <= constant_spread_max`) are rewritten to the
  instrument's `velocity_nominal`. This removes arbitrary source-MIDI conventions
  such as one bass track using velocity 100 for every note while another uses 72.
- Dynamic tracks preserve note-to-note contour. Their source median is recentered
  on `velocity_nominal`; the p10/p90 spread determines a robust scale within the
  configured `[velocity_min, velocity_max]` range.
- `max_expand_ratio` prevents a tiny source variation from being exaggerated into
  a full-range performance gesture.
- Adaptation happens in the generated split MIDI, so both sfizz and FluidSynth
  routes see the same performance translation.
- The performance profile participates in raw-stem cache names. Changing a
  velocity profile therefore forces the sampler stage to rerender while FX-only
  changes can still reuse the raw stem.

The default config enables first-pass profiles only for `electric_bass` and
`string_ensemble`, the two instrument families already observed to have strong
cross-MIDI velocity-scale mismatch in the test corpus. Other instruments remain
unchanged until their sampler ranges are calibrated.

## v0.3.12 - Minimal-intervention dynamic velocity adaptation

Dynamic velocity adaptation no longer recenters every track on
`velocity_nominal` and no longer expands narrow source dynamics. The policy is
now deliberately conservative:

1. **Preserve**: if source p10/p90 already lies inside the instrument's
   `[velocity_min, velocity_max]`, leave every source velocity unchanged.
2. **Shift**: if the robust source span fits inside the target span but lies too
   high or too low, translate the contour intact with `scale = 1`.
3. **Compress**: only when the robust source span is wider than the target span,
   shrink it to fit. Dynamic tracks are never expanded.

Constant-like tracks keep the v0.3.11 rule and are still canonicalized directly
to `velocity_nominal`. `max_expand_ratio` is no longer used; old config files may
leave that key in place harmlessly. The performance cache fingerprint is bumped
so stems rendered with the v0.3.11 mapping are not reused accidentally.

## v0.3.13 embedded multi-output FluidSynth

The GM fallback no longer launches one `fluidsynth` CLI process per track. The
Python renderer now loads `libfluidsynth` directly through a small project-local
`ctypes` binding. One SoundFont-backed synth is kept for the normal GM batch and
up to 16 one-channel fallback stems are rendered together. Each stem receives an
independent FluidSynth audio group and effects group, preserving the previous
per-track default reverb/chorus behavior without cross-stem bleed.

`--jobs` now controls FluidSynth's internal `synth.cpu-cores` setting for the GM
backend instead of the number of GM subprocesses. Tracks that themselves contain
multiple MIDI channels use the embedded FluidSynth file-renderer compatibility
path so their channel/controller semantics are not collapsed. The `tool =
"fluidsynth"` key under `[general_midi_fallback]` is removed; install the shared
library instead. `FLUIDSYNTH_LIBRARY` may point at a non-standard library path.

## v0.3.14 FluidSynth fast-path correction

The embedded FluidSynth backend keeps the v0.3.13 multi-output design, but no
longer sends singleton GM work through the Python `fluid_synth_process()` loop.
A single simple GM stem now uses FluidSynth's native file renderer directly, the
same native offline rendering path already used for multi-channel compatibility
stems. Multi-output rendering is reserved for batches containing at least two
stems. Batches larger than 16 are chunked without leaving a singleton tail.

The GM backend also stops mapping CLI `--jobs` to FluidSynth's internal
`synth.cpu-cores`: GM synthesis currently uses one core by default, because the
internal thread synchronization can cost more than it saves for light stem
workloads. The multi-output block size increases from 64 to 1024 frames, reducing
Python/ctypes/audio-writer crossings while preserving per-stem audio/effects
isolation. The raw GM cache schema is bumped again and distinguishes single-track
native renders from full-render artifacts, so a v0.3.13 batch stem cannot mask
the new single-track fast path.
