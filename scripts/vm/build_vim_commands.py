#!/usr/bin/env python3
"""Build pinned vim (tiny feature set) for the independent ARM64 guest.

Vim is pinned as a hashed release tarball in sources.lock.json (archives).
The builder verifies the archive hash, extracts it into the fresh output
directory, applies the tracked vm/commands/vim amendments, and compiles with
explicit clang commands against the supplied independent runtime. Vim's
autoconf configure is not used: it probes and links the host runtime. The
tracked vm/commands/vim/config.h replaces auto/config.h for a tiny feature
build with Vim's own termlib.c and builtin termcap entries, because the
runtime provides libSystem only. No host executables or libraries are copied
into the guest root; the executable links only /usr/lib/libSystem.B.dylib.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile

REPO = Path(__file__).resolve().parents[2]
ARCHIVE_NAME = "vim-9.2.1175"
PATCH = "vm/commands/vim/termlib-tputs.patch"
COMPAT = ("vm/commands/vim/config.h", "vm/commands/vim/osdef.h")
COMPAT_SOURCE = "vm/commands/vim/libc_builtins.c"
BUILD_INPUTS = ("scripts/vm/build_vim_commands.py", "scripts/blauos.py",
                "runtime/compat/sdk27_compile.h", PATCH, *COMPAT, COMPAT_SOURCE)
# Upstream src/Makefile OBJ_COMMON + OBJ_MAIN, with the feature variables
# (GUI, external language interfaces, netbeans, channel, xdiff, wayland)
# empty in this profile, plus termlib.c: the runtime has no termcap library,
# so Vim's own termlib provides tgetent and friends (TERM_OBJ).
VIM_SOURCES = (
    "alloc", "arabic", "arglist", "autocmd", "beval", "buffer", "change",
    "blob", "blowfish", "cindent", "clientserver", "clipboard", "cmdexpand",
    "cmdhist", "crypt", "crypt_zip", "debugger", "dict", "diff", "digraph",
    "drawline", "drawscreen", "edit", "eval", "evalbuffer", "evalfunc",
    "evalvars", "evalwindow", "ex_cmds", "ex_cmds2", "ex_docmd", "ex_eval",
    "ex_getln", "fileio", "filepath", "findfile", "float", "fold", "fuzzy",
    "getchar", "gc", "gui_xim", "hardcopy", "hardcopy_pango",
    "hardcopy_postscript", "hashtab", "help", "highlight", "if_cscope",
    "if_xcmdsrv", "image", "image_kitty", "image_sixel", "indent",
    "insexpand", "linematch", "list", "locale", "logfile", "map", "mark",
    "match", "mbyte", "memline", "menu", "misc1", "misc2", "mouse", "move",
    "normal", "ops", "option", "optionstr", "os_unix", "popupmenu",
    "popupwin", "profiler", "pty", "quickfix", "regexp", "register",
    "screen", "scriptfile", "search", "session", "sha256", "sign",
    "socketserver", "sound", "spell", "spellfile", "spellsuggest", "strings",
    "syntax", "tabpanel", "tag", "term", "terminal", "testing", "textformat",
    "textobject", "textprop", "time", "tuple", "typval", "ui", "undo",
    "usercmd", "userfunc", "version", "vim9class", "vim9cmds", "vim9compile",
    "vim9execute", "vim9expr", "vim9generics", "vim9instr", "vim9script",
    "vim9type", "viminfo", "window", "bufwrite", "termlib", "charset",
    "json", "main", "memfile", "message",
)
RUNTIME_INPUTS = ("usr/lib/libSystem.B.dylib", "usr/lib/system/libdyld.dylib", "usr/lib/dyld")
FEATURE_CONFIG = ("FEAT_TINY; no GUI/X11; no eval; no external language interfaces; "
                  "Vim termlib.c with builtin termcap entries (ansi, vt320, vt52, xterm, "
                  "dumb); no terminfo/termcap/curses/iconv/gettext libraries")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def source_verifier():
    spec = importlib.util.spec_from_file_location("blauos_vim_source_verify", REPO / "scripts/blauos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_pin(manifest: Path, name: str = ARCHIVE_NAME) -> dict:
    """Return the locked archive entry for vim, with required fields checked."""
    data = json.loads(manifest.read_text())
    pin = next((entry for entry in data.get("archives", []) if entry.get("name") == name), None)
    if pin is None:
        raise ValueError(f"{name}: not pinned in sources.lock.json archives")
    for field in ("filename", "url", "sha256", "license", "role"):
        if not pin.get(field):
            raise ValueError(f"{name}: lock entry requires {field}")
    return pin


def verify_archive(pin: dict, path: Path) -> str:
    """Hash the tarball and compare against the immutable lock; never modify it."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Archive must be a plain fetched file: {path}")
    if path.name != pin["filename"]:
        raise ValueError(f"Archive filename differs from lock: {path.name} != {pin['filename']}")
    actual = digest(path)
    if actual != pin["sha256"]:
        raise ValueError(f"{pin['name']}: archive hash differs from lock; preserved")
    return actual


