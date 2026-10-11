#!/usr/bin/env python3
"""Assemble a GPT/ext4 guest image from audited native source-built artifacts."""
import argparse
import hashlib
import json
import ipaddress
import os
import plistlib
import re
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]

import accounts


def ssh_configuration(staging, args):
    """Stage key-only SSH; ownership is applied by the filesystem formatter."""
    receipt = accounts.provision(staging, args.ssh_user, args.ssh_public_key.read_text(),
                                 args.ssh_uid, args.ssh_gid)
    ssh_dir = staging / "etc/ssh"
    ssh_dir.mkdir(exist_ok=True)
    configuration = "\n".join([
        "Port 22", "ListenAddress 0.0.0.0", "AddressFamily inet",
        "HostKey /var/db/ssh/ssh_host_ed25519_key", "HostKeyAlgorithms ssh-ed25519",
        "PubkeyAuthentication yes", "PasswordAuthentication no",
        "KbdInteractiveAuthentication no", "PermitEmptyPasswords no", "PermitRootLogin no",
        "UsePAM no", "UseDNS no", "StrictModes yes", "AllowUsers " + args.ssh_user,
        "AuthorizedKeysFile .ssh/authorized_keys", "PermitUserEnvironment no",
        "AllowAgentForwarding no", "AllowTcpForwarding no", "X11Forwarding no",
        "Ciphers chacha20-poly1305@openssh.com",
        "KexAlgorithms mlkem768x25519-sha256,sntrup761x25519-sha512,curve25519-sha256",
        "PubkeyAcceptedAlgorithms ssh-ed25519", "LogLevel VERBOSE",
        "Subsystem sftp /usr/libexec/sftp-server", "",
    ])
    (ssh_dir / "sshd_config").write_text(configuration)
    (ssh_dir / "sshd_config").chmod(0o600)
    service = {"Label": "org.blauos.sshd", "ProgramArguments": [
        "/usr/libexec/blauos-ssh-start", args.ssh_user, str(args.ssh_uid), str(args.ssh_gid)],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
        "StandardOutPath": "/dev/console", "StandardErrorPath": "/dev/console"}
    (staging / "System/Library/LaunchDaemons/org.blauos.sshd.plist").write_bytes(plistlib.dumps(service))
    # The capability probe must demonstrate that a non-root user cannot read this.
    (staging / "var/root/blauos-ssh-denied").write_text("root-only capability fixture\n")
    (staging / "var/root/blauos-ssh-denied").chmod(0o600)
    return receipt


def verify_account_inodes(filesystem, receipt):
    for entry in receipt["metadata"]:
        output = subprocess.check_output(["debugfs", "-R", "stat /" + entry["path"], str(filesystem)],
                                         text=True, stderr=subprocess.STDOUT)
        mode = re.search(r"Mode:\s+0?([0-7]{3,4})", output)
        owner = re.search(r"User:\s+(\d+)\s+Group:\s+(\d+)", output)
        if not mode or not owner or (int(mode[1], 8), int(owner[1]), int(owner[2])) != (
                int(entry["mode"], 8), entry["uid"], entry["gid"]):
            raise ValueError("Filesystem inode metadata mismatch: /" + entry["path"])
    receipt["metadata_applied"] = True
    receipt["filesystem_inode_metadata_verified"] = True


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def run(command, log, **kwargs):
    log.write("$ " + repr(command) + "\n")
    log.flush()
    subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT, **kwargs)


