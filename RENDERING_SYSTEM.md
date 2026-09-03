# Rendering System

MidiRenderPipeline v0.5 uses the former one-song procedural pipeline into a
bounded, resumable rendering system. The audio backends are still deliberately
different internally, but they share one planning, task, scheduling, and state
model.

## Core model

Planning is song-scoped; scheduling is stem-scoped.

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
a normal SFZ task produces one raw stem, while a FluidSynth task may pack
several GM stems from the same song into one physical multi-output render.
Batching therefore remains a backend optimization rather than leaking into the
logical pipeline.

## State transitions

```text
PLANNED
  -> RAW_PENDING
  -> RAW_READY
  -> FX_PENDING       (only when the patch has effects)
  -> PROCESSED_READY
  -> MIX_PENDING
  -> DONE
```

Raw stems are the durable expensive-stage cache boundary. A restarted batch
re-plans interrupted songs and reuses deterministic fingerprinted raw stems;
FX and mix are reapplied so current downstream configuration remains visible.

## Coordinator and executor pools

One long-running Python coordinator owns all scheduling state. It uses
backend-specific executor pools but enforces one global worker budget across
them.

```text
                         RenderingCoordinator
                                |
           +--------------------+--------------------+
           |                    |                    |
        SFZ pool             GM pool              FX pool
  resident processes     Process workers       Thread workers
           |                    |                    |
   one Synth / SFZ        libfluidsynth        mrp-lv2-chain
   RAM load once          embedded native       child process
                                                    |
                                               whole LV2 chain
                                |
                             MIX pool
```

Why the pools differ:

- SFZ work uses instrument-affine resident worker processes. Each process owns
  one Synth/SFZ and executes one task at a time. A warmed InstrumentKey may own
  multiple independent replicas, so same-instrument tasks can run concurrently.
- FluidSynth is embedded in Python, so GM work gets persistent process isolation.
  The worker processes are long-lived; each physical GM task still creates the
  synth/session required by the v0.3.14 native-file or multi-output fast path.
- FX work is already isolated in `mrp-lv2-chain`, so threads supervise one native
  process per effected stem.
- Mix/export runs in a thread pool sized to the resolved resource policy.

The global `--concurrency` budget is the coordinator's hard in-flight ceiling.
Backend-specific capacities are AUTO by default and cannot multiply past that
global budget; legacy `--workers` / `--jobs` spellings remain aliases.

## Backpressure

The scheduler prioritizes downstream completion:

```text
MIX > FX > RAW
```

When queued/running FX reaches `--max-fx-backlog`, new raw work is temporarily
withheld. Raw jobs already in flight are allowed to finish. This prevents a fast
sampler stage from filling scratch storage with a large RAW_READY backlog while
FX is the bottleneck.

## Bounded admission

`midi-render batch` does not expand an entire dataset into millions of task
objects. Only `--active-songs` plans are resident at a time. When one song
finishes or fails, the next MIDI is admitted.

For example, a 100,000-song corpus with `--active-songs 64` keeps roughly 64
song plans live while preserving a single long-running set of executor pools.

## SQLite resume journal

Batch mode defaults to `renders/render-state.sqlite3`. The coordinator is the
only database writer. It records song and physical-task state:

```text
songs: song_id, midi_path, output_path, status, error, timestamps
tasks: task_id, song_id, stage, backend, stem_ids, status, attempts, error
```

SQLite uses WAL mode. `DONE` songs with an existing final output are skipped on
restart. `RUNNING` songs are simply re-planned; deterministic raw cache files
allow them to resume after the expensive sampler stage when possible. `FAILED`
songs are skipped unless `--retry-failed` is supplied.

A batch run identity includes the patch-config contents, core render settings,
and stat identities for configured SFZ/SF2 assets and the native FX helper. The
song identity additionally includes a SHA-256 of the source MIDI, so editing a
file in place cannot silently match an old DONE row. `--force` ignores
DONE/FAILED state when an experiment deliberately needs to be rerun.

Raw-stem cache keys are artifact-addressed at the symbolic renderer boundary:
the final prepared MIDI file is hashed after velocity/controller/timing/split or
program-remap processing. SFZ/GM cache lookup therefore tracks the bytes actually
sent to the renderer instead of inferring them only from source/config metadata.
`--rebuild-raw` bypasses those raw cache files; for already-DONE batch songs use
it together with `--force`.

## Commands

Single-file rendering uses exactly the same coordinator with an active window
of one:

```bash
midi-render render song.mid --concurrency 5
```

Long-running dataset rendering:

```bash
midi-render batch /data/midi \
  --output-dir /data/rendered \
  --work-root /scratch/mrp \
  --state-db /scratch/mrp-state.sqlite3 \
  --active-songs 64 \
  --concurrency 32 \
  --sfz-max-replicas 2
```

Primary resource controls:

```text
--concurrency N       global simultaneous task budget
--active-songs N      bounded song planning/admission window
--sfz-max-replicas N  maximum resident replicas for one SFZ InstrumentKey
```

