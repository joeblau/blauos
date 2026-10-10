/* SPDX-License-Identifier: MIT
 * Exercises a real C runtime. Build against the target sysroot for acceptance;
 * compiling/running with the macOS SDK is only a host toolchain diagnostic.
 */
#include <Block.h>
#include <dispatch/dispatch.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <locale.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <mach-o/dyld.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <xlocale.h>

static void *thread_probe(void *value) { return value; }
extern bool _dyld_get_shared_cache_uuid(unsigned char uuid[16]);

/* Private, source-pinned XNU ABI from osfmk/mach/mk_timer.h. */
extern mach_port_name_t mk_timer_create(void);
extern kern_return_t mk_timer_destroy(mach_port_name_t);
extern kern_return_t mk_timer_arm(mach_port_name_t, uint64_t);
extern kern_return_t mk_timer_cancel(mach_port_name_t, uint64_t *);
#pragma pack(push, 4)
struct timer_message { mach_msg_header_t header; uint64_t unused[3]; };
#pragma pack(pop)

static int timer_probe(void)
{
    mach_port_name_t timer = mk_timer_create();
    mach_port_type_t type = 0;
    mach_timebase_info_data_t timebase;
    if (!MACH_PORT_VALID(timer) || mach_port_type(mach_task_self(), timer, &type) ||
        !(type & MACH_PORT_TYPE_RECEIVE) || mach_timebase_info(&timebase) ||
        !timebase.numer || !timebase.denom) return 1;
    uint64_t ticks = 20000000ULL * timebase.denom / timebase.numer;
    if (mk_timer_arm(timer, mach_absolute_time() + ticks)) return 2;
    struct { struct timer_message payload; mach_msg_max_trailer_t trailer; } receive = {0};
    kern_return_t code = mach_msg(&receive.payload.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT,
                                 0, sizeof(receive), timer, 3000, MACH_PORT_NULL);
    if (code || receive.payload.header.msgh_id ||
        receive.payload.header.msgh_size != sizeof(receive.payload) ||
        receive.payload.header.msgh_local_port != timer) return 3;
    uint64_t deadline = mach_absolute_time() + 5000000000ULL * timebase.denom / timebase.numer;
    uint64_t cancelled = 0;
    /* The kernel may coalesce the armed deadline; require its nonzero
     * scheduled time and prove that cancellation suppresses delivery. */
    if (mk_timer_arm(timer, deadline) || mk_timer_cancel(timer, &cancelled) || cancelled < deadline)
        return 4;
    if (mach_msg(&receive.payload.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0,
                 sizeof(receive), timer, 20, MACH_PORT_NULL) != MACH_RCV_TIMED_OUT) return 5;
    if (mk_timer_destroy(timer) ||
        mach_port_type(mach_task_self(), timer, &type) != KERN_INVALID_NAME) return 6;
    return 0;
}

static int fork_prepare_calls, fork_parent_calls, fork_child_calls;
static int fork_callback_failure;
static pthread_mutex_t fork_callback_lock = PTHREAD_MUTEX_INITIALIZER;
static void fork_callback_work(void)
{
    char *memory = malloc(128);
    if (!memory || pthread_mutex_lock(&fork_callback_lock)) { fork_callback_failure = 1; return; }
    memset(memory, 42, 128);
    if (memory[127] != 42 || pthread_mutex_unlock(&fork_callback_lock)) fork_callback_failure = 1;
    free(memory);
}
static void fork_prepare(void) { fork_callback_work(); ++fork_prepare_calls; }
static void fork_parent(void) { fork_callback_work(); ++fork_parent_calls; }
static void fork_child(void) { fork_callback_work(); ++fork_child_calls; }

