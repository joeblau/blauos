#!/usr/bin/env python3
"""Audit and install source-built commands in a running account using SSH/SFTP."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys

from ssh_smoke import connection_options
from ssh_workflow import compose, digest, write_json

ROOT = Path(__file__).resolve().parents[2]
CHECKS = {"files", "text", "identity", "search", "all"}


def command_artifacts(root, provenance):
    data = json.loads(provenance.read_text())
    if data.get("schema_version") != 1 or not data.get("artifacts"):
        raise ValueError("Expected a nonempty source command manifest")
    result = {}
    for entry in data["artifacts"]:
        path = entry.get("path", "")
        if not re.fullmatch(r"/(?:bin|usr/bin)/[a-z][a-z0-9_-]*", path):
            raise ValueError("Only command executables may be installed: " + path)
        name = Path(path).name
        source = root / path.lstrip("/")
        if (name in result or source.is_symlink() or not source.is_file()
                or not source.resolve().is_relative_to(root.resolve())
                or digest(source) != entry.get("sha256")):
            raise ValueError("Duplicate, escaping or mismatched command: " + name)
        result[name] = {"source": source, "sha256": entry["sha256"], "path": path}
    return result


def sftp_quote(path):
    value = str(path)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Control character in SFTP path")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run(args):
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("Use a fresh installation output directory")
    if any(args.output.resolve().is_relative_to(path.resolve())
           for path in (args.commands_root, args.runtime_root)):
        raise ValueError("Installation output must be outside input roots")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", args.username):
        raise ValueError("Invalid guest username")
    import ipaddress
    ipaddress.IPv4Address(args.guest_ip)
    commands = command_artifacts(args.commands_root, args.provenance)
    args.output.mkdir(parents=True, mode=0o700)
    result = {"schema_version": 1, "passed": False, "activated": False,
              "username": args.username, "guest_ip": args.guest_ip,
              "commands": {name: {key: value for key, value in item.items() if key != "source"}
                           for name, item in commands.items()}, "physical_boot_gate": False}
    try:
        audited, provenance = compose(args.runtime_root, args.runtime_provenance,
            [(args.commands_root, args.provenance)], args.output / "audited")
        audit = args.output / "runtime-audit.json"
        subprocess.run([sys.executable, str(ROOT / "scripts/build/runtime.py"), "inspect",
                        "--root", str(audited), "--provenance", str(provenance),
                        "--output", str(audit)], check=True, capture_output=True, text=True, timeout=300)
        if json.loads(audit.read_text()).get("offline_dependency_closure") is not True:
            raise ValueError("Independent command dependency closure failed")
        result["runtime_audit_sha256"] = digest(audit)
        options = connection_options(args.identity, args.known_hosts)
        destination = f"{args.username}@{args.guest_ip}"

        def ssh(command, name, stdin=None):
            process = subprocess.run(["ssh", *options, "-T", destination, command], input=stdin,
                capture_output=True, text=True, timeout=90)
            (args.output / (name + ".stdout")).write_text(process.stdout)
            (args.output / (name + ".stderr")).write_text(process.stderr)
            if process.returncode:
                raise RuntimeError(f"Guest {name} failed ({process.returncode}); see saved output")
            return process.stdout

        def sftp(lines, name):
            process = subprocess.run(["sftp", *options, "-b", "-", destination],
                input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=180)
            (args.output / (name + ".stdout")).write_text(process.stdout)
            (args.output / (name + ".stderr")).write_text(process.stderr)
            if process.returncode:
                raise RuntimeError(f"SFTP {name} failed ({process.returncode}); see saved output")

        path_check = ('test ! -L "$HOME/.local" && test ! -L "$HOME/.profile" || exit 1; '
                      'if [ -e "$HOME/.local" ] && [ ! -d "$HOME/.local" ]; then exit 1; fi; '
                      'if [ -e "$HOME/.profile" ] && [ ! -f "$HOME/.profile" ]; then exit 1; fi; ')
        state = ssh(path_check + 'printf "%s\\n" "$HOME"; '
                    'if [ -f "$HOME/.profile" ]; then printf "exists\\n"; else printf "absent\\n"; fi',
                    "account-preflight").splitlines()
        if len(state) != 2 or state[0] != "/var/home/" + args.username or state[1] not in {"exists", "absent"}:
            raise ValueError("Guest home/profile differs from the expected account layout")
        home = state[0]
        release = "blauos-tools-" + digest(args.provenance)[:12] + "-" + secrets.token_hex(3)
        remote = home + "/.local/" + release
        result["release"] = remote
        ssh("test ! -e " + shlex.quote(remote) + " && test ! -L " + shlex.quote(remote), "fresh-release")
        original = args.output / "original.profile"
        if state[1] == "exists":
            sftp([f"get {sftp_quote(home + '/.profile')} {sftp_quote(original)}"], "read-profile")
        else:
            original.write_bytes(b"")
        # SFTP creates directories even when the bootstrap image lacks mkdir.
        batch = [f"-mkdir {sftp_quote(home + '/.local')}", f"mkdir {sftp_quote(remote)}",
                 f"chmod 755 {sftp_quote(remote)}"]
        readback = args.output / "readback"
        readback.mkdir()
        for name, entry in sorted(commands.items()):
            batch += [f"put {sftp_quote(entry['source'])} {sftp_quote(remote + '/' + name)}",
                      f"chmod 755 {sftp_quote(remote + '/' + name)}",
                      f"get {sftp_quote(remote + '/' + name)} {sftp_quote(readback / name)}"]
        smoke = ROOT / "vm/commands/smoke.sh"
        batch.append(f"put {sftp_quote(smoke)} {sftp_quote(remote + '/.acceptance.sh')}")
        sftp(batch, "upload-commands")
        if any(digest(readback / name) != entry["sha256"] for name, entry in commands.items()):
            raise ValueError("A command changed during transfer; profile activation refused")
        result["transferred_hashes_verified"] = True
        output = ssh("PATH=" + shlex.quote(remote) + ':"$PATH"; export PATH; /bin/sh '
                     + shlex.quote(remote + "/.acceptance.sh"), "command-acceptance")
        passed = {match.group(1) for line in output.splitlines()
                  if (match := re.fullmatch(r"BLAUOS_COMMAND_CHECK ([a-z]+) pass", line))}
        if CHECKS - passed:
            raise ValueError("Actual guest command acceptance is incomplete")
        result["checks"] = sorted(passed)
        result["smoke_source_sha256"] = digest(smoke)
        current_state = ssh(path_check + 'if [ -f "$HOME/.profile" ]; then printf "exists\\n"; else printf "absent\\n"; fi',
                            "profile-recheck").strip()
        if current_state != state[1]:
            raise ValueError("Guest profile changed during installation; activation refused")
        if current_state == "exists":
            rechecked = args.output / "rechecked.profile"
            sftp([f"get {sftp_quote(home + '/.profile')} {sftp_quote(rechecked)}"], "recheck-profile")
            if rechecked.read_bytes() != original.read_bytes():
                raise ValueError("Guest profile content changed during installation; activation refused")
        profile = args.output / "profile"
        previous = original.read_bytes()
        addition = ('\n# Source-built BlauOS command tools.\n'
                    'case ":$PATH:" in\n'
                    f'  *":$HOME/.local/{release}:"*) ;;\n'
                    f'  *) PATH="$HOME/.local/{release}:$PATH" ;;\n'
                    'esac\nexport PATH\n')
        profile.write_bytes(previous + (b"\n" if previous and not previous.endswith(b"\n") else b"")
                            + addition.encode())
        temporary = home + "/.blauos-profile-" + secrets.token_hex(8)
        sftp([f"put {sftp_quote(profile)} {sftp_quote(temporary)}",
              f"chmod 600 {sftp_quote(temporary)}"], "stage-profile")
        # OpenSSH remote commands do not source login profiles automatically.
        try:
            result["activation_attempted"] = True
            ssh(shlex.join([remote + "/mv", "-f", "--", temporary, home + "/.profile"]), "activate-profile")
            result["activated"] = True
            marker = "BLAUOS_COMMAND_PATH_" + secrets.token_hex(8)
            verified = ssh('. "$HOME/.profile"; printf "' + marker
                           + ' %s\\n" "$(command -v ls)"; ls -la "$HOME"', "activated-ls")
            if marker + " " + remote + "/ls" not in verified.splitlines():
                raise ValueError("Activated PATH did not resolve the tested ls")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            try:
                current = args.output / "rollback-current.profile"
                sftp([f"get {sftp_quote(home + '/.profile')} {sftp_quote(current)}"], "rollback-check")
                if current.read_bytes() != profile.read_bytes():
                    raise ValueError("Profile changed after activation; automatic restoration refused")
                if state[1] == "exists":
                    sftp([f"put {sftp_quote(original)} {sftp_quote(temporary)}",
                          f"chmod 600 {sftp_quote(temporary)}"], "stage-restored-profile")
                    ssh(shlex.join([remote + "/mv", "-f", "--", temporary, home + "/.profile"]), "restore-profile")
                else:
                    sftp([f"rm {sftp_quote(home + '/.profile')}"], "restore-profile")
                result.update(activated=False, profile_restored=True)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                result["profile_restore_failure"] = str(error)
            raise
        result.update(passed=True, activated=True, original_profile_sha256=digest(original),
                      installed_profile_sha256=digest(profile))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result["failure"] = str(error)
    write_json(args.output / "result.json", result)
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("commands-root", "provenance", "runtime-root", "runtime-provenance", "identity", "known-hosts", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--guest-ip", default="192.168.64.201")
    args = parser.parse_args()
    try:
        return run(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
