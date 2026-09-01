#pragma once

#include <cstdint>
#include <string>
#include <vector>

enum class EventType { NoteOn, NoteOff, CC, Pitch, Program, ChannelAftertouch, PolyAftertouch };

struct Event {
    std::uint64_t frame{};
    EventType type{};
    int a{};
    int b{};
};

struct EventFile {
    int sample_rate{};
    std::uint64_t end_frame{};
    std::vector<Event> events;
};

EventFile load_event_file(const std::string& path);
