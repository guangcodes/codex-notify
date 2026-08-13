"""Minimal local diagnostics without leaking hook payloads."""

from __future__ import annotations

from datetime import datetime

from .paths import AppPaths
from .redact import safe_summary


def append_error(message: object, paths: AppPaths | None = None) -> None:
    paths = paths or AppPaths.default()
    paths.ensure_runtime_dirs()
    line = f"{datetime.now().astimezone().isoformat()} {safe_summary(str(message), 500)}\n"
    with paths.error_log.open("a", encoding="utf-8") as handle:
        handle.write(line)
    paths.error_log.chmod(0o600)
