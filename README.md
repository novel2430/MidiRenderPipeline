# Midi Render Pipeline

A deterministic MIDI-to-audio rendering system with Python planning/scheduling,
persistent RAM-resident sfizz workers for SFZ sampling, embedded `libfluidsynth`
for GM fallback, and a project-native block LV2 host for effects. Single-song
and large batch renders share the same coordinator.

## Design

```text
MIDI / MIDI dataset
  -> SongPlan / StemPlan
  -> unified RenderTask state machine
     -> RAW / persistent sfizz worker
     -> RAW / embedded FluidSynth (one or many stems per physical task)
     -> FX / native LV2 chain
     -> MIX / export
  -> bounded long-running RenderingCoordinator
     -> backend-specific executor pools
     -> global worker budget
     -> FX backpressure
     -> SQLite resume journal (batch mode)
  -> WAV
```

The current renderer deliberately assumes **one musical MIDI track = one
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

GM stems are rendered through embedded `libfluidsynth`. A single GM stem uses
FluidSynth's native file renderer directly. When two or more ordinary one-channel
GM stems are present, they are grouped into batches of up to 16: each stem is
remapped to its own MIDI channel, audio group, and effects group, so one loaded
SoundFont can render the batch while default FluidSynth reverb/chorus remain
isolated per stem. The batch loop uses 1024-frame blocks. FluidSynth currently
uses one internal synthesis core. `--concurrency` is the coordinator's global
task budget (`--jobs` remains a single-file compatibility alias) and is
intentionally not reused as `synth.cpu-cores`. A rare pipeline track that
itself uses multiple MIDI channels takes the same native single-stem file-renderer
path rather than collapsing its MIDI channel state.

## Rendering system (v0.5)

`midi-render render` and `midi-render batch` now use the same long-running task
engine. Planning remains song-scoped, while execution is stem-scoped. SFZ and
FluidSynth are both RAW-stage backends; FX is the next stem-transform stage. A
FluidSynth physical task may cover multiple logical stems so multi-output batching
remains an internal optimization.

Batch mode keeps only a bounded number of songs active, prioritizes MIX/FX over
new RAW work when downstream backlog grows, and journals state to SQLite for
crash/restart operation. Fingerprinted raw sampler files are reused for interrupted
songs, so a restart can continue from FX/mix rather than synthesize again.

See [`RENDERING_SYSTEM.md`](RENDERING_SYSTEM.md) for the task/state contracts,
backpressure semantics, and worker-pool design.

## Install

Python 3.11+:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

SFZ rendering uses the pinned MRP sfizz fork and the project-local
`mrp-sfizz-worker`. Build the worker with:

```bash
make native-sfizz-worker
```

The worker dynamically loads the patched `libsfizz.so`. Point MRP at that library
with `paths.sfizz_library` in `config/patches.toml`, or set `MRP_LIBSFIZZ`
(`LIBSFIZZ` is also accepted). The library must expose offline render API v1 and
the 64-bit memory-accounting extension. `midi-render doctor` checks the worker,
library, protocol, and API before production use.

All current LV2 effects use the project-native `mrp-lv2-chain` host with
project-local Guitarix bundles under `resources/fx/lv2/`. One effected stem
launches one helper process for its entire ordered chain; audio is read and
written in 1024-frame blocks and intermediate stages stay in memory instead of
being written as temporary WAV files. The supported production scope is the
audio/control-port style used by the current Guitarix effects. An effect may
explicitly set `backend = "lv2apply"` as a legacy compatibility path for a
plugin that needs host behaviour outside that scope.

Current project-local bundles include:

- `gx_ampegsvt.lv2`
- `gx_blueamp.lv2`
- `gx_clubdrive.lv2`
- `gx_plexi.lv2`
- `gx_ultracab.lv2`

Build the native host after installing Lilv/libsndfile development packages.
On Void Linux:

```bash
sudo xbps-install -S base-devel pkg-config lilv-devel libsndfile-devel
make native-lv2
# or build both project-local native helpers:
make native
```

This produces `resources/tools/mrp-lv2-chain`, which is intentionally a local
build artifact. `midi-render doctor` reports whether it can be found.

The final GM fallback requires the FluidSynth shared library (`libfluidsynth`)
and `resources/instruments/MuseScore_General_Full.sf2`. The pipeline calls the
C library directly through a small built-in Python `ctypes` binding; the
`fluidsynth` CLI executable is not used. `midi-render doctor` checks both the
SoundFont and `libfluidsynth`. `FLUIDSYNTH_LIBRARY=/path/to/libfluidsynth.so`
can be used when the library is installed outside the normal loader search path.

The production path does not require `lv2apply`. Lilv and libsndfile are linked
into `mrp-lv2-chain`; `lv2apply` remains useful only for the explicit legacy
backend and for `tools/bench_mrp_lv2_chain.py` reference benchmarks.

For project-local effects the native host receives
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

