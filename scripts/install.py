#!/usr/bin/env python3
"""Install codex-notify for the current macOS user."""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if sys.version_info < (3, 11):
    raise SystemExit("codex-notify requires Python 3.11 or newer")

from codex_notify.installer import Installer  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-start", action="store_true", help="只写配置，不启动 LaunchAgent")
    args = parser.parse_args()
    installer = Installer(ROOT / "src" / "codex_notify")
    installer.install(start_agent=not args.no_start)
    configure_command = f"{shlex.quote(str(installer.paths.runner))} configure"
    print(
        "安装完成：已通过 Computer Use --previous-notify 接入完成通知。"
        f"下一步：{configure_command}，然后在 Codex /hooks 中信任新 Hook。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
