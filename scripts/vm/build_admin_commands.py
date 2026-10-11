#!/usr/bin/env python3
"""Build pinned BSD identity, environment, date and process tools for ARM64."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

from build_userspace import digest, flags

ROOT = Path(__file__).resolve().parents[2]
SHELL_PROGRAMS = {
    "usr/bin/id": ("id/id.c",),
    "usr/bin/env": ("env/env.c", "env/envopts.c"),
    "bin/date": ("date/date.c", "date/vary.c", "date/netdate.c"),
}
PS_FILES = tuple("src/Userspace/ps/" + name for name in
                 ("ps.c", "fmt.c", "keyword.c", "nlist.c", "print.c", "tasks.c"))
GROUP_BRIDGE = "vm/commands/admin/getgrouplist_2.c"
DATE_PATCH = "vm/commands/admin/date-optional-utmpx.patch"
DATE_BRIDGE = "vm/commands/admin/optional_utmpx.c"
BUILD_INPUTS = ("scripts/vm/build_admin_commands.py", "scripts/vm/build_userspace.py",
                "scripts/blauos.py", "runtime/compat/sdk27_compile.h")
COMPAT_HEADER = "src/Libraries/libSystem/libc/gen/get_compat.h"


def source_revision(path, name, pins):
    spec = importlib.util.spec_from_file_location("blauos_admin_source_verify", ROOT / "scripts/blauos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify_source(pins[name], path)["revision"]


def source_input(path, relative, source, revision):
    return {"source": source, "revision": revision, "path": relative,
            "sha256": digest(path / relative)}


def build(args):
    if sys.platform != "darwin":
        raise ValueError("The native ARM64 compiler profile requires macOS")
    if args.output.exists():
        raise ValueError("Use a fresh output directory; preserve previous evidence")
    pins = {entry["name"]: entry for entry in json.loads((ROOT / "sources.lock.json").read_text())["sources"]}
    pd_revision = source_revision(args.source, "puredarwin-vm-next", pins)
    shell_revision = source_revision(args.shell_source, "apple-shell-cmds", pins)
    runtime_inputs = ("usr/lib/libSystem.B.dylib", "usr/lib/system/libdyld.dylib")
    for relative in runtime_inputs:
        if not (args.runtime_root / relative).is_file():
            raise ValueError("Missing independent runtime link input: " + relative)
    runtime_hashes = {name: digest(args.runtime_root / name) for name in runtime_inputs}
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    clang = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    args.output.mkdir(parents=True)
    staged = args.output / "root"
    overlay = args.output / "source-overlay"
    (overlay / "date").mkdir(parents=True)
    shutil.copy2(args.shell_source / "date/date.c", overlay / "date/date.c")
    subprocess.run(["git", "init", "-q", str(overlay)], check=True)
    for options in (["--check"], []):
        subprocess.run(["git", "-C", str(overlay), "apply", *options, str(ROOT / DATE_PATCH)], check=True)
    recipes = []
    for installed, sources in SHELL_PROGRAMS.items():
        recipe = {"installed": installed, "source": "apple-shell-cmds", "revision": shell_revision,
                  "source_root": args.shell_source, "sources": sources, "extra_sources": [],
                  "headers": ("env/envopts.h",) if installed.endswith("env") else
                             ("date/extern.h", "date/vary.h") if installed.endswith("date") else (),
                  "amendments": list(BUILD_INPUTS), "additional_sources": []}
        if installed.endswith("id"):
            recipe["extra_sources"] = [ROOT / GROUP_BRIDGE]
            recipe["amendments"].append(GROUP_BRIDGE)
        if installed.endswith("date"):
            recipe["amendments"] += [DATE_PATCH, DATE_BRIDGE]
            recipe["extra_sources"] = [ROOT / DATE_BRIDGE]
            recipe["additional_sources"] = [source_input(args.source, COMPAT_HEADER, "puredarwin-vm-next", pd_revision)]
        recipes.append(recipe)
    recipes.append({"installed": "bin/ps", "source": "puredarwin-vm-next", "revision": pd_revision,
                    "source_root": args.source, "sources": PS_FILES, "extra_sources": [],
                    "headers": ("src/Userspace/ps/ps.h", "src/Userspace/ps/extern.h", COMPAT_HEADER),
                    "amendments": list(BUILD_INPUTS), "additional_sources": []})
    commands = []
    artifacts = []
    with (args.output / "build.log").open("w") as log:
        for recipe in recipes:
            installed = recipe["installed"]
            destination = staged / installed
            destination.parent.mkdir(parents=True, exist_ok=True)
            selected = [recipe["source_root"] / name for name in recipe["sources"]]
            if installed == "bin/date":
                selected[0] = overlay / "date/date.c"
            command = [clang, "-std=gnu17", "-O2", "-Wall", "-Wextra", "-Wno-nullability-completeness", "-Z",
                       "-include", str(ROOT / "runtime/compat/sdk27_compile.h"),
                       "-I" + str(args.source / "src/Libraries/libSystem/libc/gen")]
            if installed == "bin/date":
                command.append("-I" + str(args.shell_source / "date"))
            command += list(map(str, selected + recipe["extra_sources"]))
            command += ["-o", str(destination), *flags(args.runtime_root, sdk)]
            commands.append(command)
            log.write("$ " + repr(command) + "\n")
            log.flush()
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
            dependencies = subprocess.check_output(["xcrun", "otool", "-L", str(destination)], text=True)
            (args.output / (destination.name + "-linked-dylibs.txt")).write_text(dependencies)
            linked = [line.strip().split(" (", 1)[0] for line in dependencies.splitlines()[1:] if line.strip()]
            if linked != ["/usr/lib/libSystem.B.dylib"]:
                raise ValueError("Unexpected runtime dependency for " + installed + ": " + repr(linked))
            upstream = [source_input(recipe["source_root"], name, recipe["source"], recipe["revision"])
                        for name in (*recipe["sources"], *recipe["headers"])]
            amendments = [{"path": name, "sha256": digest(ROOT / name)} for name in recipe["amendments"]]
            entry = {"path": "/" + installed, "sha256": digest(destination),
                     "source": recipe["source"], "revision": recipe["revision"],
                     "source_path": recipe["sources"][0], "source_file_sha256": digest(selected[0]),
                     "source_inputs": upstream + recipe["additional_sources"],
                     "build_amendments": amendments, "linked_dylibs": linked}
            if installed == "bin/date":
                entry["upstream_source_file_sha256"] = digest(args.shell_source / "date/date.c")
                entry["optional_runtime_services"] = ["pututxline legacy clock-change accounting"]
                entry["additional_sources"] = [{"source": "puredarwin-vm-next", "revision": pd_revision}]
            artifacts.append(entry)
    for name in ("whoami", "groups"):
        destination = staged / "usr/bin" / name
        shutil.copy2(staged / "usr/bin/id", destination)
        artifacts.append({**artifacts[0], "path": "/usr/bin/" + name,
                          "sha256": digest(destination), "executable_name_behavior": name})
    if any(digest(args.runtime_root / name) != runtime_hashes[name] for name in runtime_inputs):
        raise ValueError("Input runtime changed while commands were compiled")
    source_revision(args.source, "puredarwin-vm-next", pins)
    source_revision(args.shell_source, "apple-shell-cmds", pins)
    result = {"schema_version": 1, "built": True, "artifacts": artifacts,
              "runtime_link_inputs": runtime_hashes, "stock_runtime_used": False,
              "guest_execution_verified": False}
    (args.output / "provenance.json").write_text(json.dumps(result, indent=2) + "\n")
    inputs = {"schema_version": 1, "commands": commands, "source_lock_sha256": digest(ROOT / "sources.lock.json"),
              "compiler_version": subprocess.check_output([clang, "--version"], text=True).splitlines()[0],
              "compiler_sha256": digest(Path(clang)), "sdk": sdk,
              "sdk_settings_sha256": digest(Path(sdk) / "SDKSettings.json"),
              "source_revisions": {"puredarwin-vm-next": pd_revision, "apple-shell-cmds": shell_revision},
              "runtime_link_inputs": runtime_hashes, "stock_runtime_used": False}
    (args.output / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", "--shell-source", dest="shell_source", type=Path,
                        default=ROOT / ".blauos/sources/apple-shell-cmds")
    parser.add_argument("--darwin-source", dest="source", type=Path,
                        default=ROOT / ".blauos/sources/puredarwin-vm-next")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for name in ("source", "shell_source", "runtime_root", "output"):
        path = getattr(args, name)
        setattr(args, name, path.absolute() if name in ("source", "shell_source") else path.resolve())
    output_preexisting = args.output.exists()
    try:
        result = build(args)
        print(json.dumps({"built": True, "output": str(args.output),
                          "commands": [entry["path"] for entry in result["artifacts"]]}, indent=2))
        return 0
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if not output_preexisting and args.output.is_dir():
            (args.output / "failure.json").write_text(json.dumps({"built": False, "error": str(error)}, indent=2) + "\n")
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