For an A/B performance or numerical comparison against legacy `lv2apply`, keep
Lilv's CLI tools installed and run, for example:

```bash
python tools/bench_mrp_lv2_chain.py "$RAW" --effects gxsvt --repeat 3
python tools/bench_mrp_lv2_chain.py "$RAW" --effects gxsvt bass_room --repeat 3
```

The production default is block size 1024. On the tested `gxsvt` and
`gxsvt -> bass_room` chains this reached roughly 13x the legacy `lv2apply`
throughput while remaining numerically very close to the reference.

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

Long-running resumable dataset render:

```bash
midi-render batch /path/to/midi-set \
  --output-dir renders/final-batch \
  --work-root renders/work-batch \
  --state-db renders/render-state.sqlite3 \
  --active-songs 32 \
  --concurrency 5
```

Completed outputs are skipped on restart. The resumable song identity includes the
source MIDI contents, so editing a MIDI in place automatically creates a new job.
Use `--retry-failed` to retry failed songs or `--force` to ignore persisted
DONE/FAILED state. Batch outputs preserve the input directory tree below
`--output-dir`.

### Persistent SFZ working set

SFZ tasks use persistent process/Synth workers keyed by instrument identity. The
patched sfizz backend keeps normal sample preloads, then synchronously promotes a
sample to full residency on first touch. This preserves deterministic offline
output without decoding the whole instrument up front. By default an instrument
has one resident worker; `--sfz-max-replicas N` allows warmed instruments to
scale out to multiple independent resident workers when queue pressure and the
RAM pool permit it.

`--concurrency N` is the global simultaneous-task budget for the whole rendering
coordinator. SFZ, FX, and MIX have no smaller backend cap by default; FluidSynth
uses a conservative automatic process fan-out. Backend-specific concurrency
flags remain available as advanced overrides. `--sfz-memory-budget SIZE` is also
an advanced override: by default MRP derives the persistent sfizz working-set
target from host memory, learns observed high-water marks and task growth, and
trims idle least-recently-used workers when needed. Busy workers are never killed.

### Rendering logs

Rendering commands use one coordinator-owned console logger. Backend processes and
native libraries do not write progress directly to the terminal. Successful
persistent sfizz-worker, FluidSynth, and LV2 diagnostics are captured and hidden by default;
`--debug` exposes them. This also keeps headless FluidSynth ALSA/JACK/SDL warnings
out of normal long batch runs while preserving them for diagnosis.

Color is automatic on a TTY and can be controlled explicitly:

```bash
midi-render render song.mid --color always
midi-render batch /data/midi --color never
NO_COLOR=1 midi-render batch /data/midi
```

Three console levels are available:

- default: compact task lines for a single song and a low-noise batch heartbeat;
- `--verbose`: planning, cache, task start/done, scheduler details;
- `--debug`: captured backend stdout/stderr in addition to verbose output.

Batch progress and the final summary report three throughput views. `songs/min`
is the human-friendly corpus rate. `track× realtime` divides completed source
track-seconds by wall time, normalizing for both song duration and rendered track
count. `ms / track-bar` divides wall time by completed rendered track-bars; bars
follow the MIDI time-signature timeline, and derived stems such as the drum kick
layer do not count as additional source tracks. The SFZ summary reports peak
working-set/sample payload values rather than end-of-run current values, because
persistent workers are closed before the final summary.

For long experiments, `--log-file path.log` mirrors human-readable console events
without ANSI escapes and `--json-log events.jsonl` records structured events for
post-run analysis. Batch heartbeat frequency is controlled by `--heartbeat SECONDS`
(default 5).

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
GM/FluidSynth routes have distinct cache names. Raw cache fingerprints are based
on the **actual prepared MIDI bytes handed to the renderer**, plus the selected
SFZ/SoundFont identity and renderer settings (`blocksize`, `samplerate`, `quality`,
`polyphony` for sfizz; `synth_gain` and `samplerate` for embedded FluidSynth).
Therefore a change to velocity adaptation, CC64/CC1 handling, note timing,
track-splitting, note filtering, or program remapping invalidates the raw cache
whenever it changes the renderer input, without requiring a separate transform
version bump.

Patch `gain_db` and LV2 effect settings are deliberately excluded from the raw
fingerprint. The current effect chain is always run again, so tone/mix tuning does
not trigger another sampler pass. The default
single-track output is `renders/final/<song>.track-06.wav` and does not overwrite the
full-song mix.

If the raw stem does not exist yet, the selected track is rendered by the
instrument-affine persistent sfizz pool and then cached for later tuning runs.
Within one MRP run, the same SFZ stays RAM-resident and is reused across songs
until the SFZ memory budget evicts its idle worker. Use `--rebuild-raw` to ignore matching
raw cache files. In batch mode, combine `--force --rebuild-raw` when DONE songs
must also be re-planned and re-synthesized. Cache files created under older raw
cache schemas are intentionally not reused.

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
