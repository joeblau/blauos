/* Synthetic compile-only probe of the actual native pmap pointer signing/CAS sequence. */
#include <ptrauth.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdatomic.h>
typedef struct pmap *pmap_t;
struct pmap { pmap_t __ptrauth(ptrauth_key_process_independent_data, 1,
    ptrauth_string_discriminator("pmap.nested_pmap")) nested_pmap; };
bool install_signed(struct pmap *grand, pmap_t subord) {
    pmap_t subord_addr = ptrauth_sign_unauthenticated(subord,
        ptrauth_key_process_independent_data,
        ptrauth_blend_discriminator(&grand->nested_pmap,
            ptrauth_string_discriminator("pmap.nested_pmap")));
    uintptr_t expected = 0;
    return atomic_compare_exchange_strong_explicit((_Atomic(uintptr_t) *)&grand->nested_pmap,
        &expected, (uintptr_t)subord_addr, memory_order_relaxed, memory_order_relaxed);
}
pmap_t read_authenticated(struct pmap *grand) { return grand->nested_pmap; }
