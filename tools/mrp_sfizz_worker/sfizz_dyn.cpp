#include "sfizz_dyn.hpp"
#include <dlfcn.h>
#include <stdexcept>

SfizzDyn::SfizzDyn(const std::string& path) {
    handle_ = dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!handle_)
        throw std::runtime_error("dlopen libsfizz failed: " + std::string(dlerror()));

    create_synth = require<sfizz_create_synth_fn>("sfizz_create_synth");
    free_synth = require<sfizz_free_fn>("sfizz_free");
    load_file = require<sfizz_load_file_fn>("sfizz_load_file");
    set_samples_per_block = require<sfizz_set_samples_per_block_fn>("sfizz_set_samples_per_block");
    set_sample_rate = require<sfizz_set_sample_rate_fn>("sfizz_set_sample_rate");
    set_num_voices = require<sfizz_set_num_voices_fn>("sfizz_set_num_voices");
    set_sample_quality = require<sfizz_set_sample_quality_fn>("sfizz_set_sample_quality");
    enable_freewheeling = require<sfizz_enable_freewheeling_fn>("sfizz_enable_freewheeling");
    send_note_on = require<sfizz_send_note_on_fn>("sfizz_send_note_on");
    send_note_off = require<sfizz_send_note_off_fn>("sfizz_send_note_off");
    send_cc = require<sfizz_send_cc_fn>("sfizz_send_cc");
    send_pitch_wheel = require<sfizz_send_pitch_wheel_fn>("sfizz_send_pitch_wheel");
    send_program_change = require<sfizz_send_program_change_fn>("sfizz_send_program_change");
    send_channel_aftertouch = require<sfizz_send_channel_aftertouch_fn>("sfizz_send_channel_aftertouch");
    send_poly_aftertouch = require<sfizz_send_poly_aftertouch_fn>("sfizz_send_poly_aftertouch");
    render_block = require<sfizz_render_block_fn>("sfizz_render_block");
    get_offline_render_api_version = require<sfizz_get_offline_render_api_version_fn>("sfizz_get_offline_render_api_version");
    set_offline_ram_loading = require<sfizz_set_offline_ram_loading_fn>("sfizz_set_offline_ram_loading");
    set_offline_sample_loading_mode = require<sfizz_set_offline_sample_loading_mode_fn>("sfizz_set_offline_sample_loading_mode");
    seal_offline_instrument = require<sfizz_seal_offline_instrument_fn>("sfizz_seal_offline_instrument");
    begin_offline_task = require<sfizz_begin_offline_task_fn>("sfizz_begin_offline_task");
    get_num_active_voices = require<sfizz_get_num_active_voices_fn>("sfizz_get_num_active_voices");
    get_num_regions = require<sfizz_get_num_regions_fn>("sfizz_get_num_regions");
    get_num_preloaded_samples = require<sfizz_get_num_preloaded_samples_fn>("sfizz_get_num_preloaded_samples");
    get_num_bytes64 = require<sfizz_get_num_bytes64_fn>("sfizz_get_num_bytes64");
    get_offline_sample_resident_bytes64 = require<sfizz_get_offline_sample_resident_bytes64_fn>("sfizz_get_offline_sample_resident_bytes64");
    get_offline_sample_resident_peak_bytes64 = require<sfizz_get_offline_sample_resident_peak_bytes64_fn>("sfizz_get_offline_sample_resident_peak_bytes64");
    get_offline_full_resident_sample_count = require<sfizz_get_offline_full_resident_sample_count_fn>("sfizz_get_offline_full_resident_sample_count");
}

SfizzDyn::~SfizzDyn() {
    if (handle_) dlclose(handle_);
}

template<class T>
T SfizzDyn::require(const char* name) {
    dlerror();
    void* p = dlsym(handle_, name);
    if (const char* err = dlerror())
        throw std::runtime_error(std::string("missing libsfizz symbol ") + name + ": " + err);
    return reinterpret_cast<T>(p);
}
