#include "worker_engine.hpp"
#include "wav_writer.hpp"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <stdexcept>
#include <vector>

using Clock = std::chrono::steady_clock;

WorkerEngine::WorkerEngine(SfizzDyn& api, EngineConfig cfg) : api_(api), cfg_(cfg) { create_synth(); }
WorkerEngine::~WorkerEngine() { destroy_synth(); }

void WorkerEngine::create_synth() {
    synth_ = api_.create_synth();
    if (!synth_) throw std::runtime_error("sfizz_create_synth returned null");
    api_.set_samples_per_block(synth_, cfg_.block_size);
    api_.set_sample_rate(synth_, static_cast<float>(cfg_.sample_rate));
    api_.set_sample_quality(synth_, SFIZZ_PROCESS_FREEWHEELING, cfg_.quality);
    api_.set_num_voices(synth_, cfg_.polyphony);
    api_.enable_freewheeling(synth_);
}

void WorkerEngine::destroy_synth() {
    if (synth_) { api_.free_synth(synth_); synth_ = nullptr; }
}

LoadStats WorkerEngine::load(const std::string& sfz_path) {
    if (instrument_loaded_)
        throw std::runtime_error("worker already owns an instrument; start a new worker to load another SFZ");

    api_.set_offline_sample_loading_mode(synth_, cfg_.sample_loading);
    auto t0 = Clock::now();
    if (!api_.load_file(synth_, sfz_path.c_str()))
        throw std::runtime_error("libsfizz failed to load SFZ: " + sfz_path);
    ++instrument_load_count_;

    if (!api_.seal_offline_instrument(synth_))
        throw std::runtime_error("libsfizz failed to seal offline instrument baseline");
    instrument_loaded_ = true;

    auto t1 = Clock::now();
    LoadStats s;
    s.milliseconds = std::chrono::duration<double, std::milli>(t1 - t0).count();
    s.regions = api_.get_num_regions(synth_);
    s.preloaded_samples = api_.get_num_preloaded_samples(synth_);
    s.sfizz_bytes = api_.get_num_bytes64(synth_);
    s.sample_resident_bytes = api_.get_offline_sample_resident_bytes64(synth_);
    s.sample_peak_bytes = api_.get_offline_sample_resident_peak_bytes64(synth_);
    s.full_resident_samples = api_.get_offline_full_resident_sample_count(synth_);
    return s;
}

void WorkerEngine::prepare_task(unsigned int seed) {
    if (!instrument_loaded_)
        throw std::runtime_error("render requested before LOAD");
    if (!api_.begin_offline_task(synth_, seed))
        throw std::runtime_error("libsfizz rejected offline task begin");
}

void WorkerEngine::dispatch(const Event& e, int delay) {
    switch (e.type) {
    case EventType::NoteOn:
        if (e.b == 0) api_.send_note_off(synth_, delay, e.a, 0);
        else api_.send_note_on(synth_, delay, e.a, e.b);
        break;
    case EventType::NoteOff: api_.send_note_off(synth_, delay, e.a, e.b); break;
    case EventType::CC: api_.send_cc(synth_, delay, e.a, e.b); break;
    case EventType::Pitch: api_.send_pitch_wheel(synth_, delay, e.a); break;
    case EventType::Program: api_.send_program_change(synth_, delay, e.a); break;
    case EventType::ChannelAftertouch: api_.send_channel_aftertouch(synth_, delay, e.a); break;
    case EventType::PolyAftertouch: api_.send_poly_aftertouch(synth_, delay, e.a, e.b); break;
    }
}

RenderStats WorkerEngine::render(const EventFile& evf, const std::string& output_path, unsigned int seed) {
    if (!loaded()) throw std::runtime_error("render requested before LOAD");
    if (evf.sample_rate != cfg_.sample_rate)
        throw std::runtime_error("event sample rate does not match worker sample rate");
    prepare_task(seed);

    auto t0 = Clock::now();
    Wav16StereoWriter writer(output_path, cfg_.sample_rate);
    std::vector<float> left(cfg_.block_size), right(cfg_.block_size);
    float* channels[2]{left.data(), right.data()};

    std::size_t index = 0;
    std::uint64_t block_start = 0;
    const std::uint64_t last_event_frame = evf.events.empty() ? 0 : evf.events.back().frame;
    const std::uint64_t event_end = std::max(evf.end_frame, last_event_frame);

    // Render through at least the block containing END. Like sfizz_render, blocks are fixed-size.
    while (block_start <= event_end) {
        const std::uint64_t block_end = block_start + static_cast<std::uint64_t>(cfg_.block_size);
        while (index < evf.events.size() && evf.events[index].frame < block_end) {
            if (evf.events[index].frame < block_start)
                throw std::runtime_error("event cursor fell behind render block");
            dispatch(evf.events[index], static_cast<int>(evf.events[index].frame - block_start));
            ++index;
        }
        std::fill(left.begin(), left.end(), 0.0f);
        std::fill(right.begin(), right.end(), 0.0f);
        api_.render_block(synth_, channels, 2, cfg_.block_size);
        writer.write(left.data(), right.data(), cfg_.block_size);
        block_start = block_end;
    }

    const std::uint64_t max_tail_blocks = static_cast<std::uint64_t>(std::ceil(cfg_.max_tail_seconds * cfg_.sample_rate / cfg_.block_size));
    std::uint64_t tail_blocks = 0;
    bool limit_hit = false;
    auto power = [&]() {
        long double sum = 0.0;
        const auto n = static_cast<std::size_t>(cfg_.block_size) * 2;
        for (int i = 0; i < cfg_.block_size; ++i) {
            sum += static_cast<long double>(left[i]) * left[i];
            sum += static_cast<long double>(right[i]) * right[i];
        }
        return static_cast<double>(sum / n);
    }();

    while (power > cfg_.tail_threshold) {
        if (tail_blocks++ >= max_tail_blocks) { limit_hit = true; break; }
        std::fill(left.begin(), left.end(), 0.0f);
        std::fill(right.begin(), right.end(), 0.0f);
        api_.render_block(synth_, channels, 2, cfg_.block_size);
        writer.write(left.data(), right.data(), cfg_.block_size);
        long double sum = 0.0;
        for (int i = 0; i < cfg_.block_size; ++i) {
            sum += static_cast<long double>(left[i]) * left[i];
            sum += static_cast<long double>(right[i]) * right[i];
        }
        power = static_cast<double>(sum / (static_cast<long double>(cfg_.block_size) * 2.0L));
    }

    auto t1 = Clock::now();
    RenderStats s;
    s.milliseconds = std::chrono::duration<double, std::milli>(t1 - t0).count();
    s.frames = writer.frames_written();
    s.active_voices_after = api_.get_num_active_voices(synth_);
    s.tail_limit_hit = limit_hit;
    s.sfizz_bytes = api_.get_num_bytes64(synth_);
    s.sample_resident_bytes = api_.get_offline_sample_resident_bytes64(synth_);
    s.sample_peak_bytes = api_.get_offline_sample_resident_peak_bytes64(synth_);
    s.full_resident_samples = api_.get_offline_full_resident_sample_count(synth_);
    return s;
}
