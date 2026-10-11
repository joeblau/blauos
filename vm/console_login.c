/* Bind the consoleshell job's session to /dev/console as its controlling
 * terminal, then exec the interactive shell. launchd opens job descriptors
 * with O_NOCTTY before setsid() (launchd core.c job_setup_fd), so without
 * this getty-style step the console tty never gains a session or foreground
 * process group and the line discipline's interrupt characters (VINTR/VQUIT/
 * VSUSP) reach tty_pgsignal with a NULL t_pgrp - echoed as ^C but delivered
 * to no process. Fail-closed: any error exits nonzero so the launchd job
 * failure is logged; a partial session is never claimed. */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>

static int fail(const char *step)
{
	fprintf(stderr, "BLAUOS_CONSOLE_LOGIN fail %s errno=%d\n", step, errno);
	return 1;
}

int main(void)
{
	if (getsid(0) != getpid() && setsid() < 0) return fail("setsid");
	/* Deliberately not O_NOCTTY: this session leader claims the console. */
	int console = open("/dev/console", O_RDWR);
	if (console < 0) return fail("open-console");
	if (ioctl(console, TIOCSCTTY, 0) < 0) return fail("tiocsctty");
	if (tcsetpgrp(console, getpgrp()) < 0) return fail("tcsetpgrp");
	if (dup2(console, STDIN_FILENO) < 0 || dup2(console, STDOUT_FILENO) < 0 ||
	    dup2(console, STDERR_FILENO) < 0) return fail("dup2");
	if (console > STDERR_FILENO) close(console);
	execl("/bin/sh", "-sh", "-i", (char *)NULL);
	return fail("exec-shell");
}
