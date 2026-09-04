#define _POSIX_C_SOURCE 200809L

#include <lilv/lilv.h>
#include <lv2/atom/atom.h>
#include <lv2/buf-size/buf-size.h>
#include <lv2/core/lv2.h>
#include <lv2/options/options.h>
#include <lv2/parameters/parameters.h>
#include <lv2/urid/urid.h>
#include <lv2/worker/worker.h>
#include <sndfile.h>

#include <errno.h>
#include <getopt.h>
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/*
 * mrp_lv2_chain.c
 *
 * Small offline LV2-chain host for MidiRenderPipeline.
 *
 * Scope is intentionally focused on deterministic headless/offline effects:
 *   - audio and float control ports
 *   - Atom Sequence ports with empty host event streams
 *   - URID map/unmap and instantiation-time LV2 options
 *   - synchronous LV2 Worker execution for offline rendering
 *   - static controls (no automation, MIDI, transport, UI, or state restore)
 *   - mono/stereo channel adaptation between stages
 *   - block-based libsndfile I/O with an explicit deterministic FX tail
 *
 * It is NOT intended to be a general-purpose DAW host. The feature subset is
 * deliberately sufficient for project-local Guitarix effects and modern DPF
 * effects such as Dragonfly Reverb while keeping execution single-threaded.
 */

#define MAX_STAGES 32U
#define MAX_AUDIO_CHANNELS 8U
#define DEFAULT_BLOCK_SIZE 1024U
#define DEFAULT_ATOM_BUFFER_SIZE 65536U

typedef struct {
    char *symbol;
    float value;
} ControlSpec;

typedef struct {
    char *uri;
    unsigned requested_input_channels; /* 0 = plugin's native audio input count */
    ControlSpec *controls;
    size_t n_controls;
} StageSpec;

typedef enum {
    PORT_UNSUPPORTED = 0,
    PORT_CONTROL,
    PORT_AUDIO,
    PORT_ATOM,
} PortKind;

typedef struct {
    char **uris;
    size_t count;
    size_t capacity;
} UridMapState;

typedef struct {
    void *data;
    uint32_t size;
} WorkerResponse;

typedef struct {
    LilvInstance *instance;
    const LV2_Worker_Interface *interface;
    LV2_Worker_Schedule schedule;
    WorkerResponse *responses;
    size_t n_responses;
    size_t response_capacity;
} WorkerBridge;

typedef struct {
    const LilvPort *lilv_port;
    PortKind kind;
    uint32_t index;
    float value;
    bool is_input;
    bool optional;
    uint8_t *atom_buffer;
    uint32_t atom_capacity;
} PortInfo;

typedef struct {
    UridMapState urids;
    LV2_URID_Map urid_map;
    LV2_URID_Unmap urid_unmap;
    LV2_Options_Option options[6];
    LV2_Feature feature_map;
    LV2_Feature feature_unmap;
    LV2_Feature feature_options;
    LV2_Feature feature_bounded_block;
    int32_t min_block_length;
    int32_t max_block_length;
    int32_t nominal_block_length;
    int32_t sequence_size;
    float sample_rate;
    LV2_URID atom_int;
    LV2_URID atom_float;
    LV2_URID atom_sequence;
} HostFeatures;

typedef struct {
    StageSpec spec;
    const LilvPlugin *plugin;
    LilvInstance *instance;
    bool activated;
    HostFeatures *host_features;
    WorkerBridge worker;

    PortInfo *ports;
    uint32_t n_ports;

    uint32_t *audio_in_ports;
    uint32_t *audio_out_ports;
    unsigned n_audio_in;
    unsigned n_audio_out;

    float **audio_in;
    float **audio_out;
} Stage;

typedef struct {
    LilvNode *input_port;
    LilvNode *output_port;
    LilvNode *audio_port;
    LilvNode *control_port;
    LilvNode *atom_port;
    LilvNode *connection_optional;
} HostNodes;

typedef struct {
    const char *input_path;
    const char *output_path;
    const char *lv2_path;
    unsigned block_size;
    double tail_seconds;
    StageSpec stages[MAX_STAGES];
    unsigned n_stages;
} Options;

static double
now_seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1000000000.0;
}

static void
usage(FILE *out, const char *argv0)
{
    fprintf(out,
            "Usage:\n"
            "  %s -i INPUT.wav -o OUTPUT.wav [--block N] [--lv2-path PATH] \\\n\n"
            "     --plugin URI [--input-channels N] [--control SYMBOL VALUE]... \\\n\n"
            "     [--plugin URI ...]\n\n"
            "Options:\n"
            "  -i, --input PATH             Input WAV/audio file\n"
            "  -o, --output PATH            Output WAV/audio file\n"
            "  -b, --block N                Processing block size (default: %u)\n"
            "      --tail-seconds SEC       Append SEC seconds of zero-input FX tail\n"
            "      --lv2-path PATH          Override LV2_PATH before discovery\n"
            "  -p, --plugin URI             Append an LV2 plugin stage\n"
            "      --input-channels N       Stage pre-conversion channels;\n"
            "                               0/omitted = plugin native input count\n"
            "  -c, --control SYMBOL VALUE   Set float control on latest stage\n"
            "  -h, --help                   Show this help\n\n"
            "Example:\n"
            "  %s -i bass.raw.wav -o bass.native.wav --lv2-path resources/fx \\\n\n"
            "     --plugin 'http://guitarix.sourceforge.net/plugins/gx_ampegsvt_#_ampegsvt_' \\\n\n"
            "     --input-channels 1 --control BYPASS 1 --control BASS 0.60\n",
            argv0,
            DEFAULT_BLOCK_SIZE,
            argv0);
}

