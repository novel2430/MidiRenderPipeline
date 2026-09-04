# Midi Render Pipeline

MidiRenderPipeline (MRP) is a deterministic MIDI-to-audio rendering system for
single songs and large resumable datasets. Version **0.5.0** uses one shared
long-running coordinator for both modes, persistent RAM-resident sfizz workers
for SFZ instruments, embedded `libfluidsynth` for GM fallback, and a
project-native LV2 chain host for effects.

## Current architecture

```text
MIDI / MIDI dataset
  -> analyze + resolve instruments
  -> SongPlan / StemPlan
  -> RenderingCoordinator
       RAW -> FX -> MIX
       |      |     |
       |      |     +-- mixer/export thread pool
       |      +-------- mrp-lv2-chain
       +--------------- persistent sfizz pool
       +--------------- embedded FluidSynth process pool
  -> WAV
```

Planning is song-scoped and execution is stem-scoped. `midi-render render` and
`midi-render batch` use the same coordinator, task graph, renderer contracts,
and raw-cache rules. Batch mode adds bounded song admission and SQLite resume
state; it is not a wrapper around repeated single-song renderer processes.

See [`RENDERING_SYSTEM.md`](RENDERING_SYSTEM.md) for scheduler, cache, memory,
resume, failure, and logging contracts.

## Rendering assumptions and instrument resolution

MRP deliberately treats **one musical MIDI track as one logical instrument**.
This is an operational contract rather than a limitation of the MIDI standard.
The analyzer warns when a track contains multiple programs or multiple note
channels so exceptions remain visible.

Instrument resolution is **Program-first**:

1. channel-10 percussion is the hard drum special case;
2. one usable GM Program Change is the primary timbre identity;
3. track name is fallback metadata when no single usable Program is available;
4. `Melody` is a dataset role whose rendering policy is configured separately.

A Program/name disagreement is therefore not itself an error. For example, GM
Program 29 resolves to `electric_guitar_overdrive` and Program 30 resolves to
`electric_guitar_distortion` regardless of a generic guitar track label.

Rendering coverage is resolved in this order:

```text
exact dedicated SFZ
  -> configured family/shared SFZ
  -> MuseScore General Full through embedded FluidSynth
```

The original single Program Change is preserved for GM fallback when it is
trustworthy. Otherwise the registry may supply a representative GM program for
the resolved canonical instrument.

## Install

Python 3.11+:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Native helpers

Build both project-local native helpers:

```bash
make native
```

Or separately:

```bash
make native-sfizz-worker
make native-lv2
```

`make native-sfizz-worker` builds:

```text
resources/tools/mrp-sfizz-worker
```

`make native-lv2` builds:

```text
resources/tools/mrp-lv2-chain
```

The LV2 host requires Lilv, libsndfile, and LV2 development headers. On Void Linux:

```bash
sudo xbps-install -S base-devel pkg-config lilv-devel libsndfile-devel lv2-devel
make native-lv2
```

## SFZ runtime

Production SFZ rendering does **not** use the `sfizz_render` CLI. MRP launches
project-local `mrp-sfizz-worker` processes which dynamically load the pinned MRP
sfizz fork.

The current fork is based on sfizz **1.2.3** and MRP requires its persistent
offline extension API **v3 or newer**. The worker protocol used by MRP 0.5.0 is
protocol 5. `midi-render doctor` probes the actual worker/library pair before a
production run.

Point MRP at the patched library with either:

```toml
[paths]
sfizz_library = "/path/to/libsfizz.so"
```

or an environment variable:

```bash
export MRP_LIBSFIZZ=/path/to/libsfizz.so
```

`LIBSFIZZ` is accepted as a compatibility fallback.

Each resident sfizz worker owns one process, one Synth, and one loaded SFZ. The
fork retains normal sample preloads and synchronously promotes a touched sample
to full residency on first use. Each render begins from the offline baseline and
uses deterministic task seed 0.

A warmed `InstrumentKey` can own multiple independent worker replicas. The
maximum is controlled by `--sfz-max-replicas`; the default is 1. A cold key must
complete one render before it may scale out, allowing MRP to learn a real
working-set footprint before admitting another copy.

## FluidSynth runtime

GM fallback uses `libfluidsynth` directly through the project Python binding;
the `fluidsynth` CLI executable is not used.

- one GM stem uses FluidSynth's native file renderer;
- two or more ordinary one-channel GM stems from a song may be packed into one
  physical render task, up to 16 stems per batch;
- each packed stem receives an independent MIDI channel, audio group, and effects
  group;
- a source track that itself uses multiple MIDI channels takes the native
  single-stem compatibility path;
- FluidSynth's internal synthesis core count remains 1.

The fallback SoundFont is currently:

```text
resources/instruments/MuseScore_General_Full.sf2
```

Use `FLUIDSYNTH_LIBRARY=/path/to/libfluidsynth.so` when the shared library is
outside the normal dynamic-loader search path.

## LV2 effects

