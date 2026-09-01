from pathlib import Path

import mido

import midi_render.renderer as renderer


def _make_split_midi(path: Path, *, channel: int, program: int, extra_channel: int | None = None):
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
    mid.tracks.append(conductor)

    track = mido.MidiTrack()
    track.append(mido.Message("program_change", channel=channel, program=program, time=0))
    track.append(mido.Message("note_on", channel=channel, note=60, velocity=90, time=0))
    track.append(mido.Message("note_off", channel=channel, note=60, velocity=0, time=480))
    if extra_channel is not None:
        track.append(mido.Message("program_change", channel=extra_channel, program=program, time=0))
        track.append(mido.Message("note_on", channel=extra_channel, note=67, velocity=80, time=0))
        track.append(mido.Message("note_off", channel=extra_channel, note=67, velocity=0, time=480))
    mid.tracks.append(track)
    mid.save(path)


def _gm_job(tmp_path: Path, index: int, midi: Path, output: str):
    from midi_render.midi import TrackInfo
    from midi_render.patches import Patch

    sf2 = tmp_path / "MuseScore.sf2"
    sf2.write_bytes(b"sf2")
    patch = Patch(f"gm_fallback_p{index:03d}", "<gm>", sf2)
    track = TrackInfo(index, f"Track {index}", 1, (0,), (index,), ((0, index),))
    return renderer.FluidSynthJob(
        track=track,
        instrument="synth_pad",
        patch=patch,
        split_midi=midi,
        soundfont=sf2,
        synth_gain=0.2,
        output=tmp_path / output,
    )


def test_gm_batch_midi_keeps_one_conductor_and_remaps_stems(tmp_path: Path):
    a = tmp_path / "a.mid"
    b = tmp_path / "b.mid"
    _make_split_midi(a, channel=2, program=10)
    _make_split_midi(b, channel=5, program=40)
    jobs = [_gm_job(tmp_path, 1, a, "a.wav"), _gm_job(tmp_path, 2, b, "b.wav")]

    output = tmp_path / "batch.mid"
    renderer._build_gm_batch_midi(jobs, output)
    batch = mido.MidiFile(output)

    assert len(batch.tracks) == 3
    assert [msg.type for msg in batch.tracks[0] if msg.is_meta][:1] == ["set_tempo"]
    assert {msg.channel for msg in batch.tracks[1] if hasattr(msg, "channel")} == {0}
    assert {msg.channel for msg in batch.tracks[2] if hasattr(msg, "channel")} == {1}
    assert [msg.program for msg in batch.tracks[1] if msg.type == "program_change"] == [10]
    assert [msg.program for msg in batch.tracks[2] if msg.type == "program_change"] == [40]


def test_gm_renderer_batches_one_channel_jobs_and_preserves_multichannel_compatibility(
    monkeypatch, tmp_path: Path
):
    from midi_render.renderer import RenderedStem

    one_a = tmp_path / "one-a.mid"
    one_b = tmp_path / "one-b.mid"
    multi = tmp_path / "multi.mid"
    _make_split_midi(one_a, channel=2, program=10)
    _make_split_midi(one_b, channel=5, program=40)
    _make_split_midi(multi, channel=1, program=50, extra_channel=3)
    jobs = [
        _gm_job(tmp_path, 1, one_a, "a.wav"),
        _gm_job(tmp_path, 2, one_b, "b.wav"),
        _gm_job(tmp_path, 3, multi, "c.wav"),
    ]

    class Binding:
        version = "test"

    class Session:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs
            sessions.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    batches = []
    compatibility = []
    sessions = []

    def fake_batch(batch, **kwargs):
        batches.append(
            ([job.track.index for job in batch], kwargs["blocksize"])
        )
        return [
            RenderedStem(job.track, job.instrument, job.patch, job.output, 0.1)
            for job in batch
        ]

    def fake_compat(job, **kwargs):
        compatibility.append((job.track.index, kwargs["cpu_cores"]))
        return RenderedStem(job.track, job.instrument, job.patch, job.output, 0.2)

    monkeypatch.setattr(renderer, "require_fluidsynth_library", lambda: Binding())
    monkeypatch.setattr(renderer, "FluidSynthSession", Session)
    monkeypatch.setattr(renderer, "_render_gm_batch", fake_batch)
    monkeypatch.setattr(renderer, "_render_native_file_job", fake_compat)

    results = renderer.render_fluidsynth_jobs(jobs, workers=4, samplerate=48_000)
    assert batches == [([1, 2], renderer.GM_BATCH_BLOCKSIZE)]
    assert compatibility == [(3, 1)]
    assert sessions[0]["cpu_cores"] == 1
    assert sessions[0]["audio_groups"] == 2
    assert sessions[0]["effects_groups"] == 2
    assert [result.track.index for result in results] == [1, 2, 3]


