# Migration to MidiRenderPipeline 0.5.0

This file contains **upgrade notes to the current 0.5.0 runtime**, not a second
architecture manual. For current behavior, use `README.md` and
`RENDERING_SYSTEM.md` as the source of truth.

Older version-by-version implementation narratives were intentionally removed
because several intermediate contracts were later superseded (for example the
old `sfizz_render` path, pre-replica SFZ worker model, old cache schemas, and old
worker vocabulary).

## 1. Resource layout

Keep third-party sample libraries intact under:

```text
resources/instruments/
```

Typical project resources include:

```text
AccurateSalamenderGrandPiano/
MDM Acoustic Guitar v1.0 WAV/
Fashionbass/
SM_Drums/
MuldjordKit SFZ+FLAC-20201018/
Virtual-Playing-Orchestra3/
EGuitarFSBS-clean SFZ+FLAC-20260807/
EGuitarFSBS-jazz SFZ+FLAC-20260807/
EGuitarFSBS-dist1 SFZ+FLAC-20220911/
EGuitarFSBS-dist2 SFZ+FLAC-20220911/
MuseScore_General_Full.sf2
```

Do not flatten SFZ libraries: relative sample paths are part of the assets.

Project-local effects belong under:

```text
resources/fx/
resources/fx/lv2/
```

Native helper binaries are local build artifacts under:

```text
resources/tools/
```

After moving resources, run:

```bash
midi-render doctor
```

## 2. SFZ backend migration

The current production SFZ backend is no longer `pysfizz`/`sfizz_render`.
Build the project worker:

```bash
make native-sfizz-worker
```

and provide the patched MRP `libsfizz` fork through either:

```toml
[paths]
sfizz_library = "/path/to/libsfizz.so"
```

or:

```bash
export MRP_LIBSFIZZ=/path/to/libsfizz.so
```

The checked MRP fork is based on sfizz 1.2.3 and exposes persistent-offline API
v3. MRP 0.5.0 requires API v3 or newer and worker protocol 5.

Current sampler behavior:

- one resident worker = one process + one Synth + one loaded SFZ;
- deterministic-lazy sample promotion;
- deterministic task seed 0;
- workers are reused across songs;
- a warm InstrumentKey may own multiple independent replicas up to
  `--sfz-max-replicas`;
- a cold key completes one render before replica scale-out is allowed;
- idle worker replicas are the memory-eviction unit;
- busy workers are never evicted.

Do not carry forward documentation or assumptions that say an InstrumentKey is
permanently limited to one worker.

## 3. Concurrency vocabulary

Use the current canonical controls in scripts and documentation:

```text
--concurrency N
--active-songs N
--sfz-max-replicas N
```

Legacy aliases are still accepted for compatibility:

```text
render --jobs
batch  --workers
--sfz-workers
--gm-workers
--fx-workers
--mix-workers
```

but new examples should use `--concurrency` / `--*-concurrency` terminology.

Default backend policy inside the global budget:

```text
SFZ = concurrency
FX  = concurrency
MIX = concurrency
GM  = conservative AUTO, max 4 processes
```

## 4. Batch coordinator migration

Do not launch one independent renderer per song to obtain corpus parallelism.
Both `render` and `batch` now use the same long-running coordinator.

Recommended batch shape:

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

Batch scheduling priority is `MIX > FX > RAW`, with automatic FX-backlog
backpressure and bounded active-song admission.

## 5. Resume-state migration

Batch state lives in SQLite/WAL.

Current behavior:

- source MIDI content participates in song identity;
- existing DONE outputs are skipped;
- interrupted songs are re-planned and may reuse matching raw artifacts;
- failed songs require `--retry-failed` unless `--force` is used;
- `--force` bypasses persisted song state but does not itself bypass raw cache.

To force a DONE song through the sampler again:

```bash
midi-render batch ... --force --rebuild-raw
```

## 6. Raw-cache migration

Do not rely on cache files created under older schemas.

Current schemas:

```text
SFZ: raw-sfz-v3
GM:  raw-gm-v4
```

Current cache identity hashes the exact prepared MIDI bytes handed to the
renderer. This means changes to velocity adaptation, controllers, timing,
split/filter logic, or program remapping naturally invalidate a raw artifact if
they change renderer input.