The default effect backend is the project-native `mrp-lv2-chain` host. One
helper process receives a stem's complete ordered chain; audio is processed in
1024-frame blocks and intermediate stages remain in memory.

The production host is a focused headless/offline LV2 host rather than a DAW.
It supports audio/control ports plus empty Atom Sequence ports, URID map/unmap,
instantiation-time LV2 options, and synchronous LV2 Worker execution. This is
enough for the existing Guitarix effects and DPF effects such as Dragonfly
Reverb without adding UI, transport, MIDI, automation, or state-restore logic.
An individual effect may explicitly select `backend = "lv2apply"` as a legacy
compatibility path; there is no silent runtime fallback.

The base plugin discovery root remains:

```text
resources/fx/lv2
```

For a configured effect whose bundle lives elsewhere, the bundle's parent
directory is appended to the helper's `LV2_PATH`. This permits the existing
`resources/fx/dragonfly-reverb-3.2.10/*.lv2` layout without moving resources.
Project-local roots are always searched ahead of unrelated user-installed
copies.

Effects may declare `tail_seconds = N`. The native helper then renders exactly
that much additional zero-input audio after source EOF so reverb/delay tails are
not truncated. For a serial chain, configured tail durations are summed. A zero
or omitted tail preserves the previous output duration exactly. The legacy
`lv2apply` backend rejects non-zero `tail_seconds` rather than silently dropping
the tail.

FX routing is logical-stem scoped rather than sampler-backend scoped. Patch-local
`effects = [...]` remain the source/tone chain and run first; optional
`[post_effects]` entries are appended afterwards when the `StemPlan` is built.
The coordinator executes only `StemPlan.effects`, so sfizz, FluidSynth GM,
derived stems, and future RAW backends all share the same post-FX path.

```toml
[post_effects]
drums = ["dragonfly_room"]
synth_lead = ["dragonfly_plate"]
melody = ["dragonfly_plate"]
```

Unlisted instruments receive no additional post FX. In the checked configuration the main drum
stem receives Dragonfly Room while the derived `drums_kick_layer` intentionally
remains dry; GM synth lead and GM Melody receive Dragonfly Plate. Dragonfly
Room/Plate controls are written explicitly in the config rather than depending
on LV2 preset/state restoration.

`tools/bench_mrp_lv2_chain.py` remains available for A/B benchmarking against
legacy `lv2apply`.

## Commands

Check resources and native runtime discovery:

```bash
midi-render doctor
```

Inspect one MIDI:

```bash
midi-render inspect song.mid
```

Scan a MIDI file/directory for required instruments:

```bash
midi-render scan /path/to/midi-set
```

Render one song:

```bash
midi-render render song.mid
```

Render one source track for patch/effect tuning:

```bash
midi-render render song.mid --track 6
```

Long-running resumable dataset render:

```bash
midi-render batch /path/to/midi-set \
  --output-dir renders/final-batch \
  --work-root renders/work-batch \
  --state-db renders/render-state.sqlite3 \
  --active-songs 24 \
  --concurrency 24 \
  --include-melody \
  --sfz-max-replicas 2
```

### Primary execution controls

```text
--concurrency N       global simultaneous task budget (default 5)
--active-songs N      maximum planned songs resident in batch mode (default 32)
--sfz-max-replicas N  maximum resident replicas for one SFZ InstrumentKey (default 1)
```

For normal use these are the important controls. Backend capacities resolve
automatically inside the global budget:

- SFZ: up to `--concurrency`;
- FX: up to `--concurrency`;
- MIX: up to `--concurrency`;
- GM/FluidSynth: conservative automatic process fan-out, currently
  `min(concurrency, min(4, max(1, concurrency // 4)))`.

Advanced overrides:

```text
--sfz-concurrency N
--gm-concurrency N
--fx-concurrency N
--mix-concurrency N
--sfz-memory-budget SIZE
--max-fx-backlog N
```

Compatibility aliases remain accepted:

```text
render: --jobs -> --concurrency
batch:  --workers -> --concurrency
backend --*-workers -> corresponding --*-concurrency
```

An advanced backend value larger than `--concurrency` does not create extra
global execution slots.

## SFZ memory admission

Execution concurrency and sampler memory are separate controls.

With `--sfz-memory-budget` omitted, the pool derives a steady-state target from
host memory:

```text
min(70% of physical RAM, MemAvailable - 1 GiB)
```

with a 512 MiB floor.

Workers report actual sfizz-managed working-set and sample-residency statistics
after load/render. MRP keeps historical per-instrument observations, reserves
known positive task growth before reuse/scale-out, and evicts only **idle**
workers by global LRU when needed. Busy workers are never killed. The memory
budget is therefore an admission/steady-state target, not a hard process RSS
limit.

## Scheduling and backpressure

The coordinator has one global in-flight ceiling and prioritizes downstream
completion:

```text
MIX > FX > RAW
```

When queued/running FX reaches the resolved `--max-fx-backlog` threshold, new
RAW dispatch is temporarily withheld. Already-running RAW work may finish. The
default threshold is `max(concurrency * 2, 4)`.