struct loader_thread { void *handle; const char *missing; int failed; };
static pthread_mutex_t loader_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t loader_condition = PTHREAD_COND_INITIALIZER;
static int loader_ready;
static void *loader_error_worker(void *argument)
{
    struct loader_thread *state = argument;
    (void)dlerror();
    if (dlsym(state->handle, state->missing)) state->failed = 1;
    pthread_mutex_lock(&loader_lock);
    ++loader_ready;
    pthread_cond_broadcast(&loader_condition);
    while (loader_ready != 2) pthread_cond_wait(&loader_condition, &loader_lock);
    pthread_mutex_unlock(&loader_lock);
    const char *error = dlerror();
    if (!error || !strstr(error, state->missing) || dlerror()) state->failed = 1;
    return NULL;
}

static int loader_probe(const char *directory, bool require_source)
{
    char path[4096];
    char directory_path[4096];
    if (!require_source && !realpath(directory, directory_path)) return 1;
    if (snprintf(path, sizeof(path), "%s/libblauos-loader-fixture.dylib",
                 require_source ? "/usr/lib" : directory_path) >= (int)sizeof(path)) return 1;
    void *handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!handle) { fprintf(stderr, "dlopen fixture: %s\n", dlerror()); return 2; }
    int (*fixture)(int) = (int (*)(int))dlsym(handle, "blauos_loader_fixture");
    Dl_info info;
    if (!fixture || fixture(41) != 42 || !dladdr((void *)fixture, &info) ||
        strcmp(info.dli_fname, path)) return 3;
    void *(*allocate)(size_t) = (void *(*)(size_t))dlsym(handle, "malloc");
    void (*release)(void *) = (void (*)(void *))dlsym(handle, "free");
    if (!allocate || !release || allocate != malloc || release != free) return 4;
    if (require_source && (!dladdr((void *)allocate, &info) ||
                          strcmp(info.dli_fname, "/usr/lib/libSystem.B.dylib"))) return 5;
    char *memory = allocate(64);
    if (!memory) return 6;
    memset(memory, 42, 64);
    if (memory[63] != 42) return 7;
    release(memory);
    (void)dlerror();
    if (dlsym(handle, "blauos_intentionally_missing_symbol")) return 8;
    const char *error = dlerror();
    if (!error || !strstr(error, "blauos_intentionally_missing_symbol") || dlerror()) return 9;
    struct loader_thread states[2] = {{handle, "blauos_missing_thread_A", 0},
                                     {handle, "blauos_missing_thread_B", 0}};
    pthread_t workers[2];
    for (size_t i = 0; i < 2; ++i)
        if (pthread_create(&workers[i], NULL, loader_error_worker, &states[i])) return 10;
    for (size_t i = 0; i < 2; ++i)
        if (pthread_join(workers[i], NULL) || states[i].failed) return 11;
    return dlclose(handle) ? 12 : 0;
}

/* Keep the initializer blocked while seven peers contend. A once shim that
 * publishes completion before returning from its callback fails this test. */
static dispatch_once_t once_token;
static pthread_mutex_t once_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t once_condition = PTHREAD_COND_INITIALIZER;
static int once_ready, once_entered, once_release, once_calls, once_payload;
static int once_completed, once_failed;

static void once_initializer(void *context)
{
    pthread_mutex_lock(&once_lock);
    ++once_calls;
    once_entered = 1;
    pthread_cond_broadcast(&once_condition);
    while (!once_release) pthread_cond_wait(&once_condition, &once_lock);
    once_payload = *(int *)context;
    pthread_mutex_unlock(&once_lock);
}

static void *once_worker(void *context)
{
    pthread_mutex_lock(&once_lock);
    ++once_ready;
    pthread_cond_broadcast(&once_condition);
    pthread_mutex_unlock(&once_lock);
    dispatch_once_f(&once_token, context, once_initializer);
    pthread_mutex_lock(&once_lock);
    if (once_payload != 42 || !once_release) once_failed = 1;
    ++once_completed;
    pthread_mutex_unlock(&once_lock);
    return NULL;
}