def test_gm_renderer_single_simple_job_uses_native_file_renderer(monkeypatch, tmp_path: Path):
    from midi_render.renderer import RenderedStem

    midi = tmp_path / "single.mid"
    _make_split_midi(midi, channel=2, program=10)
    job = _gm_job(tmp_path, 1, midi, "single.wav")

    class Binding:
        version = "test"

    calls = []

    def fake_native(native_job, **kwargs):
        calls.append((native_job.track.index, kwargs["cpu_cores"]))
        return RenderedStem(
            native_job.track,
            native_job.instrument,
            native_job.patch,
            native_job.output,
            0.1,
        )

    monkeypatch.setattr(renderer, "require_fluidsynth_library", lambda: Binding())
    monkeypatch.setattr(renderer, "_render_native_file_job", fake_native)
    monkeypatch.setattr(
        renderer,
        "_render_gm_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("batch path used")),
    )

    results = renderer.render_fluidsynth_jobs([job], workers=8, samplerate=48_000)
    assert calls == [(1, 1)]
    assert [result.track.index for result in results] == [1]


def test_gm_batch_chunking_avoids_singleton_tail():
    assert [len(batch) for batch in renderer._chunk_simple_gm_jobs(list(range(17)))] == [15, 2]
    assert [len(batch) for batch in renderer._chunk_simple_gm_jobs(list(range(18)))] == [16, 2]
    assert [len(batch) for batch in renderer._chunk_simple_gm_jobs(list(range(33)))] == [16, 15, 2]


def test_gm_batch_blocksize_preserves_pcm_output(tmp_path: Path):
    import soundfile as sf

    total_samples = 2048

    class FakeLib:
        def __init__(self, groups: int):
            self.groups = groups
            self.sample_pos = 0

        def fluid_synth_count_audio_channels(self, synth):
            return self.groups

        def fluid_synth_count_effects_channels(self, synth):
            return 2

        def fluid_synth_count_effects_groups(self, synth):
            return self.groups

        def fluid_player_get_status(self, player):
            return (
                renderer.FLUID_PLAYER_PLAYING
                if self.sample_pos < total_samples
                else 0
            )

        def fluid_synth_process(self, synth, blocksize, nfx, fx_ptrs, nout, dry_ptrs):
            for channel in range(nout):
                for i in range(blocksize):
                    sample = ((self.sample_pos + i + channel * 17) % 1000) / 32768.0
                    dry_ptrs[channel][i] = sample
            self.sample_pos += blocksize
            return renderer.FLUID_OK

    class FakeBinding:
        def __init__(self, groups: int):
            self.lib = FakeLib(groups)

    class FakeSession:
        synth = 1

        def __init__(self, binding):
            self.binding = binding

        def reset(self):
            self.binding.lib.sample_pos = 0

        def set_all_channels_melodic(self):
            return None

        def set_channel_drum(self, channel):
            return None

        def new_player(self, midi_path):
            return 1

        def finish_player(self, player):
            return None

    def render_with_blocksize(blocksize: int, subdir: str):
        root = tmp_path / subdir
        root.mkdir()
        a = root / "a.mid"
        b = root / "b.mid"
        _make_split_midi(a, channel=2, program=10)
        _make_split_midi(b, channel=5, program=40)
        jobs = [_gm_job(root, 1, a, "a.wav"), _gm_job(root, 2, b, "b.wav")]
        binding = FakeBinding(groups=2)
        session = FakeSession(binding)
        renderer._render_gm_batch(
            jobs,
            binding=binding,
            session=session,
            samplerate=48_000,
            blocksize=blocksize,
        )
        return [sf.read(job.output, dtype="int16")[0] for job in jobs]

    pcm_64 = render_with_blocksize(64, "b64")
    pcm_1024 = render_with_blocksize(1024, "b1024")
    assert len(pcm_64) == len(pcm_1024) == 2
    for reference, candidate in zip(pcm_64, pcm_1024, strict=True):
        assert reference.shape == candidate.shape == (total_samples, 2)
        assert (reference == candidate).all()