def assemble(args):
    if sys.platform != "linux":
        raise ValueError("Run disk formatting in Linux; native Darwin artifacts may be mounted read-only")
    if args.output.exists():
        raise ValueError("Use a new output directory; prior image evidence is preserved")
    required = ["sbin/launchd", "bin/sh", "bin/netsetup", "usr/lib/dyld", "usr/lib/libSystem.B.dylib",
                "usr/lib/system/libdyld.dylib", "bin/blauos_guest_probe", "bin/blauos_runtime_probe",
                "usr/libexec/blauos-bootstrap-probe", "usr/libexec/blauos-shutdown-probe", "usr/bin/launchctl", "bin/launchctl",
                "usr/lib/libblauos-loader-fixture.dylib"]
    if args.ssh_user:
        accounts.validate_account(args.ssh_user, args.ssh_uid, args.ssh_gid)
        accounts.public_keys(args.ssh_public_key.read_text())
        required += ["usr/sbin/sshd", "usr/libexec/sshd-auth", "usr/libexec/sshd-session",
                     "usr/libexec/sftp-server", "usr/bin/ssh-keygen",
                     "usr/libexec/blauos-ssh-start", "usr/libexec/blauos-ssh-capability-probe"]
    for name in required:
        if not (args.root / name).is_file():
            raise ValueError(f"Missing source-built root artifact: /{name}")
    args.output.mkdir(parents=True)
    audit = args.output / "runtime-audit.json"
    subprocess.run([sys.executable, str(ROOT / "scripts/build/runtime.py"), "inspect", "--root", str(args.root),
                    "--provenance", str(args.provenance), "--manifest", str(ROOT / "sources.lock.json"),
                    "--output", str(audit)], check=True)
    if not json.loads(audit.read_text()).get("offline_dependency_closure"):
        raise ValueError("Independent target dependency closure is incomplete")
    staging = args.output / "root-staging"
    shutil.copytree(args.root, staging, symlinks=True)
    for name in ("dev", "cores", "etc", "etc/blauos-tests", "var/run", "var/log", "var/db", "var/root", "var/empty", "tmp", "var/tmp",
                 "System/Library/LaunchDaemons"):
        (staging / name).mkdir(parents=True, exist_ok=True)
    for name in ("tmp", "var/tmp"):
        (staging / name).chmod(0o1777)
    (staging / "var/root").chmod(0o700)
    (staging / "cores").chmod(0o700)
    configs = {
        "hosts": "127.0.0.1 localhost blauos\n::1 localhost\n",
        "resolv.conf": "nameserver " + (args.network[3] if args.network else "10.0.2.3") + "\n",
        "passwd": "root:*:0:0:root:/var/root:/bin/sh\nnobody:*:65534:65534:nobody:/var/empty:/bin/false\n",
        "master.passwd": "root:*:0:0::0:0:root:/var/root:/bin/sh\nnobody:*:65534:65534::0:0:nobody:/var/empty:/bin/false\n",
        "group": "wheel:*:0:root\nnobody:*:65534:\n",
        "shells": "/bin/sh\n",
        "ttys": "/dev/console /bin/sh -l\n",
        "profile": "export PATH=/bin:/usr/bin:/sbin:/usr/sbin\nexport TERM=xterm\n",
        "fstab": "# Root is selected by the XNU loader and filesystem driver.\n",
    }
    for name, text in configs.items():
        path = staging / "etc" / name
        if not path.exists() or (args.network and name == "resolv.conf"):
            path.write_text(text)
            path.chmod(0o644)
    fixture_names = ("org.blauos.test.bootstrap.plist", "org.blauos.test.exec-failure.plist", "org.blauos.test.shutdown.plist", "launchd-test.sh")
    for name in fixture_names:
        shutil.copyfile(ROOT / "runtime/fixtures" / name, staging / "etc/blauos-tests" / name)
    smoke_plist = plistlib.loads((ROOT / "vm/org.blauos.smoke.plist").read_bytes())
    if args.network:
        smoke_plist["ProgramArguments"] += ["--network", *args.network]
    if args.standalone:
        smoke_plist["ProgramArguments"].append("--standalone")
    (staging / "System/Library/LaunchDaemons/org.blauos.smoke.plist").write_bytes(plistlib.dumps(smoke_plist))
    account_receipt = args.output / "account.json"
    if args.ssh_user:
        account_receipt.write_text(json.dumps(ssh_configuration(staging, args), indent=2) + "\n")
    image = args.output / "blauos-arm64.img"
    esp = args.output / "esp.img"
    root_image = args.output / "root.ext4"
    image_bytes = (args.root_mb + args.esp_mb + 2) * 1024 * 1024
    with image.open("wb") as stream:
        stream.truncate(image_bytes)
    with esp.open("wb") as stream:
        stream.truncate(args.esp_mb * 1024 * 1024)
    source_id = digest(args.kernel) + digest(args.loader) + digest(args.provenance)
    disk_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "blauos-disk:" + source_id))
    root_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "blauos-root:" + source_id))
    esp_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "blauos-esp:" + source_id))
    with (args.output / "assemble.log").open("w") as log:
        run(["sgdisk", "-U", disk_uuid, "-n", f"1:2048:+{args.esp_mb}M", "-t", "1:EF00", "-u", "1:" + esp_uuid,
             "-c", "1:EFI System Partition", "-n", f"2:0:+{args.root_mb}M", "-t", "2:8300", "-u", "2:" + root_uuid,
             "-c", "2:Darwin ext4 Root", str(image)], log)
        partitions = json.loads(subprocess.check_output(["sfdisk", "--json", str(image)], text=True))["partitiontable"]["partitions"]
        run(["mkfs.fat", "-F", "32", "-n", "BLAUOS_EFI", str(esp)], log)
        run(["mmd", "-i", str(esp), "::/EFI", "::/EFI/BOOT"], log)
        run(["mcopy", "-i", str(esp), str(args.loader), "::/EFI/BOOT/BOOTAA64.EFI"], log)
        run(["mcopy", "-i", str(esp), str(args.kernel), "::/EFI/BOOT/kernel"], log)
        boot_args = args.output / "boot-args.txt"
        boot_args.write_text(args.boot_args)
        run(["mcopy", "-i", str(esp), str(boot_args), "::/EFI/BOOT/boot-args.txt"], log)
        # fakeroot simulates uid/gid changes only in the private staging copy.
        script = 'set -e; chown -R 0:0 "$1"; '
        if args.ssh_user:
            script += '"$5" "$6" apply-metadata --root "$1" --receipt "$7"; '
        script += 'exec mke2fs -q -F -t ext4 -b 4096 -O ^orphan_file -U "$2" -L blauos-root -d "$1" "$3" "$4"'
        run(["fakeroot", "sh", "-c", script, "blauos-format-root", str(staging), root_uuid,
             str(root_image), str(args.root_mb * 256), sys.executable,
             str(ROOT / "scripts/vm/accounts.py"), str(account_receipt)], log)
        if args.ssh_user:
            receipt = json.loads(account_receipt.read_text())
            verify_account_inodes(root_image, receipt)
            account_receipt.write_text(json.dumps(receipt, indent=2) + "\n")
        with image.open("r+b") as destination:
            for partition, contents in zip(partitions, (esp, root_image), strict=True):
                if contents.stat().st_size > partition["size"] * 512:
                    raise ValueError("Filesystem exceeds its partition")
                destination.seek(partition["start"] * 512)
                with contents.open("rb") as source:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
    result = {"schema_version": 1, "assembled": True, "guest_boot_passed": False,
              "image": str(image), "image_sha256": digest(image), "kernel_sha256": digest(args.kernel),
              "loader_sha256": digest(args.loader), "runtime_provenance_sha256": digest(args.provenance),
              "disk_uuid": disk_uuid, "root_uuid": root_uuid, "device_nodes": "Kernel mounts devfs; no Linux device-number substitution",
              "fixture_sha256": {name: digest(staging / "etc/blauos-tests" / name) for name in fixture_names},
              "bit_identical_rebuild_verified": False}
    if args.ssh_user:
        result["account"] = json.loads(account_receipt.read_text())
        result["ssh_login_verified"] = False
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--loader", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root-mb", type=int, default=512)
    parser.add_argument("--esp-mb", type=int, default=64)
    parser.add_argument("--boot-args", default="-v debug=0x218 serial=3 keepsyms=1 no_interrupt_masked_debug=1 kextlog=0xfffffff")
    parser.add_argument("--network", nargs=4, metavar=("ADDRESS", "NETMASK", "GATEWAY", "DNS"),
                        help="Configure the guest probe and resolver before daemon startup")
    parser.add_argument("--standalone", action="store_true",
                        help="Run the demonstration health service without an acceptance challenge")
    parser.add_argument("--ssh-user", help="Provision an initial non-root SSH account")
    parser.add_argument("--ssh-public-key", type=Path, help="Ed25519 public keys; never a private key")
    parser.add_argument("--ssh-uid", type=int, default=1000)
    parser.add_argument("--ssh-gid", type=int, default=1000)
    args = parser.parse_args()
    for name in ("root", "provenance", "kernel", "loader", "output"):
        setattr(args, name, getattr(args, name).resolve())
    if args.root_mb < 64 or args.esp_mb < 32:
        parser.error("Root must be at least 64 MB and ESP at least 32 MB")
    if args.standalone and not args.network:
        parser.error("--standalone requires an explicit --network")
    if bool(args.ssh_user) != bool(args.ssh_public_key):
        parser.error("--ssh-user and --ssh-public-key must be supplied together")
    if args.network:
        try:
            for value in args.network:
                ipaddress.IPv4Address(value)
            ipaddress.IPv4Network((args.network[0], args.network[1]), strict=False)
        except ValueError as error:
            parser.error(str(error))
    try:
        print(json.dumps(assemble(args), indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
