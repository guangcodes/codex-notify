"""Filesystem layout with a test-friendly root override."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @classmethod
    def default(cls) -> "AppPaths":
        configured = os.environ.get("CODEX_NOTIFY_HOME")
        root = Path(configured).expanduser() if configured else Path.home() / ".codex" / "codex-notify"
        return cls(root=root)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"

    @property
    def database(self) -> Path:
        return self.data_dir / "notifications.sqlite3"

    @property
    def send_lock(self) -> Path:
        return self.data_dir / "send.lock"

    @property
    def error_log(self) -> Path:
        return self.log_dir / "errors.log"

    @property
    def worker_stdout(self) -> Path:
        return self.log_dir / "worker.stdout.log"

    @property
    def worker_stderr(self) -> Path:
        return self.log_dir / "worker.stderr.log"

    @property
    def runner(self) -> Path:
        return self.root / "runner.py"

    @property
    def library_dir(self) -> Path:
        return self.root / "lib"

    @property
    def install_state(self) -> Path:
        return self.root / "install-state.json"

    @property
    def pending_install_state(self) -> Path:
        return self.root / "install-state.pending.json"

    @property
    def launch_agent(self) -> Path:
        return (
            Path.home()
            / "Library"
            / "LaunchAgents"
            / "io.github.guangcodes.codex-notify.plist"
        )

    @property
    def legacy_launch_agent(self) -> Path:
        return (
            Path.home()
            / "Library"
            / "LaunchAgents"
            / "com.guang.codex-turn-notifier.plist"
        )

    @property
    def cli_path(self) -> Path:
        return Path.home() / ".local" / "bin" / "codex-notify"

    @property
    def hooks_file(self) -> Path:
        return Path.home() / ".codex" / "hooks.json"

    @property
    def config_file(self) -> Path:
        return Path.home() / ".codex" / "config.toml"

    def ensure_runtime_dirs(self) -> None:
        for directory in (self.root, self.data_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
