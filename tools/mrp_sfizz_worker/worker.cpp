#include "sfizz_dyn.hpp"
#include "worker_engine.hpp"
#include "events.hpp"
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
std::vector<std::string> split_tab(const std::string& s) {
    std::vector<std::string> out;
    std::size_t pos = 0;
    while (true) {
        auto next = s.find('\t', pos);
        out.push_back(s.substr(pos, next == std::string::npos ? next : next - pos));
        if (next == std::string::npos) break;
        pos = next + 1;
    }
    return out;
}

void usage() {
    std::cerr << "usage: mrp-sfizz-worker --libsfizz PATH [--sample-rate 48000] [--block-size 1024] [--polyphony 256] [--quality 2]\n";
}
}

int main(int argc, char** argv) {
    try {
        std::string lib;
        EngineConfig cfg;
        for (int i = 1; i < argc; ++i) {
            std::string a = argv[i];
            auto need = [&](const char* opt) -> std::string {
                if (i + 1 >= argc) throw std::runtime_error(std::string("missing value for ") + opt);
                return argv[++i];
            };
            if (a == "--libsfizz") lib = need("--libsfizz");
            else if (a == "--sample-rate") cfg.sample_rate = std::stoi(need("--sample-rate"));
            else if (a == "--block-size") cfg.block_size = std::stoi(need("--block-size"));
            else if (a == "--polyphony") cfg.polyphony = std::stoi(need("--polyphony"));
            else if (a == "--quality") cfg.quality = std::stoi(need("--quality"));
            else if (a == "--max-tail-seconds") cfg.max_tail_seconds = std::stod(need("--max-tail-seconds"));
            else if (a == "-h" || a == "--help") { usage(); return 0; }
            else throw std::runtime_error("unknown option: " + a);
        }
        if (lib.empty()) { usage(); return 2; }

        SfizzDyn api(lib);
        const unsigned int offline_api = api.get_offline_render_api_version();
        if (offline_api < 1)
            throw std::runtime_error("libsfizz offline render API version is unsupported");
        WorkerEngine engine(api, cfg);
        std::cout << "READY\tprotocol=3\tsample_rate=" << cfg.sample_rate
                  << "\tblock_size=" << cfg.block_size << "\tpolyphony=" << cfg.polyphony
                  << "\tquality=" << cfg.quality << "\toffline_api=" << offline_api << std::endl;

        std::string line;
        while (std::getline(std::cin, line)) {
            if (line.empty()) continue;
            try {
                auto p = split_tab(line);
                if (p[0] == "LOAD") {
                    if (p.size() != 2) throw std::runtime_error("LOAD expects: LOAD<TAB>sfz");
                    auto s = engine.load(p[1]);
                    std::cout << std::fixed << std::setprecision(3)
                              << "OK\tLOAD\tms=" << s.milliseconds << "\tregions=" << s.regions
                              << "\tpreloaded_samples=" << s.preloaded_samples << "\tsfizz_bytes=" << s.sfizz_bytes << std::endl;
                } else if (p[0] == "RENDER") {
                    if (p.size() != 4)
                        throw std::runtime_error("RENDER expects: RENDER<TAB>events<TAB>wav<TAB>seed");
                    auto ev = load_event_file(p[1]);
                    unsigned int seed = static_cast<unsigned int>(std::stoul(p[3]));
                    auto s = engine.render(ev, p[2], seed);
                    std::cout << std::fixed << std::setprecision(3)
                              << "OK\tRENDER\tms=" << s.milliseconds << "\tframes=" << s.frames
                              << "\tactive_after=" << s.active_voices_after << "\ttail_limit=" << (s.tail_limit_hit ? 1 : 0)
                              << "\tinstrument_loads=" << engine.instrument_load_count() << std::endl;
                } else if (p[0] == "PING") {
                    std::cout << "OK\tPONG" << std::endl;
                } else if (p[0] == "QUIT") {
                    std::cout << "OK\tBYE" << std::endl;
                    break;
                } else {
                    throw std::runtime_error("unknown command: " + p[0]);
                }
            } catch (const std::exception& e) {
                std::cout << "ERR\t" << e.what() << std::endl;
            }
        }
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "fatal: " << e.what() << "\n";
        return 1;
    }
}
