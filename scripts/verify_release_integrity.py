#!/usr/bin/env python3
"""Compare local distributions with GitHub Release and PyPI by SHA-256."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DIST_SUFFIXES = (".whl", ".tar.gz")


def _request(
    url: str,
    *,
    token: str | None = None,
    data: bytes | None = None,
    content_type: str | None = None,
) -> bytes:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "codex-notify-release-check",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, headers=headers, data=data)
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


def _distribution_files(dist: Path) -> dict[str, Path]:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(
            "dist 必须恰好包含一个 wheel 和一个 sdist，"
            f"实际为：{sorted(path.name for path in [*wheels, *sdists])}"
        )
    return {path.name: path for path in [*wheels, *sdists]}


def _distribution_hashes(files: dict[str, Path]) -> dict[str, str]:
    return {name: _sha256(path.read_bytes()) for name, path in sorted(files.items())}


def _release_url(repository: str, tag: str) -> str:
    encoded_tag = urllib.parse.quote(tag, safe="")
    return f"https://api.github.com/repos/{repository}/releases/tags/{encoded_tag}"


def _distribution_assets(release: dict[str, object]) -> dict[str, dict[str, object]]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ValueError("GitHub Release 响应缺少 assets")
    return {
        asset["name"]: asset
        for asset in assets
        if isinstance(asset, dict)
        and isinstance(asset.get("name"), str)
        and asset["name"].endswith(DIST_SUFFIXES)
    }


def _verify_github_assets(
    release: dict[str, object], local: dict[str, str]
) -> dict[str, dict[str, object]]:
    github_assets = _distribution_assets(release)
    if set(github_assets) != set(local):
        raise ValueError(
            f"GitHub Release 文件集合不一致：{sorted(github_assets)} != {sorted(local)}"
        )
    for filename, expected in local.items():
        github_digest = github_assets[filename].get("digest")
        if github_digest != f"sha256:{expected}":
            raise ValueError(
                f"GitHub Release 哈希不一致：{filename}: "
                f"{github_digest!r} != sha256:{expected}"
            )
    return github_assets


def _ensure_github_assets(
    repository: str,
    tag: str,
    files: dict[str, Path],
    *,
    token: str,
) -> dict[str, str]:
    local = _distribution_hashes(files)
    release_url = _release_url(repository, tag)
    release = json.loads(_request(release_url, token=token))
    existing = _distribution_assets(release)
    unexpected = set(existing) - set(local)
    if unexpected:
        raise ValueError(f"GitHub Release 包含意外发行文件：{sorted(unexpected)}")

    missing: list[Path] = []
    for filename, expected in local.items():
        asset = existing.get(filename)
        if asset is None:
            missing.append(files[filename])
            continue
        github_digest = asset.get("digest")
        if github_digest != f"sha256:{expected}":
            raise ValueError(
                f"拒绝覆盖 GitHub Release 资产：{filename}: "
                f"{github_digest!r} != sha256:{expected}"
            )

    if not missing:
        return local

    upload_template = release.get("upload_url")
    if not isinstance(upload_template, str):
        raise ValueError("GitHub Release 响应缺少 upload_url")
    upload_url = upload_template.split("{", 1)[0]
    for path in missing:
        query = urllib.parse.urlencode({"name": path.name})
        _request(
            f"{upload_url}?{query}",
            token=token,
            data=path.read_bytes(),
            content_type="application/octet-stream",
        )

    refreshed = json.loads(_request(release_url, token=token))
    _verify_github_assets(refreshed, local)
    return local


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, help="owner/repository")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument(
        "--ensure-github-assets",
        action="store_true",
        help="幂等上传缺失资产；同名资产摘要不一致时失败关闭",
    )
    arguments = parser.parse_args()
    version = arguments.tag.removeprefix("v")
    files = _distribution_files(arguments.dist)
    token = os.environ.get("GH_TOKEN")
    if arguments.ensure_github_assets:
        if not token:
            raise ValueError("幂等上传 GitHub Release 资产需要 GH_TOKEN")
        local = _ensure_github_assets(
            arguments.repository,
            arguments.tag,
            files,
            token=token,
        )
        print(json.dumps(local, indent=2, sort_keys=True))
        return 0
    local = _distribution_hashes(files)

    github = json.loads(
        _request(
            _release_url(arguments.repository, arguments.tag),
            token=token,
        )
    )
    _verify_github_assets(github, local)
    pypi = _retry_json(f"https://pypi.org/pypi/codex-notify/{version}/json")
    pypi_hashes = {
        item["filename"]: item["digests"]["sha256"] for item in pypi["urls"]
    }
    if set(pypi_hashes) != set(local):
        raise ValueError(f"PyPI 文件集合不一致：{sorted(pypi_hashes)} != {sorted(local)}")

    for filename, expected in local.items():
        if pypi_hashes.get(filename) != expected:
            raise ValueError(f"PyPI 哈希不一致：{filename}")
    print(json.dumps(local, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
