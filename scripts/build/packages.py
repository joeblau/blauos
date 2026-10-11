#!/usr/bin/env python3
"""Validate pinned server-image package recipes and compare build receipts.

Package recipes live in packages/*.json (schema_version 1). Each recipe names
the source-built component, its pinned sources, license metadata, declared
dependencies, the repo builder that produces it, and the tracked patches that
are explicit build inputs. Validation never fetches or compiles; it proves the
recipe pins agree with sources.lock.json, toolchain.lock.json, and
manifests/puredarwin-flake.lock, and records the result as a JSON receipt.

Recipe fields:
  schema_version   integer, exactly 1
  name             lowercase identifier; must equal the recipe filename stem
  summary          one-line description
  license          {"spdx": <expression string or null>, "notes": <string>,
                    "determined_from": <string>}; SPDX is recorded only where
                    determinable from the locked license notices, otherwise
                    null with notes citing the component notices
  sources          nonempty list of {"kind": "git"|"archive"|"workspace", ...}
                    entries: git/archive pins must match sources.lock.json
                    exactly (git sources that also appear in
                    manifests/puredarwin-flake.lock must match the locked
                    flake revision too); workspace sources are repo-native
                    files recorded as {"kind": "workspace", "lock_name",
                    "path", "sha256"} and validated against the current
                    checkout by content hash
  depends          list of other recipe names; must be acyclic
  builder          {"script": repo-relative builder path, "command": argv,
                    "patches": repo-relative tracked patch paths}
  outputs          nonempty list of relative install paths
  isolation        build-isolation statement
  rebuild          {"clean_rebuild_verified": bool, "note": str}

Subcommands:
  validate   check every recipe against the lock files and write a receipt
  compare    clean-rebuild comparison: hash-compare the artifact sets of two
             build receipts (xnu.py result.json, openssh.py/build_commands.py
             provenance.json shape) and exit nonzero on any difference
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

ROOT = Path(__file__).resolve().parents[2]

SPDX_LICENSE_IDS = frozenset({
    "APSL-2.0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC",
    "Unicode-DFS-2016", "GPL-2.0-or-later", "MIT",
})
SPDX_EXCEPTION_IDS = frozenset({"LLVM-exception"})

NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
GIT_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")


class RecipeError(ValueError):
    """A recipe or lock file failed validation."""


def canonical_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_spdx(expression: str) -> None:
    """Parse a restricted SPDX expression; raise RecipeError on any deviation."""
    tokens = re.findall(r"\(|\)|[A-Za-z0-9.:+-]+", expression)
    if not tokens or "".join(tokens).replace("(", "").replace(")", "") == "":
        raise RecipeError(f"Empty SPDX expression: {expression!r}")
    position = 0

    def peek():
        return tokens[position] if position < len(tokens) else None

    def take():
        nonlocal position
        token = peek()
        position += 1
        return token

    def term():
        token = take()
        if token == "(":
            expression_inner()
            if take() != ")":
                raise RecipeError(f"Unbalanced parenthesis in SPDX expression: {expression!r}")
            return
        if token not in SPDX_LICENSE_IDS:
            raise RecipeError(f"Unknown SPDX license identifier {token!r} in {expression!r}")
        if peek() == "WITH":
            take()
            exception = take()
            if exception not in SPDX_EXCEPTION_IDS:
                raise RecipeError(f"Unknown SPDX exception {exception!r} in {expression!r}")

    def expression_inner():
        term()
        while peek() in ("AND", "OR"):
            take()
            term()

    expression_inner()
    if position != len(tokens):
        raise RecipeError(f"Trailing tokens in SPDX expression: {expression!r}")


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RecipeError(f"{path}: {error}")
    if not isinstance(value, dict):
        raise RecipeError(f"{path}: top-level JSON object required")
    return value


def load_recipes(directory: Path) -> dict[str, dict]:
    if not directory.is_dir():
        raise RecipeError(f"Recipe directory missing: {directory}")
    recipes = {}
    for path in sorted(directory.glob("*.json")):
        recipe = load_json(path)
        name = recipe.get("name")
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            raise RecipeError(f"{path}: name must be a lowercase identifier")
        if path.stem != name:
            raise RecipeError(f"{path}: filename must match recipe name {name!r}")
        if name in recipes:
            raise RecipeError(f"{path}: duplicate recipe name {name!r}")
        recipes[name] = recipe
    if not recipes:
        raise RecipeError(f"No recipes found in {directory}")
    return recipes


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RecipeError(message)


def validate_license(name: str, license_field) -> None:
    require(isinstance(license_field, dict), f"{name}: license metadata object required")
    spdx = license_field.get("spdx")
    notes = license_field.get("notes")
    determined_from = license_field.get("determined_from")
    require(isinstance(notes, str) and notes.strip(), f"{name}: license.notes required")
    require(isinstance(determined_from, str) and determined_from.strip(),
            f"{name}: license.determined_from citation required")
    if spdx is None:
        require("license" in notes.lower() or "notice" in notes.lower() or "mixed" in notes.lower(),
                f"{name}: null SPDX requires notes citing the component license notices")
    else:
        require(isinstance(spdx, str), f"{name}: license.spdx must be a string or null")
        parse_spdx(spdx)


def validate_source(name: str, source: dict, lock: dict, flake_nodes: dict,
                    root: Path = ROOT) -> list[str]:
    """Return the validation checks performed for one pinned source."""
    checks = []
    lock_name = source.get("lock_name")
    require(isinstance(lock_name, str) and lock_name, f"{name}: source lock_name required")
    kind = source.get("kind")
    if kind == "workspace":
        relative = source.get("path")
        path = PurePosixPath(relative) if isinstance(relative, str) else None
        require(path is not None and not path.is_absolute() and ".." not in path.parts,
                f"{name}: workspace source path must be a relative path without '..'")
        target = (root / relative).resolve()
        require(target.is_relative_to(root.resolve()) and target.is_file(),
                f"{name}: workspace source {relative!r} does not exist in the repository")
        require(source.get("sha256") == file_sha256(target),
                f"{name}: workspace source {relative!r} sha256 does not match the checkout")
        require(SHA256_PATTERN.fullmatch(source["sha256"]),
                f"{name}: {lock_name} sha256 must be 64 lowercase hexadecimal characters")
        checks.append(f"{lock_name}.workspace-sha256")
        return checks
    if kind == "git":
        pin = next((item for item in lock.get("sources", []) if item["name"] == lock_name), None)
        require(pin is not None, f"{name}: {lock_name} is not pinned in sources.lock.json")
        for field in ("url", "revision", "tree", "tag"):
            value = source.get(field)
            pinned = pin.get(field)
            if pinned is None:
                require(value is None, f"{name}: {lock_name} recipe sets {field} absent from sources.lock.json")
            else:
                require(value == pinned,
                        f"{name}: {lock_name} {field} {value!r} does not match sources.lock.json {pinned!r}")
            checks.append(f"{lock_name}.{field}")
        require(GIT_REVISION_PATTERN.fullmatch(source["revision"]),
                f"{name}: {lock_name} revision must be a full lowercase commit hash")
        flake = flake_nodes.get(lock_name)
        if flake is not None:
            locked = flake.get("locked", {})
            require(locked.get("rev") == source["revision"],
                    f"{name}: {lock_name} revision does not match manifests/puredarwin-flake.lock "
                    f"({locked.get('rev')})")
            checks.append(f"{lock_name}.flake-rev")
    elif kind == "archive":
        pin = next((item for item in lock.get("archives", []) if item["name"] == lock_name), None)
        require(pin is not None, f"{name}: {lock_name} is not pinned in sources.lock.json archives")
        for field in ("url", "sha256", "filename"):
            require(source.get(field) == pin.get(field),
                    f"{name}: {lock_name} {field} does not match sources.lock.json archives")
            checks.append(f"{lock_name}.{field}")
        require(SHA256_PATTERN.fullmatch(source["sha256"]),
                f"{name}: {lock_name} sha256 must be 64 lowercase hexadecimal characters")
    else:
        raise RecipeError(f"{name}: source kind must be 'git', 'archive', or 'workspace', found {kind!r}")
    return checks


def validate_builder(name: str, builder, root: Path) -> list[str]:
    checks = []
    require(isinstance(builder, dict), f"{name}: builder object required")
    script = builder.get("script")
    require(isinstance(script, str) and script, f"{name}: builder.script required")
    script_path = (root / script).resolve()
    require(script_path.is_relative_to(root.resolve()) and script_path.is_file(),
            f"{name}: builder script {script!r} does not exist in the repository")
    checks.append(f"builder.script:{script}")
    command = builder.get("command")
    require(isinstance(command, list) and len(command) >= 2 and
            all(isinstance(part, str) for part in command),
            f"{name}: builder.command must be a nonempty argv list")
    require(command[0] == "python3" and command[1] == script,
            f"{name}: builder.command must invoke its builder script")
    checks.append("builder.command")
    patches = builder.get("patches")
    require(isinstance(patches, list), f"{name}: builder.patches list required")
    for patch in patches:
        patch_path = (root / patch).resolve()
        require(isinstance(patch, str) and patch_path.is_relative_to(root.resolve()) and
                patch_path.is_file(),
                f"{name}: tracked patch {patch!r} does not exist in the repository")
        checks.append(f"builder.patch:{patch}")
    return checks


def dependency_order(recipes: dict[str, dict]) -> list[str]:
    order, state = [], {}

    def visit(name: str, stack: tuple[str, ...]) -> None:
        current = state.get(name)
        if current == "done":
            return
        if current == "visiting":
            raise RecipeError(f"Dependency cycle: {' -> '.join(stack + (name,))}")
        state[name] = "visiting"
        for dependency in recipes[name].get("depends", []):
            if dependency not in recipes:
                raise RecipeError(f"{name}: unknown dependency {dependency!r}")
            visit(dependency, stack + (name,))
        state[name] = "done"
        order.append(name)

    for name in sorted(recipes):
        visit(name, ())
    return order


def validate_recipes(recipes_dir: Path, manifest: Path, flake_lock: Path,
                     toolchain_lock: Path, root: Path = ROOT) -> dict:
    """Validate every recipe against the lock files; return the receipt data."""
    lock = load_json(manifest)
    toolchain = load_json(toolchain_lock)
    flake = load_json(flake_lock)
    flake_nodes = flake.get("nodes", {})
    recipes = load_recipes(recipes_dir)
    order = dependency_order(recipes)
    entries = []
    for name in order:
        recipe = recipes[name]
        require(recipe.get("schema_version") == 1, f"{name}: schema_version 1 required")
        require(isinstance(recipe.get("summary"), str) and recipe["summary"].strip(),
                f"{name}: summary required")
        validate_license(name, recipe.get("license"))
        sources = recipe.get("sources")
        require(isinstance(sources, list) and sources, f"{name}: sources must be a nonempty list")
        checks = []
        lock_names = set()
        for source in sources:
            require(isinstance(source, dict), f"{name}: source entries must be objects")
            require(source.get("lock_name") not in lock_names,
                    f"{name}: duplicate source {source.get('lock_name')!r}")
            lock_names.add(source.get("lock_name"))
            checks.extend(validate_source(name, source, lock, flake_nodes, root))
        checks.extend(validate_builder(name, recipe.get("builder"), root))
        outputs = recipe.get("outputs")
        require(isinstance(outputs, list) and outputs, f"{name}: outputs must be a nonempty list")
        for output in outputs:
            path = PurePosixPath(output) if isinstance(output, str) else None
            require(path is not None and not path.is_absolute() and ".." not in path.parts,
                    f"{name}: output {output!r} must be a relative path without '..'")
        checks.append(f"outputs:{len(outputs)}")
        isolation = recipe.get("isolation")
        require(isinstance(isolation, str) and isolation.strip(),
                f"{name}: isolation statement required")
        rebuild = recipe.get("rebuild")
        require(isinstance(rebuild, dict) and isinstance(rebuild.get("clean_rebuild_verified"), bool) and
                isinstance(rebuild.get("note"), str) and rebuild["note"].strip(),
                f"{name}: rebuild.clean_rebuild_verified (bool) and rebuild.note required")
        entries.append({
            "name": name,
            "recipe_sha256": file_sha256(recipes_dir / f"{name}.json"),
            "license_spdx": recipe["license"]["spdx"],
            "sources": sorted(lock_names),
            "depends": recipe.get("depends", []),
            "checks": checks,
        })
    transitive = []
    for item in lock.get("transitive_locks", []):
        path = root / item["path"]
        actual = file_sha256(path) if path.is_file() else None
        require(actual == item["sha256"],
                f"transitive lock {item['path']} sha256 {actual} does not match sources.lock.json")
        transitive.append({"path": item["path"], "sha256": actual})
    require(toolchain.get("schema_version") == 1 and isinstance(toolchain.get("tools"), dict),
            "toolchain.lock.json schema_version 1 with recorded tools required")
    return {
        "schema_version": 1,
        "result": "pass",
        "recipes_dir": str(recipes_dir),
        "manifest_sha256": file_sha256(manifest),
        "toolchain_lock_sha256": file_sha256(toolchain_lock),
        "flake_lock_sha256": file_sha256(flake_lock),
        "transitive_locks": transitive,
        "build_order": order,
        "recipes": entries,
        "rebuild_verified": False,
        "limits": "Validation compares recipe pins and license metadata against the lock "
                  "files only. It does not fetch, compile, or clean-rebuild any source; "
                  "artifact-level rebuild verification is recorded per recipe under "
                  "rebuild.clean_rebuild_verified and remains open for every recipe.",
    }


def receipt_artifacts(receipt: dict, receipt_path: str) -> dict[str, str]:
    """Map artifact basename -> sha256 from a build receipt (result/provenance JSON)."""
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list):
        raise RecipeError(f"{receipt_path}: receipt has no artifacts list")
    result = {}
    for entry in artifacts:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or \
                not isinstance(entry.get("sha256"), str):
            raise RecipeError(f"{receipt_path}: artifact entries require path and sha256")
        if not SHA256_PATTERN.fullmatch(entry["sha256"]):
            raise RecipeError(f"{receipt_path}: artifact {entry['path']!r} sha256 malformed")
        # Rebuilds run under different roots; the basename is the stable identity.
        basename = PurePosixPath(entry["path"]).name
        if basename in result and result[basename] != entry["sha256"]:
            raise RecipeError(f"{receipt_path}: conflicting hashes for artifact {basename!r}")
        result[basename] = entry["sha256"]
    if not result:
        raise RecipeError(f"{receipt_path}: receipt artifact list is empty")
    return result


def compare_receipts(first: dict, second: dict) -> dict:
    """Hash-compare the artifact sets of two build receipts (clean-rebuild check)."""
    first_artifacts = receipt_artifacts(first, "first receipt")
    second_artifacts = receipt_artifacts(second, "second receipt")
    only_first = sorted(set(first_artifacts) - set(second_artifacts))
    only_second = sorted(set(second_artifacts) - set(first_artifacts))
    mismatched = sorted(name for name in set(first_artifacts) & set(second_artifacts)
                        if first_artifacts[name] != second_artifacts[name])
    report = {
        "schema_version": 1,
        "match": not only_first and not only_second and not mismatched,
        "artifact_count_first": len(first_artifacts),
        "artifact_count_second": len(second_artifacts),
        "only_first": only_first,
        "only_second": only_second,
        "hash_mismatch": mismatched,
        "compared": sorted(set(first_artifacts) & set(second_artifacts)),
        "artifact_set_sha256_first": canonical_digest(first_artifacts),
        "artifact_set_sha256_second": canonical_digest(second_artifacts),
    }
    for key, receipt in (("recipe_sha256_first", first), ("recipe_sha256_second", second)):
        value = receipt.get("recipe_sha256")
        if isinstance(value, str) and SHA256_PATTERN.fullmatch(value):
            report[key] = value
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subcommands = parser.add_subparsers(dest="action", required=True)
    validate = subcommands.add_parser("validate", help="validate recipes against the lock files")
    validate.add_argument("--recipes", type=Path, default=ROOT / "packages")
    validate.add_argument("--manifest", type=Path, default=ROOT / "sources.lock.json")
    validate.add_argument("--flake-lock", type=Path, default=ROOT / "manifests/puredarwin-flake.lock")
    validate.add_argument("--toolchain-lock", type=Path, default=ROOT / "toolchain.lock.json")
    validate.add_argument("--output", type=Path,
                          help="write the validation receipt JSON here")
    compare = subcommands.add_parser("compare", help="compare two build receipts for a clean rebuild")
    compare.add_argument("first", type=Path)
    compare.add_argument("second", type=Path)
    compare.add_argument("--output", type=Path,
                         help="write the comparison report JSON here")
    args = parser.parse_args()
    if args.action == "validate":
        receipt = validate_recipes(args.recipes, args.manifest, args.flake_lock, args.toolchain_lock)
        receipt["command"] = ["python3", "scripts/build/packages.py", "validate",
                              "--recipes", str(args.recipes)]
        text = json.dumps(receipt, indent=2) + "\n"
        if args.output:
            if args.output.exists():
                raise RecipeError(f"Refusing to overwrite existing receipt: {args.output}")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text)
        print(f"validate: pass; {len(receipt['recipes'])} recipes, "
              f"build order {' -> '.join(receipt['build_order'])}")
        return 0
    first = load_json(args.first)
    second = load_json(args.second)
    report = compare_receipts(first, second)
    report["first_receipt"] = str(args.first)
    report["second_receipt"] = str(args.second)
    report["command"] = ["python3", "scripts/build/packages.py", "compare",
                         str(args.first), str(args.second)]
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        if args.output.exists():
            raise RecipeError(f"Refusing to overwrite existing report: {args.output}")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    if report["match"]:
        print(f"compare: match; {report['artifact_count_first']} artifacts identical")
        return 0
    print(f"compare: mismatch; only_first={report['only_first']} "
          f"only_second={report['only_second']} hash_mismatch={report['hash_mismatch']}",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RecipeError as error:
        print(f"package validation blocked: {error}", file=sys.stderr)
        sys.exit(1)
