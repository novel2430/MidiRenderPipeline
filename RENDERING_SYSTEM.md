# Rendering System

This document describes the **current MidiRenderPipeline 0.5.0 execution
contract**. Historical implementation details belong in `MIGRATION.md`; this file
is intentionally written as a present-tense architecture reference.

## 1. Core model

Planning is song-scoped and scheduling is stem-scoped.

```text
MIDI
  -> SongPlan
       -> StemPlan(track 1, raw_backend=sfz)
       -> StemPlan(track 2, raw_backend=fluidsynth)
       -> StemPlan(track 3, raw_backend=sfz, effects=[...])
  -> RenderTask graph
       RAW -> FX -> MIX
```

`StemPlan` is the logical unit. `RenderTask.stem_ids` is intentionally plural:

- a normal SFZ physical task produces one logical raw stem;
- a FluidSynth physical task may pack several logical one-channel GM stems from
  the same song into one multi-output render.

Backend batching therefore remains an execution optimization and does not leak
into the logical song model.

## 2. State transitions

```text
PLANNED
  -> RAW_PENDING
  -> RAW_READY
  -> FX_PENDING        only when the patch has effects
  -> PROCESSED_READY
  -> MIX_PENDING
  -> DONE
```

A stem without effects proceeds directly from `RAW_READY` to
`PROCESSED_READY`.

Raw stems are the durable expensive-stage cache boundary. FX and mix are
recomputed from cached RAW artifacts when downstream configuration changes.

## 3. Coordinator and executor pools

One long-running Python `RenderingCoordinator` owns scheduling state for both
single-song and batch rendering.

```text
                          RenderingCoordinator
                                  |
            +---------------------+--------------------+
            |                     |                    |
      persistent SFZ          GM process            FX thread
          pool                  pool                  pool
            |                     |                    |
   mrp-sfizz-worker         libfluidsynth        mrp-lv2-chain
   one Synth / worker      embedded native       one child/stem
            |
          RAM-resident SFZ                         MIX thread pool
```

### SFZ

`PersistentSfizzPool` is instrument-affine. One resident worker process owns one
Synth and one loaded SFZ and executes one task at a time.

An `InstrumentKey` includes the resolved SFZ asset identity plus block size,
sample rate, quality, and polyphony. A key may own multiple independent worker
replicas up to `--sfz-max-replicas`.

A cold key may start one worker. It must complete at least one render before the
pool may scale that same key out, so replica admission can use an observed
working-set footprint instead of guessing multiple unknown first-touch loads.

### FluidSynth

GM work runs in persistent Python worker processes for process isolation. A
physical task creates the FluidSynth synth/session it needs:

- singleton/simple GM stem -> native file renderer;
- 2..16 ordinary one-channel GM stems -> one multi-output synth/session;
- source track with multiple MIDI channels -> native single-stem compatibility
  render.

FluidSynth internal `synth.cpu-cores` remains 1.

### FX

FX tasks are supervised by a thread pool. The project-native `mrp-lv2-chain`
child process owns the complete ordered effect chain for one stem.

### MIX

Mix/export tasks use a thread pool. MIX is downstream of all required processed
stems for the song.

## 4. Global and backend concurrency

`--concurrency` is the hard global in-flight task ceiling. Backend capacities
cannot multiply beyond it.

Default policy:

```text
SFZ = concurrency
FX  = concurrency
MIX = concurrency
GM  = min(concurrency, min(4, max(1, concurrency // 4)))
```

Advanced overrides:

```text
--sfz-concurrency N
--gm-concurrency N
--fx-concurrency N
--mix-concurrency N
```

Each override is clamped to the global concurrency budget.

Compatibility aliases remain accepted by the CLI:

```text
render --jobs          -> --concurrency
batch  --workers       -> --concurrency
--*-workers            -> matching --*-concurrency
```

They are aliases only; current documentation and logs use the canonical
`concurrency` vocabulary.

## 5. Scheduling priority and backpressure

The central dispatch priority is:

```text
MIX > FX > RAW
```

The scheduler keeps finishing downstream work ahead of creating more expensive
intermediate data.

When queued/running FX reaches `--max-fx-backlog`, new RAW dispatch is withheld.
RAW tasks already in flight are allowed to finish. The automatic backlog
threshold is:

```text
max(concurrency * 2, 4)
```

This is a dispatch throttle, not a separate queue or worker count.

## 6. Bounded song admission

Batch mode never needs to materialize the whole corpus as task objects.
`--active-songs` limits the number of live song plans. Completing or failing one
song admits the next input.

Default:

```text
--active-songs 32
```

This control is independent of task concurrency. A large active-song window can
help expose enough instrument diversity and same-instrument pressure to keep
workers busy, while `--concurrency` remains the execution ceiling.

## 7. Persistent SFZ memory policy

SFZ execution capacity and SFZ resident memory are deliberately separate.

With no explicit `--sfz-memory-budget`, MRP computes:

```text
by_total     = 70% of physical RAM
by_available = max(MemAvailable - 1 GiB, 512 MiB)
budget       = max(512 MiB, min(by_total, by_available))
```

The budget is a steady-state admission target, not a strict OS RSS cap.

Each worker reports:

- sfizz-managed working-set bytes;
- resident sample bytes;
- peak sample bytes;
- number of fully resident samples;
- positive working-set growth observed during a task.

Historical observations survive worker eviction. They are used to reserve known
future growth and to decide whether a warm key can admit another replica.