static void *
xcalloc(size_t n, size_t size)
{
    void *p = calloc(n, size);
    if (!p) {
        fprintf(stderr, "error: out of memory\n");
        exit(2);
    }
    return p;
}

static void *
xrealloc(void *ptr, size_t size)
{
    void *p = realloc(ptr, size);
    if (!p) {
        fprintf(stderr, "error: out of memory\n");
        exit(2);
    }
    return p;
}

static char *
xstrdup(const char *s)
{
    char *p = strdup(s);
    if (!p) {
        fprintf(stderr, "error: out of memory\n");
        exit(2);
    }
    return p;
}

static bool
parse_unsigned(const char *text, unsigned *value)
{
    char *end = NULL;
    errno = 0;
    const unsigned long n = strtoul(text, &end, 10);
    if (errno || !end || *end != '\0' || n == 0 || n > UINT32_MAX) {
        return false;
    }
    *value = (unsigned)n;
    return true;
}

static bool
parse_nonnegative_double(const char *text, double *value)
{
    char *end = NULL;
    errno = 0;
    const double n = strtod(text, &end);
    if (errno || !end || *end != '\0' || !isfinite(n) || n < 0.0) {
        return false;
    }
    *value = n;
    return true;
}

static int
append_control(StageSpec *stage, const char *symbol, const char *value_text)
{
    char *end = NULL;
    errno = 0;
    const float value = strtof(value_text, &end);
    if (errno || !end || *end != '\0' || !isfinite(value)) {
        fprintf(stderr, "error: invalid control value '%s' for %s\n", value_text, symbol);
        return 1;
    }

    stage->controls = xrealloc(stage->controls, (stage->n_controls + 1) * sizeof(ControlSpec));
    stage->controls[stage->n_controls].symbol = xstrdup(symbol);
    stage->controls[stage->n_controls].value = value;
    ++stage->n_controls;
    return 0;
}

static int
parse_options(int argc, char **argv, Options *opts)
{
    enum {
        OPT_LV2_PATH = 1000,
        OPT_INPUT_CHANNELS,
        OPT_TAIL_SECONDS,
    };

    static const struct option long_options[] = {
        {"input", required_argument, NULL, 'i'},
        {"output", required_argument, NULL, 'o'},
        {"block", required_argument, NULL, 'b'},
        {"plugin", required_argument, NULL, 'p'},
        {"control", required_argument, NULL, 'c'},
        {"lv2-path", required_argument, NULL, OPT_LV2_PATH},
        {"input-channels", required_argument, NULL, OPT_INPUT_CHANNELS},
        {"tail-seconds", required_argument, NULL, OPT_TAIL_SECONDS},
        {"help", no_argument, NULL, 'h'},
        {NULL, 0, NULL, 0},
    };

    opts->block_size = DEFAULT_BLOCK_SIZE;

    int c = 0;
    while ((c = getopt_long(argc, argv, "i:o:b:p:c:h", long_options, NULL)) != -1) {
        switch (c) {
        case 'i':
            opts->input_path = optarg;
            break;
        case 'o':
            opts->output_path = optarg;
            break;
        case 'b':
            if (!parse_unsigned(optarg, &opts->block_size)) {
                fprintf(stderr, "error: invalid block size '%s'\n", optarg);
                return 1;
            }
            break;
        case 'p': {
            if (opts->n_stages >= MAX_STAGES) {
                fprintf(stderr, "error: too many plugin stages (max %u)\n", MAX_STAGES);
                return 1;
            }
            StageSpec *stage = &opts->stages[opts->n_stages++];
            stage->uri = xstrdup(optarg);
            break;
        }
        case 'c': {
            if (opts->n_stages == 0) {
                fprintf(stderr, "error: --control must follow --plugin\n");
                return 1;
            }
            if (optind >= argc) {
                fprintf(stderr, "error: --control requires SYMBOL VALUE\n");
                return 1;
            }
            const char *symbol = optarg;
            const char *value = argv[optind++];
            if (append_control(&opts->stages[opts->n_stages - 1], symbol, value)) {
                return 1;
            }
            break;
        }
        case OPT_LV2_PATH:
            opts->lv2_path = optarg;
            break;
        case OPT_TAIL_SECONDS:
            if (!parse_nonnegative_double(optarg, &opts->tail_seconds)) {
                fprintf(stderr, "error: invalid --tail-seconds '%s'\n", optarg);
                return 1;
            }
            break;
        case OPT_INPUT_CHANNELS: {
            if (opts->n_stages == 0) {
                fprintf(stderr, "error: --input-channels must follow --plugin\n");
                return 1;
            }
            unsigned n = 0;
            if (!parse_unsigned(optarg, &n) || n > MAX_AUDIO_CHANNELS) {
                fprintf(stderr, "error: invalid --input-channels '%s'\n", optarg);
                return 1;
            }
            opts->stages[opts->n_stages - 1].requested_input_channels = n;
            break;
        }
        case 'h':
            usage(stdout, argv[0]);
            exit(0);
        default:
            usage(stderr, argv[0]);
            return 1;
        }
    }

    if (!opts->input_path || !opts->output_path || opts->n_stages == 0) {
        usage(stderr, argv[0]);
        return 1;
    }
    if (optind != argc) {
        fprintf(stderr, "error: unexpected positional argument '%s'\n", argv[optind]);
        return 1;
    }
    return 0;
}

