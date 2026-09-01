# Midi Render Pipeline

A small, deterministic MIDI-to-audio pipeline built around the proven
`sfizz_render` command-line process workflow.

## Design

```text
MIDI
  -> track analyzer
  -> instrument resolver
  -> patch registry (config/patches.toml)
     -> exact dedicated SFZ
     -> family/shared SFZ fallback
     -> MuseScore General SF2 / FluidSynth fallback
  -> parallel renderer subprocesses
  -> declarative per-patch effect chains
     -> project-local Guitarix LV2 via lv2apply
  -> stem gains + mix + peak normalization
  -> WAV
```

The current pipeline deliberately assumes **one musical MIDI track = one
logical instrument**. This is an operational contract, not a claim about the
MIDI standard. The analyzer warns when a track contains multiple programs or
note channels so exceptions are visible.

Instrument resolution is now strictly **Program-first**. When a track contains
exactly one GM Program Change, that Program is the primary timbre identity even
when the track name says something different. Track names are fallback metadata
only, used when there is no single usable Program. Program/name conflicts are
therefore not treated as errors or warnings. In particular, Program 29 resolves
to `electric_guitar_overdrive` and Program 30 to
`electric_guitar_distortion` regardless of the track label. `Melody` remains a
dataset role, while channel-10 percussion remains the hard drum special case.

Rendering uses three coverage layers: an exact dedicated patch first, then an
explicitly configured family/shared patch, and finally MuseScore General Full
through FluidSynth. A single source Program Change is preserved for the GM
fallback. If no single Program is available, the registry may provide a
representative GM program for the name-derived canonical instrument instead.

## Install

Python 3.11+:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

`pysfizz` is a project dependency. Its wheel ships the `sfizz_render` helper
under `site-packages/bin/`; Midi Render Pipeline discovers that location
automatically, so `sfizz_render` does not need to be manually symlinked into
`PATH`.

All current LV2 effects use Lilv's native `lv2apply` with project-local
Guitarix bundles under `resources/fx/lv2/`:

- `gx_ampegsvt.lv2`
- `gx_blueamp.lv2`
- `gx_clubdrive.lv2`
- `gx_plexi.lv2`
- `gx_ultracab.lv2`


The final GM fallback additionally requires `fluidsynth` on `PATH` and
`resources/instruments/MuseScore_General_Full.sf2`. `midi-render doctor` checks
both the SoundFont and configured FluidSynth command.

`lv2apply`, `lv2ls`, and `lv2info` are provided by Lilv's command-line package.
On Void Linux:

```bash
sudo xbps-install -S lilv
```

For project-local effects the renderer sets
`LV2_PATH=resources/fx/lv2` and invokes plugins by their stable LV2 URI. This
keeps runtime discovery tied to the project-local build even when an older
copy of the same Guitarix plugin exists under `~/.lv2`.

## Build the project-local LV2 bundles

After cloning/moving `GxPlugins.lv2` into `resources/fx/`, build the selected
bass/guitar effects into the runtime directory:

```bash
cd resources/fx/GxPlugins.lv2
DEST="$PWD/../lv2"
mkdir -p "$DEST"

for p in GxSVT.lv2 GxBlueAmp.lv2 GxClubDrive.lv2 GxPlexi.lv2 GxUltraCab.lv2; do
    make -C "$p" clean
    make -C "$p" -j"$(nproc)"
    make -C "$p" install INSTALL_DIR="$DEST"
done
```

Then `midi-render doctor` should report all configured effect entries as `OK`
(the two UltraCab presets intentionally point at the same bundle).

## Commands

Check resources and renderer discovery:

```bash
midi-render doctor
```

Inspect one MIDI:

```bash
midi-render inspect song.mid
```

Scan a set:

```bash
midi-render scan /path/to/midi-set
```

Render:

```bash
midi-render render song.mid
```

By default `Melody` is excluded. When `--include-melody` is used, its rendering
source is controlled by `[melody]` in `config/patches.toml`. The current shipped
config pins Melody to GM program 71 for stable comparison renders:

```toml
[melody]
mode = "gm"
gm_program = 71
```

To let each Melody track's own GM Program choose among installed dedicated,
family, and GM sources, switch to `auto`:

```toml
[melody]
mode = "auto"
```

In `gm` mode, omit `gm_program` to preserve a trustworthy source Program when
present, or set it explicitly to force a stable MuseScore GM timbre.

Or force one canonical instrument regardless of the source Program:

```toml
[melody]
mode = "instrument"
instrument = "flute"
```

The forced instrument still uses the normal dedicated -> family -> GM policy.
If its dedicated source is unavailable, the representative GM Program for that
instrument is used rather than the Melody track's original Program.

For exploratory rendering while the patch library is incomplete:

```bash
midi-render render song.mid --skip-unconfigured
```

Keep generated split MIDI files:

```bash
midi-render render song.mid --keep-work
```

## Resource layout

```text
resources/
  instruments/   # third-party SFZ/SF2 libraries, kept intact
  fx/
    GxPlugins.lv2/ # Guitarix plugin source trees
    lv2/           # project-local compiled LV2 bundles used at runtime
  tools/           # optional helper tools; no current LV2 runtime dependency
```

