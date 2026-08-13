#!/usr/bin/env python3
"""Fail when a wheel or sdist contains local/runtime-only artifacts."""

from __future__ import annotations

import argparse
import re
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


FORBIDDEN_PARTS = {
    ".DS_Store",
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "notifications.sqlite3",
    "install-state.json",
    "install-state.pending.json",
}
FORBIDDEN_SUFFIXES = {".log", ".pyc", ".pyo", ".swp"}
PRIVATE_TEXT_PATTERNS = {
    "macOS 用户绝对路径": re.compile(rb"/Users/[A-Za-z0-9._-]+/"),
    "Codex 私有技能或记忆路径": re.compile(rb"\.codex/(?:skills|memories)/"),
    "本机名称": re.compile(rb"[A-Za-z0-9._-]+sMacBook", re.IGNORECASE),
}
SDIST_ALLOWED_TOP_LEVEL = {
    "LICENSE",
    "PKG-INFO",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
    "setup.cfg",
    "src",
    "tests",
}


def _safe_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"归档包含不安全路径：{name}")
    return path


def _verify_entry(name: str, content: bytes | None) -> None:
    path = _safe_path(name)
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        raise ValueError(f"归档包含不应发布的路径：{name}")
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        raise ValueError(f"归档包含不应发布的文件：{name}")
    if content is None:
        return
    for label, pattern in PRIVATE_TEXT_PATTERNS.items():
        if pattern.search(content):
            raise ValueError(f"归档文件 {name} 包含{label}")


def _verify_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            entry = _safe_path(info.filename)
            root = entry.parts[0] if entry.parts else ""
            if root != "codex_notify" and not (
                root.startswith("codex_notify-") and root.endswith(".dist-info")
            ):
                raise ValueError(f"wheel 包含白名单外路径：{info.filename}")
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                raise ValueError(f"wheel 包含符号链接：{info.filename}")
            _verify_entry(info.filename, None if info.is_dir() else archive.read(info))


def _verify_sdist(path: Path) -> None:
    expected_root = path.name.removesuffix(".tar.gz")
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            entry = _safe_path(member.name)
            if not entry.parts or entry.parts[0] != expected_root:
                raise ValueError(f"sdist 包含错误的根目录：{member.name}")
            if len(entry.parts) > 1 and entry.parts[1] not in SDIST_ALLOWED_TOP_LEVEL:
                raise ValueError(f"sdist 包含白名单外路径：{member.name}")
            if not member.isfile() and not member.isdir():
                raise ValueError(f"sdist 包含不支持的归档类型：{member.name}")
            content = None
            if member.isfile():
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError(f"无法读取归档文件：{member.name}")
                content = extracted.read()
            _verify_entry(member.name, content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archives", nargs="+", type=Path)
    arguments = parser.parse_args()
    saw_wheel = False
    saw_sdist = False
    for archive in arguments.archives:
        if archive.suffix == ".whl":
            saw_wheel = True
            _verify_wheel(archive)
        elif archive.name.endswith(".tar.gz"):
            saw_sdist = True
            _verify_sdist(archive)
        else:
            raise ValueError(f"不支持的发行归档：{archive}")
    if not saw_wheel or not saw_sdist:
        raise ValueError("必须同时检查 wheel 和 sdist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