The persistent SFZ identity additionally includes the renderer contract, task
seed, worker protocol, offline API/sample-loading contract, worker/library
binary identities when available, SFZ asset identity, and render settings.

## 7. FluidSynth migration

The GM backend does not launch the `fluidsynth` CLI.

Install/provide the `libfluidsynth` shared library and keep
`MuseScore_General_Full.sf2` under the configured instruments root.

The current fast path is:

- one GM stem -> native FluidSynth file renderer;
- multiple ordinary one-channel GM stems -> packed multi-output render, up to
  16 per physical task;
- source multi-channel track -> native single-stem compatibility render;
- internal FluidSynth synthesis core count -> 1.

Do not carry forward older notes that map `--jobs` to FluidSynth
`synth.cpu-cores`.

## 8. LV2 migration

The default effect path uses the project-native block host:

```toml
[effect_renderer]
backend = "native-lv2"
tool = "mrp-lv2-chain"
block_size = 1024
```

Build it with:

```bash
make native-lv2
```

One stem's complete ordered chain is processed in one helper process. The native
host now also supplies the focused URID/options/Atom/Worker subset required by
modern headless DPF effects such as Dragonfly Reverb. It does not implement a
DAW transport, UI, MIDI automation, or LV2 state restoration.

Effects that need audible decay after source EOF can set `tail_seconds`; the
helper renders that exact zero-input tail, and serial-chain tail values are
summed. Non-zero tails require the native backend. Bundle parents are appended
to `LV2_PATH`, so bundles do not need to be physically under
`resources/fx/lv2`.

`lv2apply` is now only an explicit compatibility backend or benchmark reference;
there is no automatic fallback.

FX routing is no longer inferred from the renderer patch at execution time.
`StemPlan.effects` is authoritative and combines patch-local tone effects with
logical post effects:

```toml
[post_effects]
drums = ["dragonfly_room"]
synth_lead = ["dragonfly_plate"]
melody = ["dragonfly_plate"]
```

This allows FluidSynth GM and derived stems to use the same FX stage as sfizz.
Configs without `[post_effects]` retain their previous routing. The checked
project config intentionally changes the default mix by adding room reverb to
main drums and plate reverb to synth lead/Melody; `drums_kick_layer` stays dry.
Patch-local effects still run first, so existing chains such as
`electric_bass -> gxsvt` are preserved.

## 9. Resolver and patch-policy migration

Current resolution is Program-first. A single usable GM Program Change is the
primary timbre identity; track name is fallback metadata. Program/name conflict
is not itself an error.

Coverage order:

```text
exact SFZ -> family/shared SFZ -> embedded FluidSynth GM fallback
```

Current Melody policy in the checked configuration is:

```toml
[melody]
mode = "gm"
gm_program = 71
```

If older local configs still use a different Melody mode intentionally, keep
that choice; this note describes only the current repository default.

## 10. Performance Adapter migration

The current dynamic-track policy is minimal intervention:

1. keep an already-safe robust velocity range unchanged;
2. shift it when the contour fits but is offset;
3. compress only when the source span exceeds the target span.

Dynamic tracks are never expanded. Constant-like tracks are mapped to the
instrument profile's nominal velocity.

Only explicitly configured instrument profiles are adapted. Old notes referring
to a dynamic-expansion policy or `max_expand_ratio` should not be used to explain
current behavior.

## 11. Logging migration

Rendering output is coordinator-owned.

Current controls:

```text
--color auto|always|never
--verbose
--debug
--log-file FILE
--json-log FILE
--heartbeat SECONDS
```

Successful backend stderr is captured and hidden in normal operation;
`--debug` exposes diagnostics. Failures still surface backend error text.

## 12. Version/cache identifiers in 0.5.0

These identifiers serve different compatibility domains and are **not expected
to share the package version number**:

```text
package version                 0.5.0
render-system schema            render-system-v2
prepared-MIDI contract          artifact-addressed-v1
SFZ raw schema                  raw-sfz-v3
GM raw schema                   raw-gm-v4
SFZ renderer contract           mrp-persistent-sfizz-v3
sfizz worker protocol           5
minimum sfizz offline API       3
sfizz sample-loading code       deterministic-lazy / 2
```

Only the package/public release version must match `pyproject.toml` and
`midi_render.__version__`. Cache/protocol/schema versions are internal contract
identifiers and should be bumped only when their own compatibility boundary
changes.
