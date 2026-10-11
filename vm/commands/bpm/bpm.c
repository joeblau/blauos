/* bpm - BlauOS package manager for the independent ARM64 Darwin guest.
 *
 * The guest runtime provides libSystem only (no zlib, no libarchive, no
 * TLS stack, no third-party JSON/crypto libraries), so this tool is
 * self-contained: it verifies signed .bpm packages (uncompressed ustar
 * with manifest.json + manifest.json.sig + payload/), installs and removes
 * files with a local database, and re-hashes installed files on demand.
 *
 * The signature scheme is byte-compatible with the repository's release
 * tooling (scripts/release/signing.py): Ed25519 (RFC 8032) over the
 * canonical signing payload
 *   {"key_id":...,"manifest_sha256":...,"schema_version":1,
 *    "scheme":"ed25519-pure-python-rfc8032","signed_at":...}
 * verified against the image-baked trust root /etc/blauos/trust-root.json.
 * The guest enforces key presence, key status (active/retired accepted,
 * revoked/unknown rejected), the manifest hash, and every per-file payload
 * SHA-256 before writing anything. Key validity-window policy is evaluated
 * host-side by the signing tooling; the guest has no trusted clock.
 *
 * Fail-closed everywhere: any verification, format, or I/O error exits
 * nonzero with a BLAUOS_BPM <command> fail line and no partial install.
 */
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define MAX_PACKAGE_BYTES  (256ULL * 1024 * 1024 + 384 * 1024)
#define MAX_MANIFEST_BYTES (256 * 1024)
#define MAX_SIG_BYTES      (64 * 1024)
#define MAX_JSON_BYTES     (1024 * 1024)
#define MAX_JSON_DEPTH     24
#define MAX_JSON_NODES     4096
#define MAX_JSON_CHARS     (512 * 1024)
#define MAX_JSON_KIDS      8192
#define MAX_FILES          4096
#define MAX_FILE_BYTES     (64ULL * 1024 * 1024)
#define MAX_PATH           512
#define MAX_RECV_BYTES     (32 * 1024 * 1024)
#define TAR_BLOCK          512

static const char *opt_root = "/";
static const char *opt_db = "/var/db/bpm/db";
static const char *opt_trust = "/etc/blauos/trust-root.json";
static const char *opt_repo = "/var/db/bpm/repo";
static const char *g_command = "?";

static int fail(const char *detail)
{
    fprintf(stderr, "BLAUOS_BPM %s fail %s errno=%d\n", g_command, detail, errno);
    return 1;
}

/* ---------------------------------------------------------------- SHA-256 */

typedef struct {
    uint32_t h[8];
    uint64_t total;
    unsigned char block[64];
    size_t used;
} sha256_ctx;

static uint32_t rotr32(uint32_t x, unsigned n) { return x >> n | x << (32 - n); }