static void
free_options(Options *opts)
{
    for (unsigned s = 0; s < opts->n_stages; ++s) {
        StageSpec *stage = &opts->stages[s];
        free(stage->uri);
        for (size_t i = 0; i < stage->n_controls; ++i) {
            free(stage->controls[i].symbol);
        }
        free(stage->controls);
    }
}

static void
free_host_nodes(HostNodes *nodes)
{
    lilv_node_free(nodes->connection_optional);
    lilv_node_free(nodes->atom_port);
    lilv_node_free(nodes->control_port);
    lilv_node_free(nodes->audio_port);
    lilv_node_free(nodes->output_port);
    lilv_node_free(nodes->input_port);
}

static HostNodes
make_host_nodes(LilvWorld *world)
{
    HostNodes nodes = {
        .input_port = lilv_new_uri(world, LV2_CORE__InputPort),
        .output_port = lilv_new_uri(world, LV2_CORE__OutputPort),
        .audio_port = lilv_new_uri(world, LV2_CORE__AudioPort),
        .control_port = lilv_new_uri(world, LV2_CORE__ControlPort),
        .atom_port = lilv_new_uri(world, LV2_ATOM__AtomPort),
        .connection_optional = lilv_new_uri(world, LV2_CORE__connectionOptional),
    };
    return nodes;
}

static LV2_URID
host_map_uri(LV2_URID_Map_Handle handle, const char *uri)
{
    UridMapState *state = (UridMapState *)handle;
    if (!uri) {
        return 0;
    }
    for (size_t i = 0; i < state->count; ++i) {
        if (strcmp(state->uris[i], uri) == 0) {
            return (LV2_URID)(i + 1U);
        }
    }
    if (state->count == state->capacity) {
        const size_t next = state->capacity ? state->capacity * 2U : 32U;
        state->uris = xrealloc(state->uris, next * sizeof(char *));
        state->capacity = next;
    }
    state->uris[state->count] = xstrdup(uri);
    ++state->count;
    return (LV2_URID)state->count;
}

static const char *
host_unmap_uri(LV2_URID_Unmap_Handle handle, LV2_URID urid)
{
    UridMapState *state = (UridMapState *)handle;
    if (urid == 0 || (size_t)urid > state->count) {
        return NULL;
    }
    return state->uris[urid - 1U];
}

static void
free_host_features(HostFeatures *host)
{
    for (size_t i = 0; i < host->urids.count; ++i) {
        free(host->urids.uris[i]);
    }
    free(host->urids.uris);
    memset(host, 0, sizeof(*host));
}

static void
init_host_features(HostFeatures *host, unsigned block_size, unsigned sample_rate)
{
    memset(host, 0, sizeof(*host));
    host->urid_map.handle = &host->urids;
    host->urid_map.map = host_map_uri;
    host->urid_unmap.handle = &host->urids;
    host->urid_unmap.unmap = host_unmap_uri;

    host->atom_int = host_map_uri(&host->urids, LV2_ATOM__Int);
    host->atom_float = host_map_uri(&host->urids, LV2_ATOM__Float);
    host->atom_sequence = host_map_uri(&host->urids, LV2_ATOM__Sequence);

    host->min_block_length = 1;
    host->max_block_length = (int32_t)block_size;
    host->nominal_block_length = (int32_t)block_size;
    host->sequence_size = (int32_t)DEFAULT_ATOM_BUFFER_SIZE;
    host->sample_rate = (float)sample_rate;

    host->options[0] = (LV2_Options_Option){
        LV2_OPTIONS_INSTANCE, 0, host_map_uri(&host->urids, LV2_BUF_SIZE__minBlockLength),
        sizeof(int32_t), host->atom_int, &host->min_block_length};
    host->options[1] = (LV2_Options_Option){
        LV2_OPTIONS_INSTANCE, 0, host_map_uri(&host->urids, LV2_BUF_SIZE__maxBlockLength),
        sizeof(int32_t), host->atom_int, &host->max_block_length};
    host->options[2] = (LV2_Options_Option){
        LV2_OPTIONS_INSTANCE, 0, host_map_uri(&host->urids, LV2_BUF_SIZE__nominalBlockLength),
        sizeof(int32_t), host->atom_int, &host->nominal_block_length};
    host->options[3] = (LV2_Options_Option){
        LV2_OPTIONS_INSTANCE, 0, host_map_uri(&host->urids, LV2_BUF_SIZE__sequenceSize),
        sizeof(int32_t), host->atom_int, &host->sequence_size};
    host->options[4] = (LV2_Options_Option){
        LV2_OPTIONS_INSTANCE, 0, host_map_uri(&host->urids, LV2_PARAMETERS__sampleRate),
        sizeof(float), host->atom_float, &host->sample_rate};
    host->options[5] = (LV2_Options_Option){0};

    host->feature_map = (LV2_Feature){LV2_URID__map, &host->urid_map};
    host->feature_unmap = (LV2_Feature){LV2_URID__unmap, &host->urid_unmap};
    host->feature_options = (LV2_Feature){LV2_OPTIONS__options, host->options};
    host->feature_bounded_block = (LV2_Feature){LV2_BUF_SIZE__boundedBlockLength, NULL};
}

