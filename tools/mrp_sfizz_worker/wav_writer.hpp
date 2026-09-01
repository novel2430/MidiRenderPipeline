#pragma once

#include <cstdint>
#include <fstream>
#include <string>

class Wav16StereoWriter {
public:
    Wav16StereoWriter(const std::string& path, int sample_rate);
    ~Wav16StereoWriter();
    void write(const float* left, const float* right, int frames);
    std::uint64_t frames_written() const { return frames_; }
private:
    void finalize();
    std::ofstream out_;
    int sample_rate_{};
    std::uint64_t frames_{};
    bool finalized_{};
};
