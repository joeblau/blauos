/* SPDX-License-Identifier: MIT
 * Declarations and source metadata for the headless BSD file command profile.
 * Darwin SDK ABI definitions remain intact; this is not a runtime substitute.
 */
#ifndef BLAUOS_FILE_COMPAT_H
#define BLAUOS_FILE_COMPAT_H
#include <sys/param.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sysexits.h>
#ifndef __RCSID
#define __RCSID(x)
#endif
#ifndef __FBSDID
#define __FBSDID(x)
#endif
char *fflagstostr(unsigned long);
char *getbsize(int *, long *);
void strmode(int, char *);
int lchmod(const char *, mode_t);
void *reallocarray(void *, size_t, size_t);
char *user_from_uid(uid_t, int);
char *group_from_gid(gid_t, int);
#endif
