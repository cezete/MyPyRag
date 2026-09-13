"""Standalone ingestion worker entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

from mypyrag.cli import main as cli_main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mypyrag-worker")
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help="Resolve .env and configured directories here (default: cwd)",
    )
    args = parser.parse_args(argv)
    return cli_main(["--base-dir", str(args.base_dir), "watch"])


if __name__ == "__main__":
    raise SystemExit(main())