Batch planning is bounded by `--active-songs`: completing or failing one song
admits the next input rather than expanding the entire dataset into memory.

## Resume state and raw cache

Batch state is journaled in SQLite/WAL. The source MIDI content hash is part of
the song identity, so editing a file in place creates a new logical job.

On restart:

- existing `DONE` outputs are skipped;
- interrupted/RUNNING songs are re-planned;
- `FAILED` songs stay skipped unless `--retry-failed` is supplied;
- `--force` ignores persisted DONE/FAILED state and re-plans inputs.

Raw stems are the expensive-stage cache boundary. Cache fingerprints hash the
**actual prepared MIDI bytes** handed to the renderer after split/filter,
velocity adaptation, controller/timing preservation, and GM program remapping.

Current raw cache schemas are:

```text
SFZ: raw-sfz-v3
GM:  raw-gm-v4
```

SFZ cache identity also includes the persistent renderer contract, worker
protocol, offline API/sample-loading contract, deterministic seed, worker
binary/library identities when available, selected SFZ content identity, and
core renderer settings.

Patch `gain_db`, LV2 parameters, and the master stage remain downstream of the
raw cache boundary, so tone/mix tuning can reuse sampler output.

Use:

```bash
--rebuild-raw
```

to bypass matching raw artifacts. For an already-DONE batch song that must be
fully resynthesized, use both:

```bash
--force --rebuild-raw
```

## Logging and throughput metrics

The coordinator is the only owner of user-facing rendering progress. Successful
sfizz, FluidSynth, and LV2 diagnostics are captured rather than interleaved into
the terminal.

```text
--verbose              planning/cache/task details
--debug                captured backend diagnostics
--color auto|always|never
--log-file FILE        ANSI-free human-readable log
--json-log FILE        structured JSONL event log
--heartbeat SECONDS    batch refresh interval (default 5)
```

`NO_COLOR` is respected with `--color auto`.

Batch summaries report complementary end-to-end metrics:

- `songs/min`;
- `track× realtime`;
- `ms / track-bar`.

These include planning, synthesis, FX, mixing, cache hits, and scheduler overhead
in wall-clock time.

## Resource layout

```text
resources/
  instruments/       third-party SFZ/SF2 libraries, kept intact
  fx/
    GxPlugins.lv2/   Guitarix source trees when retained locally
    lv2/             compiled project-local LV2 bundles
  tools/
    mrp-sfizz-worker local build artifact
    mrp-lv2-chain    local build artifact
```

Do not flatten SFZ libraries into a shared sample folder; their `.sfz` files
commonly rely on relative sample paths.

The patch registry separates library roots from exact patches. Instrument,
family fallback, GM fallback, performance-adapter, effect, drum-layer, and master
policy all live in `config/patches.toml` rather than Python code.

## Current patch policy

The checked-in registry currently selects, among others:

- piano -> Accurate Salamander Grand Piano;
- acoustic guitar -> MDM Acoustic Guitar;
- GM 26 Jazz Guitar -> FSBS Jazz;
- GM 27 clean electric guitar -> configured FSBS clean source;
- GM 29 overdriven guitar -> FSBS Distorted #1;
- GM 30 distortion guitar -> FSBS Distorted #2;
- bass -> Fashionbass + GxSVT;
- drums -> SM Drums with optional Muldjord kick reinforcement;
- orchestral strings/brass/core woodwinds/choir/harp/timpani/vibraphone -> VPO;
- remaining covered GM programs -> MuseScore General Full through FluidSynth.

Program-specific FSBS electric-guitar patches contain their intended baked tone
and currently have no extra LV2 chain. Bass retains the configured GxSVT chain.

The current Melody policy is explicit GM fallback:

```toml
[melody]
mode = "gm"
gm_program = 71
```

## Performance Adapter

The sampler-input Performance Adapter is enabled only for instruments that have
an explicit profile in `config/patches.toml`.

For constant-like tracks, positive note-on velocities are rewritten to the
profile's nominal value. Dynamic tracks use minimal intervention based on the
configured robust percentile range:

1. keep the source unchanged if the robust range is already safe;
2. shift without rescaling when the contour fits but is offset;
3. compress only when the source dynamic span is wider than the target span.

Dynamic velocity is never expanded.

This keeps responsibilities separate:

```text
resolver                 -> which instrument
Performance Adapter      -> playing intensity semantics
patch gain / mixer/master -> mix loudness
```

Unprofiled instruments keep source MIDI velocity unchanged.

## Master output

Full-song rendering uses the checked-in master configuration:

```toml
[master]
normalize_peak_db = -1.0
gain_db = -10.0
```

The mixer peak-normalizes first and applies master gain second. `render --track`
intentionally bypasses the full-song master stage so patch/effect tuning remains
directly audible.

## Testing

```bash
make test
# equivalent to:
python -m pytest -q
```

The test suite covers CLI policy, instrument resolution, MIDI transforms,
patch/config parsing, cache identity, coordinator behavior, logging, effects,
mixing, and persistent sfizz pool semantics.
