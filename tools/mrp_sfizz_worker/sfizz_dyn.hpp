#pragma once

#include "sfizz_abi_min.h"
#include <string>

class SfizzDyn {
public:
    explicit SfizzDyn(const std::string& path);
    ~SfizzDyn();
    SfizzDyn(const SfizzDyn&) = delete;
    SfizzDyn& operator=(const SfizzDyn&) = delete;

    sfizz_create_synth_fn create_synth{};
    sfizz_free_fn free_synth{};
    sfizz_load_file_fn load_file{};
    sfizz_set_samples_per_block_fn set_samples_per_block{};
    sfizz_set_sample_rate_fn set_sample_rate{};
    sfizz_set_num_voices_fn set_num_voices{};
    sfizz_set_sample_quality_fn set_sample_quality{};
    sfizz_enable_freewheeling_fn enable_freewheeling{};
    sfizz_send_note_on_fn send_note_on{};
    sfizz_send_note_off_fn send_note_off{};
    sfizz_send_cc_fn send_cc{};
    sfizz_send_pitch_wheel_fn send_pitch_wheel{};
    sfizz_send_program_change_fn send_program_change{};
    sfizz_send_channel_aftertouch_fn send_channel_aftertouch{};
    sfizz_send_poly_aftertouch_fn send_poly_aftertouch{};
    sfizz_render_block_fn render_block{};
    sfizz_get_offline_render_api_version_fn get_offline_render_api_version{};
    sfizz_set_offline_ram_loading_fn set_offline_ram_loading{};
    sfizz_seal_offline_instrument_fn seal_offline_instrument{};
    sfizz_begin_offline_task_fn begin_offline_task{};
    sfizz_get_num_active_voices_fn get_num_active_voices{};
    sfizz_get_num_regions_fn get_num_regions{};
    sfizz_get_num_preloaded_samples_fn get_num_preloaded_samples{};
    sfizz_get_num_bytes64_fn get_num_bytes64{};

private:
    void* handle_{};
    template<class T> T require(const char* name);
};