static void sha256_compress(sha256_ctx *c, const unsigned char *p)
{
    static const uint32_t k[64] = {
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    };
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
        w[i] = (uint32_t)p[4 * i] << 24 | (uint32_t)p[4 * i + 1] << 16 |
               (uint32_t)p[4 * i + 2] << 8 | p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = rotr32(w[i - 15], 7) ^ rotr32(w[i - 15], 18) ^ w[i - 15] >> 3;
        uint32_t s1 = rotr32(w[i - 2], 17) ^ rotr32(w[i - 2], 19) ^ w[i - 2] >> 10;
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = c->h[0], b = c->h[1], d = c->h[3], e = c->h[4], f = c->h[5], g = c->h[6],
             h = c->h[7], cc = c->h[2];
    for (int i = 0; i < 64; i++) {
        uint32_t s1 = rotr32(e, 6) ^ rotr32(e, 11) ^ rotr32(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t t1 = h + s1 + ch + k[i] + w[i];
        uint32_t s0 = rotr32(a, 2) ^ rotr32(a, 13) ^ rotr32(a, 22);
        uint32_t maj = (a & b) ^ (a & cc) ^ (b & cc);
        uint32_t t2 = s0 + maj;
        h = g; g = f; f = e; e = d + t1; d = cc; cc = b; b = a; a = t1 + t2;
    }
    c->h[0] += a; c->h[1] += b; c->h[2] += cc; c->h[3] += d;
    c->h[4] += e; c->h[5] += f; c->h[6] += g; c->h[7] += h;
}

static void sha256_init(sha256_ctx *c)
{
    static const uint32_t iv[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(c->h, iv, sizeof iv);
    c->total = 0;
    c->used = 0;
}

static void sha256_update(sha256_ctx *c, const void *data, size_t len)
{
    const unsigned char *p = data;
    c->total += len;
    while (len) {
        size_t take = 64 - c->used;
        if (take > len) take = len;
        memcpy(c->block + c->used, p, take);
        c->used += take;
        p += take;
        len -= take;
        if (c->used == 64) {
            sha256_compress(c, c->block);
            c->used = 0;
        }
    }
}

static void sha256_final(sha256_ctx *c, unsigned char out[32])
{
    uint64_t bits = c->total * 8;
    unsigned char pad = 0x80;
    sha256_update(c, &pad, 1);
    unsigned char zero = 0;
    while (c->used != 56) sha256_update(c, &zero, 1);
    unsigned char length[8];
    for (int i = 0; i < 8; i++) length[i] = (unsigned char)(bits >> (56 - 8 * i));
    sha256_update(c, length, 8);
    for (int i = 0; i < 8; i++) {
        out[4 * i] = (unsigned char)(c->h[i] >> 24);
        out[4 * i + 1] = (unsigned char)(c->h[i] >> 16);
        out[4 * i + 2] = (unsigned char)(c->h[i] >> 8);
        out[4 * i + 3] = (unsigned char)c->h[i];
    }
}

/* ---------------------------------------------------------------- SHA-512 */

typedef struct {
    uint64_t h[8];
    uint64_t total_lo, total_hi;
    unsigned char block[128];
    size_t used;
} sha512_ctx;

static uint64_t rotr64(uint64_t x, unsigned n) { return x >> n | x << (64 - n); }

static void sha512_compress(sha512_ctx *c, const unsigned char *p)
{
    static const uint64_t k[80] = {
        0x428a2f98d728ae22ULL, 0x7137449123ef65cdULL, 0xb5c0fbcfec4d3b2fULL, 0xe9b5dba58189dbbcULL,
        0x3956c25bf348b538ULL, 0x59f111f1b605d019ULL, 0x923f82a4af194f9bULL, 0xab1c5ed5da6d8118ULL,
        0xd807aa98a3030242ULL, 0x12835b0145706fbeULL, 0x243185be4ee4b28cULL, 0x550c7dc3d5ffb4e2ULL,
        0x72be5d74f27b896fULL, 0x80deb1fe3b1696b1ULL, 0x9bdc06a725c71235ULL, 0xc19bf174cf692694ULL,
        0xe49b69c19ef14ad2ULL, 0xefbe4786384f25e3ULL, 0x0fc19dc68b8cd5b5ULL, 0x240ca1cc77ac9c65ULL,
        0x2de92c6f592b0275ULL, 0x4a7484aa6ea6e483ULL, 0x5cb0a9dcbd41fbd4ULL, 0x76f988da831153b5ULL,
        0x983e5152ee66dfabULL, 0xa831c66d2db43210ULL, 0xb00327c898fb213fULL, 0xbf597fc7beef0ee4ULL,
        0xc6e00bf33da88fc2ULL, 0xd5a79147930aa725ULL, 0x06ca6351e003826fULL, 0x142929670a0e6e70ULL,
        0x27b70a8546d22ffcULL, 0x2e1b21385c26c926ULL, 0x4d2c6dfc5ac42aedULL, 0x53380d139d95b3dfULL,
        0x650a73548baf63deULL, 0x766a0abb3c77b2a8ULL, 0x81c2c92e47edaee6ULL, 0x92722c851482353bULL,
        0xa2bfe8a14cf10364ULL, 0xa81a664bbc423001ULL, 0xc24b8b70d0f89791ULL, 0xc76c51a30654be30ULL,
        0xd192e819d6ef5218ULL, 0xd69906245565a910ULL, 0xf40e35855771202aULL, 0x106aa07032bbd1b8ULL,
        0x19a4c116b8d2d0c8ULL, 0x1e376c085141ab53ULL, 0x2748774cdf8eeb99ULL, 0x34b0bcb5e19b48a8ULL,
        0x391c0cb3c5c95a63ULL, 0x4ed8aa4ae3418acbULL, 0x5b9cca4f7763e373ULL, 0x682e6ff3d6b2b8a3ULL,
        0x748f82ee5defb2fcULL, 0x78a5636f43172f60ULL, 0x84c87814a1f0ab72ULL, 0x8cc702081a6439ecULL,
        0x90befffa23631e28ULL, 0xa4506cebde82bde9ULL, 0xbef9a3f7b2c67915ULL, 0xc67178f2e372532bULL,
        0xca273eceea26619cULL, 0xd186b8c721c0c207ULL, 0xeada7dd6cde0eb1eULL, 0xf57d4f7fee6ed178ULL,
        0x06f067aa72176fbaULL, 0x0a637dc5a2c898a6ULL, 0x113f9804bef90daeULL, 0x1b710b35131c471bULL,
        0x28db77f523047d84ULL, 0x32caab7b40c72493ULL, 0x3c9ebe0a15c9bebcULL, 0x431d67c49c100d4cULL,
        0x4cc5d4becb3e42b6ULL, 0x597f299cfc657e2aULL, 0x5fcb6fab3ad6faecULL, 0x6c44198c4a475817ULL,
    };
    uint64_t w[80];
    for (int i = 0; i < 16; i++) {
        uint64_t v = 0;
        for (int j = 0; j < 8; j++) v = v << 8 | p[8 * i + j];
        w[i] = v;
    }
    for (int i = 16; i < 80; i++) {
        uint64_t s0 = rotr64(w[i - 15], 1) ^ rotr64(w[i - 15], 8) ^ w[i - 15] >> 7;
        uint64_t s1 = rotr64(w[i - 2], 19) ^ rotr64(w[i - 2], 61) ^ w[i - 2] >> 6;
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint64_t a = c->h[0], b = c->h[1], cc = c->h[2], d = c->h[3],
             e = c->h[4], f = c->h[5], g = c->h[6], h = c->h[7];
    for (int i = 0; i < 80; i++) {
        uint64_t s1 = rotr64(e, 14) ^ rotr64(e, 18) ^ rotr64(e, 41);
        uint64_t ch = (e & f) ^ (~e & g);
        uint64_t t1 = h + s1 + ch + k[i] + w[i];
        uint64_t s0 = rotr64(a, 28) ^ rotr64(a, 34) ^ rotr64(a, 39);
        uint64_t maj = (a & b) ^ (a & cc) ^ (b & cc);
        uint64_t t2 = s0 + maj;
        h = g; g = f; f = e; e = d + t1; d = cc; cc = b; b = a; a = t1 + t2;
    }
    c->h[0] += a; c->h[1] += b; c->h[2] += cc; c->h[3] += d;
    c->h[4] += e; c->h[5] += f; c->h[6] += g; c->h[7] += h;
}

static void sha512_init(sha512_ctx *c)
{
    static const uint64_t iv[8] = {
        0x6a09e667f3bcc908ULL, 0xbb67ae8584caa73bULL, 0x3c6ef372fe94f82bULL, 0xa54ff53a5f1d36f1ULL,
        0x510e527fade682d1ULL, 0x9b05688c2b3e6c1fULL, 0x1f83d9abfb41bd6bULL, 0x5be0cd19137e2179ULL,
    };
    memcpy(c->h, iv, sizeof iv);
    c->total_lo = c->total_hi = 0;
    c->used = 0;
}

static void sha512_update(sha512_ctx *c, const void *data, size_t len)
{
    const unsigned char *p = data;
    uint64_t old = c->total_lo;
    c->total_lo += len;
    if (c->total_lo < old) c->total_hi++;
    while (len) {
        size_t take = 128 - c->used;
        if (take > len) take = len;
        memcpy(c->block + c->used, p, take);
        c->used += take;
        p += take;
        len -= take;
        if (c->used == 128) {
            sha512_compress(c, c->block);
            c->used = 0;
        }
    }
}

static void sha512_final(sha512_ctx *c, unsigned char out[64])
{
    uint64_t bits_hi = (c->total_hi << 3) | (c->total_lo >> 61);
    uint64_t bits_lo = c->total_lo << 3;
    unsigned char pad = 0x80;
    sha512_update(c, &pad, 1);
    unsigned char zero = 0;
    while (c->used != 112) sha512_update(c, &zero, 1);
    unsigned char length[16];
    for (int i = 0; i < 8; i++) length[i] = (unsigned char)(bits_hi >> (56 - 8 * i));
    for (int i = 0; i < 8; i++) length[8 + i] = (unsigned char)(bits_lo >> (56 - 8 * i));
    sha512_update(c, length, 16);
    for (int i = 0; i < 8; i++)
        for (int j = 0; j < 8; j++) out[8 * i + j] = (unsigned char)(c->h[i] >> (56 - 8 * j));
}

/* ------------------------- field arithmetic mod 2^255-19 (unsigned __int128)
 * Affine Edwards formulas mirroring the repository's RFC 8032 reference
 * implementation in scripts/release/signing.py, validated host-side against
 * the RFC 8032 test vectors and cross-checked against that reference. */

typedef unsigned __int128 u128;
typedef struct { uint64_t v[4]; } fe;          /* little-endian 256-bit, < p */
typedef struct { fe x, y; } point;

static const fe FE_P = {{0xffffffffffffffedULL, 0xffffffffffffffffULL,
                         0xffffffffffffffffULL, 0x7fffffffffffffffULL}};
static const fe FE_D = {{0x75eb4dca135978a3ULL, 0x00700a4d4141d8abULL,
                         0x8cc740797779e898ULL, 0x52036cee2b6ffe73ULL}};
static const fe FE_I = {{0xc4ee1b274a0ea0b0ULL, 0x2f431806ad2fe478ULL,
                         0x2b4d00993dfbd7a7ULL, 0x2b8324804fc1df0bULL}};
/* p - 2, (p+3)/8 as exponents for inversion/square roots */
static const fe EXP_INV = {{0xffffffffffffffebULL, 0xffffffffffffffffULL,
                            0xffffffffffffffffULL, 0x7fffffffffffffffULL}};
static const fe EXP_SQRT = {{0xfffffffffffffffeULL, 0xffffffffffffffffULL,
                             0xffffffffffffffffULL, 0x0fffffffffffffffULL}};
/* Group order L = 2^252 + 27742317777372353535851937790883648493 */
static const fe FE_L = {{0x5812631a5cf5d3edULL, 0x14def9dea2f79cd6ULL,
                         0x0000000000000000ULL, 0x1000000000000000ULL}};

static int fe_cmp(const fe *a, const fe *b)
{
    for (int i = 3; i >= 0; i--) {
        if (a->v[i] != b->v[i]) return a->v[i] < b->v[i] ? -1 : 1;
    }
    return 0;
}

static bool fe_is_zero(const fe *a)
{
    return (a->v[0] | a->v[1] | a->v[2] | a->v[3]) == 0;
}

/* a - b, requires a >= b */
static void fe_sub_raw(fe *r, const fe *a, const fe *b)
{
    u128 borrow = 0;
    for (int i = 0; i < 4; i++) {
        u128 t = (u128)a->v[i] - (u128)b->v[i] - borrow;
        r->v[i] = (uint64_t)t;
        borrow = (t >> 127) & 1;  /* wrapped iff the subtraction went negative */
    }
}

static void fe_reduce_p(fe *a)
{
    while (fe_cmp(a, &FE_P) >= 0) {
        fe t;
        fe_sub_raw(&t, a, &FE_P);
        *a = t;
    }
}

static void fe_add(fe *r, const fe *a, const fe *b)
{
    u128 carry = 0;
    for (int i = 0; i < 4; i++) {
        carry += (u128)a->v[i] + b->v[i];
        r->v[i] = (uint64_t)carry;
        carry >>= 64;
    }
    /* a,b < p so the sum is below 2^256; folding is only needed for safety. */
    while (carry) {
        u128 c2 = carry * 38;
        carry = 0;
        for (int i = 0; i < 4 && c2; i++) {
            c2 += r->v[i];
            r->v[i] = (uint64_t)c2;
            c2 >>= 64;
        }
        carry = c2;
    }
    fe_reduce_p(r);
}

static void fe_sub(fe *r, const fe *a, const fe *b)
{
    if (fe_cmp(a, b) >= 0) {
        fe_sub_raw(r, a, b);
        return;
    }
    /* a - b mod p = (a + p) - b; a + p stays below 2^256 because a < p. */
    fe t;
    u128 carry = 0;
    for (int i = 0; i < 4; i++) {
        carry += (u128)a->v[i] + FE_P.v[i];
        t.v[i] = (uint64_t)carry;
        carry >>= 64;
    }
    fe_sub_raw(r, &t, b);
}

static void fe_mul(fe *r, const fe *a, const fe *b)
{
    uint64_t h[8] = {0};
    for (int i = 0; i < 4; i++) {
        u128 carry = 0;
        for (int j = 0; j < 4; j++) {
            u128 t = (u128)a->v[i] * b->v[j] + h[i + j] + carry;
            h[i + j] = (uint64_t)t;
            carry = t >> 64;
        }
        h[i + 4] += (uint64_t)carry;
    }
    /* 2^256 = 38 (mod p): fold the high half into the low half. */
    u128 carry = 0;
    for (int i = 0; i < 4; i++) {
        carry += (u128)h[i] + (u128)38 * h[i + 4];
        r->v[i] = (uint64_t)carry;
        carry >>= 64;
    }
    while (carry) {
        u128 c2 = carry * 38;
        carry = 0;
        for (int i = 0; i < 4 && c2; i++) {
            c2 += r->v[i];
            r->v[i] = (uint64_t)c2;
            c2 >>= 64;
        }
        carry = c2;
    }
    fe_reduce_p(r);
}

static void fe_sqr(fe *r, const fe *a) { fe_mul(r, a, a); }

static void fe_pow(fe *r, const fe *base, const fe *exp)
{
    fe result = {{1, 0, 0, 0}};
    for (int bit = 255; bit >= 0; bit--) {
        fe_sqr(&result, &result);
        if ((exp->v[bit / 64] >> (bit % 64)) & 1) fe_mul(&result, &result, base);
    }
    *r = result;
}

static void fe_inv(fe *r, const fe *a) { fe_pow(r, a, &EXP_INV); }

/* Recover x from y with the given sign bit, as in RFC 8032 decoding. */
static bool fe_xrecover(fe *x, const fe *y)
{
    fe y2, num, den, inv, xx, cand, check;
    fe_sqr(&y2, y);
    {
        fe one = {{1, 0, 0, 0}};
        fe_sub(&num, &y2, &one);
    }
    fe_mul(&den, &FE_D, &y2);
    {
        fe one = {{1, 0, 0, 0}};
        fe_add(&den, &den, &one);
    }
    fe_inv(&inv, &den);
    fe_mul(&xx, &num, &inv);
    fe_pow(&cand, &xx, &EXP_SQRT);
    fe_sqr(&check, &cand);
    if (fe_cmp(&check, &xx) != 0) fe_mul(&cand, &cand, &FE_I);
    fe_sqr(&check, &cand);
    if (fe_cmp(&check, &xx) != 0) return false;
    if (cand.v[0] & 1) fe_sub(&cand, &FE_P, &cand);  /* keep the even root */
    *x = cand;
    return true;
}

static void ed_add(point *r, const point *p, const point *q)
{
    fe x1x2, y1y2, x1y2, x2y1, common, num, den, inv;
    fe_mul(&x1x2, &p->x, &q->x);
    fe_mul(&y1y2, &p->y, &q->y);
    fe_mul(&x1y2, &p->x, &q->y);
    fe_mul(&x2y1, &q->x, &p->y);
    fe_mul(&common, &FE_D, &x1x2);
    fe_mul(&common, &common, &y1y2);
    fe one = {{1, 0, 0, 0}};
    fe_add(&num, &x1y2, &x2y1);
    fe_add(&den, &one, &common);
    fe_inv(&inv, &den);
    fe_mul(&r->x, &num, &inv);
    fe_add(&num, &y1y2, &x1x2);
    fe_sub(&den, &one, &common);
    fe_inv(&inv, &den);
    fe_mul(&r->y, &num, &inv);
}

static void ed_scalarmult(point *r, const point *p, const fe *scalar)
{
    point result = {{{0, 0, 0, 0}}, {{1, 0, 0, 0}}};
    point addend = *p;
    for (int bit = 0; bit < 256; bit++) {
        if ((scalar->v[bit / 64] >> (bit % 64)) & 1) {
            point t;
            ed_add(&t, &result, &addend);
            result = t;
        }
        point t;
        ed_add(&t, &addend, &addend);
        addend = t;
    }
    *r = result;
}

static bool ed_decode(point *out, const unsigned char data[32])
{
    fe y;
    for (int i = 0; i < 4; i++) {
        uint64_t v = 0;
        for (int j = 0; j < 8; j++) v |= (uint64_t)data[8 * i + j] << (8 * j);
        y.v[i] = v;
    }
    int sign = data[31] >> 7;
    y.v[3] &= 0x7fffffffffffffffULL;
    if (fe_cmp(&y, &FE_P) >= 0) return false;
    fe x;
    if (!fe_xrecover(&x, &y)) return false;
    if ((int)(x.v[0] & 1) != sign) fe_sub(&x, &FE_P, &x);
    /* On-curve check: -x^2 + y^2 = 1 + d x^2 y^2 */
    fe x2, y2, lhs, rhs, t;
    fe_sqr(&x2, &x);
    fe_sqr(&y2, &y);
    {
        fe zero = {{0, 0, 0, 0}};
        fe_sub(&lhs, &zero, &x2);
        fe_add(&lhs, &lhs, &y2);
        fe one = {{1, 0, 0, 0}};
        fe_sub(&lhs, &lhs, &one);
    }
    fe_mul(&rhs, &FE_D, &x2);
    fe_mul(&rhs, &rhs, &y2);
    fe_sub(&t, &lhs, &rhs);
    if (!fe_is_zero(&t)) return false;
    out->x = x;
    out->y = y;
    return true;
}

/* Reduce a 512-bit little-endian integer modulo the group order L. */
static void fe_mod_l(fe *r, const unsigned char wide[64])
{
    fe rem = {{0, 0, 0, 0}};
    for (int bit = 511; bit >= 0; bit--) {
        int carry = (int)(rem.v[3] >> 63);
        rem.v[3] = rem.v[3] << 1 | rem.v[2] >> 63;
        rem.v[2] = rem.v[2] << 1 | rem.v[1] >> 63;
        rem.v[1] = rem.v[1] << 1 | rem.v[0] >> 63;
        rem.v[0] = rem.v[0] << 1 | (uint64_t)((wide[bit / 8] >> (bit % 8)) & 1);
        (void)carry; /* rem < 2L < 2^254, so doubling never carries out */
        if (fe_cmp(&rem, &FE_L) >= 0) {
            fe t;
            fe_sub_raw(&t, &rem, &FE_L);
            rem = t;
        }
    }
    *r = rem;
}

static const point ED_BASE = {{{0, 0, 0, 0}}, {{0x6666666666666658ULL, 0x6666666666666666ULL,
                                                0x6666666666666666ULL, 0x6666666666666666ULL}}};

static bool ed25519_verify(const unsigned char pub[32], const unsigned char *msg, size_t msg_len,
                           const unsigned char sig[64])
{
    static point base;
    static bool base_ready = false;
    if (!base_ready) {
        base = ED_BASE;
        if (!fe_xrecover(&base.x, &base.y)) return false;
        base_ready = true;
    }
    point a, rpoint;
    if (!ed_decode(&a, pub)) return false;
    if (!ed_decode(&rpoint, sig)) return false;
    fe s;
    for (int i = 0; i < 4; i++) {
        uint64_t v = 0;
        for (int j = 0; j < 8; j++) v |= (uint64_t)sig[32 + 8 * i + j] << (8 * j);
        s.v[i] = v;
    }
    if (fe_cmp(&s, &FE_L) >= 0) return false;
    unsigned char hash[64];
    sha512_ctx ctx;
    sha512_init(&ctx);
    sha512_update(&ctx, sig, 32);
    sha512_update(&ctx, pub, 32);
    sha512_update(&ctx, msg, msg_len);
    sha512_final(&ctx, hash);
    fe h;
    fe_mod_l(&h, hash);
    point left, ha, right;
    ed_scalarmult(&left, &base, &s);
    ed_scalarmult(&ha, &a, &h);
    ed_add(&right, &rpoint, &ha);
    return fe_cmp(&left.x, &right.x) == 0 && fe_cmp(&left.y, &right.y) == 0;
}

/* ------------------------------------------------------- minimal JSON DOM */

enum jtype { J_NULL, J_FALSE, J_TRUE, J_INT, J_STR, J_ARR, J_OBJ };

typedef struct jval {
    int type;
    long long num;          /* J_INT */
    char *str;              /* J_STR, and object keys */
    int nkids;              /* J_ARR: values; J_OBJ: key/value pairs */
    struct jval **kids;
} jval;

static jval j_pool[MAX_JSON_NODES];
static int j_pool_used;
static char j_chars[MAX_JSON_CHARS];
static size_t j_chars_used;
static jval *j_kids[MAX_JSON_KIDS];
static int j_kids_used;

static jval *jnode(int type)
{
    if (j_pool_used >= MAX_JSON_NODES) return NULL;
    jval *v = &j_pool[j_pool_used++];
    v->type = type;
    v->num = 0;
    v->str = NULL;
    v->nkids = 0;
    v->kids = NULL;
    return v;
}

typedef struct {
    const unsigned char *data;
    size_t len, pos;
    int depth;
} jparse;

static jval *jvalue(jparse *p);

static void jskip_ws(jparse *p)
{
    while (p->pos < p->len && (p->data[p->pos] == ' ' || p->data[p->pos] == '\t' ||
                               p->data[p->pos] == '\n' || p->data[p->pos] == '\r'))
        p->pos++;
}

static bool jexpect(jparse *p, char c)
{
    jskip_ws(p);
    if (p->pos >= p->len || p->data[p->pos] != (unsigned char)c) return false;
    p->pos++;
    return true;
}

static int hexval(int c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static char *jparse_string(jparse *p)
{
    if (p->pos >= p->len || p->data[p->pos] != '"') return NULL;
    p->pos++;
    size_t start = j_chars_used;
    while (p->pos < p->len) {
        unsigned char c = p->data[p->pos++];
        if (c == '"') {
            if (j_chars_used + 1 > MAX_JSON_CHARS) return NULL;
            j_chars[j_chars_used++] = 0;
            return j_chars + start;
        }
        if (c < 0x20) return NULL;
        if (c == '\\') {
            if (p->pos >= p->len) return NULL;
            unsigned char esc = p->data[p->pos++];
            unsigned char out;
            switch (esc) {
            case '"': out = '"'; break;
            case '\\': out = '\\'; break;
            case '/': out = '/'; break;
            case 'b': out = '\b'; break;
            case 'f': out = '\f'; break;
            case 'n': out = '\n'; break;
            case 'r': out = '\r'; break;
            case 't': out = '\t'; break;
            case 'u': {
                if (p->pos + 4 > p->len) return NULL;
                int cp = 0;
                for (int i = 0; i < 4; i++) {
                    int h = hexval(p->data[p->pos + i]);
                    if (h < 0) return NULL;
                    cp = cp << 4 | h;
                }
                p->pos += 4;
                unsigned char buf[4];
                int n;
                if (cp >= 0xd800 && cp <= 0xdfff) return NULL;  /* no surrogate pairs needed; fail closed */
                if (cp < 0x80) {
                    buf[0] = (unsigned char)cp; n = 1;
                } else if (cp < 0x800) {
                    buf[0] = (unsigned char)(0xc0 | cp >> 6);
                    buf[1] = (unsigned char)(0x80 | (cp & 0x3f)); n = 2;
                } else {
                    buf[0] = (unsigned char)(0xe0 | cp >> 12);
                    buf[1] = (unsigned char)(0x80 | (cp >> 6 & 0x3f));
                    buf[2] = (unsigned char)(0x80 | (cp & 0x3f)); n = 3;
                }
                if (j_chars_used + (size_t)n > MAX_JSON_CHARS) return NULL;
                memcpy(j_chars + j_chars_used, buf, (size_t)n);
                j_chars_used += (size_t)n;
                continue;
            }
            default: return NULL;
            }
            if (j_chars_used + 1 > MAX_JSON_CHARS) return NULL;
            j_chars[j_chars_used++] = out;
            continue;
        }
        if (j_chars_used + 1 > MAX_JSON_CHARS) return NULL;
        j_chars[j_chars_used++] = (char)c;
    }
    return NULL;
}

static jval *jarray(jparse *p)
{
    jval *v = jnode(J_ARR);
    if (!v || !jexpect(p, '[')) return NULL;
    if (p->depth++ >= MAX_JSON_DEPTH) return NULL;
    jskip_ws(p);
    if (p->pos < p->len && p->data[p->pos] == ']') {
        p->pos++;
        p->depth--;
        return v;
    }
    jval **items = NULL;
    int count = 0, capacity = 0;
    for (;;) {
        jval *item = jvalue(p);
        if (!item) goto fail;
        if (count == capacity) {
            capacity = capacity ? capacity * 2 : 8;
            jval **grown = realloc(items, (size_t)capacity * sizeof *items);
            if (!grown) goto fail;
            items = grown;
        }
        items[count++] = item;
        jskip_ws(p);
        if (p->pos < p->len && p->data[p->pos] == ',') {
            p->pos++;
            continue;
        }
        if (p->pos < p->len && p->data[p->pos] == ']') {
            p->pos++;
            break;
        }
        goto fail;
    }
    p->depth--;
    if (j_kids_used + count > MAX_JSON_KIDS) goto fail;
    if (count) memcpy(&j_kids[j_kids_used], items, (size_t)count * sizeof *items);
    free(items);
    v->nkids = count;
    v->kids = &j_kids[j_kids_used];
    j_kids_used += count;
    return v;
fail:
    free(items);
    return NULL;
}

static jval *jobject(jparse *p)
{
    jval *v = jnode(J_OBJ);
    if (!v || !jexpect(p, '{')) return NULL;
    if (p->depth++ >= MAX_JSON_DEPTH) return NULL;
    jskip_ws(p);
    if (p->pos < p->len && p->data[p->pos] == '}') {
        p->pos++;
        p->depth--;
        return v;
    }
    jval **pairs = NULL;
    int count = 0, capacity = 0;
    for (;;) {
        jskip_ws(p);
        char *key = jparse_string(p);
        if (!key || !jexpect(p, ':')) goto fail;
        jval *item = jvalue(p);
        if (!item) goto fail;
        for (int i = 0; i < count; i++)
            if (!strcmp(pairs[2 * i]->str, key)) goto fail;  /* duplicate key */
        jval *kval = jnode(J_STR);
        if (!kval) goto fail;
        kval->str = key;
        if (count == capacity) {
            capacity = capacity ? capacity * 2 : 8;
            jval **grown = realloc(pairs, (size_t)capacity * 2 * sizeof *pairs);
            if (!grown) goto fail;
            pairs = grown;
        }
        pairs[2 * count] = kval;
        pairs[2 * count + 1] = item;
        count++;
        jskip_ws(p);
        if (p->pos < p->len && p->data[p->pos] == ',') {
            p->pos++;
            continue;
        }
        if (p->pos < p->len && p->data[p->pos] == '}') {
            p->pos++;
            break;
        }
        goto fail;
    }
    p->depth--;
    if (j_kids_used + 2 * count > MAX_JSON_KIDS) goto fail;
    if (count) memcpy(&j_kids[j_kids_used], pairs, (size_t)(2 * count) * sizeof *pairs);
    free(pairs);
    v->nkids = count;
    v->kids = &j_kids[j_kids_used];
    j_kids_used += 2 * count;
    return v;
fail:
    free(pairs);
    return NULL;
}

static jval *jnumber(jparse *p)
{
    size_t start = p->pos;
    if (p->pos < p->len && p->data[p->pos] == '-') p->pos++;
    if (p->pos >= p->len || p->data[p->pos] < '0' || p->data[p->pos] > '9') return NULL;
    if (p->data[p->pos] == '0') {
        p->pos++;
    } else {
        while (p->pos < p->len && p->data[p->pos] >= '0' && p->data[p->pos] <= '9') p->pos++;
    }
    if (p->pos < p->len && (p->data[p->pos] == '.' || p->data[p->pos] == 'e' || p->data[p->pos] == 'E'))
        return NULL;  /* integers only in the package formats */
    size_t len = p->pos - start;
    if (len > 20) return NULL;
    char buf[24];
    memcpy(buf, p->data + start, len);
    buf[len] = 0;
    errno = 0;
    char *end = NULL;
    long long value = strtoll(buf, &end, 10);
    if (errno || !end || *end) return NULL;
    jval *v = jnode(J_INT);
    if (!v) return NULL;
    v->num = value;
    return v;
}

static jval *jvalue(jparse *p)
{
    jskip_ws(p);
    if (p->pos >= p->len) return NULL;
    unsigned char c = p->data[p->pos];
    if (c == '{') return jobject(p);
    if (c == '[') return jarray(p);
    if (c == '"') {
        char *s = jparse_string(p);
        if (!s) return NULL;
        jval *v = jnode(J_STR);
        if (!v) return NULL;
        v->str = s;
        return v;
    }
    if (c == '-' || (c >= '0' && c <= '9')) return jnumber(p);
    if (p->len - p->pos >= 4 && !memcmp(p->data + p->pos, "true", 4)) {
        p->pos += 4;
        return jnode(J_TRUE);
    }
    if (p->len - p->pos >= 5 && !memcmp(p->data + p->pos, "false", 5)) {
        p->pos += 5;
        return jnode(J_FALSE);
    }
    if (p->len - p->pos >= 4 && !memcmp(p->data + p->pos, "null", 4)) {
        p->pos += 4;
        return jnode(J_NULL);
    }
    return NULL;
}

static jval *json_parse(const unsigned char *data, size_t len)
{
    if (!len || len > MAX_JSON_BYTES) return NULL;
    j_pool_used = 0;
    j_chars_used = 0;
    j_kids_used = 0;
    jparse p = {data, len, 0, 0};
    jval *v = jvalue(&p);
    jskip_ws(&p);
    if (!v || p.pos != p.len) return NULL;
    return v;
}

static jval *jget(const jval *obj, const char *key)
{
    if (!obj || obj->type != J_OBJ) return NULL;
    for (int i = 0; i < obj->nkids; i++)
        if (strcmp(obj->kids[2 * i]->str, key) == 0) return obj->kids[2 * i + 1];
    return NULL;
}

static bool jstr_is(const jval *v, const char *s)
{
    return v && v->type == J_STR && strcmp(v->str, s) == 0;
}

static bool hex_string(const char *s, size_t want)
{
    if (strlen(s) != want) return false;
    for (size_t i = 0; i < want; i++) {
        char c = s[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    }
    return true;
}

static bool hex_decode(const char *s, unsigned char *out, size_t bytes)
{
    if (!hex_string(s, bytes * 2)) return false;
    for (size_t i = 0; i < bytes; i++)
        out[i] = (unsigned char)(hexval((unsigned char)s[2 * i]) << 4 | hexval((unsigned char)s[2 * i + 1]));
    return true;
}

/* ------------------------------------------------------------- ustar read */

typedef struct {
    char name[256];
    unsigned long long size;
    unsigned mode;
    bool is_dir;
} tarent;

static unsigned long long tar_octal(const unsigned char *field, size_t len)
{
    unsigned long long value = 0;
    size_t i = 0;
    while (i < len && field[i] == ' ') i++;
    for (; i < len && field[i] >= '0' && field[i] <= '7'; i++)
        value = value * 8 + (unsigned)(field[i] - '0');
    return value;
}

/* Returns 1 with a member (stream positioned at its data), 0 at end. */
static int tar_next(FILE *stream, tarent *member)
{
    unsigned char header[TAR_BLOCK];
    if (fread(header, 1, TAR_BLOCK, stream) != TAR_BLOCK) return -1;
    bool zero = true;
    for (int i = 0; i < TAR_BLOCK; i++)
        if (header[i]) zero = false;
    if (zero) return 0;
    unsigned long long sum = 0;
    for (int i = 0; i < TAR_BLOCK; i++)
        sum += (i >= 148 && i < 156) ? ' ' : header[i];
    if (sum != tar_octal(header + 148, 8)) return -1;
    char name[101], prefix[156];
    memcpy(name, header, 100);
    name[100] = 0;
    memcpy(prefix, header + 345, 155);
    prefix[155] = 0;
    int type = header[156];
    if (type != '0' && type != 0 && type != '5') return -1;
    if (!name[0]) return -1;
    if (prefix[0]) {
        int needed = snprintf(member->name, sizeof member->name, "%s/%s", prefix, name);
        if (needed <= 0 || (size_t)needed >= sizeof member->name) return -1;
    } else {
        size_t len = strlen(name);
        if (len >= sizeof member->name) return -1;
        memcpy(member->name, name, len + 1);
    }
    member->size = tar_octal(header + 124, 12);
    member->mode = (unsigned)tar_octal(header + 100, 8) & 07777;
    member->is_dir = type == '5';
    return 1;
}

/* Skip the unconsumed remainder of a member's padded data block. */
static int tar_skip(FILE *stream, unsigned long long size, unsigned long long consumed)
{
    unsigned long long total = (size + TAR_BLOCK - 1) / TAR_BLOCK * TAR_BLOCK;
    if (consumed > total) return -1;
    return fseek(stream, (long)(total - consumed), SEEK_CUR);
}

/* ------------------------------------------------------------ file helpers */

static bool read_file(const char *path, unsigned char **data, size_t *len, size_t limit)
{
    int fd = open(path, O_RDONLY);
    if (fd < 0) return false;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || (uint64_t)st.st_size > limit || st.st_size == 0) {
        close(fd);
        return false;
    }
    unsigned char *buf = malloc((size_t)st.st_size);
    if (!buf) {
        close(fd);
        return false;
    }
    size_t got = 0;
    while (got < (size_t)st.st_size) {
        ssize_t n = read(fd, buf + got, (size_t)st.st_size - got);
        if (n <= 0) {
            free(buf);
            close(fd);
            return false;
        }
        got += (size_t)n;
    }
    close(fd);
    *data = buf;
    *len = got;
    return true;
}

static void hash_hex(const unsigned char digest[32], char out[65])
{
    static const char digits[] = "0123456789abcdef";
    for (int i = 0; i < 32; i++) {
        out[2 * i] = digits[digest[i] >> 4];
        out[2 * i + 1] = digits[digest[i] & 15];
    }
    out[64] = 0;
}

static bool sha256_file_fd(int fd, char out[65])
{
    sha256_ctx ctx;
    sha256_init(&ctx);
    unsigned char block[65536];
    for (;;) {
        ssize_t n = read(fd, block, sizeof block);
        if (n < 0) return false;
        if (n == 0) break;
        sha256_update(&ctx, block, (size_t)n);
    }
    unsigned char digest[32];
    sha256_final(&ctx, digest);
    hash_hex(digest, out);
    return true;
}

/* Validate a package file path: absolute, normalized, printable, bounded, and
 * never inside package-manager state or over the baked trust root. */
static bool valid_guest_path(const char *path)
{
    size_t len = strlen(path);
    if (len < 2 || len > 400 || path[0] != '/') return false;
    if (strstr(path, "//") || !strncmp(path, "/var/db/bpm/", 12) ||
        !strcmp(path, "/etc/blauos/trust-root.json")) return false;
    const char *component = path;
    while (*component) {
        const char *slash = strchr(component + 1, '/');
        size_t clen = slash ? (size_t)(slash - component - 1) : strlen(component + 1);
        if (!clen || (clen == 1 && component[1] == '.') ||
            (clen == 2 && component[1] == '.' && component[2] == '.')) return false;
        if (!slash) break;
        component = slash;
    }
    for (size_t i = 0; i < len; i++)
        if ((unsigned char)path[i] < 32 || (unsigned char)path[i] > 126) return false;
    return true;
}

static void join_path(char *out, size_t size, const char *base, const char *path)
{
    if (!strcmp(base, "/"))
        snprintf(out, size, "%s", path);
    else
        snprintf(out, size, "%s%s", base, path);
}

/* Create every missing parent directory of path (which starts with base). */
static bool make_parents(const char *base, const char *path)
{
    char buffer[MAX_PATH];
    join_path(buffer, sizeof buffer, base, path);
    char *slash = buffer;
    if (strncmp(buffer, base, strlen(base))) return false;
    while ((slash = strchr(slash + 1, '/'))) {
        *slash = 0;
        if (mkdir(buffer, 0755) && errno != EEXIST) return false;
        struct stat st;
        if (stat(buffer, &st) || !S_ISDIR(st.st_mode)) return false;
        *slash = '/';
    }
    return true;
}

static bool write_atomic(const char *path, const unsigned char *data, size_t len)
{
    char temp[MAX_PATH];
    int needed = snprintf(temp, sizeof temp, "%s.tmp.%d", path, (int)getpid());
    if (needed <= 0 || (size_t)needed >= sizeof temp) return false;
    int fd = open(temp, O_WRONLY | O_CREAT | O_EXCL, 0644);
    if (fd < 0) return false;
    size_t done = 0;
    while (done < len) {
        ssize_t n = write(fd, data + done, len - done);
        if (n <= 0) {
            close(fd);
            unlink(temp);
            return false;
        }
        done += (size_t)n;
    }
    if (close(fd)) {
        unlink(temp);
        return false;
    }
    if (rename(temp, path)) {
        unlink(temp);
        return false;
    }
    return true;
}

/* -------------------------------------------------------- manifest handling */

typedef struct {
    char path[401];
    char sha256[65];
    unsigned long long size;
    unsigned mode;
} pfile;

typedef struct {
    char name[65];
    char version[65];
    char depends[16][65];
    int ndepends;
    pfile files[MAX_FILES];
    int nfiles;
    unsigned char *raw;   /* exact manifest bytes, stored as the db record */
    size_t raw_len;
} pmanifest;

static bool name_ok(const char *s)
{
    size_t len = strlen(s);
    if (!len || len > 64) return false;
    for (size_t i = 0; i < len; i++) {
        char c = s[i];
        bool ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '+' || c == '.' || c == '-';
        if (!ok) return false;
        if (i == 0 && !(c >= 'a' && c <= 'z') && !(c >= '0' && c <= '9')) return false;
    }
    return true;
}

static bool version_ok(const char *s)
{
    size_t len = strlen(s);
    if (!len || len > 64 || s[0] < '0' || s[0] > '9') return false;
    for (size_t i = 0; i < len; i++) {
        char c = s[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
              c == '.' || c == '+' || c == '-')) return false;
    }
    return true;
}

static bool parse_manifest(const unsigned char *data, size_t len, pmanifest *out)
{
    jval *root = json_parse(data, len);
    if (!root || root->type != J_OBJ) return false;
    jval *schema = jget(root, "schema_version");
    if (!schema || schema->type != J_INT || schema->num != 1) return false;
    if (!jstr_is(jget(root, "manifest_kind"), "blauos-package")) return false;
    jval *name = jget(root, "name");
    jval *version = jget(root, "version");
    if (!name || name->type != J_STR || !name_ok(name->str)) return false;
    if (!version || version->type != J_STR || !version_ok(version->str)) return false;
    memset(out, 0, sizeof *out);
    snprintf(out->name, sizeof out->name, "%s", name->str);
    snprintf(out->version, sizeof out->version, "%s", version->str);
    jval *depends = jget(root, "depends");
    if (!depends || depends->type != J_ARR || depends->nkids > 16) return false;
    for (int i = 0; i < depends->nkids; i++) {
        jval *dep = depends->kids[i];
        if (dep->type != J_STR || !name_ok(dep->str)) return false;
        snprintf(out->depends[out->ndepends++], 65, "%s", dep->str);
    }
    jval *files = jget(root, "files");
    if (!files || files->type != J_ARR || !files->nkids || files->nkids > MAX_FILES) return false;
    for (int i = 0; i < files->nkids; i++) {
        jval *entry = files->kids[i];
        if (entry->type != J_OBJ) return false;
        jval *path = jget(entry, "path");
        jval *hash = jget(entry, "sha256");
        jval *size = jget(entry, "size");
        jval *mode = jget(entry, "mode");
        if (!path || path->type != J_STR || !valid_guest_path(path->str)) return false;
        if (!hash || hash->type != J_STR || !hex_string(hash->str, 64)) return false;
        if (!size || size->type != J_INT || size->num < 0 || (unsigned long long)size->num > MAX_FILE_BYTES)
            return false;
        if (!mode || mode->type != J_STR || strlen(mode->str) != 4 || mode->str[0] != '0') return false;
        unsigned parsed = 0;
        for (int d = 1; d < 4; d++) {
            if (mode->str[d] < '0' || mode->str[d] > '7') return false;
            parsed = parsed * 8 + (unsigned)(mode->str[d] - '0');
        }
        pfile *file = &out->files[out->nfiles];
        snprintf(file->path, sizeof file->path, "%s", path->str);
        snprintf(file->sha256, sizeof file->sha256, "%s", hash->str);
        file->size = (unsigned long long)size->num;
        file->mode = parsed;
        for (int j = 0; j < out->nfiles; j++)
            if (!strcmp(out->files[j].path, file->path)) return false;
        out->nfiles++;
    }
    out->raw = (unsigned char *)data;
    out->raw_len = len;
    return true;
}

/* ------------------------------------------------- signature/trust checking */

static bool status_ok_for_verify(const char *status)
{
    return !strcmp(status, "active") || !strcmp(status, "retired");
}

/* Find the public key for key_id in the trust root; fail for revoked/unknown. */
static bool trust_lookup(const unsigned char *trust_data, size_t trust_len,
                         const char *key_id, unsigned char public_key[32], const char **why)
{
    jval *root = json_parse(trust_data, trust_len);
    if (!root || root->type != J_OBJ) {
        *why = "trust-root-parse";
        return false;
    }
    if (!jstr_is(jget(root, "trust_root_kind"), "blauos-release-signing")) {
        *why = "trust-root-kind";
        return false;
    }
    jval *keys = jget(root, "keys");
    if (!keys || keys->type != J_ARR) {
        *why = "trust-root-keys";
        return false;
    }
    for (int i = 0; i < keys->nkids; i++) {
        jval *entry = keys->kids[i];
        jval *id = entry->type == J_OBJ ? jget(entry, "key_id") : NULL;
        if (!id || id->type != J_STR) {
            *why = "trust-root-entry";
            return false;
        }
        if (strcmp(id->str, key_id)) continue;
        jval *status = jget(entry, "status");
        jval *public = jget(entry, "public_key");
        if (!status || status->type != J_STR || !status_ok_for_verify(status->str)) {
            *why = "key-not-trusted";
            return false;
        }
        if (!public || public->type != J_STR || !hex_decode(public->str, public_key, 32)) {
            *why = "key-format";
            return false;
        }
        return true;
    }
    *why = "unknown-key";
    return false;
}

/* Verify signature_bytes (signature JSON) over manifest_bytes against the
 * trust root. Mirrors scripts/release/signing.py verify_manifest policy. */
static bool verify_signature(const unsigned char *manifest, size_t manifest_len,
                             const unsigned char *signature, size_t signature_len,
                             const unsigned char *trust, size_t trust_len,
                             char key_id_out[65], const char **why)
{
    jval *sig = json_parse(signature, signature_len);
    if (!sig || sig->type != J_OBJ) {
        *why = "signature-parse";
        return false;
    }
    jval *schema = jget(sig, "schema_version");
    if (!schema || schema->type != J_INT || schema->num != 1 ||
        !jstr_is(jget(sig, "scheme"), "ed25519-pure-python-rfc8032")) {
        *why = "signature-scheme";
        return false;
    }
    jval *key_id = jget(sig, "key_id");
    jval *digest = jget(sig, "manifest_sha256");
    jval *signed_at = jget(sig, "signed_at");
    jval *signature_hex = jget(sig, "signature");
    if (!key_id || key_id->type != J_STR || !hex_string(key_id->str, 32)) {
        *why = "signature-key-id";
        return false;
    }
    if (!digest || digest->type != J_STR || !hex_string(digest->str, 64)) {
        *why = "signature-digest";
        return false;
    }
    if (!signed_at || signed_at->type != J_STR || strlen(signed_at->str) > 64) {
        *why = "signature-date";
        return false;
    }
    for (const char *c = signed_at->str; *c; c++)
        if (*c < 0x20 || *c > 0x7e || *c == '"' || *c == '\\') {
            *why = "signature-date-charset";
            return false;
        }
    unsigned char sig_bytes[64];
    if (!signature_hex || signature_hex->type != J_STR ||
        !hex_decode(signature_hex->str, sig_bytes, 64)) {
        *why = "signature-format";
        return false;
    }
    /* Copy every needed field: trust_lookup re-enters the JSON parser, and its
     * arena reset would invalidate the signature tree's strings. */
    char key_id_copy[33], digest_copy[65], signed_at_copy[65];
    snprintf(key_id_copy, sizeof key_id_copy, "%s", key_id->str);
    snprintf(digest_copy, sizeof digest_copy, "%s", digest->str);
    snprintf(signed_at_copy, sizeof signed_at_copy, "%s", signed_at->str);
    unsigned char actual[32];
    sha256_ctx ctx;
    sha256_init(&ctx);
    sha256_update(&ctx, manifest, manifest_len);
    sha256_final(&ctx, actual);
    char actual_hex[65];
    hash_hex(actual, actual_hex);
    if (strcmp(actual_hex, digest_copy)) {
        *why = "manifest-tampered";
        return false;
    }
    unsigned char public_key[32];
    if (!trust_lookup(trust, trust_len, key_id_copy, public_key, why)) return false;
    /* Exact canonical signing payload, key order matching the host signer. */
    char payload[512];
    int needed = snprintf(payload, sizeof payload,
                          "{\"key_id\":\"%s\",\"manifest_sha256\":\"%s\",\"schema_version\":1,"
                          "\"scheme\":\"ed25519-pure-python-rfc8032\",\"signed_at\":\"%s\"}",
                          key_id_copy, digest_copy, signed_at_copy);
    if (needed <= 0 || (size_t)needed >= sizeof payload) {
        *why = "payload-size";
        return false;
    }
    if (!ed25519_verify(public_key, (const unsigned char *)payload, (size_t)needed, sig_bytes)) {
        *why = "ed25519";
        return false;
    }
    snprintf(key_id_out, 65, "%s", key_id_copy);
    return true;
}

/* --------------------------------------------------------------- database */

static void db_record_path(char *out, size_t size, const char *name)
{
    int needed = snprintf(out, size, "%s/%s.json", opt_db, name);
    (void)needed;
}

static bool db_load(const char *name, pmanifest *out)
{
    char path[MAX_PATH];
    db_record_path(path, sizeof path, name);
    unsigned char *data;
    size_t len;
    if (!read_file(path, &data, &len, MAX_MANIFEST_BYTES)) return false;
    if (!parse_manifest(data, len, out)) {
        free(data);
        return false;
    }
    if (strcmp(out->name, name)) {
        free(data);
        return false;
    }
    return true;  /* out->raw owns data */
}

static bool db_installed(const char *name)
{
    char path[MAX_PATH];
    db_record_path(path, sizeof path, name);
    struct stat st;
    return !stat(path, &st) && S_ISREG(st.st_mode);
}

/* --------------------------------------------------------------- commands */

static int cmd_list(void)
{
    DIR *dir = opendir(opt_db);
    if (!dir) {
        if (errno == ENOENT) {
            printf("BLAUOS_BPM list count=0\n");
            return 0;
        }
        return fail("db-open");
    }
    int count = 0;
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        size_t len = strlen(entry->d_name);
        if (len < 6 || strcmp(entry->d_name + len - 5, ".json")) continue;
        pmanifest manifest;
        char name[65];
        snprintf(name, sizeof name, "%.*s", (int)(len - 5), entry->d_name);
        if (!name_ok(name) || !db_load(name, &manifest)) {
            closedir(dir);
            return fail("db-record");
        }
        printf("%s %s\n", manifest.name, manifest.version);
        free(manifest.raw);
        count++;
    }
    closedir(dir);
    printf("BLAUOS_BPM list count=%d\n", count);
    return 0;
}

static int verify_one(const char *name, bool *ok)
{
    pmanifest manifest;
    if (!db_load(name, &manifest)) return fail("db-record");
    for (int i = 0; i < manifest.nfiles; i++) {
        char dest[MAX_PATH];
        join_path(dest, sizeof dest, opt_root, manifest.files[i].path);
        int fd = open(dest, O_RDONLY);
        if (fd < 0) {
            fprintf(stderr, "BLAUOS_BPM verify fail missing %s\n", manifest.files[i].path);
            free(manifest.raw);
            *ok = false;
            return 1;
        }
        char hex[65];
        bool hashed = sha256_file_fd(fd, hex);
        close(fd);
        if (!hashed || strcmp(hex, manifest.files[i].sha256)) {
            fprintf(stderr, "BLAUOS_BPM verify fail hash %s\n", manifest.files[i].path);
            free(manifest.raw);
            *ok = false;
            return 1;
        }
    }
    printf("BLAUOS_BPM verify name=%s version=%s files=%d pass\n",
           manifest.name, manifest.version, manifest.nfiles);
    free(manifest.raw);
    return 0;
}

static int cmd_verify(const char *name)
{
    bool ok = true;
    if (name) return verify_one(name, &ok);
    DIR *dir = opendir(opt_db);
    if (!dir) {
        if (errno == ENOENT) return 0;  /* nothing installed; nothing to fail */
        return fail("db-open");
    }
    int status = 0;
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        size_t len = strlen(entry->d_name);
        if (len < 6 || strcmp(entry->d_name + len - 5, ".json")) continue;
        char record[65];
        snprintf(record, sizeof record, "%.*s", (int)(len - 5), entry->d_name);
        if (!name_ok(record) || verify_one(record, &ok)) status = 1;
    }
    closedir(dir);
    return status;
}

static int cmd_remove(const char *name)
{
    if (!name_ok(name)) return fail("name");
    pmanifest manifest;
    if (!db_load(name, &manifest)) return fail("not-installed");
    DIR *dir = opendir(opt_db);
    if (dir) {
        struct dirent *entry;
        while ((entry = readdir(dir))) {
            size_t len = strlen(entry->d_name);
            if (len < 6 || strcmp(entry->d_name + len - 5, ".json")) continue;
            char other[65];
            snprintf(other, sizeof other, "%.*s", (int)(len - 5), entry->d_name);
            if (!strcmp(other, name) || !name_ok(other)) continue;
            pmanifest dependent;
            if (!db_load(other, &dependent)) continue;
            for (int i = 0; i < dependent.ndepends; i++)
                if (!strcmp(dependent.depends[i], name)) {
                    fprintf(stderr, "BLAUOS_BPM remove fail required-by %s\n", dependent.name);
                    free(dependent.raw);
                    free(manifest.raw);
                    closedir(dir);
                    return 1;
                }
            free(dependent.raw);
        }
        closedir(dir);
    }
    /* Fail closed: only remove files whose recorded hash still matches. */
    for (int i = 0; i < manifest.nfiles; i++) {
        char dest[MAX_PATH];
        join_path(dest, sizeof dest, opt_root, manifest.files[i].path);
        struct stat st;
        if (lstat(dest, &st) || !S_ISREG(st.st_mode)) {
            free(manifest.raw);
            return fail("missing-recorded-file");
        }
        int fd = open(dest, O_RDONLY);
        if (fd < 0) {
            free(manifest.raw);
            return fail("open-recorded-file");
        }
        char hex[65];
        bool hashed = sha256_file_fd(fd, hex);
        close(fd);
        if (!hashed || strcmp(hex, manifest.files[i].sha256)) {
            free(manifest.raw);
            return fail("modified-recorded-file");
        }
    }
    for (int i = 0; i < manifest.nfiles; i++) {
        char dest[MAX_PATH];
        join_path(dest, sizeof dest, opt_root, manifest.files[i].path);
        if (unlink(dest)) {
            free(manifest.raw);
            return fail("unlink");
        }
    }
    char record[MAX_PATH];
    db_record_path(record, sizeof record, name);
    if (unlink(record)) {
        free(manifest.raw);
        return fail("db-unlink");
    }
    printf("BLAUOS_BPM remove name=%s files=%d pass\n", name, manifest.nfiles);
    free(manifest.raw);
    return 0;
}

/* mkdir -p for an absolute directory path. */
static bool mkdir_p(const char *path)
{
    char buffer[MAX_PATH];
    size_t len = strlen(path);
    if (!len || len >= sizeof buffer || path[0] != '/') return false;
    memcpy(buffer, path, len + 1);
    for (char *slash = buffer + 1; ; slash++) {
        if (*slash && *slash != '/') continue;
        bool end = !*slash;
        *slash = 0;
        if (mkdir(buffer, 0755) && errno != EEXIST) return false;
        struct stat st;
        if (stat(buffer, &st) || !S_ISDIR(st.st_mode)) return false;
        if (end) return true;
        *slash = '/';
    }
}

/* The "runtime" dependency is the independent base image itself: always
 * present, never a package record. */
static bool dependency_satisfied(const char *name)
{
    return !strcmp(name, "runtime") || db_installed(name);
}

struct install_state {
    char staging[MAX_PATH];
    int committed;
};

static void cleanup_staging(struct install_state *state, pmanifest *manifest)
{
    for (int i = 0; i < manifest->nfiles; i++) {
        char staged[MAX_PATH];
        join_path(staged, sizeof staged, state->staging, manifest->files[i].path);
        unlink(staged);
        /* Best-effort removal of the per-file staging directories, deepest first. */
        char dir[MAX_PATH];
        join_path(dir, sizeof dir, state->staging, manifest->files[i].path);
        char *slash = strrchr(dir, '/');
        while (slash && slash > dir) {
            *slash = 0;
            if (!strcmp(dir, state->staging)) break;
            rmdir(dir);
            slash = strrchr(dir, '/');
        }
    }
    rmdir(state->staging);
}

static int install_from_stream(FILE *stream, pmanifest *manifest,
                               const unsigned char *trust, size_t trust_len)
{
    /* Pass A: collect and verify the signed manifest before any write. */
    unsigned char *manifest_bytes = NULL, *sig_bytes = NULL;
    size_t manifest_len = 0, sig_len = 0;
    tarent member;
    int rc;
    while ((rc = tar_next(stream, &member)) == 1) {
        unsigned long long consumed = 0;
        if (!member.is_dir && !strcmp(member.name, "manifest.json")) {
            if (manifest_bytes || !member.size || member.size > MAX_MANIFEST_BYTES) goto bad_pass_a;
            manifest_bytes = malloc(member.size);
            if (!manifest_bytes || fread(manifest_bytes, 1, member.size, stream) != member.size)
                goto bad_pass_a;
            manifest_len = member.size;
            consumed = member.size;
        } else if (!member.is_dir && !strcmp(member.name, "manifest.json.sig")) {
            if (sig_bytes || !member.size || member.size > MAX_SIG_BYTES) goto bad_pass_a;
            sig_bytes = malloc(member.size);
            if (!sig_bytes || fread(sig_bytes, 1, member.size, stream) != member.size)
                goto bad_pass_a;
            sig_len = member.size;
            consumed = member.size;
        }
        if (tar_skip(stream, member.is_dir ? 0 : member.size, consumed)) goto bad_pass_a;
    }
    if (rc) goto bad_pass_a;
    if (!manifest_bytes || !sig_bytes) goto bad_pass_a;
    goto pass_a_loaded;

bad_pass_a:
    free(manifest_bytes);
    free(sig_bytes);
    return fail("package-format");

pass_a_loaded:
    if (!parse_manifest(manifest_bytes, manifest_len, manifest)) {
        free(manifest_bytes);
        free(sig_bytes);
        return fail("manifest-schema");
    }
    {
        char key_id[65];
        const char *why = "unknown";
        bool verified = verify_signature(manifest_bytes, manifest_len, sig_bytes, sig_len,
                                         trust, trust_len, key_id, &why);
        free(sig_bytes);
        if (!verified) {
            free(manifest_bytes);
            return fail(why);
        }
    }
    if (db_installed(manifest->name)) {
        free(manifest->raw);
        return fail("already-installed");
    }
    for (int i = 0; i < manifest->ndepends; i++)
        if (!dependency_satisfied(manifest->depends[i])) {
            fprintf(stderr, "BLAUOS_BPM install fail missing-dependency %s\n", manifest->depends[i]);
            free(manifest->raw);
            return 1;
        }

    /* Pass B: extract payload members into staging, hashing as we go. */
    struct install_state state;
    state.committed = 0;
    {
        char tmpbase[MAX_PATH];
        int needed = snprintf(tmpbase, sizeof tmpbase, "%s", opt_db);
        if (needed <= 0 || (size_t)needed >= sizeof tmpbase) {
            free(manifest->raw);
            return fail("db-path");
        }
        char *slash = strrchr(tmpbase, '/');
        if (!slash || slash == tmpbase) {
            free(manifest->raw);
            return fail("db-path");
        }
        strcpy(slash, "/tmp");
        if (!mkdir_p(tmpbase)) {
            free(manifest->raw);
            return fail("tmp-mkdir");
        }
        needed = snprintf(state.staging, sizeof state.staging, "%s/bpm-%d", tmpbase, (int)getpid());
        if (needed <= 0 || (size_t)needed >= sizeof state.staging) {
            free(manifest->raw);
            return fail("tmp-path");
        }
        if (mkdir(state.staging, 0700)) {
            free(manifest->raw);
            return fail("tmp-mkdtemp");
        }
    }
    int matched = 0;
    if (fseek(stream, 0, SEEK_SET)) goto install_fail;
    while ((rc = tar_next(stream, &member)) == 1) {
        if (member.is_dir || strncmp(member.name, "payload/", 8)) {
            if (tar_skip(stream, member.is_dir ? 0 : member.size, 0)) goto install_fail;
            continue;
        }
        const char *guest = member.name + 7;  /* keep the leading slash */
        pfile *record = NULL;
        for (int i = 0; i < manifest->nfiles; i++)
            if (!strcmp(manifest->files[i].path, guest)) record = &manifest->files[i];
        if (!record || record->size != member.size || record->mode != member.mode) goto install_fail;
        char staged[MAX_PATH];
        join_path(staged, sizeof staged, state.staging, guest);
        if (!make_parents(state.staging, guest)) goto install_fail;
        int fd = open(staged, O_WRONLY | O_CREAT | O_EXCL, 0600);
        if (fd < 0) goto install_fail;
        sha256_ctx hash;
        sha256_init(&hash);
        unsigned long long remaining = member.size;
        bool io_ok = true;
        while (remaining && io_ok) {
            unsigned char block[16384];
            size_t want = remaining > sizeof block ? sizeof block : (size_t)remaining;
            size_t got = fread(block, 1, want, stream);
            if (got != want) break;
            sha256_update(&hash, block, got);
            size_t done = 0;
            while (done < got) {
                ssize_t n = write(fd, block + done, got - done);
                if (n <= 0) {
                    io_ok = false;
                    break;
                }
                done += (size_t)n;
            }
            remaining -= got;
        }
        bool closed = !close(fd);
        unsigned char digest[32];
        sha256_final(&hash, digest);
        char hex[65];
        hash_hex(digest, hex);
        if (!io_ok || !closed || remaining || strcmp(hex, record->sha256)) goto install_fail;
        matched++;
        if (tar_skip(stream, member.size, member.size)) goto install_fail;  /* pad only */
    }
    if (rc) goto install_fail;
    if (matched != manifest->nfiles) goto install_fail;

    /* Commit: refuse to overwrite anything; rename staged files into place. */
    const char *commit_reason = "commit";
    for (int i = 0; i < manifest->nfiles; i++) {
        char dest[MAX_PATH], staged[MAX_PATH];
        join_path(dest, sizeof dest, opt_root, manifest->files[i].path);
        join_path(staged, sizeof staged, state.staging, manifest->files[i].path);
        struct stat st;
        if (!lstat(dest, &st) || errno != ENOENT) {
            commit_reason = "destination-exists";
            goto commit_fail;
        }
        if (!make_parents(opt_root, manifest->files[i].path)) goto commit_fail;
        if (rename(staged, dest)) goto commit_fail;
        state.committed++;
        if (chmod(dest, manifest->files[i].mode)) goto commit_fail;
    }
    {
        char record[MAX_PATH];
        db_record_path(record, sizeof record, manifest->name);
        if (!mkdir_p(opt_db)) goto commit_fail;
        if (!write_atomic(record, manifest->raw, manifest->raw_len)) goto commit_fail;
    }
    cleanup_staging(&state, manifest);
    printf("BLAUOS_BPM install name=%s version=%s files=%d pass\n",
           manifest->name, manifest->version, manifest->nfiles);
    free(manifest->raw);
    return 0;

commit_fail:
    for (int i = 0; i < state.committed; i++) {
        char dest[MAX_PATH];
        join_path(dest, sizeof dest, opt_root, manifest->files[i].path);
        unlink(dest);
    }
    cleanup_staging(&state, manifest);
    free(manifest->raw);
    return fail(commit_reason);
install_fail:
    cleanup_staging(&state, manifest);
    free(manifest->raw);
    return fail("payload-verify");
}

static int cmd_install(const char *arg)
{
    char package_path[MAX_PATH];
    if (strchr(arg, '/') || strstr(arg, ".bpm")) {
        int needed = snprintf(package_path, sizeof package_path, "%s", arg);
        if (needed <= 0 || (size_t)needed >= sizeof package_path) return fail("path");
    } else {
        /* Resolve a bare name through the signed repo index. */
        if (!name_ok(arg)) return fail("name");
        char index_path[MAX_PATH], sig_path[MAX_PATH];
        int n1 = snprintf(index_path, sizeof index_path, "%s/index.json", opt_repo);
        int n2 = snprintf(sig_path, sizeof sig_path, "%s/index.json.sig", opt_repo);
        if (n1 <= 0 || n2 <= 0) return fail("repo-path");
        unsigned char *index_bytes, *sig_bytes, *trust_bytes;
        size_t index_len, sig_len, trust_len;
        if (!read_file(index_path, &index_bytes, &index_len, MAX_JSON_BYTES) ||
            !read_file(sig_path, &sig_bytes, &sig_len, MAX_SIG_BYTES) ||
            !read_file(opt_trust, &trust_bytes, &trust_len, MAX_JSON_BYTES))
            return fail("repo-read");
        char key_id[65];
        const char *why = "unknown";
        bool verified = verify_signature(index_bytes, index_len, sig_bytes, sig_len,
                                         trust_bytes, trust_len, key_id, &why);
        free(sig_bytes);
        free(trust_bytes);
        if (!verified) {
            free(index_bytes);
            return fail(why);
        }
        jval *index = json_parse(index_bytes, index_len);
        const char *file = NULL, *sha = NULL;
        if (index && index->type == J_OBJ &&
            jstr_is(jget(index, "manifest_kind"), "blauos-package-index")) {
            jval *packages = jget(index, "packages");
            if (packages && packages->type == J_ARR)
                for (int i = 0; i < packages->nkids; i++) {
                    jval *entry = packages->kids[i];
                    jval *name = entry->type == J_OBJ ? jget(entry, "name") : NULL;
                    jval *member = entry->type == J_OBJ ? jget(entry, "file") : NULL;
                    jval *hash = entry->type == J_OBJ ? jget(entry, "sha256") : NULL;
                    if (!name || name->type != J_STR || !member || member->type != J_STR ||
                        !hash || hash->type != J_STR) continue;
                    if (!strcmp(name->str, arg)) {
                        if (file) {
                            free(index_bytes);
                            return fail("repo-ambiguous");
                        }
                        file = member->str;
                        sha = hash->str;
                    }
                }
        }
        if (!file || !sha) {
            free(index_bytes);
            return fail("not-in-repo");
        }
        for (const char *c = file; *c; c++)
            if (!((*c >= 'a' && *c <= 'z') || (*c >= '0' && *c <= '9') || *c == '.' ||
                  *c == '-' || *c == '_')) {
                free(index_bytes);
                return fail("repo-file-name");
            }
        int needed = snprintf(package_path, sizeof package_path, "%s/%s", opt_repo, file);
        if (needed <= 0 || (size_t)needed >= sizeof package_path) {
            free(index_bytes);
            return fail("repo-path");
        }
        int fd = open(package_path, O_RDONLY);
        if (fd < 0) {
            free(index_bytes);
            return fail("repo-open");
        }
        char hex[65];
        bool hashed = sha256_file_fd(fd, hex);
        close(fd);
        free(index_bytes);
        if (!hashed || strcmp(hex, sha)) return fail("repo-hash");
    }
    unsigned char *trust;
    size_t trust_len;
    if (!read_file(opt_trust, &trust, &trust_len, MAX_JSON_BYTES)) return fail("trust-root");
    struct stat st;
    if (stat(package_path, &st) || !S_ISREG(st.st_mode) ||
        (unsigned long long)st.st_size > MAX_PACKAGE_BYTES) {
        free(trust);
        return fail("package-file");
    }
    FILE *stream = fopen(package_path, "rb");
    if (!stream) {
        free(trust);
        return fail("package-open");
    }
    pmanifest manifest;
    int status = install_from_stream(stream, &manifest, trust, trust_len);
    fclose(stream);
    free(trust);
    return status;
}

static int cmd_repo(void)
{
    char index_path[MAX_PATH], sig_path[MAX_PATH];
    int n1 = snprintf(index_path, sizeof index_path, "%s/index.json", opt_repo);
    int n2 = snprintf(sig_path, sizeof sig_path, "%s/index.json.sig", opt_repo);
    if (n1 <= 0 || n2 <= 0) return fail("repo-path");
    unsigned char *index_bytes, *sig_bytes, *trust_bytes;
    size_t index_len, sig_len, trust_len;
    if (!read_file(index_path, &index_bytes, &index_len, MAX_JSON_BYTES) ||
        !read_file(sig_path, &sig_bytes, &sig_len, MAX_SIG_BYTES) ||
        !read_file(opt_trust, &trust_bytes, &trust_len, MAX_JSON_BYTES))
        return fail("repo-read");
    char key_id[65];
    const char *why = "unknown";
    bool verified = verify_signature(index_bytes, index_len, sig_bytes, sig_len,
                                     trust_bytes, trust_len, key_id, &why);
    free(sig_bytes);
    free(trust_bytes);
    if (!verified) {
        free(index_bytes);
        return fail(why);
    }
    jval *index = json_parse(index_bytes, index_len);
    if (!index || index->type != J_OBJ ||
        !jstr_is(jget(index, "manifest_kind"), "blauos-package-index")) {
        free(index_bytes);
        return fail("index-schema");
    }
    jval *packages = jget(index, "packages");
    if (!packages || packages->type != J_ARR) {
        free(index_bytes);
        return fail("index-packages");
    }
    for (int i = 0; i < packages->nkids; i++) {
        jval *entry = packages->kids[i];
        jval *name = entry->type == J_OBJ ? jget(entry, "name") : NULL;
        jval *version = entry->type == J_OBJ ? jget(entry, "version") : NULL;
        jval *member = entry->type == J_OBJ ? jget(entry, "file") : NULL;
        if (!name || name->type != J_STR || !version || version->type != J_STR ||
            !member || member->type != J_STR) {
            free(index_bytes);
            return fail("index-entry");
        }
        printf("%s %s %s\n", name->str, version->str, member->str);
    }
    printf("BLAUOS_BPM repo count=%d pass\n", packages->nkids);
    free(index_bytes);
    return 0;
}

/* Receive a package over the serial console: hexadecimal lines on stdin,
 * terminated by "END <64 lowercase hex sha256>". Fail closed on any frame
 * error; the output file is only kept when the final digest matches. */
static int cmd_recv(const char *output)
{
    if (strlen(output) >= MAX_PATH) return fail("path");
    int fd = open(output, O_WRONLY | O_CREAT | O_EXCL, 0644);
    if (fd < 0) return fail("open-output");
    sha256_ctx hash;
    sha256_init(&hash);
    size_t total = 0;
    char line[512];
    char final_hex[65] = {0};
    bool done = false, ok = true;
    while (!done && ok && fgets(line, sizeof line, stdin)) {
        size_t len = strlen(line);
        while (len && (line[len - 1] == '\n' || line[len - 1] == '\r')) line[--len] = 0;
        if (!len) continue;
        if (!strncmp(line, "END ", 4)) {
            if (!hex_string(line + 4, 64)) {
                ok = false;
                break;
            }
            unsigned char digest[32];
            sha256_final(&hash, digest);
            hash_hex(digest, final_hex);
            if (strcmp(final_hex, line + 4)) ok = false;
            done = true;
            break;
        }
        if (len > 400 || len % 2) {
            ok = false;
            break;
        }
        unsigned char block[200];
        if (!hex_decode(line, block, len / 2)) {
            ok = false;
            break;
        }
        if (total + len / 2 > MAX_RECV_BYTES) {
            ok = false;
            break;
        }
        size_t written = 0;
        while (written < len / 2) {
            ssize_t n = write(fd, block + written, len / 2 - written);
            if (n <= 0) {
                ok = false;
                break;
            }
            written += (size_t)n;
        }
        sha256_update(&hash, block, len / 2);
        total += len / 2;
    }
    close(fd);
    if (!done || !ok) {
        unlink(output);
        return fail("recv-stream");
    }
    printf("BLAUOS_BPM recv pass sha256=%s bytes=%zu\n", final_hex, total);
    return 0;
}

static int usage(void)
{
    fprintf(stderr,
            "usage: bpm [--root DIR] [--db DIR] [--trust-root FILE] [--repo DIR] command [args]\n"
            "  install <package.bpm|name>  verify + install a signed package\n"
            "  remove <name>               remove a package (refuses dependents)\n"
            "  list                        list installed packages\n"
            "  verify [name]               re-hash installed files\n"
            "  repo                        list the signed local repo index\n"
            "  recv <output>               receive a hex-framed file over the console\n");
    return 2;
}

int main(int argc, char **argv)
{
    int i = 1;
    while (i < argc && !strncmp(argv[i], "--", 2)) {
        const char *value = i + 1 < argc ? argv[i + 1] : NULL;
        if (!value) return usage();
        if (!strcmp(argv[i], "--root")) opt_root = value;
        else if (!strcmp(argv[i], "--db")) opt_db = value;
        else if (!strcmp(argv[i], "--trust-root")) opt_trust = value;
        else if (!strcmp(argv[i], "--repo")) opt_repo = value;
        else return usage();
        i += 2;
    }
    if (i >= argc) return usage();
    const char *command = argv[i++];
    g_command = command;
    if (!strcmp(command, "install") && i < argc) return cmd_install(argv[i]);
    if (!strcmp(command, "remove") && i < argc) return cmd_remove(argv[i]);
    if (!strcmp(command, "list")) return cmd_list();
    if (!strcmp(command, "verify")) return cmd_verify(i < argc ? argv[i] : NULL);
    if (!strcmp(command, "repo")) return cmd_repo();
    if (!strcmp(command, "recv") && i < argc) return cmd_recv(argv[i]);
    return usage();
}