static void
free_planar(float **channels, unsigned n_channels)
{
    if (!channels) {
        return;
    }
    for (unsigned ch = 0; ch < n_channels; ++ch) {
        free(channels[ch]);
    }
    free(channels);
}

static float **
alloc_planar(unsigned n_channels, unsigned block_size)
{
    float **channels = xcalloc(n_channels ? n_channels : 1U, sizeof(float *));
    for (unsigned ch = 0; ch < n_channels; ++ch) {
        channels[ch] = xcalloc(block_size, sizeof(float));
    }
    return channels;
}

static void
free_worker_responses(WorkerBridge *worker)
{
    for (size_t i = 0; i < worker->n_responses; ++i) {
        free(worker->responses[i].data);
    }
    free(worker->responses);
    worker->responses = NULL;
    worker->n_responses = 0;
    worker->response_capacity = 0;
}

static LV2_Worker_Status
worker_respond(LV2_Worker_Respond_Handle handle, uint32_t size, const void *data)
{
    WorkerBridge *worker = (WorkerBridge *)handle;
    if (worker->n_responses == worker->response_capacity) {
        const size_t next = worker->response_capacity ? worker->response_capacity * 2U : 4U;
        worker->responses = xrealloc(worker->responses, next * sizeof(WorkerResponse));
        worker->response_capacity = next;
    }
    WorkerResponse *response = &worker->responses[worker->n_responses++];
    response->size = size;
    response->data = NULL;
    if (size > 0) {
        response->data = xcalloc(size, 1U);
        memcpy(response->data, data, size);
    }
    return LV2_WORKER_SUCCESS;
}

static LV2_Worker_Status
worker_schedule(LV2_Worker_Schedule_Handle handle, uint32_t size, const void *data)
{
    WorkerBridge *worker = (WorkerBridge *)handle;
    if (!worker->instance || !worker->interface || !worker->interface->work) {
        return LV2_WORKER_ERR_UNKNOWN;
    }
    return worker->interface->work(
        lilv_instance_get_handle(worker->instance), worker_respond, worker, size, data);
}

static int
worker_finish_cycle(WorkerBridge *worker)
{
    if (!worker->interface || !worker->instance) {
        return 0;
    }
    LV2_Handle handle = lilv_instance_get_handle(worker->instance);
    size_t i = 0;
    while (i < worker->n_responses) {
        WorkerResponse *response = &worker->responses[i++];
        if (worker->interface->work_response) {
            const LV2_Worker_Status status = worker->interface->work_response(
                handle, response->size, response->data);
            if (status != LV2_WORKER_SUCCESS) {
                fprintf(stderr, "error: LV2 worker response failed with status %d\n", (int)status);
                free_worker_responses(worker);
                return 1;
            }
        }
    }
    free_worker_responses(worker);
    if (worker->interface->end_run) {
        const LV2_Worker_Status status = worker->interface->end_run(handle);
        if (status != LV2_WORKER_SUCCESS) {
            fprintf(stderr, "error: LV2 worker end_run failed with status %d\n", (int)status);
            return 1;
        }
    }
    return 0;
}

static void
reset_atom_ports(Stage *stage)
{
    for (uint32_t p = 0; p < stage->n_ports; ++p) {
        PortInfo *info = &stage->ports[p];
        if (info->kind != PORT_ATOM || !info->atom_buffer) {
            continue;
        }
        LV2_Atom_Sequence *seq = (LV2_Atom_Sequence *)info->atom_buffer;
        memset(seq, 0, sizeof(*seq));
        seq->atom.type = stage->host_features->atom_sequence;
        seq->body.unit = 0;
        seq->body.pad = 0;
        if (info->is_input) {
            seq->atom.size = sizeof(LV2_Atom_Sequence_Body);
        } else {
            seq->atom.size = info->atom_capacity - (uint32_t)sizeof(LV2_Atom);
        }
    }
}

static bool
supported_required_feature(const char *uri)
{
    return uri && (strcmp(uri, LV2_OPTIONS__options) == 0 ||
                   strcmp(uri, LV2_URID__map) == 0 ||
                   strcmp(uri, LV2_URID__unmap) == 0 ||
                   strcmp(uri, LV2_WORKER__schedule) == 0 ||
                   strcmp(uri, LV2_BUF_SIZE__boundedBlockLength) == 0);
}