static int once_probe(void)
{
    pthread_t workers[8];
    int payload = 42;
    for (size_t i = 0; i < 8; ++i)
        if (pthread_create(&workers[i], NULL, once_worker, &payload)) return 1;
    pthread_mutex_lock(&once_lock);
    while (once_ready != 8 || !once_entered)
        pthread_cond_wait(&once_condition, &once_lock);
    pthread_mutex_unlock(&once_lock);
    usleep(20000);
    pthread_mutex_lock(&once_lock);
    if (once_completed) once_failed = 1;
    once_release = 1;
    pthread_cond_broadcast(&once_condition);
    pthread_mutex_unlock(&once_lock);
    for (size_t i = 0; i < 8; ++i)
        if (pthread_join(workers[i], NULL)) return 2;
    dispatch_once_f(&once_token, &payload, once_initializer);
    return once_failed || once_calls != 1 || once_completed != 8;
}

static int blocks_probe(void)
{
    int captured = 41;
    int (^stack_block)(int) = ^(int value) { return captured + value; };
    int (^heap_block)(int) = Block_copy(stack_block);
    if (!heap_block || heap_block == stack_block) return 1;
    captured = 99;
    if (heap_block(1) != 42) return 2;
    int (^retained)(int) = Block_copy(heap_block);
    Block_release(heap_block);
    if (!retained || retained(2) != 43) return 3;
    Block_release(retained);
    __block int counter = 0;
    void (^increment)(void) = Block_copy(^{ ++counter; });
    if (!increment) return 4;
    increment();
    counter += 4;
    increment();
    if (counter != 6) return 5;
    Block_release(increment);
    return 0;
}

