/* goldmine-embed — the multimodal embedding helper for goldmine (digup-plan-002).
 *
 * A resident pure-C11 process that turns images and audio windows into 768-dim vectors through
 * the vendored llama.cpp b11461 static libraries: mtmd's own stb_image/miniaudio decode the
 * input, the ImageSizing port resizes images to the vision token budget, and the text model
 * (EmbeddingGemma 2, mean pooling, L2 in double) produces the vector. The call sequence is
 * DigUp's LlamaEmbedder.swift ported call for call; the resize is DigUp's ImageSizing.swift
 * (line-isomorphic to mtmd-image.cpp's copy of the same Pillow resampler).
 *
 * Frozen context params (design §2.1): n_ctx = n_batch = n_ubatch = 2048 (512 allowed for
 * query-only), n_seq_max 16, kv_unified, embeddings on, pooling MEAN, n_gpu_layers 999,
 * mtmd image_min_tokens 1 / image_max_tokens 1120 (images arrive at their final size — mtmd
 * never rescales them). gemma-embedding2 has no KV cache; a batch clear between requests is
 * the whole reset.
 *
 * Protocol (amendments B3/B4): line-delimited flat JSON objects on stdin/stdout, one request
 * per line, one response per request, strict order. First line is the ready event.
 *   {"op":"ping"}                                            -> {"id":N,"ok":true,"pong":true}
 *   {"op":"caps"}                                            -> {"id":N,"ok":true,"vision":..,"audio":..,"sample_rate":..,"dim":768}
 *   {"op":"embed","kind":"image","id":N,"budget":B,"path_b64":B64}   -> {"id":N,"ok":true,"vec_b64":B64}
 *   {"op":"embed","kind":"audio","id":N,"wav_b64":B64}               -> {"id":N,"ok":true,"vec_b64":B64}
 *   {"op":"quit"}                                            -> helper exits 0
 * Paths arrive base64 (non-UTF-8 paths are legal bytes). Per-request errors answer
 * {"id":N,"ok":false,"error":{"code":..,"message":..}} and keep the process alive; only
 * startup failures (2) and stdin read errors that are not EOF (3) ever exit.
 *
 * Spec: reports/execution/digup-plan-002/design/helper-design-v1.md + amendments-001.md
 * (B1-B4, M2, minors 9/11) + imagesizing-verification-note.md.
 */

#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "llama.h"
#include "mtmd.h"
#include "mtmd-helper.h"

#define HELPER_VERSION "goldmine-embed 1"
#define PROTO_VERSION 1
#define DIM_OUT 768                 /* llama_model_n_embd_out of the pinned model; hard pin */
#define MAX_LINE (16u * 1024u * 1024u)  /* protocol bound; a 30 s WAV is ~1.28 MB of base64 */
#define MAX_MSG 300                 /* DigUp's per-input error prefix length */

/* ---------------------------------------------------------------- globals (only the runtime) */

static struct llama_model *g_model;
static struct llama_context *g_lctx;
static mtmd_context *g_media;
static struct llama_batch_ext *g_batch;
static const struct llama_vocab *g_vocab;
static int g_dim_in;                /* encoder row width (llama_model_n_embd_inp) */
static int g_n_ctx;                 /* 512 or 2048 */
static bool g_cap_vision, g_cap_audio;
static int g_sample_rate;           /* -1 when the mmproj has no audio */
static volatile sig_atomic_t g_stop;    /* SIGTERM: set in the handler, read between requests only */
static int g_log_level = GGML_LOG_LEVEL_INFO;  /* CONT lines continue at this level */

/* llama.cpp logs a lot at load time: WARN+ to stderr, the rest dropped (DigUp LlamaLog). */
static void log_warn_plus(enum ggml_log_level level, const char *text, void *user_data) {
    (void)user_data;
    if (level != GGML_LOG_LEVEL_CONT) g_log_level = (int)level;
    if (g_log_level < GGML_LOG_LEVEL_WARN || text == NULL) return;
    fputs(text, stderr);
}

static void stop_handler(int sig) { (void)sig; g_stop = 1; }

/* ------------------------------------------------------------ SHA-256 (self-contained, no CommonCrypto) */

typedef struct {
    uint32_t state[8];
    uint64_t bits;
    uint8_t buf[64];
    size_t buf_len;
} sha256_t;

static const uint32_t SHA256_K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