static int
validate_required_features(const LilvPlugin *plugin, const char *plugin_uri)
{
    LilvNodes *required = lilv_plugin_get_required_features(plugin);
    if (!required) {
        return 0;
    }
    int status = 0;
    LILV_FOREACH(nodes, i, required) {
        const LilvNode *node = lilv_nodes_get(required, i);
        const char *uri = lilv_node_as_uri(node);
        if (!supported_required_feature(uri)) {
            fprintf(stderr,
                    "error: <%s> requires unsupported LV2 host feature <%s>\n",
                    plugin_uri,
                    uri ? uri : "non-URI feature");
            status = 1;
            break;
        }
    }
    lilv_nodes_free(required);
    return status;
}

static bool
plugin_requires_feature(const LilvPlugin *plugin, const char *feature_uri)
{
    LilvNodes *required = lilv_plugin_get_required_features(plugin);
    if (!required) {
        return false;
    }
    bool found = false;
    LILV_FOREACH(nodes, i, required) {
        const char *uri = lilv_node_as_uri(lilv_nodes_get(required, i));
        if (uri && strcmp(uri, feature_uri) == 0) {
            found = true;
            break;
        }
    }
    lilv_nodes_free(required);
    return found;
}

static void
free_stage(Stage *stage)
{
    if (stage->instance) {
        if (stage->activated) {
            lilv_instance_deactivate(stage->instance);
        }
        lilv_instance_free(stage->instance);
    }
    free_worker_responses(&stage->worker);
    if (stage->ports) {
        for (uint32_t p = 0; p < stage->n_ports; ++p) {
            free(stage->ports[p].atom_buffer);
        }
    }
    free_planar(stage->audio_out, stage->n_audio_out);
    free_planar(stage->audio_in, stage->n_audio_in);
    free(stage->audio_out_ports);
    free(stage->audio_in_ports);
    free(stage->ports);
    memset(stage, 0, sizeof(*stage));
}