static int locale_probe(void)
{
    const char *input = "0xc7815537c05600d3";
    char *end = NULL;
    if (strtoull(input, &end, 0) != 0xc7815537c05600d3ULL || *end) return 1;
    locale_t locale = newlocale(LC_ALL_MASK, "C", NULL);
    if (!locale) return 2;
    if (strtoull_l(input, &end, 0, locale) != 0xc7815537c05600d3ULL || *end) return 3;
    if (strtol_l("-42x", &end, 10, locale) != -42 || strcmp(end, "x")) return 4;
    if (strtoul_l("42x", &end, 10, locale) != 42 || strcmp(end, "x")) return 5;
    if (strtoll_l("-9223372036854775808", &end, 10, locale) != LLONG_MIN || *end) return 6;
    errno = 0;
    if (strtoull_l("18446744073709551616", &end, 10, locale) != ULLONG_MAX || errno != ERANGE || *end) return 7;
    if (strtod_l("3.125x", &end, locale) != 3.125 || strcmp(end, "x")) return 8;
    if (strncasecmp_l("DarWin", "dARwIN", 6, locale)) return 9;
    char buffer[32];
    if (snprintf_l(buffer, sizeof(buffer), locale, "%d %.3f", 42, 3.125) != 8 || strcmp(buffer, "42 3.125")) return 10;
    FILE *file = tmpfile();
    if (!file || fprintf_l(file, locale, "%s", buffer) != 8 || fflush(file) || fseek(file, 0, SEEK_SET)) return 11;
    memset(buffer, 0, sizeof(buffer));
    if (fread(buffer, 1, 8, file) != 8 || strcmp(buffer, "42 3.125") || fclose(file)) return 12;
    locale_t previous = uselocale(locale);
    if (!previous || strtoull(input, &end, 0) != 0xc7815537c05600d3ULL || *end || !uselocale(previous)) return 13;
    freelocale(locale);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc != 2 && (argc != 3 || strcmp(argv[2], "--require-source-images"))) {
        fprintf(stderr, "usage: runtime-probe WRITABLE_TEST_DIRECTORY [--require-source-images]\n");
        return 64;
    }
    char *allocation = malloc(4096);
    if (!allocation) return 1;
    memset(allocation, 0xa5, 4096);
    allocation = realloc(allocation, 8192);
    if (!allocation) return 2;
    for (size_t i = 0; i < 4096; ++i)
        if ((unsigned char)allocation[i] != 0xa5) return 3;
    free(allocation);
    puts("BLAUOS_RUNTIME allocation=pass");
    if (locale_probe()) return 19;
    puts("BLAUOS_RUNTIME locale_conversions=pass");

    char path[4096];
    if (snprintf(path, sizeof(path), "%s/blauos-probe.XXXXXX", argv[1]) >= (int)sizeof(path))
        return 4;
    int fd = mkstemp(path);
    if (fd < 0) { perror("mkstemp"); return 5; }
    /* Unlink immediately: neither a successful nor failed probe leaves data. */
    if (unlink(path)) return 6;
    const char content[] = "blauos-file-io";
    char received[sizeof(content)];
    if (write(fd, content, sizeof(content)) != sizeof(content) ||
        fsync(fd) || lseek(fd, 0, SEEK_SET) != 0 ||
        read(fd, received, sizeof(received)) != sizeof(received) ||
        memcmp(received, content, sizeof(content)) || close(fd)) return 7;
    puts("BLAUOS_RUNTIME file_io=pass");

    void *mapping = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (mapping == MAP_FAILED) return 8;
    *(volatile int *)mapping = 42;
    if (*(volatile int *)mapping != 42 || munmap(mapping, 4096) || getpid() <= 0) return 9;
    puts("BLAUOS_RUNTIME syscall=pass");

    pthread_t thread;
    void *thread_result = NULL;
    if (pthread_create(&thread, NULL, thread_probe, &thread_result) ||
        pthread_join(thread, &thread_result) || thread_result != &thread_result) return 10;
    puts("BLAUOS_RUNTIME pthread=pass");
    if (once_probe()) return 17;
    puts("BLAUOS_RUNTIME concurrent_once=pass");
    if (blocks_probe()) return 18;
    puts("BLAUOS_RUNTIME blocks_lifetime=pass");
    int timer_status = timer_probe();
    if (timer_status) { fprintf(stderr, "Mach timer probe failed at step %d\n", timer_status); return 20; }
    puts("BLAUOS_RUNTIME mach_timer=pass");

    if (pthread_atfork(fork_prepare, fork_parent, fork_child)) return 21;
    pid_t child = fork();
    if (child < 0) return 11;
    if (!child) _exit(fork_prepare_calls == 1 && !fork_parent_calls && fork_child_calls == 1 &&
                     !fork_callback_failure ? 23 : 24);
    int status;
    while (waitpid(child, &status, 0) < 0) { if (errno != EINTR) return 12; }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 23) return 13;
    if (fork_prepare_calls != 1 || fork_parent_calls != 1 || fork_child_calls || fork_callback_failure) return 22;
    puts("BLAUOS_RUNTIME atfork_handlers=pass");
    puts("BLAUOS_RUNTIME process_exit=pass");
    int loader_status = loader_probe(argv[1], argc == 3);
    if (loader_status) { fprintf(stderr, "Dynamic loader probe failed at step %d\n", loader_status); return 23; }
    puts("BLAUOS_RUNTIME dynamic_loader=pass");

    bool system_seen = false, dyld_library_seen = false, objc_seen = false;
    for (uint32_t i = 0; i < _dyld_image_count(); ++i) {
        const char *name = _dyld_get_image_name(i);
        if (!name) return 14;
        printf("BLAUOS_RUNTIME image=%s\n", name);
        system_seen |= !strcmp(name, "/usr/lib/libSystem.B.dylib");
        dyld_library_seen |= !strcmp(name, "/usr/lib/system/libdyld.dylib");
        objc_seen |= !strcmp(name, "/usr/lib/libobjc.A.dylib");
        if (argc == 3 && i != 0 && strncmp(name, "/usr/lib/", 9)) return 15;
    }
    if (argc == 3) {
        unsigned char uuid[16];
        if (!system_seen || !dyld_library_seen || !objc_seen || _dyld_get_shared_cache_uuid(uuid)) return 16;
        puts("BLAUOS_RUNTIME required_images=pass shared_cache=absent");
    }
    puts("BLAUOS_RUNTIME result=pass");
    return 0;
}
