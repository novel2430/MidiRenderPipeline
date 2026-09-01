#include "wav_writer.hpp"
#include <algorithm>
#include <array>
#include <stdexcept>

namespace {
void put_u16(std::ostream& o, std::uint16_t v) {
    char b[2]{char(v & 0xff), char((v >> 8) & 0xff)}; o.write(b, 2);
}
void put_u32(std::ostream& o, std::uint32_t v) {
    char b[4]{char(v & 0xff), char((v >> 8) & 0xff), char((v >> 16) & 0xff), char((v >> 24) & 0xff)}; o.write(b, 4);
}
std::int16_t f32_to_s16(float x) {
    x = std::max(-1.0f, std::min(1.0f, x));
    // Match dr_wav's conversion used by sfizz_render: map [-1,1] to [-32768,32767].
    int r = static_cast<int>((x + 1.0f) * 32767.5f) - 32768;
    return static_cast<std::int16_t>(r);
}
}

Wav16StereoWriter::Wav16StereoWriter(const std::string& path, int sample_rate) : sample_rate_(sample_rate) {
    out_.open(path, std::ios::binary | std::ios::trunc);
    if (!out_) throw std::runtime_error("cannot open WAV output: " + path);
    out_.write("RIFF", 4); put_u32(out_, 0); out_.write("WAVE", 4);
    out_.write("fmt ", 4); put_u32(out_, 16); put_u16(out_, 1); put_u16(out_, 2);
    put_u32(out_, static_cast<std::uint32_t>(sample_rate_));
    put_u32(out_, static_cast<std::uint32_t>(sample_rate_ * 2 * 2));
    put_u16(out_, 4); put_u16(out_, 16);
    out_.write("data", 4); put_u32(out_, 0);
}

Wav16StereoWriter::~Wav16StereoWriter() { try { finalize(); } catch (...) {} }

void Wav16StereoWriter::write(const float* left, const float* right, int frames) {
    for (int i = 0; i < frames; ++i) {
        auto l = f32_to_s16(left[i]); auto r = f32_to_s16(right[i]);
        put_u16(out_, static_cast<std::uint16_t>(l));
        put_u16(out_, static_cast<std::uint16_t>(r));
    }
    frames_ += static_cast<std::uint64_t>(frames);
}

void Wav16StereoWriter::finalize() {
    if (finalized_) return;
    finalized_ = true;
    const std::uint64_t data_bytes64 = frames_ * 4;
    if (data_bytes64 > 0xffffffffULL - 36ULL)
        throw std::runtime_error("WAV output exceeds RIFF 4GB limit");
    const auto data_bytes = static_cast<std::uint32_t>(data_bytes64);
    out_.seekp(4); put_u32(out_, 36 + data_bytes);
    out_.seekp(40); put_u32(out_, data_bytes);
    out_.flush();
}