static int
configure_stage(Stage *stage,
                const StageSpec *spec,
                LilvWorld *world,
                const LilvPlugins *plugins,
                const HostNodes *nodes,
                HostFeatures *host_features,
                unsigned block_size,
                unsigned sample_rate)
{
    memset(stage, 0, sizeof(*stage));
    stage->spec = *spec; /* borrowed strings/control array owned by Options */
    stage->host_features = host_features;

    LilvNode *uri = lilv_new_uri(world, spec->uri);
    if (!uri) {
        fprintf(stderr, "error: invalid plugin URI <%s>\n", spec->uri);
        return 1;
    }
    stage->plugin = lilv_plugins_get_by_uri(plugins, uri);
    lilv_node_free(uri);
    if (!stage->plugin) {
        fprintf(stderr, "error: plugin <%s> not found\n", spec->uri);
        return 1;
    }
    if (validate_required_features(stage->plugin, spec->uri)) {
        return 1;
    }

    stage->n_ports = lilv_plugin_get_num_ports(stage->plugin);
    stage->ports = xcalloc(stage->n_ports, sizeof(PortInfo));
    float *defaults = xcalloc(stage->n_ports, sizeof(float));
    lilv_plugin_get_port_ranges_float(stage->plugin, NULL, NULL, defaults);

    for (uint32_t p = 0; p < stage->n_ports; ++p) {
        PortInfo *info = &stage->ports[p];
        const LilvPort *port = lilv_plugin_get_port_by_index(stage->plugin, p);
        info->lilv_port = port;
        info->index = p;
        info->value = isnan(defaults[p]) ? 0.0f : defaults[p];
        info->optional = lilv_port_has_property(stage->plugin, port, nodes->connection_optional);

        if (lilv_port_is_a(stage->plugin, port, nodes->input_port)) {
            info->is_input = true;
        } else if (lilv_port_is_a(stage->plugin, port, nodes->output_port)) {
            info->is_input = false;
        } else if (!info->optional) {
            fprintf(stderr, "error: <%s> port %u is neither input nor output\n", spec->uri, p);
            free(defaults);
            return 1;
        }

        if (lilv_port_is_a(stage->plugin, port, nodes->control_port)) {
            info->kind = PORT_CONTROL;
        } else if (lilv_port_is_a(stage->plugin, port, nodes->audio_port)) {
            info->kind = PORT_AUDIO;
            if (info->is_input) {
                ++stage->n_audio_in;
            } else {
                ++stage->n_audio_out;
            }
        } else if (lilv_port_is_a(stage->plugin, port, nodes->atom_port)) {
            info->kind = PORT_ATOM;
            info->atom_capacity = DEFAULT_ATOM_BUFFER_SIZE;
            info->atom_buffer = xcalloc(info->atom_capacity, 1U);
        } else if (!info->optional) {
            fprintf(stderr,
                    "error: <%s> port %u has unsupported required type; MRP host supports audio/control/Atom Sequence ports\n",
                    spec->uri,
                    p);
            free(defaults);
            return 1;
        }
    }
    free(defaults);

    if (stage->n_audio_in == 0 || stage->n_audio_out == 0 ||
        stage->n_audio_in > MAX_AUDIO_CHANNELS || stage->n_audio_out > MAX_AUDIO_CHANNELS) {
        fprintf(stderr,
                "error: <%s> has unsupported audio I/O shape %u -> %u (supported 1..%u)\n",
                spec->uri,
                stage->n_audio_in,
                stage->n_audio_out,
                MAX_AUDIO_CHANNELS);
        return 1;
    }

    const unsigned requested = spec->requested_input_channels ? spec->requested_input_channels : stage->n_audio_in;
    if (requested != 1 && requested != stage->n_audio_in) {
        fprintf(stderr,
                "error: <%s>: requested input_channels=%u cannot map to %u plugin inputs using lv2apply semantics\n",
                spec->uri,
                requested,
                stage->n_audio_in);
        return 1;
    }

    for (size_t c = 0; c < spec->n_controls; ++c) {
        const ControlSpec *control = &spec->controls[c];
        LilvNode *sym = lilv_new_string(world, control->symbol);
        const LilvPort *port = lilv_plugin_get_port_by_symbol(stage->plugin, sym);
        lilv_node_free(sym);
        if (!port) {
            fprintf(stderr, "error: <%s>: unknown control port '%s'\n", spec->uri, control->symbol);
            return 1;
        }
        const uint32_t index = lilv_port_get_index(stage->plugin, port);
        if (stage->ports[index].kind != PORT_CONTROL || !stage->ports[index].is_input) {
            fprintf(stderr, "error: <%s>: '%s' is not an input control port\n", spec->uri, control->symbol);
            return 1;
        }
        stage->ports[index].value = control->value;
    }

    stage->audio_in_ports = xcalloc(stage->n_audio_in, sizeof(uint32_t));
    stage->audio_out_ports = xcalloc(stage->n_audio_out, sizeof(uint32_t));
    for (uint32_t p = 0, i = 0, o = 0; p < stage->n_ports; ++p) {
        if (stage->ports[p].kind == PORT_AUDIO) {
            if (stage->ports[p].is_input) {
                stage->audio_in_ports[i++] = p;
            } else {
                stage->audio_out_ports[o++] = p;
            }
        }
    }

    stage->audio_in = alloc_planar(stage->n_audio_in, block_size);
    stage->audio_out = alloc_planar(stage->n_audio_out, block_size);

    stage->worker.schedule.handle = &stage->worker;
    stage->worker.schedule.schedule_work = worker_schedule;
    const LV2_Feature worker_feature = {LV2_WORKER__schedule, &stage->worker.schedule};
    const LV2_Feature *features[] = {
        &host_features->feature_map,
        &host_features->feature_unmap,
        &host_features->feature_options,
        &host_features->feature_bounded_block,
        &worker_feature,
        NULL,
    };
    stage->instance = lilv_plugin_instantiate(stage->plugin, (double)sample_rate, features);
    if (!stage->instance) {
        fprintf(stderr,
                "error: failed to instantiate <%s> with MRP offline host features\n",
                spec->uri);
        return 1;
    }
    stage->worker.instance = stage->instance;
    stage->worker.interface = (const LV2_Worker_Interface *)lilv_instance_get_extension_data(
        stage->instance, LV2_WORKER__interface);
    if (plugin_requires_feature(stage->plugin, LV2_WORKER__schedule) && !stage->worker.interface) {
        fprintf(stderr,
                "error: <%s> requires LV2 Worker scheduling but provides no worker interface\n",
                spec->uri);
        return 1;
    }

    for (uint32_t p = 0; p < stage->n_ports; ++p) {
        if (stage->ports[p].kind == PORT_CONTROL) {
            lilv_instance_connect_port(stage->instance, p, &stage->ports[p].value);
        } else if (stage->ports[p].kind == PORT_ATOM) {
            lilv_instance_connect_port(stage->instance, p, stage->ports[p].atom_buffer);
        } else if (stage->ports[p].kind == PORT_UNSUPPORTED) {
            lilv_instance_connect_port(stage->instance, p, NULL);
        }
    }
    for (unsigned ch = 0; ch < stage->n_audio_in; ++ch) {
        lilv_instance_connect_port(stage->instance, stage->audio_in_ports[ch], stage->audio_in[ch]);
    }
    for (unsigned ch = 0; ch < stage->n_audio_out; ++ch) {
        lilv_instance_connect_port(stage->instance, stage->audio_out_ports[ch], stage->audio_out[ch]);
    }

    reset_atom_ports(stage);
    lilv_instance_activate(stage->instance);
    stage->activated = true;
    return 0;
}

static int
run_stage(Stage *stage, uint32_t nframes)
{
    reset_atom_ports(stage);
    for (unsigned ch = 0; ch < stage->n_audio_out; ++ch) {
        memset(stage->audio_out[ch], 0, (size_t)nframes * sizeof(float));
    }
    lilv_instance_run(stage->instance, nframes);
    return worker_finish_cycle(&stage->worker);
}

