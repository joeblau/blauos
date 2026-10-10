#!/bin/sh
# SPDX-License-Identifier: MIT
# Run inside the isolated source-built guest, never against host launchd.
set -eu
[ "${BLAUOS_GUEST_TESTING:-}" = "1" ] || { echo "guest test environment required" >&2; exit 64; }
test_fixture=/etc/blauos-tests/org.blauos.test.bootstrap.plist
test_probe=/usr/libexec/blauos-bootstrap-probe
test_ctl=/usr/bin/launchctl
test_label=org.blauos.test.bootstrap
test_failed_fixture=/etc/blauos-tests/org.blauos.test.exec-failure.plist
test_failed_label=org.blauos.test.exec-failure
trap '"$test_ctl" unload "$test_fixture" >/dev/null 2>&1 || true; "$test_ctl" unload "$test_failed_fixture" >/dev/null 2>&1 || true' EXIT INT TERM
"$test_ctl" load "$test_fixture"
test_first=
test_second=
test_attempt=0
while [ "$test_attempt" -lt 30 ]; do
  test_first=$("$test_probe" --client) && break
  test_attempt=$((test_attempt + 1))
  sleep 1
done
[ -n "$test_first" ] || { echo "bootstrap round-trip failed" >&2; exit 1; }
echo "BLAUOS_LAUNCHD job_launch=pass bootstrap_registration=pass ipc_roundtrip=pass"
test_attempt=0
while [ "$test_attempt" -lt 30 ]; do
  test_second=$("$test_probe" --client) && [ "$test_second" != "$test_first" ] && break
  test_attempt=$((test_attempt + 1))
  sleep 1
done
[ -n "$test_second" ] && [ "$test_second" != "$test_first" ] || { echo "supervision failed" >&2; exit 2; }
echo "BLAUOS_LAUNCHD supervision=pass first_pid=$test_first restarted_pid=$test_second"
test_attempt=0
while ! "$test_probe" --reaped "$test_first"; do
  test_attempt=$((test_attempt + 1))
  [ "$test_attempt" -lt 30 ] || { echo "exited launchd child remains present" >&2; exit 6; }
  sleep 1
done
echo "BLAUOS_LAUNCHD child_reaping=pass reaped_pid=$test_first"
"$test_ctl" unload "$test_fixture"
if "$test_ctl" list "$test_label" >/dev/null 2>&1; then echo "job still loaded" >&2; exit 3; fi
echo "BLAUOS_LAUNCHD job_unload=pass"
if "$test_ctl" load /etc/blauos-tests/absent.plist >/dev/null 2>&1; then echo "missing job accepted" >&2; exit 4; fi
echo "BLAUOS_LAUNCHD missing_job_error=pass"
test_exec_error=
if "$test_ctl" load "$test_failed_fixture" >/dev/null 2>&1; then
  test_attempt=0
  while [ "$test_attempt" -lt 30 ]; do
    test_failed_status=$("$test_ctl" list "$test_failed_label" 2>/dev/null) || test_failed_status=
    # This launchd exports a raw wait status. ENOENT (2) becomes 2 << 8.
    case "$test_failed_status" in *'"LastExitStatus" = 512;'*) test_exec_error=reported; break;; esac
    test_attempt=$((test_attempt + 1))
    sleep 1
  done
else
  test_exec_error=rejected
fi
[ -n "$test_exec_error" ] || { echo "failed executable error not reported" >&2; exit 5; }
"$test_ctl" unload "$test_failed_fixture" >/dev/null 2>&1 || true
echo "BLAUOS_LAUNCHD failed_executable_error=pass behavior=$test_exec_error"
# Whole PID 1 shutdown is a separate VM poweroff/child-reaping test. This
# fixture does not report that criterion as passing.
echo "BLAUOS_LAUNCHD result=service_subset_pass system_shutdown=unverified"
