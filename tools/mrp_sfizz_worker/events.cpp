#include "events.hpp"
#include <fstream>
#include <sstream>
#include <stdexcept>

EventFile load_event_file(const std::string& path) {
    std::ifstream in(path);
    if (!in) throw std::runtime_error("cannot open event file: " + path);

    EventFile out;
    std::string line;
    std::size_t lineno = 0;
    while (std::getline(in, line)) {
        ++lineno;
        if (line.empty() || line[0] == '#') continue;
        std::istringstream ss(line);
        std::string tag;
        ss >> tag;
        if (tag == "MRPEV1") {
            ss >> out.sample_rate;
            if (!ss || out.sample_rate <= 0)
                throw std::runtime_error("bad MRPEV1 header at line " + std::to_string(lineno));
        } else if (tag == "END") {
            ss >> out.end_frame;
            if (!ss) throw std::runtime_error("bad END at line " + std::to_string(lineno));
        } else {
            Event e;
            e.frame = std::stoull(tag);
            std::string kind;
            ss >> kind >> e.a;
            if (kind == "note_on") { e.type = EventType::NoteOn; ss >> e.b; }
            else if (kind == "note_off") { e.type = EventType::NoteOff; ss >> e.b; }
            else if (kind == "cc") { e.type = EventType::CC; ss >> e.b; }
            else if (kind == "pitch") { e.type = EventType::Pitch; e.b = 0; }
            else if (kind == "program") { e.type = EventType::Program; e.b = 0; }
            else if (kind == "aftertouch") { e.type = EventType::ChannelAftertouch; e.b = 0; }
            else if (kind == "polytouch") { e.type = EventType::PolyAftertouch; ss >> e.b; }
            else throw std::runtime_error("unknown event kind at line " + std::to_string(lineno));
            if (!ss) throw std::runtime_error("bad event at line " + std::to_string(lineno));
            out.events.push_back(e);
        }
    }
    if (out.sample_rate <= 0) throw std::runtime_error("event file is missing MRPEV1 header");
    for (std::size_t i = 1; i < out.events.size(); ++i)
        if (out.events[i].frame < out.events[i - 1].frame)
            throw std::runtime_error("events are not frame-sorted");
    if (!out.events.empty() && out.end_frame < out.events.back().frame)
        out.end_frame = out.events.back().frame;
    return out;
}
