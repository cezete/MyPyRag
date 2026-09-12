"""Small CLI; status never imports the heavy Docling runtime."""

import argparse
import logging
import time
from collections import Counter
from pathlib import Path

from mypyrag.config import Config
from mypyrag.converter import DoclingAdapter
from mypyrag.pipeline import Pipeline
from mypyrag.storage import load_manifest

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mypyrag")
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help="Resolve .env and configured directories here (default: cwd)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("watch")
    process = commands.add_parser("process")
    process.add_argument("file", type=Path)
    commands.add_parser("status")
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.base_dir)
        logging.basicConfig(
            level=config.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
        if args.command == "status":
            counts: Counter[str] = Counter()
            invalid = 0
            for root in (config.in_dir, config.done_dir, config.error_dir):
                if not root.exists():
                    continue
                for directory in sorted(root.iterdir()):
                    if directory.is_symlink() or not directory.is_dir():
                        continue
                    if not (directory / "manifest.json").exists():
                        if (directory / "receipt.json").exists():
                            print(f"PENDING_RECEIPT {directory}")
                            invalid += 1
                        continue
                    try:
                        manifest = load_manifest(directory)
                        counts[manifest.current_state] += 1
                        print(
                            f"{manifest.current_state:12} {manifest.document_id[:12]} "
                            f"{manifest.original_filename} last={manifest.last_successful_state} "
                            f"chunks={manifest.chunk_count}"
                            + (f" error={manifest.last_error}" if manifest.last_error else "")
                            + f" [{directory}]"
                        )
                    except Exception:
                        log.exception("Invalid manifest: %s", directory)
                        invalid += 1
            print(
                f"Total: {sum(counts.values())}; invalid/pending: {invalid}; "
                + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
            )
            return 1 if invalid else 0
        pipeline = Pipeline(config, DoclingAdapter())
        if args.command == "process":
            return 0 if pipeline.process(args.file) else 1
        while True:
            pipeline.cycle()
            time.sleep(config.poll_interval_seconds)
    except KeyboardInterrupt:
        return 130
    except Exception:
        log.exception("MyPyRag failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