static uint32_t ror32(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

static void sha256_compress(sha256_t *s, const uint8_t *p) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
        w[i] = ((uint32_t)p[i * 4] << 24) | ((uint32_t)p[i * 4 + 1] << 16) |
               ((uint32_t)p[i * 4 + 2] << 8) | (uint32_t)p[i * 4 + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ror32(w[i - 15], 7) ^ ror32(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ror32(w[i - 2], 17) ^ ror32(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = s->state[0], b = s->state[1], c = s->state[2], d = s->state[3];
    uint32_t e = s->state[4], f = s->state[5], g = s->state[6], h = s->state[7];
    for (int i = 0; i < 64; i++) {
        uint32_t S1 = ror32(e, 6) ^ ror32(e, 11) ^ ror32(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t t1 = h + S1 + ch + SHA256_K[i] + w[i];
        uint32_t S0 = ror32(a, 2) ^ ror32(a, 13) ^ ror32(a, 22);
        uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t t2 = S0 + maj;
        h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    s->state[0] += a; s->state[1] += b; s->state[2] += c; s->state[3] += d;
    s->state[4] += e; s->state[5] += f; s->state[6] += g; s->state[7] += h;
}

static void sha256_init(sha256_t *s) {
    static const uint32_t H0[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(s->state, H0, sizeof H0);
    s->bits = 0;
    s->buf_len = 0;
}

static void sha256_update(sha256_t *s, const uint8_t *p, size_t n) {
    s->bits += (uint64_t)n * 8;
    while (n > 0) {
        size_t take = 64 - s->buf_len;
        if (take > n) take = n;
        memcpy(s->buf + s->buf_len, p, take);
        s->buf_len += take;
        p += take;
        n -= take;
        if (s->buf_len == 64) {
            sha256_compress(s, s->buf);
            s->buf_len = 0;
        }
    }
}

static void sha256_final(sha256_t *s, uint8_t out[32]) {
    const uint64_t bits = s->bits;
    uint8_t pad = 0x80;
    sha256_update(s, &pad, 1);
    pad = 0;
    while (s->buf_len != 56) sha256_update(s, &pad, 1);
    uint8_t len[8];
    for (int i = 0; i < 8; i++) len[i] = (uint8_t)(bits >> (56 - i * 8));
    /* len appended without counting its bits (bits was saved above) */
    memcpy(s->buf + 56, len, 8);
    sha256_compress(s, s->buf);
    s->buf_len = 0;
    for (int i = 0; i < 8; i++) {
        out[i * 4 + 0] = (uint8_t)(s->state[i] >> 24);
        out[i * 4 + 1] = (uint8_t)(s->state[i] >> 16);
        out[i * 4 + 2] = (uint8_t)(s->state[i] >> 8);
        out[i * 4 + 3] = (uint8_t)(s->state[i]);
    }
}

/* ---------------------------------------------------------------- small utilities */

static void sha256_hex(const uint8_t *data, size_t n, char out[65]) {
    sha256_t s;
    uint8_t d[32];
    sha256_init(&s);
    sha256_update(&s, data, n);
    sha256_final(&s, d);
    static const char hex[] = "0123456789abcdef";
    for (int i = 0; i < 32; i++) {
        out[i * 2] = hex[d[i] >> 4];
        out[i * 2 + 1] = hex[d[i] & 15];
    }
    out[64] = 0;
}

static const char B64_TAB[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/* NULL on allocation failure only; never sees request data. */
static char *b64_encode(const uint8_t *data, size_t n) {
    size_t out_len = 4 * ((n + 2) / 3);
    char *out = malloc(out_len + 1);
    if (!out) return NULL;
    size_t o = 0;
    for (size_t i = 0; i < n; i += 3) {
        uint32_t v = (uint32_t)data[i] << 16;
        if (i + 1 < n) v |= (uint32_t)data[i + 1] << 8;
        if (i + 2 < n) v |= data[i + 2];
        out[o++] = B64_TAB[(v >> 18) & 63];
        out[o++] = B64_TAB[(v >> 12) & 63];
        out[o++] = (i + 1 < n) ? B64_TAB[(v >> 6) & 63] : '=';
        out[o++] = (i + 2 < n) ? B64_TAB[v & 63] : '=';
    }
    out[o] = 0;
    return out;
}

static int b64_val(int c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

/* 0 ok, -1 bad character/shape, -2 allocation failure. Length-tracked input; no strlen. */
static int b64_decode(const char *in, size_t in_len, uint8_t **out_data, size_t *out_len) {
    uint8_t *out = malloc(in_len / 4 * 3 + 3);
    if (!out) return -2;
    size_t o = 0;
    uint32_t acc = 0;
    int nbits = 0, seen_pad = 0;
    for (size_t i = 0; i < in_len; i++) {
        char c = in[i];
        if (c == '=') { seen_pad = 1; continue; }
        if (seen_pad) { free(out); return -1; }  /* data after padding */
        int v = b64_val(c);
        if (v < 0) { free(out); return -1; }
        acc = (acc << 6) | (uint32_t)v;
        nbits += 6;
        if (nbits >= 8) {
            nbits -= 8;
            out[o++] = (uint8_t)(acc >> nbits);
        }
    }
    *out_data = out;
    *out_len = o;
    return 0;
}

/* Growable byte buffer for the parser (request strings are never NUL-terminated in place). */
typedef struct { char *p; size_t len, cap; } sbuf_t;

static int sbuf_reserve(sbuf_t *b, size_t extra) {
    if (b->len + extra <= b->cap) return 1;
    size_t ncap = b->cap ? b->cap : 64;
    while (ncap < b->len + extra) ncap *= 2;
    char *np = realloc(b->p, ncap);
    if (!np) return 0;
    b->p = np;
    b->cap = ncap;
    return 1;
}

static int sbuf_putc(sbuf_t *b, char c) {
    if (!sbuf_reserve(b, 1)) return 0;
    b->p[b->len++] = c;
    return 1;
}

static void sbuf_free(sbuf_t *b) {
    free(b->p);
    b->p = NULL;
    b->len = b->cap = 0;
}

static int sbuf_put_utf8(sbuf_t *b, uint32_t cp) {
    if (cp < 0x80) return sbuf_putc(b, (char)cp);
    if (cp < 0x800)
        return sbuf_putc(b, (char)(0xC0 | (cp >> 6))) &&
               sbuf_putc(b, (char)(0x80 | (cp & 0x3F)));
    if (cp < 0x10000)
        return sbuf_putc(b, (char)(0xE0 | (cp >> 12))) &&
               sbuf_putc(b, (char)(0x80 | ((cp >> 6) & 0x3F))) &&
               sbuf_putc(b, (char)(0x80 | (cp & 0x3F)));
    return sbuf_putc(b, (char)(0xF0 | (cp >> 18))) &&
           sbuf_putc(b, (char)(0x80 | ((cp >> 12) & 0x3F))) &&
           sbuf_putc(b, (char)(0x80 | ((cp >> 6) & 0x3F))) &&
           sbuf_putc(b, (char)(0x80 | (cp & 0x3F)));
}

/* ------------------------------------------------------------ stdout responses */

static void cleanup(void);   /* frees the runtime; defined with the embedding section */

/* Every response is one '\n'-terminated line; stdout is line-buffered AND flushed here (B3).
   A failed flush means the parent is gone (EPIPE): free the runtime and leave with _exit(0).
   _exit deliberately skips the libraries' C++ static destructors — ggml-metal's device
   registry is a lazily-initialized static whose destructor aborts when it runs before the
   llama objects are freed (GGML_ASSERT rsets empty), and its registration time can be later
   than any atexit handler. The kernel reclaims everything; the parent sees a quiet 0. */
static void line_done(void) {
    if (fflush(stdout) == EOF) {
        cleanup();
        _exit(0);
    }
}

/* Error-message escaping keeps stdout ASCII-safe: a non-UTF-8 path byte becomes \u00XX
   instead of corrupting the parent's UTF-8 decode. */
static void put_json_escaped(const char *s, size_t n) {
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c == '"') {
            fputs("\\\"", stdout);
        } else if (c == '\\') {
            fputs("\\\\", stdout);
        } else if (c < 0x20 || c >= 0x7f) {
            printf("\\u%04x", c);
        } else {
            putchar(c);
        }
    }
}

__attribute__((format(printf, 3, 4)))
static void respond_error(int64_t id, const char *code, const char *fmt, ...) {
    char msg[640];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);
    size_t n = strlen(msg);
    if (n > MAX_MSG) n = MAX_MSG;
    printf("{\"id\":%" PRId64 ",\"ok\":false,\"error\":{\"code\":\"%s\",\"message\":\"", id, code);
    put_json_escaped(msg, n);
    fputs("\"}}\n", stdout);
    line_done();
}

static void respond_ping(int64_t id) {
    printf("{\"id\":%" PRId64 ",\"ok\":true,\"pong\":true}\n", id);
    line_done();
}

static void respond_caps(int64_t id) {
    printf("{\"id\":%" PRId64 ",\"ok\":true,\"vision\":%s,\"audio\":%s,\"sample_rate\":%d,\"dim\":%d}\n",
           id, g_cap_vision ? "true" : "false", g_cap_audio ? "true" : "false", g_sample_rate, DIM_OUT);
    line_done();
}

/* ------------------------------------------------------------ JSON: flat-object parser (amendment B4.2) */

enum { K_OP = 1u << 0, K_ID = 1u << 1, K_KIND = 1u << 2, K_BUDGET = 1u << 3,
       K_PATH = 1u << 4, K_WAV = 1u << 5 };

typedef struct {
    unsigned have;          /* K_* bitmask: presence also detects duplicate keys */
    int64_t id, budget;
    char *op, *kind, *path_b64, *wav_b64;   /* heap, NUL-terminated; a decoded NUL is bad_request */
    size_t op_len, kind_len, path_len, wav_len;
} request_t;

static void request_free(request_t *r) {
    free(r->op);
    free(r->kind);
    free(r->path_b64);
    free(r->wav_b64);
    memset(r, 0, sizeof *r);
}

typedef struct { const char *s; size_t len, i; } jp_t;

static void jp_ws(jp_t *p) {
    while (p->i < p->len) {
        char c = p->s[p->i];
        if (c == ' ' || c == '\t' || c == '\r' || c == '\n') p->i++;
        else break;
    }
}

static int jp_hex4(jp_t *p, uint32_t *out) {
    if (p->len - p->i < 4) return -1;
    uint32_t v = 0;
    for (int k = 0; k < 4; k++) {
        char c = p->s[p->i + (size_t)k];
        v <<= 4;
        if (c >= '0' && c <= '9') v |= (uint32_t)(c - '0');
        else if (c >= 'a' && c <= 'f') v |= (uint32_t)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F') v |= (uint32_t)(c - 'A' + 10);
        else return -1;
    }
    p->i += 4;
    *out = v;
    return 0;
}

/* Full string grammar: \" \\ \/ \b \f \n \r \t \uXXXX incl. surrogate pairs, decoded to UTF-8.
   Raw control characters and any decoded NUL are bad_request (request strings become C
   strings and base64, neither of which can carry an embedded NUL).
   Returns 0 ok, -1 grammar, -2 allocation; on 0 the buffer holds content + '\0'. */
static int jp_string(jp_t *p, sbuf_t *out) {
    out->len = 0;
    if (p->i >= p->len || p->s[p->i] != '"') return -1;
    p->i++;
    for (;;) {
        if (p->i >= p->len) return -1;
        unsigned char c = (unsigned char)p->s[p->i];
        if (c == '"') {
            p->i++;
            break;
        }
        if (c == '\\') {
            p->i++;
            if (p->i >= p->len) return -1;
            char e = p->s[p->i];
            if (e == 'u') {
                p->i++;
                uint32_t cp;
                if (jp_hex4(p, &cp) != 0) return -1;
                if (cp >= 0xD800 && cp <= 0xDBFF) {          /* high surrogate needs its pair */
                    if (p->len - p->i < 2 || p->s[p->i] != '\\' || p->s[p->i + 1] != 'u') return -1;
                    p->i += 2;
                    uint32_t lo;
                    if (jp_hex4(p, &lo) != 0) return -1;
                    if (lo < 0xDC00 || lo > 0xDFFF) return -1;
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                    return -1;                               /* lone low surrogate */
                }
                if (cp == 0) return -1;                      /* NUL inside a request string */
                if (!sbuf_put_utf8(out, cp)) return -2;
            } else {
                static const char esc[] = "\"\\/bfnrt";
                static const char raw[] = "\"\\/\b\f\n\r\t";
                const char *hit = strchr(esc, e);
                if (e == 0 || !hit) return -1;
                if (!sbuf_putc(out, raw[hit - esc])) return -2;
                p->i++;
            }
        } else {
            if (c < 0x20) return -1;                         /* raw control character */
            if (!sbuf_putc(out, (char)c)) return -2;
            p->i++;
        }
    }
    return sbuf_putc(out, '\0') ? 0 : -2;
}

/* Integers only (id, budget). strtoll does the range check; the shape scan keeps "1.5"/"1e3"
   from silently truncating. ERANGE -> bad_request. */
static int jp_number(jp_t *p, int64_t *out) {
    size_t s = p->i;
    if (p->i < p->len && p->s[p->i] == '-') p->i++;
    size_t d0 = p->i;
    while (p->i < p->len && p->s[p->i] >= '0' && p->s[p->i] <= '9') p->i++;
    if (p->i == d0) return -1;
    if (p->i < p->len) {
        char c = p->s[p->i];
        if (c == '.' || c == 'e' || c == 'E') return -1;
    }
    size_t n = p->i - s;
    char tmp[32];
    if (n >= sizeof tmp) return -1;
    memcpy(tmp, p->s + s, n);
    tmp[n] = 0;
    errno = 0;
    char *end = NULL;
    long long v = strtoll(tmp, &end, 10);
    if (errno == ERANGE || end != tmp + n) return -1;
    *out = (int64_t)v;
    return 0;
}

/* Unknown keys are ignored for forward compatibility; their values may be anything, so this
   skips strings (with escapes), numbers, literals and one nested object/array. */
static int jp_skip(jp_t *p) {
    jp_ws(p);
    if (p->i >= p->len) return -1;
    char c = p->s[p->i];
    if (c == '"') {
        sbuf_t tmp = {0};
        int rc = jp_string(p, &tmp);
        sbuf_free(&tmp);
        return rc == 0 ? 0 : -1;
    }
    if (c == '{' || c == '[') {
        int depth = 0;
        bool in_str = false, esc = false;
        for (; p->i < p->len; p->i++) {
            char d = p->s[p->i];
            if (in_str) {
                if (esc) esc = false;
                else if (d == '\\') esc = true;
                else if (d == '"') in_str = false;
                continue;
            }
            if (d == '"') in_str = true;
            else if (d == '{' || d == '[') depth++;
            else if (d == '}' || d == ']') {
                if (--depth == 0) {
                    p->i++;
                    return 0;
                }
            }
        }
        return -1;
    }
    if (p->len - p->i >= 4 && memcmp(p->s + p->i, "true", 4) == 0) { p->i += 4; return 0; }
    if (p->len - p->i >= 5 && memcmp(p->s + p->i, "false", 5) == 0) { p->i += 5; return 0; }
    if (p->len - p->i >= 4 && memcmp(p->s + p->i, "null", 4) == 0) { p->i += 4; return 0; }
    if (c == '-' || (c >= '0' && c <= '9')) {
        int64_t v;
        return jp_number(p, &v);
    }
    return -1;
}

/* Depth-1 object over the known key set. 0 ok, -1 bad_request, -2 internal (allocation). */
static int parse_request(const char *line, size_t len, request_t *r) {
    jp_t p = {line, len, 0};
    sbuf_t key = {0}, val = {0};
    int rc = -1;
    jp_ws(&p);
    if (p.i >= p.len || p.s[p.i] != '{') goto out;
    p.i++;
    jp_ws(&p);
    if (p.i < p.len && p.s[p.i] == '}') {
        p.i++;
        goto done;
    }
    for (;;) {
        jp_ws(&p);
        int src = jp_string(&p, &key);
        if (src != 0) { rc = src; goto out; }
        jp_ws(&p);
        if (p.i >= p.len || p.s[p.i] != ':') goto out;
        p.i++;
        jp_ws(&p);
        size_t klen = key.len - 1;                     /* jp_string appends the NUL */
        unsigned kid = 0;
        if (klen == 2 && memcmp(key.p, "op", 2) == 0) kid = K_OP;
        else if (klen == 2 && memcmp(key.p, "id", 2) == 0) kid = K_ID;
        else if (klen == 4 && memcmp(key.p, "kind", 4) == 0) kid = K_KIND;
        else if (klen == 6 && memcmp(key.p, "budget", 6) == 0) kid = K_BUDGET;
        else if (klen == 8 && memcmp(key.p, "path_b64", 8) == 0) kid = K_PATH;
        else if (klen == 7 && memcmp(key.p, "wav_b64", 7) == 0) kid = K_WAV;
        if (kid == 0) {
            if (jp_skip(&p) != 0) goto out;            /* unknown key: ignore, any value type */
        } else {
            if (r->have & kid) goto out;               /* duplicate key -> bad_request */
            if (kid == K_ID || kid == K_BUDGET) {
                int64_t v;
                if (jp_number(&p, &v) != 0) goto out;
                if (kid == K_ID) r->id = v;
                else r->budget = v;
            } else {
                src = jp_string(&p, &val);
                if (src != 0) { rc = src; goto out; }
                char **slot = kid == K_OP ? &r->op : kid == K_KIND ? &r->kind :
                              kid == K_PATH ? &r->path_b64 : &r->wav_b64;
                size_t *lslot = kid == K_OP ? &r->op_len : kid == K_KIND ? &r->kind_len :
                                kid == K_PATH ? &r->path_len : &r->wav_len;
                *slot = val.p;                          /* steal the buffer (NUL-terminated) */
                *lslot = val.len - 1;
                val.p = NULL;
                val.cap = 0;
            }
            r->have |= kid;
        }
        jp_ws(&p);
        if (p.i < p.len && p.s[p.i] == ',') {
            p.i++;
            continue;
        }
        if (p.i < p.len && p.s[p.i] == '}') {
            p.i++;
            break;
        }
        goto out;
    }
done:
    jp_ws(&p);
    if (p.i != p.len) goto out;                        /* trailing garbage */
    rc = 0;
out:
    sbuf_free(&key);
    sbuf_free(&val);
    return rc;
}

/* ------------------------------------------------------------ ImageSizing port (design §3) */

typedef struct { int h, w, zero_both; } target_size_t;

/* transformers' Gemma image processor: scale to fill `budget` soft tokens (each 3x3 patches
   of 16 px), aspect kept, each side rounded DOWN to a multiple of 48 px. Swift semantics:
   Int() truncates toward zero, Int/Int divides truncating — the rescue rules below rely on
   the integer division happening FIRST. */
static target_size_t image_sizing_target(int height, int width, int budget) {
    target_size_t t = {0, 0, 0};
    const int patch = 16, pooling = 3;
    const int max_patches = budget * pooling * pooling;
    const double factor = sqrt((double)max_patches * (double)(patch * patch) /
                               ((double)height * (double)width));
    const int side = pooling * patch;                              /* 48 */
    t.h = (int)floor(factor * (double)height / side) * side;
    t.w = (int)floor(factor * (double)width / side) * side;
    if (t.h == 0 && t.w == 0) {
        t.zero_both = 1;                                           /* "image too thin to embed" */
        return t;
    }
    const int max_side = (max_patches / (pooling * pooling)) * side;   /* exact: budget * 48 */
    if (t.h == 0) {
        t.h = side;
        t.w = width / height * side;                               /* integer division first */
        if (t.w > max_side) t.w = max_side;
    } else if (t.w == 0) {
        t.w = side;
        t.h = height / width * side;
        if (t.h > max_side) t.h = max_side;
    }
    return t;
}

/* Catmull-Rom bicubic with a = -0.5 (Pillow's choice; GGML/PyTorch use -0.75). */
static double bicubic_filter(double value) {
    const double x = fabs(value), a = -0.5;
    if (x < 1) return ((a + 2) * x - (a + 3)) * x * x + 1;
    if (x < 2) return (((x - 5) * x + 8) * x - 4) * a;
    return 0;
}

typedef struct { int *bounds; int32_t *weights; int size; } coeffs_t;

static void coeffs_free(coeffs_t *c) {
    free(c->bounds);
    free(c->weights);
    memset(c, 0, sizeof *c);
}

/* Per-output-pixel weights in 22-bit fixed point (32 - 8 bit pixels - 2 headroom): a channel
   sum then stays under 255 * 2^22 + 2^21 < 2^31. Half-pixel centers, kernel widening when
   shrinking, per-pixel normalization when total != 0, quantization adds +/-0.5 then
   truncates toward zero (never std::round — that would round twice). */
static int coeffs_init(coeffs_t *c, int in_size, int out_size) {
    memset(c, 0, sizeof *c);
    const double scale = (double)in_size / (double)out_size;
    const double filter_scale = scale > 1.0 ? scale : 1.0;
    const double support = 2 * filter_scale;
    const int size = (int)ceil(support) * 2 + 1;
    c->bounds = malloc((size_t)out_size * 2 * sizeof(int));
    c->weights = malloc((size_t)out_size * (size_t)size * sizeof(int32_t));
    double *row = malloc((size_t)size * sizeof(double));
    if (!c->bounds || !c->weights || !row) {
        free(row);
        coeffs_free(c);
        return -1;
    }
    c->size = size;
    for (int out = 0; out < out_size; out++) {
        const double center = ((double)out + 0.5) * scale;
        int first = (int)(center - support + 0.5);          /* truncate toward zero, then clamp */
        if (first < 0) first = 0;
        int count = (int)(center + support + 0.5);
        if (count > in_size) count = in_size;
        count -= first;
        double total = 0;
        for (int x = 0; x < size; x++) {
            row[x] = x < count
                         ? bicubic_filter(((double)(x + first) - center + 0.5) / filter_scale)
                         : 0;
            total += row[x];
        }
        for (int x = 0; x < size; x++) {
            const double w = (x < count && total != 0) ? row[x] / total : row[x];
            c->weights[(size_t)out * size + x] =
                (int32_t)(w * (double)(1 << 22) + (w < 0 ? -0.5 : 0.5));
        }
        c->bounds[out * 2] = first;
        c->bounds[out * 2 + 1] = count;
    }
    free(row);
    return 0;
}

/* acc >> 22 then clamp 0..255. int64 accumulators are belt-and-braces over the Swift Int32
   (the bound already fits int32); no float in the apply loop. */
static inline uint8_t clip_u8(int64_t acc) {
    int64_t v = acc >> 22;
    if (v < 0) return 0;
    if (v > 255) return 255;
    return (uint8_t)v;
}

static uint8_t *resample_h(const uint8_t *src, int in_w, int rows, int to_w) {
    coeffs_t c;
    if (coeffs_init(&c, in_w, to_w) != 0) return NULL;
    uint8_t *out = malloc((size_t)to_w * (size_t)rows * 3);
    if (!out) {
        coeffs_free(&c);
        return NULL;
    }
    for (int y = 0; y < rows; y++) {
        const uint8_t *srow = src + (size_t)y * (size_t)in_w * 3;
        uint8_t *orow = out + (size_t)y * (size_t)to_w * 3;
        for (int x = 0; x < to_w; x++) {
            const int first = c.bounds[x * 2], count = c.bounds[x * 2 + 1];
            const uint8_t *p = srow + (size_t)first * 3;
            int64_t r = 1 << 21, g = 1 << 21, b = 1 << 21;    /* rounding bias: 0.5 in fixed point */
            for (int k = 0; k < count; k++) {
                const int64_t w = c.weights[(size_t)x * c.size + k];
                r += (int64_t)p[0] * w;
                g += (int64_t)p[1] * w;
                b += (int64_t)p[2] * w;
                p += 3;
            }
            orow[x * 3 + 0] = clip_u8(r);
            orow[x * 3 + 1] = clip_u8(g);
            orow[x * 3 + 2] = clip_u8(b);
        }
    }
    coeffs_free(&c);
    return out;
}

static uint8_t *resample_v(const uint8_t *src, int row_w, int rows, int to_h) {
    coeffs_t c;
    if (coeffs_init(&c, rows, to_h) != 0) return NULL;
    const size_t stride = (size_t)row_w * 3;
    uint8_t *out = malloc(stride * (size_t)to_h);
    int64_t *acc = malloc(stride * sizeof(int64_t));
    if (!out || !acc) {
        free(out);
        free(acc);
        coeffs_free(&c);
        return NULL;
    }
    for (int y = 0; y < to_h; y++) {
        const int first = c.bounds[y * 2], count = c.bounds[y * 2 + 1];
        for (size_t i = 0; i < stride; i++) acc[i] = 1 << 21;
        for (int k = 0; k < count; k++) {
            const uint8_t *row = src + (size_t)(first + k) * stride;
            const int64_t w = c.weights[(size_t)y * c.size + k];
            for (size_t i = 0; i < stride; i++) acc[i] += (int64_t)row[i] * w;
        }
        for (size_t i = 0; i < stride; i++) out[(size_t)y * stride + i] = clip_u8(acc[i]);
    }
    free(acc);
    coeffs_free(&c);
    return out;
}

/* Packed RGB. Both dims change -> horizontal to (to_w x h), then vertical; a single changed
   dim runs its pass alone; no change -> byte copy. Pass ORDER is load-bearing (Swift :34-35). */
static uint8_t *resize_bicubic(const uint8_t *src, int w, int h, int to_w, int to_h) {
    const bool hw = to_w != w, vh = to_h != h;
    if (hw && vh) {
        uint8_t *mid = resample_h(src, w, h, to_w);
        if (!mid) return NULL;
        uint8_t *out = resample_v(mid, to_w, h, to_h);
        free(mid);
        return out;
    }
    if (hw) return resample_h(src, w, h, to_w);
    if (vh) return resample_v(src, w, h, to_h);
    uint8_t *out = malloc((size_t)w * (size_t)h * 3);
    if (out) memcpy(out, src, (size_t)w * (size_t)h * 3);
    return out;
}

/* ------------------------------------------------------------ --selftest-resize (no models, no stdin) */

/* px(x,y,c) = (x*7 + y*13 + c*29) & 255 — deterministic, exercises the kernel's negative
   lobe (channel c offsets each pixel by 29). */
static uint8_t *gradient_image(int w, int h) {
    uint8_t *px = malloc((size_t)w * (size_t)h * 3);
    if (!px) return NULL;
    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            uint8_t *p = px + ((size_t)y * (size_t)w + (size_t)x) * 3;
            for (int c = 0; c < 3; c++) p[c] = (uint8_t)((x * 7 + y * 13 + c * 29) & 255);
        }
    }
    return px;
}

static int selftest_line(const char *name, int h, int w, int budget, const target_size_t *t,
                         const uint8_t *out) {
    if (!out) return -1;
    char hex[65];
    sha256_hex(out, (size_t)t->h * (size_t)t->w * 3, hex);
    const size_t n = (size_t)t->h * (size_t)t->w * 3;
    const size_t mid = ((size_t)(t->h / 2) * (size_t)t->w + (size_t)(t->w / 2)) * 3;
    printf("{\"case\":\"%s\",\"in\":[%d,%d],\"budget\":%d,\"target\":[%d,%d],"
           "\"out_sha256\":\"%s\",\"first_pixel\":[%u,%u,%u],\"center_pixel\":[%u,%u,%u],"
           "\"last_pixel\":[%u,%u,%u],\"size\":%zu}\n",
           name, h, w, budget, t->h, t->w, hex, out[0], out[1], out[2], out[mid], out[mid + 1],
           out[mid + 2], out[n - 3], out[n - 2], out[n - 1], n);
    line_done();
    return 0;
}

static int selftest_resize(void) {
    printf("{\"event\":\"selftest\",\"helper\":\"%s\"}\n", HELPER_VERSION);
    line_done();
    static const struct { const char *name; int h, w, budget; } cases[] = {
        {"gradient-1024x768-b140", 1024, 768, 140},   /* design §3.1 worked example */
        {"gradient-1280x800-b280", 1280, 800, 280},
        {"gradient-300x200-b70", 300, 200, 70},       /* upscale: sips never does this, we do */
        {"gradient-94x100-b140", 94, 100, 140},       /* small input, large upscale */
        {"gradient-48x48-b1120", 48, 48, 1120},       /* max budget from the minimum image */
        {"gradient-2048x100-b140", 2048, 100, 140},   /* extreme aspect */
        {"gradient-256x256-b140", 256, 256, 140},     /* 256x256 -> 528x528 upscale pin */
    };
    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {
        uint8_t *img = gradient_image(cases[i].w, cases[i].h);
        target_size_t t = image_sizing_target(cases[i].h, cases[i].w, cases[i].budget);
        uint8_t *out = img ? resize_bicubic(img, cases[i].w, cases[i].h, t.w, t.h) : NULL;
        int rc = selftest_line(cases[i].name, cases[i].h, cases[i].w, cases[i].budget, &t, out);
        free(out);
        free(img);
        if (rc != 0) return 2;
    }
    /* Identity: documents the byte-copy path inside resize_bicubic (target == input). */
    uint8_t *img = gradient_image(480, 480);
    target_size_t t = {480, 480, 0};
    uint8_t *out = img ? resize_bicubic(img, 480, 480, 480, 480) : NULL;
    int rc = selftest_line("gradient-480x480-identity", 480, 480, 140, &t, out);
    free(out);
    free(img);
    return rc == 0 ? 0 : 2;
}

/* ------------------------------------------------------------ embedding */

static void cleanup(void) {
    if (g_batch) llama_batch_ext_free(g_batch);
    if (g_media) mtmd_free(g_media);
    if (g_lctx) llama_free(g_lctx);
    if (g_model) llama_model_free(g_model);
    g_batch = NULL;
    g_media = NULL;
    g_lctx = NULL;
    g_model = NULL;
}

/* DigUp LlamaEmbedder.place: pos[0] carries the sequence position (non-M-RoPE reads only it),
   every entry becomes a pooling output. */
static int batch_place(int32_t idx, llama_pos *pos) {
    if (idx < 0) return -1;
    llama_pos p[4] = {(*pos)++, 0, 0, 0};
    if (!llama_batch_ext_set_pos(g_batch, idx, p)) return -1;
    if (!llama_batch_ext_set_output_embd(g_batch, idx, true)) return -1;
    return 0;
}

/* One text embed ("<bos> text <eos>"), used only for the two startup warmups that build the
   Metal pipelines. Truncation is transformers' cut: tokens[0..ctx-2] + <eos>. */
static int embed_text_once(const char *text) {
    const int32_t text_len = (int32_t)strlen(text);
    int32_t n = llama_tokenize(g_vocab, text, text_len, NULL, 0, true, true);
    if (n == INT32_MIN) return -1;
    if (n < 0) n = -n;    /* size probe: the needed count comes back negative (llama.h) */
    if (n == 0) return -1;
    llama_token *toks = malloc((size_t)n * sizeof(llama_token));
    if (!toks) return -1;
    int32_t m = llama_tokenize(g_vocab, text, text_len, toks, n, true, true);
    if (m < 0 || m > n) {
        free(toks);
        return -1;
    }
    if (m > g_n_ctx) {
        toks[g_n_ctx - 1] = llama_vocab_eos(g_vocab);
        m = g_n_ctx;
    }
    llama_batch_ext_clear(g_batch);
    llama_pos pos = 0;
    for (int32_t k = 0; k < m; k++) {
        int32_t idx = llama_batch_ext_add_token(g_batch, 0, toks[k]);
        if (idx < 0 || batch_place(idx, &pos) != 0) {
            free(toks);
            llama_batch_ext_clear(g_batch);
            return -1;
        }
    }
    free(toks);
    if (llama_process(g_lctx, LLAMA_PROCESS_TYPE_ENCODE, g_batch) != 0) {
        llama_batch_ext_clear(g_batch);
        return -1;
    }
    llama_batch_ext_clear(g_batch);
    return llama_get_embeddings_seq(g_lctx, 0) != NULL ? 0 : -1;
}

/* The shared tail of both modalities: one marker tokenized against one bitmap, encoder rows
   and text tokens in one sequence, one llama_process, MEAN pool + L2 (double accumulation —
   DigUp's exact normalization; the Python l2_post layer on top is a float32 no-op).
   Takes bitmap ownership; frees chunks and bitmap on every path, clears the batch on the
   way out so a failed request cannot leak into the next one. */
static void run_bitmap_embed(mtmd_bitmap *bm, int64_t id) {
    mtmd_input_chunks *chunks = mtmd_input_chunks_init();
    if (!chunks) {
        respond_error(id, "internal", "out of memory");
        mtmd_bitmap_free(bm);
        return;
    }
    const char *marker = mtmd_default_marker();
    struct mtmd_input_text it = {marker, strlen(marker), /*add_special*/ true,
                                 /*parse_special*/ true};
    const mtmd_bitmap *bitmaps[1] = {bm};
    const int32_t st = mtmd_tokenize(g_media, chunks, &it, bitmaps, 1);
    if (st != 0) {
        respond_error(id, "tokenize_failed", "mtmd_tokenize failed (%d)", (int)st);
        goto out;
    }
    /* Mean pooling needs the whole sequence in one ubatch. */
    const size_t n_chunks = mtmd_input_chunks_size(chunks);
    size_t total = 0;
    for (size_t i = 0; i < n_chunks; i++)
        total += mtmd_input_chunk_get_n_tokens(mtmd_input_chunks_get(chunks, i));
    if (total > (size_t)g_n_ctx - 1) {
        respond_error(id, "too_many_tokens", "%zu tokens exceed ctx %d", total, g_n_ctx);
        goto out;
    }
    llama_batch_ext_clear(g_batch);
    llama_pos pos = 0;
    for (size_t i = 0; i < n_chunks; i++) {
        const mtmd_input_chunk *ch = mtmd_input_chunks_get(chunks, i);
        if (mtmd_input_chunk_get_type(ch) == MTMD_INPUT_CHUNK_TYPE_TEXT) {
            size_t n = 0;
            const llama_token *toks = mtmd_input_chunk_get_tokens_text(ch, &n);
            for (size_t k = 0; k < n; k++) {
                int32_t idx = llama_batch_ext_add_token(g_batch, 0, toks[k]);
                if (idx < 0 || batch_place(idx, &pos) != 0) {
                    respond_error(id, "internal", "can't place token %zu in the batch", k);
                    goto out;
                }
            }
        } else {
            if (mtmd_encode_chunk(g_media, ch) != 0) {
                respond_error(id, "encode_failed", "the encoder failed on this input");
                goto out;
            }
            const float *rows = mtmd_get_output_embd(g_media);
            if (!rows) {
                respond_error(id, "encode_failed", "no encoder output rows");
                goto out;
            }
            const size_t n = mtmd_input_chunk_get_n_tokens(ch);
            for (size_t r = 0; r < n; r++) {
                struct llama_embd e = {rows + r * (size_t)g_dim_in, 1, (size_t)g_dim_in};
                int32_t idx = llama_batch_ext_add_embd(g_batch, 0, e);   /* copied by the batch */
                if (idx < 0 || batch_place(idx, &pos) != 0) {
                    respond_error(id, "internal", "can't place encoder row %zu in the batch", r);
                    goto out;
                }
            }
        }
    }
    if (llama_process(g_lctx, LLAMA_PROCESS_TYPE_ENCODE, g_batch) != 0) {
        respond_error(id, "process_failed", "llama_process failed");
        goto out;
    }
    const float *pooled = llama_get_embeddings_seq(g_lctx, 0);
    if (!pooled) {
        respond_error(id, "process_failed", "no pooled embedding for sequence 0");
        goto out;
    }
    float vec[DIM_OUT];
    double sum = 0;
    for (int i = 0; i < DIM_OUT; i++) sum += (double)pooled[i] * (double)pooled[i];
    const float scale = sum > 0 ? (float)(1.0 / sqrt(sum)) : 0.0f;
    for (int i = 0; i < DIM_OUT; i++) vec[i] = pooled[i] * scale;
    char *b64 = b64_encode((const uint8_t *)vec, sizeof vec);   /* 768 float32, host LE */
    if (!b64) {
        respond_error(id, "internal", "out of memory");
        goto out;
    }
    printf("{\"id\":%" PRId64 ",\"ok\":true,\"vec_b64\":\"%s\"}\n", id, b64);
    free(b64);
    line_done();
out:
    mtmd_input_chunks_free(chunks);
    mtmd_bitmap_free(bm);
    llama_batch_ext_clear(g_batch);
}

static void handle_embed_image(const request_t *r) {
    /* DigUp budgets: LlamaEmbedder.budgets. */
    static const int legal[] = {70, 140, 280, 560, 1120};
    if (!(r->have & K_BUDGET)) {
        respond_error(r->id, "budget_invalid", "budget must be one of 70/140/280/560/1120");
        return;
    }
    bool ok = false;
    for (size_t i = 0; i < sizeof legal / sizeof legal[0]; i++) ok |= r->budget == legal[i];
    if (!ok) {
        respond_error(r->id, "budget_invalid", "budget must be one of 70/140/280/560/1120");
        return;
    }
    if (!(r->have & K_PATH)) {
        respond_error(r->id, "bad_request", "embed kind image needs path_b64");
        return;
    }
    if (!g_cap_vision) {
        respond_error(r->id, "unsupported_modality", "mmproj has no vision support");
        return;
    }
    uint8_t *path_bytes = NULL;
    size_t path_len = 0;
    const int d = b64_decode(r->path_b64, r->path_len, &path_bytes, &path_len);
    if (d == -1) {
        respond_error(r->id, "bad_request", "path_b64 is not valid base64");
        return;
    }
    if (d == -2) {
        respond_error(r->id, "internal", "out of memory");
        return;
    }
    /* Paths may be non-UTF-8 bytes; the C API needs a NUL-terminated C string. */
    char *path = malloc(path_len + 1);
    if (!path) {
        free(path_bytes);
        respond_error(r->id, "internal", "out of memory");
        return;
    }
    memcpy(path, path_bytes, path_len);
    path[path_len] = 0;
    free(path_bytes);

    struct mtmd_helper_bitmap_wrapper w =
        mtmd_helper_bitmap_init_from_file(g_media, path, false, mtmd_helper_init_opt_default());
    if (w.video_ctx) {
        if (w.bitmap) mtmd_bitmap_free(w.bitmap);
        mtmd_helper_video_free(w.video_ctx);
        respond_error(r->id, "video_not_supported",
                      "video goes in as frames and sound, not as a file: %s", path);
        free(path);
        return;
    }
    if (!w.bitmap) {
        respond_error(r->id, "decode_failed", "can't read %s", path);
        free(path);
        return;
    }
    mtmd_bitmap *bm = w.bitmap;
    if (mtmd_bitmap_is_audio(bm)) {
        respond_error(r->id, "not_image", "not an image: %s", path);
        mtmd_bitmap_free(bm);
        free(path);
        return;
    }
    const uint32_t nx = mtmd_bitmap_get_nx(bm), ny = mtmd_bitmap_get_ny(bm);
    target_size_t t = image_sizing_target((int)ny, (int)nx, (int)r->budget);
    if (t.zero_both) {
        respond_error(r->id, "too_thin", "image too thin to embed (%ux%u at budget %lld)",
                      ny, nx, (long long)r->budget);
        mtmd_bitmap_free(bm);
        free(path);
        return;
    }
    if (t.h != (int)ny || t.w != (int)nx) {
        uint8_t *out = resize_bicubic(mtmd_bitmap_get_data(bm), (int)nx, (int)ny, t.w, t.h);
        mtmd_bitmap *resized = out ? mtmd_bitmap_init((uint32_t)t.w, (uint32_t)t.h, out) : NULL;
        free(out);
        mtmd_bitmap_free(bm);
        bm = resized;
        if (!bm) {
            respond_error(r->id, "internal", "can't resize %s to %dx%d", path, t.h, t.w);
            free(path);
            return;
        }
    }
    free(path);
    run_bitmap_embed(bm, r->id);   /* takes ownership of the bitmap */
}

static void handle_embed_audio(const request_t *r) {
    if (!(r->have & K_WAV)) {
        respond_error(r->id, "bad_request", "embed kind audio needs wav_b64");
        return;
    }
    if (!g_cap_audio) {
        respond_error(r->id, "unsupported_modality", "mmproj has no audio support");
        return;
    }
    uint8_t *wav = NULL;
    size_t wav_len = 0;
    const int d = b64_decode(r->wav_b64, r->wav_len, &wav, &wav_len);
    if (d == -1) {
        respond_error(r->id, "bad_request", "wav_b64 is not valid base64");
        return;
    }
    if (d == -2) {
        respond_error(r->id, "internal", "out of memory");
        return;
    }
    struct mtmd_helper_bitmap_wrapper w =
        mtmd_helper_bitmap_init_from_buf(g_media, wav, wav_len, false,
                                         mtmd_helper_init_opt_default());
    free(wav);
    if (w.video_ctx) {
        if (w.bitmap) mtmd_bitmap_free(w.bitmap);
        mtmd_helper_video_free(w.video_ctx);
        respond_error(r->id, "video_not_supported", "video goes in as frames and sound, not as a file");
        return;
    }
    if (!w.bitmap) {
        respond_error(r->id, "decode_failed", "can't decode the wav bytes");
        return;
    }
    mtmd_bitmap *bm = w.bitmap;
    if (!mtmd_bitmap_is_audio(bm)) {
        respond_error(r->id, "not_audio", "not audio: the bytes decoded as an image");
        mtmd_bitmap_free(bm);
        return;
    }
    /* 30 s defensive cut (DigUp LlamaEmbedder:220-228): goldmine windows are <= 30 s by
       construction; this is parity plus safety. */
    const size_t samples = mtmd_bitmap_get_n_bytes(bm) / sizeof(float);
    const size_t limit = (size_t)30 * (size_t)g_sample_rate;
    if (samples > limit) {
        mtmd_bitmap *prefix =
            mtmd_bitmap_init_from_audio(limit, (const float *)mtmd_bitmap_get_data(bm));
        mtmd_bitmap_free(bm);
        bm = prefix;
        if (!bm) {
            respond_error(r->id, "internal", "can't cut the audio at 30 s");
            return;
        }
    }
    run_bitmap_embed(bm, r->id);   /* takes ownership of the bitmap */
}

/* Returns 1 when the helper should exit ({"op":"quit"}). */
static int handle_request(const request_t *r) {
    llama_batch_ext_clear(g_batch);   /* every request starts from an empty batch */
    int quit = 0;
    if (!(r->have & K_OP)) {
        respond_error(r->id, "bad_request", "request needs an \"op\"");
    } else if (strcmp(r->op, "ping") == 0) {
        respond_ping(r->id);
    } else if (strcmp(r->op, "caps") == 0) {
        respond_caps(r->id);
    } else if (strcmp(r->op, "embed") == 0) {
        if (!(r->have & K_KIND)) {
            respond_error(r->id, "bad_request", "embed needs a \"kind\"");
        } else if (strcmp(r->kind, "image") == 0) {
            handle_embed_image(r);
        } else if (strcmp(r->kind, "audio") == 0) {
            handle_embed_audio(r);
        } else {
            respond_error(r->id, "unsupported_modality", "unknown embed kind: %s", r->kind);
        }
    } else if (strcmp(r->op, "quit") == 0) {
        quit = 1;
    } else {
        respond_error(r->id, "unknown_op", "unknown op: %s", r->op);
    }
    llama_batch_ext_clear(g_batch);   /* ...and leaves it clean, success or error */
    return quit;
}

/* ------------------------------------------------------------ read loop */

/* read() with unconditional EINTR retry; a SIGTERM that lands while blocked reads as a
   clean stop (the handler only sets the flag — request boundaries are the only checkpoints). */
static ssize_t read_some(void *p, size_t n) {
    for (;;) {
        ssize_t got = read(STDIN_FILENO, p, n);
        if (got >= 0) return got;
        if (errno == EINTR) {
            if (g_stop) return 0;
            continue;
        }
        return -1;
    }
}

/* Returns the process exit code: 0 (EOF / quit / SIGTERM at a boundary) or 3 (stdin read
   error that is not EOF). A request in flight always completes first. */
static int serve(void) {
    const size_t hard_cap = MAX_LINE + 1;   /* a line of exactly 16 MiB + '\n' still fits */
    size_t cap = 1u << 16, len = 0;
    char *buf = malloc(cap);
    if (!buf) {
        fprintf(stderr, "goldmine-embed: out of memory\n");
        return 2;
    }
    int discarding = 0;   /* oversize line: drop bytes until its newline, then bad_request */
    for (;;) {
        if (g_stop) break;
        char *nl = memchr(buf, '\n', len);
        if (nl) {
            const size_t line_len = (size_t)(nl - buf);
            if (discarding) {
                discarding = 0;
                respond_error(0, "bad_request", "request line exceeds 16 MiB");
            } else {
                request_t r;
                memset(&r, 0, sizeof r);
                const int prc = parse_request(buf, line_len, &r);
                if (prc == 0) {
                    const int quit = handle_request(&r);
                    request_free(&r);
                    if (quit) break;
                } else {
                    request_free(&r);
                    if (prc == -2) respond_error(0, "internal", "out of memory");
                    else respond_error(0, "bad_request", "request is not a flat JSON object");
                }
            }
            len -= line_len + 1;
            memmove(buf, nl + 1, len);
            continue;
        }
        if (!discarding && len >= hard_cap) {
            discarding = 1;   /* refuse the line, not the ~1 GB process (amendment B4.3) */
            len = 0;
        }
        if (discarding) {
            char scratch[1u << 16];
            const ssize_t got = read_some(scratch, sizeof scratch);
            if (got == 0) goto eof_or_stop;
            if (got < 0) goto read_error;
            const char *d = memchr(scratch, '\n', (size_t)got);
            if (d) {
                discarding = 0;
                respond_error(0, "bad_request", "request line exceeds 16 MiB");
                const size_t tail = (size_t)got - (size_t)(d - scratch) - 1;
                memcpy(buf, d + 1, tail);   /* tail < sizeof scratch <= cap */
                len = tail;
            }
            continue;
        }
        if (len == cap) {
            size_t ncap = cap * 2;
            if (ncap > hard_cap) ncap = hard_cap;
            char *nb = realloc(buf, ncap);
            if (!nb) {   /* cannot grow: treat as the oversize case, stay alive */
                discarding = 1;
                len = 0;
                continue;
            }
            buf = nb;
            cap = ncap;
        }
        const ssize_t got = read_some(buf + len, cap - len);
        if (got == 0) goto eof_or_stop;
        if (got < 0) goto read_error;
        len += (size_t)got;
        continue;

    read_error:
        fprintf(stderr, "goldmine-embed: stdin read error: %s\n", strerror(errno));
        free(buf);
        return 3;
    }
eof_or_stop:
    free(buf);
    return 0;
}

/* ------------------------------------------------------------ main / startup (design §2.1) */

static void usage(FILE *f) {
    fprintf(f,
            "usage: goldmine-embed --model TEXT.GGUF --mmproj MMPROJ.GGUF [--ctx 512|2048]\n"
            "                     [--n-gpu-layers N] [--version] [--selftest-resize]\n");
}

static double seconds_since(const struct timespec *t0) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    const double s = (double)(now.tv_sec - t0->tv_sec) +
                     (double)(now.tv_nsec - t0->tv_nsec) * 1e-9;
    return floor(s * 100.0) / 100.0;   /* DigUp's 2-decimal rounding */
}

static int parse_int_arg(const char *s, long long *out) {
    errno = 0;
    char *end = NULL;
    long long v = strtoll(s, &end, 10);
    if (errno != 0 || end == s || *end != 0) return -1;
    *out = v;
    return 0;
}

int main(int argc, char **argv) {
    /* First statement of main (amendment B3): stdout is line-buffered before anything else,
       and every emitted line is flushed explicitly as well. */
    setvbuf(stdout, NULL, _IOLBF, 0);

    const char *model_path = NULL, *mmproj_path = NULL;
    long long n_ctx = 2048, n_gpu_layers = 999;
    int want_selftest = 0, want_version = 0;
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        long long v = 0;
        if (strcmp(a, "--version") == 0) want_version = 1;
        else if (strcmp(a, "--selftest-resize") == 0) want_selftest = 1;
        else if (strcmp(a, "--model") == 0 && i + 1 < argc) model_path = argv[++i];
        else if (strcmp(a, "--mmproj") == 0 && i + 1 < argc) mmproj_path = argv[++i];
        else if (strcmp(a, "--ctx") == 0 && i + 1 < argc && parse_int_arg(argv[++i], &v) == 0) n_ctx = v;
        else if (strcmp(a, "--n-gpu-layers") == 0 && i + 1 < argc && parse_int_arg(argv[++i], &v) == 0) n_gpu_layers = v;
        else {
            fprintf(stderr, "goldmine-embed: bad flag: %s\n", a);
            usage(stderr);
            return 2;
        }
    }
    if (want_version) {
        printf("%s\n", HELPER_VERSION);
        line_done();
        return 0;
    }
    if (want_selftest) return selftest_resize();   /* no models, no stdin */
    if (!model_path || !mmproj_path) {
        fprintf(stderr, "goldmine-embed: --model and --mmproj are required\n");
        usage(stderr);
        return 2;
    }
    if (n_ctx != 512 && n_ctx != 2048) {   /* amendment minor-11 (Q2): only these two */
        fprintf(stderr, "goldmine-embed: --ctx must be 512 or 2048\n");
        return 2;
    }

    signal(SIGPIPE, SIG_IGN);   /* writes to a dead parent return EPIPE instead of killing us */
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = stop_handler;
    sigemptyset(&sa.sa_mask);
    /* No SA_RESTART: a read() blocked between requests must wake up to notice SIGTERM. */
    sigaction(SIGTERM, &sa, NULL);

    llama_log_set(log_warn_plus, NULL);
    mtmd_helper_log_set(log_warn_plus, NULL);
    llama_backend_init();

    struct timespec t0;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    /* 1. text model */
    struct llama_model_params mp = llama_model_default_params();
    mp.n_gpu_layers = (int32_t)n_gpu_layers;
    g_model = llama_model_load_from_file(model_path, mp);
    if (!g_model) {
        fprintf(stderr, "goldmine-embed: can't load %s\n", model_path);
        return 2;
    }

    /* 2. context — frozen params; one input must fit one ubatch */
    struct llama_context_params cp = llama_context_default_params();
    cp.n_ctx = (uint32_t)n_ctx;
    cp.n_batch = (uint32_t)n_ctx;
    cp.n_ubatch = (uint32_t)n_ctx;
    cp.n_seq_max = 16;
    cp.kv_unified = true;
    cp.embeddings = true;
    cp.pooling_type = LLAMA_POOLING_TYPE_MEAN;
    g_lctx = llama_init_from_model(g_model, cp);
    if (!g_lctx) {
        fprintf(stderr, "goldmine-embed: can't create a llama.cpp context\n");
        cleanup();
        return 2;
    }
    /* gemma-embedding2 builds no memory: a llama.cpp change that adds one must fail loudly
       here instead of silently cross-contaminating inputs (design §2.1). */
    assert(llama_get_memory(g_lctx) == NULL);

    /* 3. mtmd — never rescale: images arrive at their final size (ImageSizing) */
    struct mtmd_context_params mcp = mtmd_context_params_default();
    mcp.use_gpu = true;
    mcp.print_timings = false;
    mcp.warmup = true;
    mcp.image_min_tokens = 1;
    mcp.image_max_tokens = 1120;
    g_media = mtmd_init_from_file(mmproj_path, g_model, mcp);
    if (!g_media) {
        fprintf(stderr, "goldmine-embed: can't load %s\n", mmproj_path);
        cleanup();
        return 2;
    }

    /* 4. batch + derived sizes */
    g_batch = llama_batch_ext_init(g_lctx);
    g_vocab = llama_model_get_vocab(g_model);
    const int dim_out = llama_model_n_embd_out(g_model);
    g_dim_in = llama_model_n_embd_inp(g_model);
    g_n_ctx = (int)n_ctx;
    if (!g_batch || !g_vocab || dim_out != DIM_OUT || g_dim_in <= 0) {
        fprintf(stderr, "goldmine-embed: model shape is wrong (dim_out=%d dim_in=%d)\n",
                dim_out, g_dim_in);
        cleanup();
        return 2;
    }

    /* 5. capability probe — honest degrade data for the ready event */
    g_cap_vision = mtmd_support_vision(g_media);
    g_cap_audio = mtmd_support_audio(g_media);
    g_sample_rate = mtmd_get_audio_sample_rate(g_media);

    /* 6. two warmup embeds build the Metal pipelines (DigUp pays this at panel-open time) */
    if (embed_text_once("task: search result | query: warm up") != 0 ||
        embed_text_once("task: search result | query: warm up") != 0) {
        fprintf(stderr, "goldmine-embed: warmup embed failed\n");
        cleanup();
        return 2;
    }

    /* 7. ready line, then serve */
    printf("{\"event\":\"ready\",\"proto\":%d,\"helper\":\"%s\",\"dim\":%d,\"vision\":%s,"
           "\"audio\":%s,\"sample_rate\":%d,\"load_seconds\":%.2f}\n",
           PROTO_VERSION, HELPER_VERSION, DIM_OUT, g_cap_vision ? "true" : "false",
           g_cap_audio ? "true" : "false", g_sample_rate, seconds_since(&t0));
    line_done();

    const int rc = serve();
    cleanup();   /* reverse order: batch, mtmd, context, model */
    return rc;
}
