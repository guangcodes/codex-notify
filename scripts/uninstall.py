#!/usr/bin/env python3
"""Remove codex-notify hooks and runtime."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if sys.version_info < (3, 11):
    raise SystemExit("codex-notify requires Python 3.11 or newer")

from codex_notify.installer import Installer  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--purge", action="store_true", help="同时删除本地队列和日志")
    args = parser.parse_args()
    Installer(ROOT / "src" / "codex_notify").uninstall(purge=args.purge)
    print("已卸载。" if args.purge else "已卸载；本地队列和日志仍保留。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
