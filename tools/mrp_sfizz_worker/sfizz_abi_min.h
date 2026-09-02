#pragma once

// Minimal subset of the libsfizz C ABI required by the MRP persistent worker.
// The two offline-baseline functions are provided by the pinned MRP sfizz
// patch/fork; they are part of MRP's required persistent-renderer contract.

#include <cstddef>
#include <cstdint>

extern "C" {

struct sfizz_synth_t;

enum sfizz_process_mode_t {
    SFIZZ_PROCESS_LIVE = 0,
    SFIZZ_PROCESS_FREEWHEELING = 1,
};

enum sfizz_offline_sample_loading_mode_t {
    SFIZZ_OFFLINE_LOADING_DEFAULT = 0,
    SFIZZ_OFFLINE_LOADING_FULL_RAM = 1,
    SFIZZ_OFFLINE_LOADING_DETERMINISTIC_LAZY = 2,
};

using sfizz_create_synth_fn = sfizz_synth_t* (*)();
using sfizz_free_fn = void (*)(sfizz_synth_t*);
using sfizz_load_file_fn = bool (*)(sfizz_synth_t*, const char*);
using sfizz_set_samples_per_block_fn = void (*)(sfizz_synth_t*, int);
using sfizz_set_sample_rate_fn = void (*)(sfizz_synth_t*, float);
using sfizz_set_num_voices_fn = void (*)(sfizz_synth_t*, int);
using sfizz_set_sample_quality_fn = void (*)(sfizz_synth_t*, sfizz_process_mode_t, int);
using sfizz_enable_freewheeling_fn = void (*)(sfizz_synth_t*);
using sfizz_send_note_on_fn = void (*)(sfizz_synth_t*, int, int, int);
using sfizz_send_note_off_fn = void (*)(sfizz_synth_t*, int, int, int);
using sfizz_send_cc_fn = void (*)(sfizz_synth_t*, int, int, int);
using sfizz_send_pitch_wheel_fn = void (*)(sfizz_synth_t*, int, int);
using sfizz_send_program_change_fn = void (*)(sfizz_synth_t*, int, int);
using sfizz_send_channel_aftertouch_fn = void (*)(sfizz_synth_t*, int, int);
using sfizz_send_poly_aftertouch_fn = void (*)(sfizz_synth_t*, int, int, int);
using sfizz_render_block_fn = void (*)(sfizz_synth_t*, float**, int, int);
using sfizz_get_offline_render_api_version_fn = unsigned int (*)();
using sfizz_set_offline_ram_loading_fn = void (*)(sfizz_synth_t*, bool);
using sfizz_set_offline_sample_loading_mode_fn = void (*)(
    sfizz_synth_t*, sfizz_offline_sample_loading_mode_t);
using sfizz_seal_offline_instrument_fn = bool (*)(sfizz_synth_t*);
using sfizz_begin_offline_task_fn = bool (*)(sfizz_synth_t*, unsigned int);
using sfizz_get_num_active_voices_fn = int (*)(sfizz_synth_t*);
using sfizz_get_num_regions_fn = int (*)(sfizz_synth_t*);
using sfizz_get_num_preloaded_samples_fn = std::size_t (*)(sfizz_synth_t*);
using sfizz_get_num_bytes64_fn = std::uint64_t (*)(sfizz_synth_t*);
using sfizz_get_offline_sample_resident_bytes64_fn = std::uint64_t (*)(sfizz_synth_t*);
using sfizz_get_offline_sample_resident_peak_bytes64_fn = std::uint64_t (*)(sfizz_synth_t*);
using sfizz_get_offline_full_resident_sample_count_fn = std::uint64_t (*)(sfizz_synth_t*);

} // extern "C"