static void
copy_or_convert(float **dst,
                unsigned dst_channels,
                float *const *src,
                unsigned src_channels,
                size_t nframes)
{
    if (dst_channels == src_channels) {
        for (unsigned ch = 0; ch < dst_channels; ++ch) {
            memcpy(dst[ch], src[ch], nframes * sizeof(float));
        }
        return;
    }

    if (dst_channels == 1) {
        if (src_channels == 1) {
            memcpy(dst[0], src[0], nframes * sizeof(float));
        } else {
            for (size_t f = 0; f < nframes; ++f) {
                dst[0][f] = 0.5f * (src[0][f] + src[1][f]);
            }
        }
        return;
    }

    if (dst_channels == 2 && src_channels == 1) {
        memcpy(dst[0], src[0], nframes * sizeof(float));
        memcpy(dst[1], src[0], nframes * sizeof(float));
        return;
    }

    if (dst_channels == 2 && src_channels >= 2) {
        memcpy(dst[0], src[0], nframes * sizeof(float));
        memcpy(dst[1], src[1], nframes * sizeof(float));
        return;
    }

    if (dst_channels == src_channels) {
        for (unsigned ch = 0; ch < dst_channels; ++ch) {
            memcpy(dst[ch], src[ch], nframes * sizeof(float));
        }
        return;
    }

    fprintf(stderr, "error: unsupported channel conversion %u -> %u\n", src_channels, dst_channels);
    exit(5);
}

static void
prepare_stage_input(Stage *stage,
                    float *const *src,
                    unsigned src_channels,
                    size_t nframes,
                    float *mono_scratch)
{
    const unsigned requested = stage->spec.requested_input_channels
                                   ? stage->spec.requested_input_channels
                                   : stage->n_audio_in;

    if (requested == 1) {
        if (src_channels == 1) {
            memcpy(mono_scratch, src[0], nframes * sizeof(float));
        } else {
            for (size_t f = 0; f < nframes; ++f) {
                mono_scratch[f] = 0.5f * (src[0][f] + src[1][f]);
            }
        }
        /* lv2apply accepts mono input for any number of audio input ports and
         * distributes it round-robin; for mono this is simply duplication. */
        for (unsigned ch = 0; ch < stage->n_audio_in; ++ch) {
            memcpy(stage->audio_in[ch], mono_scratch, nframes * sizeof(float));
        }
        return;
    }

    copy_or_convert(stage->audio_in, stage->n_audio_in, src, src_channels, nframes);
}

static void
interleave(float *dst, float *const *src, unsigned channels, size_t nframes)
{
    for (size_t f = 0; f < nframes; ++f) {
        for (unsigned ch = 0; ch < channels; ++ch) {
            dst[f * channels + ch] = src[ch][f];
        }
    }
}

static void
deinterleave(float **dst, const float *src, unsigned channels, size_t nframes)
{
    for (size_t f = 0; f < nframes; ++f) {
        for (unsigned ch = 0; ch < channels; ++ch) {
            dst[ch][f] = src[f * channels + ch];
        }
    }
}

