#!/usr/bin/env python3
"""Compare local distributions with GitHub Release and PyPI by SHA-256."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


def _request(url: str, *, token: str | None = None) -> bytes:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "codex-notify-release-check",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _retry_json(url: str, attempts: int = 12) -> dict[str, object]:
    for attempt in range(attempts):
        try:
            return json.loads(_request(url))
        except urllib.error.HTTPError as exc:
            if exc.code not in {404, 429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise
        except urllib.error.URLError:
            if attempt == attempts - 1:
                raise
        time.sleep(5)
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, help="owner/repository")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--dist", required=True, type=Path)
    arguments = parser.parse_args()
    version = arguments.tag.removeprefix("v")
    local = {
        path.name: _sha256(path.read_bytes())
        for path in sorted(arguments.dist.iterdir())
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    }
    if len(local) != 2:
        raise ValueError(
            f"dist 必须恰好包含一个 wheel 和一个 sdist，实际为：{sorted(local)}"
        )

    github = json.loads(
        _request(
            f"https://api.github.com/repos/{arguments.repository}/releases/tags/{arguments.tag}",
            token=os.environ.get("GH_TOKEN"),
        )
    )
    github_assets = {
        asset["name"]: asset
        for asset in github["assets"]
        if asset["name"].endswith((".whl", ".tar.gz"))
    }
    pypi = _retry_json(f"https://pypi.org/pypi/codex-notify/{version}/json")
    pypi_hashes = {
        item["filename"]: item["digests"]["sha256"] for item in pypi["urls"]
    }
    if set(github_assets) != set(local):
        raise ValueError(
            f"GitHub Release 文件集合不一致：{sorted(github_assets)} != {sorted(local)}"
        )
    if set(pypi_hashes) != set(local):
        raise ValueError(f"PyPI 文件集合不一致：{sorted(pypi_hashes)} != {sorted(local)}")

    for filename, expected in local.items():
        asset = github_assets.get(filename)
        if asset is None:
            raise ValueError(f"GitHub Release 缺少发行文件：{filename}")
        github_digest = asset.get("digest")
        if github_digest != f"sha256:{expected}":
            raise ValueError(
                f"GitHub Release 哈希不一致：{filename}: {github_digest!r} != sha256:{expected}"
            )
        if pypi_hashes.get(filename) != expected:
            raise ValueError(f"PyPI 哈希不一致：{filename}")
    print(json.dumps(local, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