Do not flatten an SFZ library into a shared sample directory. Keep each
third-party library intact because SFZ files commonly use relative sample paths.

The registry separates **library roots** from **patches**. A library is a folder
on disk; a patch is one exact SFZ selected from that folder. Adding/replacing an
instrument normally requires editing only `config/patches.toml`, not Python.

## Current patch policy

The registry prefers the dedicated sources already present in
`resources/instruments/`:

- piano -> Accurate Salamander Grand Piano
- steel/nylon acoustic guitar family -> MDM Acoustic Guitar
- GM 26 Jazz Guitar -> FSBS Jazz
- GM 27 Clean Guitar -> the configured `electric_guitar_clean` baked source
- GM 29 Overdriven Guitar -> FSBS Distorted #1
- GM 30 Distortion Guitar -> FSBS Distorted #2
- bass -> Fashionbass + GxSVT
- drums -> SM Drums, with optional Muldjord kick reinforcement
- orchestral strings / brass / core woodwinds / choir / harp / timpani / vibraphone -> VPO

VPO uses simple sustain/pizzicato/tremolo/open/hit programs where appropriate,
while string ensemble, cello, flute, and clarinet currently use the
`normal-mod-wheel` variants. The pipeline preserves controller events already in
the source MIDI but does not synthesize CC1/mod-wheel automation, so those patches
use source CC1 when present and otherwise fall back to the SFZ's default controller
state.

`[family_fallbacks]` intentionally allows several GM instruments to share a good
dedicated source. The starter policy maps muted/harmonics guitar to the clean
guitar patch and muted trumpet to the normal VPO trumpet. Exact patches always
win over family mappings.

Anything still uncovered goes to `[general_midi_fallback]`, currently
`MuseScore_General_Full.sf2` via FluidSynth. This makes unconfigured synth pads,
leads, organs, saxophones, ethnic instruments, sound effects, and other GM
programs render instead of being skipped. `--skip-unconfigured` remains useful
only for tracks that have neither a usable dedicated/family source nor a safe GM
fallback program.

Effect chains remain data-driven: each SFZ patch lists effect names. GM fallback
stems do not receive an implicit LV2 chain; they use the fallback `gain_db` and
then enter the normal mixer/master path.

### Fast single-track tuning

To render only one MIDI track using the current `config/patches.toml` settings:

```bash
midi-render render song.mid --track 6
```

For this single-track path, an existing raw renderer stem under
`renders/work/<song>/stems/` is reused automatically. Exact SFZ, family SFZ, and
GM/FluidSynth routes have distinct cache names. Raw cache names also contain a
render fingerprint covering the source MIDI, selected SFZ/SoundFont identity, and
sampler settings (`blocksize`, `samplerate`, `quality`, `polyphony` for sfizz;
`tool`, `synth_gain`, and `samplerate` for FluidSynth). Changing any of those
inputs invalidates the raw cache instead of silently reusing an incompatible stem.

Patch `gain_db` and LV2 effect settings are deliberately excluded from the raw
fingerprint. The current effect chain is always run again, so tone/mix tuning does
not trigger another sampler pass. The default
single-track output is `renders/final/<song>.track-06.wav` and does not overwrite the
full-song mix.

If the raw stem does not exist yet, the selected track is rendered with `sfizz_render`
once and then cached for later tuning runs. Cache files created before the render
fingerprint was introduced are intentionally not reused.

## Master output

Full-song renders use the single master configuration in `config/patches.toml`:

```toml
[master]
normalize_peak_db = -1.0
gain_db = -10.0
```

The mixer first peak-normalizes to `normalize_peak_db`, then applies `gain_db`.
Selected-track renders (`render --track N`) intentionally bypass the master stage so tone and patch gain remain directly audible while tuning.

## Performance Adapter

Generic MIDI files often use velocity partly as a track-level loudness knob,
while detailed sample libraries interpret velocity as playing intensity and may
switch sample layers. That mismatch can make the same renderer config sound very
different across MIDI sources.

v0.3.11 adds a sampler-input Performance Adapter configured in `patches.toml` (v0.3.12 changes the dynamic-track policy to minimal intervention):

```toml
[performance]
enabled = true
constant_spread_max = 4.0
low_percentile = 0.10
high_percentile = 0.90
[performance.instruments.electric_bass]
velocity_min = 50
velocity_nominal = 60
velocity_max = 65
```

For a constant-like track, all positive note-on velocities are rewritten to the
instrument's nominal value. Dynamic tracks use minimal intervention based on the
robust p10/p90 range: if that range is already inside the configured performance
range the velocities are left untouched; if the contour fits but sits too high or
too low it is shifted without rescaling; only a contour wider than the target
range is compressed. Dynamic velocity is never expanded.

This deliberately separates responsibilities:

- MIDI Program / resolver: which instrument to render;
- Performance Adapter: how strongly that instrument is played;
- patch `gain_db` / mixer: how loud that instrument sits in the mix.

Only instruments with an explicit `[performance.instruments.<name>]` profile are
adapted. Unprofiled instruments keep the source MIDI velocity unchanged.
