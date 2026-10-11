/* blauOS tracked configuration for the tiny-feature serial-console Vim build.
 *
 * Replaces the autoconf-generated auto/config.h: vim's configure cannot run
 * for this profile because it probes the host runtime and links host
 * libraries. Every entry below is reviewed against the independently built
 * Darwin runtime and the macOS SDK headers. The runtime provides libSystem
 * only: no termcap, terminfo, curses, iconv, X11 or gettext, so those
 * HAVE_/FEAT_ switches stay undefined and Vim's own termlib.c plus its
 * builtin termcap entries (ansi, vt320, vt52, xterm, dumb) are used instead.
 */
#ifndef BLAUOS_VIM_CONFIG_H
#define BLAUOS_VIM_CONFIG_H

#define UNIX 1
#define FEAT_TINY 1

#define HAVE_ATTRIBUTE_UNUSED 1
#define VIM_SIZEOF_INT 4
#define VIM_SIZEOF_LONG 8
#define SIZEOF_OFF_T 8
#define SIZEOF_TIME_T 8
#define TIME_WITH_SYS_TIME 1
#define SYS_SELECT_WITH_SYS_TIME 1
#define SELECT_TYPE_ARG234 (fd_set *)

/* SDK headers present in the independent-runtime compile profile. */
#define HAVE_DIRENT_H 1
#define HAVE_ERRNO_H 1
#define HAVE_FCNTL_H 1
#define HAVE_INTTYPES_H 1
#define HAVE_LANGINFO_H 1
#define HAVE_LIBGEN_H 1
#define HAVE_LOCALE_H 1
#define HAVE_MATH_H 1
#define HAVE_POLL_H 1
#define HAVE_PWD_H 1
#define HAVE_SETJMP_H 1
#define HAVE_STDINT_H 1
#define HAVE_STDLIB_H 1
#define HAVE_STRINGS_H 1
#define HAVE_STRING_H 1
#define HAVE_SYS_IOCTL_H 1
#define HAVE_SYS_PARAM_H 1
#define HAVE_SYS_POLL_H 1
#define HAVE_SYS_RESOURCE_H 1
#define HAVE_SYS_SELECT_H 1
#define HAVE_SYS_TIME_H 1
#define HAVE_SYS_TYPES_H 1
#define HAVE_SYS_UTSNAME_H 1
#define HAVE_SYS_WAIT_H 1
#define HAVE_TERMIOS_H 1
#define HAVE_UNISTD_H 1
#define HAVE_UTIME_H 1
#define HAVE_WCHAR_H 1
#define HAVE_WCTYPE_H 1

/* libSystem entry points the independent runtime provides. */
#define HAVE_CLOCK_GETTIME 1
#define HAVE_DIRFD 1
#define HAVE_FCHDIR 1
#define HAVE_FCHMOD 1
#define HAVE_FCHOWN 1
#define HAVE_FD_CLOEXEC 1
#define HAVE_FLOCK 1
#define HAVE_FSEEKO 1
#define HAVE_FSYNC 1
#define HAVE_FTRUNCATE 1
#define HAVE_GETCWD 1
#define HAVE_GETPGID 1
#define HAVE_GETPWNAM 1
#define HAVE_GETPWUID 1
#define HAVE_GETRLIMIT 1
#define HAVE_GETTIMEOFDAY 1
#define HAVE_ISINF 1
#define HAVE_ISNAN 1
#define HAVE_LOCALTIME_R 1
#define HAVE_LSTAT 1
#define HAVE_MBLEN 1
#define HAVE_MEMSET 1
#define HAVE_MKDTEMP 1
#define HAVE_NANOSLEEP 1
#define HAVE_OPENDIR 1
#define HAVE_PUTENV 1
#define HAVE_QSORT 1
#define HAVE_READLINK 1
#define HAVE_RENAME 1
#define HAVE_SELECT 1
#define HAVE_SETENV 1
#define HAVE_SETPGID 1
#define HAVE_SETSID 1
#define HAVE_SIGACTION 1
#define HAVE_SIGALTSTACK 1
#define HAVE_SIGPROCMASK 1
#define HAVE_SIGSETJMP 1
#define HAVE_STRCASECMP 1
#define HAVE_STRCOLL 1
#define HAVE_STRERROR 1
#define HAVE_STRFTIME 1
#define HAVE_STRNCASECMP 1
#define HAVE_STRPBRK 1
#define HAVE_STRPTIME 1
#define HAVE_STRTOL 1
#define HAVE_ST_BLKSIZE 1
#define HAVE_SYSCONF 1
#define HAVE_TOWLOWER 1
#define HAVE_TOWUPPER 1
#define HAVE_ISWUPPER 1
#define HAVE_TZSET 1
#define HAVE_UNSETENV 1
#define HAVE_USLEEP 1
#define HAVE_UTIME 1
#define HAVE_UTIMES 1

/* Vim's own src/termlib.c supplies the termcap entry points; term.c declares
 * UP/BC/PC/ospeed as extern and termlib.c defines them. */
#define HAVE_TGETENT 1
#define OSPEED_EXTERN 1
#define UP_BC_PC_EXTERN 1

#endif