Eviction unit: **worker replica**, not InstrumentKey.

Eviction rules:

- only idle workers are eligible;
- eviction is global LRU;
- busy workers are never killed;
- worker processes are closed when the coordinator closes;
- a failed/crashed worker is invalidated rather than silently reused.

## 8. Persistent SFZ worker contract

The production SFZ path is:

```text
Python coordinator
  -> mrp-sfizz-worker
  -> patched libsfizz
```

Current contract identifiers:

```text
MRP package version:       0.5.0
worker protocol:           5
minimum offline API:       3
task seed:                 0
sample-loading policy:     deterministic-lazy
renderer contract:         mrp-persistent-sfizz-v3
raw cache schema:          raw-sfz-v3
```

The pinned fork is based on sfizz 1.2.3.

Each task restores the fork's offline baseline before rendering. The production
path has no fallback to the old per-stem `sfizz_render` subprocess renderer.

Worker crash, timeout, protocol error, or render failure invalidates that
resident entry and removes a partial raw output if one was produced.

## 9. FluidSynth raw contract

Current GM raw cache schema:

```text
raw-gm-v4
```

The raw identity includes the exact prepared MIDI bytes, SoundFont identity,
render mode, synth gain, sample rate, one-core policy, and multi-output block
size where applicable.

## 10. Artifact-addressed raw cache

The prepared MIDI bytes handed to a renderer are the authoritative symbolic
cache input.

MRP hashes the final prepared MIDI after operations such as:

- track split/filtering;
- velocity adaptation;
- controller preservation;
- timing preservation;
- drum/kick note filtering;
- GM program remapping.

Any transformation that changes the renderer input bytes therefore invalidates
the raw stem without requiring a hand-maintained transform-version bump.

Downstream patch gain, effect parameters, and master settings are excluded from
the raw sampler fingerprint so those stages can be retuned without forcing
another sampler pass.

`--rebuild-raw` bypasses matching raw artifacts. In batch mode it does not by
itself bypass a persisted DONE song; use `--force --rebuild-raw` when both state
and raw cache must be ignored.

## 11. Batch SQLite resume journal

Batch mode defaults to:

```text
renders/render-state.sqlite3
```

SQLite runs in WAL mode. The coordinator is the only writer.

The journal records song state and physical task attempts. Important behavior:

- `DONE` + existing final output -> skipped;
- interrupted/RUNNING -> re-planned on the next run;
- `FAILED` -> skipped unless `--retry-failed`;
- `--force` -> ignore persisted DONE/FAILED state and re-plan.

The song identity contains a SHA-256 of source MIDI contents. Editing a MIDI in
place therefore cannot accidentally reuse an old DONE row.

The batch rendering-system identity currently uses schema
`render-system-v2` plus the prepared-MIDI contract `artifact-addressed-v1`. It
includes patch-config contents, core rendering settings, configured asset
identities, the native FX helper identity, and the persistent sfizz renderer
identity.

## 12. Failure semantics

A physical task failure marks its song FAILED and records the error. Other songs
continue.

Work already in flight for the failed song may return, but no new downstream
work is scheduled for it. Planning failures are also persisted per song and do
not abort the rest of corpus admission.

There is no silent renderer substitution after a backend failure.

## 13. Console/logging contract

The coordinator is the sole owner of user-facing rendering progress. Native
backends return or expose captured diagnostics instead of printing successful
progress directly into the shared terminal.

Event vocabulary is intentionally small:

```text
PLAN CACHE RAW FX MIX DONE WARN FAIL
```

RAW events carry their backend (`sfz` or `fluidsynth`) as metadata.

Normal single-song mode prints compact task completion. Normal batch mode uses a
low-noise heartbeat rather than one success line per stem.

```text
--verbose              planning/cache/task details
--debug                successful backend diagnostics
--color auto|always|never
--log-file FILE        human-readable ANSI-free sink
--json-log FILE        structured JSONL sink
--heartbeat SECONDS    batch dashboard interval, default 5
```

`NO_COLOR` is respected when color mode is `auto`.

Backend stderr handling:

- persistent sfizz worker stderr is continuously drained into bounded
  diagnostics;
- embedded FluidSynth native stderr is captured inside its process worker,
  including headless ALSA/JACK/SDL noise;
- `mrp-lv2-chain` and explicit legacy `lv2apply` output is captured by the FX
  worker;
- backend failures still surface their diagnostic error text.

## 14. Throughput metrics

Batch summaries expose three end-to-end measures:

- `songs/min` — completed songs per wall-clock minute;
- `track× realtime` — source music seconds times rendered logical source-track
  count divided by wall time;
- `ms / track-bar` — wall-clock milliseconds per rendered source track-bar.

Track-bar accounting follows the MIDI time-signature timeline. Derived stems
sharing the same source track index are counted once for structural metrics.

## 15. Reference commands

Single song:

```bash
midi-render render song.mid --concurrency 5
```

Dataset:

```bash
midi-render batch /data/midi \
  --output-dir /data/rendered \
  --work-root /scratch/mrp \
  --state-db /scratch/mrp-state.sqlite3 \
  --active-songs 24 \
  --concurrency 24 \
  --include-melody \
  --sfz-max-replicas 2
```

For most runs, tune only `--concurrency`, `--active-songs`, and optionally
`--sfz-max-replicas`; leave backend caps, FX backlog, and SFZ RAM admission on
AUTO unless profiling gives a concrete reason to override them.