def extract_archive(archive: Path, destination: Path) -> Path:
    """Extract only regular files and directories inside destination."""
    destination.mkdir()
    with tarfile.open(archive) as tarball:
        members = tarball.getmembers()
        for member in members:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unsafe archive member: {member.name}")
            if not (member.isdir() or member.isreg()):
                raise ValueError(f"Unsupported archive member type: {member.name}")
        tarball.extractall(destination, filter="data")
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError("Archive does not contain exactly one top-level directory")
    return roots[0]


def pathdef_source(cflags: str, lflags: str) -> str:
    """Deterministic replacement for the Makefile-generated auto/pathdef.c."""
    return ("\n".join([
        "/* pathdef.c generated by scripts/vm/build_vim_commands.py. */",
        '#include "vim.h"',
        'char_u *default_vim_dir = (char_u *)"/usr/share/vim";',
        'char_u *default_vimruntime_dir = (char_u *)"/usr/share/vim/vim92";',
        'char_u *all_cflags = (char_u *)' + json.dumps(cflags) + ';',
        'char_u *all_lflags = (char_u *)' + json.dumps(lflags) + ';',
        'char_u *compiled_user = (char_u *)"blauos-builder";',
        'char_u *compiled_sys = (char_u *)"blauos-build-host";',
    ]) + "\n")