Normally those are the only concurrency knobs a user needs. The coordinator
resolves backend capacity automatically: SFZ, FX, and MIX may use the full global
budget, while GM/FluidSynth uses a conservative process fan-out that grows with
`--concurrency` and currently tops out at four. The following remain advanced
overrides and are not required for normal use:

```text
--sfz-concurrency N
--gm-concurrency N
--fx-concurrency N
--mix-concurrency N
--sfz-memory-budget SIZE
--max-fx-backlog N
```

Legacy `--workers`, `--jobs`, and `--*-workers` spellings remain accepted as CLI
aliases. Backend overrides are effective caps inside the global budget; specifying
a value larger than `--concurrency` does not create additional execution slots.

Batch performance is reported in three complementary units:

- `songs/min`: completed songs per wall-clock minute;
- `track× realtime`: completed source MIDI duration multiplied by the number of
  rendered logical source tracks, divided by wall time;
- `ms / track-bar`: wall-clock milliseconds divided by rendered source
  track-bars. Bar-equivalents are integrated over the MIDI time-signature
  timeline, so changing meter is handled directly and tempo does not distort the
  structural unit. Derived stems that share a source track index are counted once.

These are end-to-end coordinator metrics, so planning, synthesis, FX, mixing,
cache hits, and scheduler overhead are all reflected in the measured wall time.

## Failure semantics

A physical task failure marks its song FAILED and records the error. Other songs
continue. Work already in flight for the failed song is allowed to return, but
no new downstream work is scheduled for it. This avoids turning one malformed
MIDI or plugin failure into a corpus-wide abort.

Planning failures are also persisted per song and do not stop the rest of batch
admission.

## Persistent SFZ worker boundary

The SFZ backend uses a `PersistentSfizzPool` keyed by the resolved SFZ asset and
render settings. One resident worker owns one process, one Synth, and one loaded
instrument for its lifetime. An InstrumentKey may own multiple independent
replicas up to `--sfz-max-replicas`; a cold key must complete one render before
it may scale out so the pool has a real working-set observation for admission.

Execution concurrency and memory are separate controls. Global `--concurrency`
limits all in-flight work. SFZ uses that full execution budget by default, while
its independent auto RAM pool controls resident worker population and idle-LRU
eviction. Each worker reports current sfizz-managed bytes after LOAD and after
every RENDER. The pool records observed per-instrument worker peaks and positive
task growth, uses those observations for later admission, and never kills busy
workers. The memory budget is therefore a steady-state target rather than a hard
RSS allocation guarantee.

Every render starts with offline-baseline restore and seed 0. Worker crashes,
timeouts, protocol errors, or render failures invalidate that resident entry and
remove partial raw WAV output; there is no silent legacy-renderer fallback.


## Observability and console UI (v0.4.2)

The coordinator is the sole owner of user-facing rendering progress. Backend workers
return values, timings, and captured diagnostics instead of printing directly. This
keeps concurrent SFZ/GM/FX work from interleaving terminal output and gives batch
mode one coherent view of the system.

Event vocabulary is intentionally small: `PLAN`, `CACHE`, `RAW`, `FX`, `MIX`,
`DONE`, `WARN`, and `FAIL`. RAW tasks carry a backend attribute (`sfz` or
`fluidsynth`) rather than inventing a second task vocabulary. The default TTY color
semantics are blue RAW/SFZ, cyan FluidSynth, magenta FX, yellow MIX, green
DONE/cache, yellow warning, red failure, and gray metadata. `--color auto` is the
default; `always`, `never`, and the `NO_COLOR` environment convention are supported.

Single-song normal mode shows one compact line per completed task plus the final
output. Batch normal mode suppresses per-stem success spam and emits a periodic
heartbeat containing completion, active/pending work, cache hits, failures, and
throughput. `--verbose` enables planning/cache/task details. `--debug` additionally
shows backend diagnostics captured during successful tasks.

The production backends are quiet by construction:

- persistent sfizz-worker stderr is continuously drained and retained as a bounded diagnostic tail;
- embedded FluidSynth runs in an isolated GM worker whose native process stderr is
  captured at the file-descriptor level, including ALSA/JACK/SDL messages emitted by
  C libraries;
- `mrp-lv2-chain` and legacy `lv2apply` stdout/stderr are captured by the FX worker;
- mixer gain details are no longer printed from DSP code.

A task failure still surfaces its real backend error. Successful diagnostics are only
expanded in debug mode, so hiding headless warnings does not sacrifice failure
information.

Persistent sinks are optional and independent of console rendering:

```text
--log-file FILE    append human-readable, ANSI-free displayed events
--json-log FILE    append structured JSONL events (including hidden task events)
--heartbeat SEC    batch dashboard refresh interval (default 5)
```

The JSONL sink is intended for corpus-scale audit/throughput analysis and is not tied
to TTY formatting.
