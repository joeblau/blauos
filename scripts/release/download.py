#!/usr/bin/env python3
"""Resumable, verified, disk-bounded BlauOS release download and caching.

Fetches a signed release manifest and its artifacts over HTTP(S) with:

- manifest signature verification against an operator trust root
  (scripts/release/signing.py) before any artifact is trusted;
- resumable transfers: partial downloads live in NAME.part files with a JSON
  state record, and resume uses HTTP Range requests from the recorded offset;
  a server that ignores or mismatches the range restarts from scratch;
- per-file SHA-256 verification against the signed manifest; a verified file
  is atomically renamed into place, so complete and incomplete files are
  always distinguishable and a process restart never confuses them;
- a disk-bounded cache with LRU eviction of previously verified artifacts;
- timeouts and retry with exponential backoff.

This tool only stages verified bytes into a cache directory. It never touches
active system assets, never authorizes a boot, and never runs as an updater.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import http.client
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from release import (ReleaseError, file_sha256, hex64, load_json, load_json_file,  # noqa: E402
                     receipt, require, utcnow, write_json)
import signing  # noqa: E402

MAX_METADATA_BYTES = 32 * 1024 * 1024
CHUNK = 1024 * 256


class DownloadError(ReleaseError):
    """Base class for fetch/cache failures."""


class OfflineError(DownloadError):
    """The host could not reach the release server at all."""


class IntegrityError(DownloadError):
    """Downloaded bytes failed hash or signature verification."""


class DiskBoundError(DownloadError):
    """The cache bound could not be satisfied even after eviction."""


class TruncatedError(DownloadError):
    """The connection ended before the artifact was complete; state was kept."""


def request_bytes(url: str, timeout: float, offset: int = 0) -> tuple[bytes, int, bool]:
    """GET url (optionally from offset). Returns (body, status, ranged)."""
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read(MAX_METADATA_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code == 416 and offset:
            raise DownloadError(f"range beyond end of {url}; restarting transfer")
        raise DownloadError(f"HTTP {error.code} for {url}") from error
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        raise OfflineError(f"cannot reach {url}: {error}") from error
    if offset:
        require(status == 206, f"server ignored Range for {url}; partial state is unsafe to extend")
        content_range = response.headers.get("Content-Range", "")
        require(content_range.startswith(f"bytes {offset}-"),
                f"server returned a mismatched Content-Range for {url}")
    else:
        require(status == 200, f"unexpected HTTP {status} for {url}")
    require(len(body) <= MAX_METADATA_BYTES, f"{url}: metadata exceeds size bound")
    return body, status, status == 206


def stream_artifact(url: str, offset: int, timeout: float):
    """Open a streaming GET. Returns (response, ranged)."""
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        raise DownloadError(f"HTTP {error.code} for {url}") from error
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        raise OfflineError(f"cannot reach {url}: {error}") from error
    if offset:
        if response.status != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
            response.close()
            raise DownloadError(f"server did not honor resume offset {offset} for {url}")
    elif response.status != 200:
        response.close()
        raise DownloadError(f"unexpected HTTP {response.status} for {url}")
    return response, offset > 0


def with_retries(operation, retries: int, backoff: float, retryable=(DownloadError,),
                 fatal=()):
    attempt = 0
    while True:
        try:
            return operation()
        except fatal:
            raise
        except retryable:
            attempt += 1
            if attempt > retries:
                raise
            time.sleep(backoff * (2 ** (attempt - 1)))


class Cache:
    """Disk-bounded cache of verified artifacts plus in-progress transfers."""

    def __init__(self, root: Path, max_bytes: int):
        require(max_bytes > 0, "cache bound must be positive")
        self.root = root
        self.max_bytes = max_bytes
        self.artifacts = root / "artifacts"
        self.partial = root / "partial"
        self.index_path = root / "cache-index.json"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.partial.mkdir(parents=True, exist_ok=True)
        self.index = self._load_index()

    def _load_index(self) -> dict:
        if self.index_path.is_file():
            index = load_json_file(self.index_path)
            require(isinstance(index.get("entries"), dict), "cache index: entries object required")
            return index
        return {"schema_version": 1, "entries": {}}

    def _save_index(self) -> None:
        write_json(self.index_path, self.index)

    def usage(self) -> int:
        total = 0
        for directory in (self.artifacts, self.partial):
            for path in directory.rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
        return total

    def evict_until(self, needed: int, keep: set[str]) -> None:
        """Evict least-recently-used verified artifacts until `needed` bytes fit."""
        while self.usage() + needed > self.max_bytes:
            candidates = [(entry.get("last_access", ""), name)
                          for name, entry in self.index["entries"].items()
                          if name not in keep and (self.artifacts / name).is_file()]
            if not candidates:
                raise DiskBoundError(
                    f"cache bound {self.max_bytes} bytes cannot fit {needed} more bytes; "
                    "no evictable verified artifacts remain")
            _, name = min(candidates)
            (self.artifacts / name).unlink()
            del self.index["entries"][name]
            self._save_index()

    def record_verified(self, name: str, sha256: str, size: int) -> None:
        self.index["entries"][name] = {"sha256": sha256, "size": size, "last_access": utcnow()}
        self._save_index()

    def touch(self, name: str) -> None:
        if name in self.index["entries"]:
            self.index["entries"][name]["last_access"] = utcnow()
            self._save_index()

    def verified_path(self, name: str, sha256: str) -> Path | None:
        target = self.artifacts / name
        entry = self.index["entries"].get(name)
        if target.is_file() and entry and entry.get("sha256") == sha256:
            if file_sha256(target) == sha256:
                self.touch(name)
                return target
            target.unlink()
            del self.index["entries"][name]
            self._save_index()
        return None


def _safe_name(name: str) -> str:
    require(isinstance(name, str) and name and not Path(name).is_absolute()
            and ".." not in Path(name).parts, f"unsafe artifact name: {name!r}")
    return name


def fetch_artifact(cache: Cache, base_url: str, artifact: dict,
                   timeout: float, retries: int, backoff: float) -> dict:
    """Fetch one artifact with resume, verification, and cache bounding."""
    name = _safe_name(artifact.get("path"))
    expected = hex64(artifact.get("sha256"), f"artifact {name} sha256")
    size = artifact.get("size")
    require(type(size) is int and size >= 0, f"artifact {name}: size required")
    url = f"{base_url.rstrip('/')}/{name}"
    existing = cache.verified_path(name, expected)
    if existing is not None:
        return {"path": name, "status": "cached", "sha256": expected, "bytes_fetched": 0}
    part = cache.partial / (name.replace("/", "__") + ".part")
    state_path = cache.partial / (name.replace("/", "__") + ".state.json")
    offset = 0
    if part.is_file() and state_path.is_file():
        state = load_json_file(state_path)
        if state.get("url") == url and state.get("sha256") == expected \
                and state.get("received") == part.stat().st_size:
            offset = part.stat().st_size
        else:
            part.unlink()
            state_path.unlink()
    cache.evict_until(size - offset, keep={name})
    digest = hashlib.sha256()
    received = offset
    resumed_from = offset
    if offset:
        with part.open("rb") as stream:
            for chunk in iter(lambda: stream.read(CHUNK), b""):
                digest.update(chunk)
        if received == size:
            # A previous run finished the transfer but stopped before rename.
            if digest.hexdigest() == expected:
                target = cache.artifacts / name
                target.parent.mkdir(parents=True, exist_ok=True)
                part.replace(target)
                state_path.unlink(missing_ok=True)
                cache.record_verified(name, expected, size)
                return {"path": name, "status": "completed-from-partial",
                        "sha256": expected, "bytes_fetched": 0, "resumed_from": offset}
            part.unlink()
            state_path.unlink(missing_ok=True)
            received = 0
            digest = hashlib.sha256()

    def transfer():
        nonlocal received, digest, resumed_from
        try:
            response, _ = stream_artifact(url, received if received else 0, timeout)
        except DownloadError:
            if not received:
                raise
            # A server that ignores or mismatches the range cannot extend the
            # partial file; discard it and restart from offset zero.
            received = 0
            resumed_from = 0
            digest = hashlib.sha256()
            part.unlink(missing_ok=True)
            response, _ = stream_artifact(url, 0, timeout)
        mode = "ab" if received else "wb"
        try:
            with response, part.open(mode) as output:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    try:
                        output.write(chunk)
                    except OSError as error:
                        if error.errno == errno.ENOSPC:
                            raise DiskBoundError(f"disk full while writing {part}") from error
                        raise
                    digest.update(chunk)
                    received += len(chunk)
                    write_json(state_path, {"url": url, "sha256": expected,
                                            "received": received, "updated_at": utcnow()})
        except (http.client.HTTPException, ConnectionError) as error:
            raise OfflineError(f"connection dropped during {url}: {error}") from error
        if received != size:
            raise TruncatedError(f"artifact {name}: truncated transfer "
                                 f"({received} of {size} bytes); retained {part} for resume")

    with_retries(transfer, retries, backoff, fatal=(DiskBoundError,))
    if digest.hexdigest() != expected:
        part.unlink()
        state_path.unlink(missing_ok=True)
        raise IntegrityError(f"artifact {name}: SHA-256 mismatch; corrupted bytes discarded")
    target = cache.artifacts / name
    target.parent.mkdir(parents=True, exist_ok=True)
    part.replace(target)
    state_path.unlink(missing_ok=True)
    cache.record_verified(name, expected, size)
    return {"path": name, "status": "fetched" if resumed_from == 0 else "resumed",
            "sha256": expected, "bytes_fetched": received - resumed_from,
            "resumed_from": resumed_from}


def fetch_release(base_url: str, cache: Cache, trust_root: Path, manifest_name: str,
                  timeout: float = 30.0, retries: int = 3, backoff: float = 0.5) -> dict:
    """Fetch and verify a signed release manifest, then every listed artifact."""
    _safe_name(manifest_name)
    manifest_url = f"{base_url.rstrip('/')}/{manifest_name}"
    manifest_bytes, _, _ = with_retries(
        lambda: request_bytes(manifest_url, timeout), retries, backoff)
    signature_bytes, _, _ = with_retries(
        lambda: request_bytes(manifest_url + ".sig.json", timeout), retries, backoff)
    manifest = load_json(manifest_bytes)
    require(manifest.get("schema_version") == 1 and manifest.get("manifest_kind") == "blauos-release",
            "fetched metadata is not a blauos release manifest")
    # Verify the fetched bytes through the same code path as offline files.
    transient = cache.root / "metadata"
    manifest_file = transient / manifest_name
    signature_file = transient / (manifest_name + ".sig.json")
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    manifest_file.write_bytes(manifest_bytes)
    signature_file.write_bytes(signature_bytes)
    verification = signing.verify_manifest(trust_root, manifest_file, signature_file)
    artifacts = manifest.get("artifacts")
    require(isinstance(artifacts, list), "manifest: artifacts array required")
    results = [fetch_artifact(cache, base_url, artifact, timeout, retries, backoff)
               for artifact in artifacts]
    return {"schema_version": 1, "release_id": manifest.get("release_id"),
            "manifest_sha256": verification["manifest_sha256"],
            "signature_verified": True, "key_id": verification["key_id"],
            "artifacts": results, "cache_usage_bytes": cache.usage(),
            "cache_bound_bytes": cache.max_bytes,
            "boot_authorization_performed": False,
            "active_system_assets_touched": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    fetch = parser.add_subparsers(dest="command", required=True).add_parser(
        "fetch", help="Fetch a signed release into a bounded cache")
    fetch.add_argument("--base-url", required=True)
    fetch.add_argument("--manifest-name", default="release-manifest.json")
    fetch.add_argument("--trust-root", type=Path, required=True)
    fetch.add_argument("--cache", type=Path, required=True)
    fetch.add_argument("--max-cache-bytes", type=int, default=512 * 1024 * 1024)
    fetch.add_argument("--timeout", type=float, default=30.0)
    fetch.add_argument("--retries", type=int, default=3)
    fetch.add_argument("--backoff", type=float, default=0.5)
    fetch.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    require(args.timeout > 0 and args.retries >= 0 and args.backoff >= 0,
            "timeout/retries/backoff out of range")
    cache = Cache(args.cache, args.max_cache_bytes)
    result = fetch_release(args.base_url, cache, args.trust_root, args.manifest_name,
                           args.timeout, args.retries, args.backoff)
    if args.receipt:
        receipt(args.receipt, sys.argv,
                {"base_url": args.base_url, "trust_root_sha256": file_sha256(args.trust_root)},
                {"cache": str(args.cache)}, result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ReleaseError, ValueError, KeyError, TypeError) as error:
        print(f"Release download blocked: {error}", file=sys.stderr)
        sys.exit(1)