def build(args: argparse.Namespace) -> dict:
    if platform.system() != "Darwin":
        raise ValueError("this compiler profile requires a macOS build host")
    # Reject even dangling symlinks without writing anything into them.
    if args.output.exists() or args.output.is_symlink():
        raise ValueError(f"output already exists; use a fresh directory: {args.output}")
    archive, runtime = args.archive.absolute(), args.runtime_root.resolve()
    target_output = args.output.resolve()
    for protected in (archive.parent.resolve(), runtime):
        if target_output == protected or protected in target_output.parents:
            raise ValueError(f"output must be outside input trees: {protected}")
    manifest = REPO / "sources.lock.json"
    pin = archive_pin(manifest)
    archive_sha256 = verify_archive(pin, archive)
    verifier = source_verifier()
    git_environment = verifier.clean_git_environment()
    for relative in RUNTIME_INPUTS:
        if not (runtime / relative).is_file():
            raise ValueError("Missing independent runtime link input: " + relative)
    runtime_inputs = {name: digest(runtime / name) for name in RUNTIME_INPUTS}
    sdk = Path(subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip())
    clang = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    bridge = REPO / "runtime/compat/sdk27_compile.h"
    amendments = [{"path": name, "sha256": digest(REPO / name)} for name in BUILD_INPUTS]
    args.output.mkdir(parents=True)
    args.created_output = True
    output = args.output.resolve()
    source = extract_archive(archive, output / "vim-source")
    subprocess.run(["git", "init", "-q", str(source)], check=True, capture_output=True, env=git_environment)
    patch = REPO / PATCH
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=source, check=True, capture_output=True,
                   env=git_environment)
    subprocess.run(["git", "apply", str(patch)], cwd=source, check=True, capture_output=True, env=git_environment)
    auto = source / "src/auto"
    auto.mkdir(exist_ok=True)
    shutil.copy2(REPO / "vm/commands/vim/config.h", auto / "config.h")
    shutil.copy2(REPO / "vm/commands/vim/osdef.h", auto / "osdef.h")
    src = source / "src"
    compile_flags = ["-std=gnu17", "-O2", "-Wall", "-Wextra", "-Wno-unused",
                     "-Wno-nullability-completeness", "-include", str(bridge),
                     "-arch", "arm64", "-isysroot", str(sdk), "-mmacosx-version-min=11.0",
                     "-fno-builtin", "-fno-stack-protector", "-DHAVE_CONFIG_H",
                     "-I", str(src), "-I", str(src / "proto")]
    link_flags = ["-arch", "arm64", "-isysroot", str(sdk), "-mmacosx-version-min=11.0",
                  "-Z", "-nostdlib", "-L" + str(runtime / "usr/lib"),
                  "-Wl,-dylib_file,/usr/lib/system/libdyld.dylib:" + str(runtime / "usr/lib/system/libdyld.dylib"),
                  "-Wl,-platform_version,macos,11.0,11.5", "-Wl,-fixup_chains", "-lSystem",
                  "-Wl,-dylinker_install_name,/usr/lib/dyld"]
    (auto / "pathdef.c").write_text(pathdef_source(" ".join([clang, *compile_flags]),
                                                  " ".join([clang, *link_flags])))
    objects = output / "objects"
    objects.mkdir()
    root = output / "root"
    destination = root / "usr/bin/vim"
    destination.parent.mkdir(parents=True)
    commands = []
    with (output / "build.log").open("w") as log:
        for name in (*VIM_SOURCES, "pathdef", "libc_builtins"):
            input_path = (auto / "pathdef.c" if name == "pathdef" else
                          REPO / COMPAT_SOURCE if name == "libc_builtins" else src / (name + ".c"))
            command = [clang, *compile_flags, "-c", str(input_path), "-o", str(objects / (name + ".o"))]
            commands.append(command)
            log.write(json.dumps(command) + "\n")
            log.flush()
            process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            if process.returncode:
                raise ValueError(f"compile failed for {name}.c; see build.log")
        command = [clang, *link_flags, *map(str, sorted(objects.glob("*.o"))), "-o", str(destination)]
        commands.append(command)
        log.write(json.dumps(command) + "\n")
        log.flush()
        process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        if process.returncode:
            raise ValueError("link failed for /usr/bin/vim; see build.log")
    destination.chmod(0o755)
    dependencies = subprocess.check_output(["xcrun", "otool", "-L", str(destination)], text=True)
    (output / "vim-linked-dylibs.txt").write_text(dependencies)
    linked = [line.strip().split(" (", 1)[0] for line in dependencies.splitlines()[1:] if line.strip()]
    if linked != ["/usr/lib/libSystem.B.dylib"]:
        raise ValueError("Unexpected runtime dependency for /usr/bin/vim: " + repr(linked))
    upstream = [{"path": f"src/{name}.c", "sha256": digest(src / (name + ".c"))} for name in VIM_SOURCES]
    entry = {"path": "/usr/bin/vim", "sha256": digest(destination), "source": ARCHIVE_NAME,
             "revision": archive_sha256, "archive_sha256": archive_sha256,
             "upstream_revision": pin.get("upstream_revision"), "version": pin.get("version"),
             "source_path": "src/main.c", "source_file_sha256": digest(src / "main.c"),
             "upstream_source_files": upstream, "build_amendments": amendments,
             "linked_dylibs": linked, "feature_config": FEATURE_CONFIG,
             "generated_files": ["src/auto/config.h", "src/auto/osdef.h", "src/auto/pathdef.c"],
             "compat_source_files": [COMPAT_SOURCE]}
    changed_inputs = [name for name, sha in runtime_inputs.items() if digest(runtime / name) != sha]
    changed_amendments = [item["path"] for item in amendments if digest(REPO / item["path"]) != item["sha256"]]
    if changed_inputs or changed_amendments:
        raise ValueError("build inputs changed during compilation: "
                         + ", ".join(changed_inputs + changed_amendments))
    verify_archive(pin, archive)
    result = {"schema_version": 1, "built": True, "artifacts": [entry], "errors": [],
              "source_name": ARCHIVE_NAME, "archive_sha256": archive_sha256,
              "upstream_revision": pin.get("upstream_revision"), "version": pin.get("version"),
              "source_lock_sha256": digest(manifest), "sdk": str(sdk),
              "sdk_settings_sha256": digest(sdk / "SDKSettings.json"),
              "compiler": clang, "compiler_sha256": digest(Path(clang)),
              "compiler_version": subprocess.check_output([clang, "--version"], text=True).splitlines()[0],
              "runtime_root": str(runtime), "runtime_link_inputs": runtime_inputs,
              "stock_runtime_used": False, "guest_execution_verified": False}
    write(output / "provenance.json", result)
    write(output / "commands.json", commands)
    license_text = (source / "runtime/doc/uganda.txt").read_text()
    (output / "third-party-notices.txt").write_text(
        f"{ARCHIVE_NAME}: Vim license (charityware), runtime/doc/uganda.txt\n" + license_text)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path,
                        default=REPO / ".blauos/inputs" / (ARCHIVE_NAME + ".tar.gz"))
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.created_output = False
    try:
        result = build(args)
        print(json.dumps({"built": result["built"], "output": str(args.output.resolve()),
                          "commands": [entry["path"] for entry in result["artifacts"]],
                          "errors": result["errors"]}, indent=2))
        return 0 if result["built"] else 1
    except (OSError, ValueError, RuntimeError, KeyError, StopIteration, subprocess.SubprocessError) as error:
        if args.created_output:
            write(args.output / "failure.json", {"built": False, "error": str(error)})
        print(f"vim command build: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
