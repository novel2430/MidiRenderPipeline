#pragma once

#include "events.hpp"
#include "sfizz_dyn.hpp"
#include <cstdint>
#include <string>

struct EngineConfig {
    int sample_rate = 48000;
    int block_size = 1024;
    int polyphony = 256;
    int quality = 2;
    double tail_threshold = 1e-12;
    double max_tail_seconds = 30.0;
};

struct LoadStats {
    double milliseconds{};
    int regions{};
    std::size_t preloaded_samples{};
    std::uint64_t sfizz_bytes{};
};

struct RenderStats {
    double milliseconds{};
    std::uint64_t frames{};
    int active_voices_after{};
    bool tail_limit_hit{};
};

class WorkerEngine {
public:
    WorkerEngine(SfizzDyn& api, EngineConfig cfg);
    ~WorkerEngine();

    LoadStats load(const std::string& sfz_path);
    RenderStats render(const EventFile& events, const std::string& output_path, unsigned int seed = 0);
    bool loaded() const { return instrument_loaded_; }
    unsigned int instrument_load_count() const { return instrument_load_count_; }

private:
    void create_synth();
    void destroy_synth();
    void prepare_task(unsigned int seed);
    void dispatch(const Event& e, int delay);

    SfizzDyn& api_;
    EngineConfig cfg_;
    sfizz_synth_t* synth_{};
    bool instrument_loaded_{};
    unsigned int instrument_load_count_{};
};
