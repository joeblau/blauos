/*
 * Source regression harness for the PureDarwin kernel PRNG adapter.
 * Build with the pinned corecrypto/Yarrow sources on a Darwin host. The fixed
 * test vectors are deliberately public and must never seed a running guest.
 * This checks adapter seed use, synchronization and callback semantics; it
 * cannot establish the quality of any platform entropy source.
 */
#include <corecrypto/cckprng.h>
#include <corecrypto/ccrng.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static struct cckprng_ctx shared;
static int entropy_calls;

static int32_t entropy_fixture(size_t *length, void *output, void *argument)
{
    int *calls = argument;
    (*calls)++;
    if (*length < 32) return -1;
    memset(output, 0xd9, 32);
    *length = 32;
    return 32; /* XNU returns accepted sample count, rather than errno=0. */
}

static void *generate_thread(void *unused)
{
    (void)unused;
    unsigned char output[64];
    for (int i = 0; i < 100; i++) cckprng_generate(&shared, 0, sizeof(output), output);
    return NULL;
}

int main(int argc, char **argv)
{
    unsigned char seed_a[32], seed_b[32], nonce[8], output_a[64], output_b[64];
    memset(seed_a, 0x31, sizeof(seed_a)); memset(seed_b, 0xb7, sizeof(seed_b));
    memset(nonce, 0x42, sizeof(nonce));
    struct cckprng_ctx a = {0}, b = {0};
    if (argc == 2 && !strcmp(argv[1], "--short-seed")) {
        cckprng_init(&a, 1, 0, NULL, NULL, 1, seed_a, sizeof(nonce), nonce);
        fputs("short seed was incorrectly accepted\n", stderr); return 1;
    }
    cckprng_init(&a, 1, 0, NULL, NULL, sizeof(seed_a), seed_a, sizeof(nonce), nonce);
    cckprng_generate(&a, 0, sizeof(output_a), output_a);
    cckprng_init(&b, 1, 0, NULL, NULL, sizeof(seed_b), seed_b, sizeof(nonce), nonce);
    cckprng_generate(&b, 0, sizeof(output_b), output_b);
    if (!memcmp(output_a, output_b, sizeof(output_a))) {
        fputs("distinct admitted seed vectors produced identical output\n", stderr); return 1;
    }
    puts("PRNG_SEED_REGRESSION distinct_seeds pass");
    cckprng_init_with_getentropy(&shared, 1, sizeof(seed_a), seed_a,
                                sizeof(nonce), nonce, entropy_fixture, &entropy_calls);
    cckprng_generate(&shared, 0, sizeof(output_a), output_a);
    cckprng_refresh(&shared);
    if (entropy_calls != 2 || shared.bytes_since_entropy != 0) {
        fputs("accepted entropy callback was not consumed/refreshed\n", stderr); return 1;
    }
    puts("PRNG_SEED_REGRESSION entropy_callback pass");
    int error = -1;
    struct ccrng_state *provider = ccrng(&error);
    if (!provider || error || provider->generate(provider, sizeof(output_b), output_b) ||
        shared.bytes_generated != 128) {
        fputs("default provider did not use the initialized kernel PRNG\n", stderr); return 1;
    }
    puts("PRNG_SEED_REGRESSION default_provider pass");
    pthread_t threads[8];
    for (int i = 0; i < 8; i++) if (pthread_create(&threads[i], NULL, generate_thread, NULL)) return 1;
    for (int i = 0; i < 8; i++) if (pthread_join(threads[i], NULL)) return 1;
    if (shared.bytes_generated != 128 + 8 * 100 * 64) {
        fputs("concurrent generation lost state/accounting updates\n", stderr); return 1;
    }
    puts("PRNG_SEED_REGRESSION concurrent_generation pass");
    puts("PRNG_SEED_REGRESSION platform_entropy_quality not_tested");
    return 0;
}