int
main(int argc, char **argv)
{
    const double total_t0 = now_seconds();
    Options opts = {0};
    if (parse_options(argc, argv, &opts)) {
        free_options(&opts);
        return 1;
    }

    if (opts.lv2_path && setenv("LV2_PATH", opts.lv2_path, 1) != 0) {
        fprintf(stderr, "error: failed to set LV2_PATH: %s\n", strerror(errno));
        free_options(&opts);
        return 1;
    }

    SF_INFO in_info = {0};
    SNDFILE *in_file = sf_open(opts.input_path, SFM_READ, &in_info);
    if (!in_file) {
        fprintf(stderr, "error: cannot open input %s: %s\n", opts.input_path, sf_strerror(NULL));
        free_options(&opts);
        return 1;
    }
    if (in_info.channels < 1 || in_info.channels > (int)MAX_AUDIO_CHANNELS) {
        fprintf(stderr, "error: unsupported input channel count %d\n", in_info.channels);
        sf_close(in_file);
        free_options(&opts);
        return 1;
    }

    const double world_t0 = now_seconds();
    LilvWorld *world = lilv_world_new();
    if (!world) {
        fprintf(stderr, "error: lilv_world_new failed\n");
        sf_close(in_file);
        free_options(&opts);
        return 1;
    }
    lilv_world_load_all(world);
    const double world_seconds = now_seconds() - world_t0;

    const LilvPlugins *plugins = lilv_world_get_all_plugins(world);
    HostNodes nodes = make_host_nodes(world);
    HostFeatures host_features;
    init_host_features(&host_features, opts.block_size, (unsigned)in_info.samplerate);

    const double setup_t0 = now_seconds();
    Stage *stages = xcalloc(opts.n_stages, sizeof(Stage));
    unsigned configured = 0;
    for (; configured < opts.n_stages; ++configured) {
        if (configure_stage(&stages[configured],
                            &opts.stages[configured],
                            world,
                            plugins,
                            &nodes,
                            &host_features,
                            opts.block_size,
                            (unsigned)in_info.samplerate)) {
            for (unsigned i = 0; i <= configured; ++i) {
                free_stage(&stages[i]);
            }
            free(stages);
            free_host_features(&host_features);
            free_host_nodes(&nodes);
            lilv_world_free(world);
            sf_close(in_file);
            free_options(&opts);
            return 1;
        }
    }
    const double setup_seconds = now_seconds() - setup_t0;

    const unsigned output_channels = stages[opts.n_stages - 1].n_audio_out;

    /* Match the current Python+lv2apply pipeline's file-format behaviour:
     * _ensure_channels writes FLOAT only when it actually has to convert
     * channel count, after which lv2apply preserves that format. */
    bool converted_channels = false;
    unsigned chain_channels = (unsigned)in_info.channels;
    for (unsigned s = 0; s < opts.n_stages; ++s) {
        const unsigned requested = stages[s].spec.requested_input_channels
                                       ? stages[s].spec.requested_input_channels
                                       : stages[s].n_audio_in;
        if (chain_channels != requested) {
            converted_channels = true;
        }
        chain_channels = stages[s].n_audio_out;
    }

    SF_INFO out_info = in_info;
    out_info.channels = (int)output_channels;
    if (converted_channels) {
        out_info.format = (in_info.format & SF_FORMAT_TYPEMASK) | SF_FORMAT_FLOAT;
    }
    SNDFILE *out_file = sf_open(opts.output_path, SFM_WRITE, &out_info);
    if (!out_file) {
        fprintf(stderr, "error: cannot open output %s: %s\n", opts.output_path, sf_strerror(NULL));
        for (unsigned i = 0; i < opts.n_stages; ++i) {
            free_stage(&stages[i]);
        }
        free(stages);
        free_host_features(&host_features);
        free_host_nodes(&nodes);
        lilv_world_free(world);
        sf_close(in_file);
        free_options(&opts);
        return 1;
    }

    const unsigned input_channels = (unsigned)in_info.channels;
    float *input_interleaved = xcalloc((size_t)opts.block_size * input_channels, sizeof(float));
    float *output_interleaved = xcalloc((size_t)opts.block_size * output_channels, sizeof(float));
    float **input_planar = alloc_planar(input_channels, opts.block_size);
    float *mono_scratch = xcalloc(opts.block_size, sizeof(float));

    sf_count_t total_frames = 0;
    sf_count_t tail_frames_written = 0;
    int render_status = 0;
    const sf_count_t requested_tail_frames =
        (sf_count_t)ceil(opts.tail_seconds * (double)in_info.samplerate);
    const double render_t0 = now_seconds();
    bool input_done = false;
    while (!input_done || tail_frames_written < requested_tail_frames) {
        sf_count_t nframes_io = 0;
        if (!input_done) {
            nframes_io = sf_readf_float(in_file, input_interleaved, opts.block_size);
            if (nframes_io < 0) {
                fprintf(stderr, "error: failed reading input: %s\n", sf_strerror(in_file));
                render_status = 1;
                break;
            }
            if (nframes_io == 0) {
                input_done = true;
                continue;
            }
            deinterleave(input_planar, input_interleaved, input_channels, (size_t)nframes_io);
        } else {
            const sf_count_t remaining = requested_tail_frames - tail_frames_written;
            nframes_io = remaining < (sf_count_t)opts.block_size
                             ? remaining
                             : (sf_count_t)opts.block_size;
            for (unsigned ch = 0; ch < input_channels; ++ch) {
                memset(input_planar[ch], 0, (size_t)nframes_io * sizeof(float));
            }
        }

        const size_t nframes = (size_t)nframes_io;
        float **current = input_planar;
        unsigned current_channels = input_channels;

        for (unsigned s = 0; s < opts.n_stages; ++s) {
            Stage *stage = &stages[s];
            prepare_stage_input(stage, current, current_channels, nframes, mono_scratch);
            if (run_stage(stage, (uint32_t)nframes)) {
                render_status = 1;
                break;
            }
            current = stage->audio_out;
            current_channels = stage->n_audio_out;
        }
        if (render_status) {
            break;
        }

        interleave(output_interleaved, current, output_channels, nframes);
        if (sf_writef_float(out_file, output_interleaved, nframes_io) != nframes_io) {
            fprintf(stderr, "error: failed writing output: %s\n", sf_strerror(out_file));
            render_status = 1;
            break;
        }
        total_frames += nframes_io;
        if (input_done) {
            tail_frames_written += nframes_io;
        }
    }
    const double render_seconds = now_seconds() - render_t0;

    const int in_close_status = sf_close(in_file);
    const int out_close_status = sf_close(out_file);
    if (in_close_status || out_close_status) {
        fprintf(stderr, "warning: libsndfile reported an error while closing a file\n");
        render_status = 1;
    }

    free(mono_scratch);
    free_planar(input_planar, input_channels);
    free(output_interleaved);
    free(input_interleaved);

    for (unsigned i = 0; i < opts.n_stages; ++i) {
        free_stage(&stages[i]);
    }
    free(stages);
    free_host_features(&host_features);
    free_host_nodes(&nodes);
    lilv_world_free(world);

    const double total_seconds = now_seconds() - total_t0;
    fprintf(stderr,
            "MRP-LV2: stages=%u block=%u sr=%d frames=%lld tail=%lld world=%.4fs setup=%.4fs render=%.4fs total=%.4fs\n",
            opts.n_stages,
            opts.block_size,
            in_info.samplerate,
            (long long)total_frames,
            (long long)tail_frames_written,
            world_seconds,
            setup_seconds,
            render_seconds,
            total_seconds);

    free_options(&opts);
    return render_status;
}
