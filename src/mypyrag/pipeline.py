"""One restartable state-driven ingestion application."""

import json
import logging
import uuid
from pathlib import Path

from filelock import FileLock

from mypyrag.chunker import ChunkArtifactWriter, DoclingHybridChunker, StructuredChunker
from mypyrag.config import Config
from mypyrag.converter import FORMATS, Converter
from mypyrag.model import Manifest, State, now, reached
from mypyrag.storage import (
    StabilityTracker,
    atomic_json,
    load_manifest,
    safe_name,
    save_manifest,
    sha256,
    signature,
    source_path,
    sync_directory,
)

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self, config: Config, converter: Converter, chunker: StructuredChunker | None = None
    ) -> None:
        self.config = config
        self.converter = converter
        self.chunker = chunker or DoclingHybridChunker(config)
        self.chunk_writer = ChunkArtifactWriter()
        self.stability = StabilityTracker(config.file_stable_seconds)
        for root in self.roots:
            root.mkdir(parents=True, exist_ok=True)
        if len({root.stat().st_dev for root in self.roots}) != 1:
            raise ValueError("IN, DONE and ERROR must be on the same filesystem for atomic moves")
        self.lock = FileLock(config.in_dir / ".mypyrag.lock", timeout=0)

    @property
    def roots(self) -> tuple[Path, Path, Path]:
        return self.config.in_dir, self.config.done_dir, self.config.error_dir

    def manifests(self) -> list[tuple[Path, Manifest]]:
        """Strict inventory: invalid manifests prevent unsafe duplicate registration."""
        result = []
        for root in self.roots:
            for directory in sorted(root.iterdir()):
                if directory.is_dir() and not directory.is_symlink():
                    path = directory / "manifest.json"
                    if path.exists():
                        result.append((directory, load_manifest(directory)))
        return result

    def _recover_receipts(self) -> bool:
        ok = True
        for directory in sorted(self.config.in_dir.iterdir()):
            journal = directory / "receipt.json"
            if not directory.is_dir() or directory.is_symlink() or not journal.exists():
                continue
            try:
                with journal.open(encoding="utf-8") as stream:
                    receipt = json.load(stream)
                manifest = Manifest.from_dict(receipt["manifest"])
                if (directory / "manifest.json").exists():
                    journal.unlink()
                    continue
                source = directory / "source" / manifest.original_filename
                if source.parent.resolve() != (directory / "source").resolve():
                    raise ValueError("Unsafe receipt filename")
                raw = Path(receipt["raw_path"])
                if not source.exists():
                    if raw.is_symlink() or not raw.is_file() or sha256(raw) != manifest.document_id:
                        raise ValueError("Interrupted receipt: original input missing or changed")
                    raw.rename(source)
                    sync_directory(raw.parent)
                    sync_directory(source.parent)
                if source.is_symlink() or sha256(source) != manifest.document_id:
                    raise ValueError("Interrupted receipt: source hash mismatch")
                save_manifest(directory, manifest)
                journal.unlink()
                log.info("Recovered receipt document_id=%s", manifest.document_id)
            except Exception:
                log.exception("Cannot recover receipt directory=%s; inputs preserved", directory)
                ok = False
        return ok

    def _register(self, raw: Path) -> Path | None:
        if raw.is_symlink() or not raw.is_file():
            raise ValueError(f"Input must be a regular, non-symlink file: {raw}")
        raw = raw.resolve()
        if raw.stat().st_dev != self.config.in_dir.stat().st_dev:
            raise ValueError("Input must be on the IN filesystem; copy it into IN first")
        for root in self.roots:
            if raw.is_relative_to(root.resolve()) and raw.parent != self.config.in_dir.resolve():
                raise ValueError("Cannot ingest files from a managed document directory")
        before = signature(raw)
        document_id = sha256(raw)
        if signature(raw) != before:
            raise ValueError("Input changed during hashing; left in place")
        for directory, manifest in self.manifests():
            if manifest.document_id == document_id:
                log.warning(
                    "Duplicate document_id=%s input=%s existing=%s; input left in place",
                    document_id,
                    raw,
                    directory,
                )
                return None
        # Unfinished receipts also reserve their identity; never create a second copy.
        for journal in self.config.in_dir.glob("*/receipt.json"):
            with journal.open(encoding="utf-8") as stream:
                if json.load(stream)["manifest"]["document_id"] == document_id:
                    raise ValueError("Matching unfinished receipt exists; input preserved")
        name = safe_name(raw.stem, document_id)
        directory = self.config.in_dir / name
        while directory.exists():
            directory = self.config.in_dir / f"{name}-{uuid.uuid4().hex[:8]}"
        directory.mkdir()
        for child in ("source", "JSON", "CHUNKS"):
            (directory / child).mkdir()
        source_type, mime = FORMATS.get(
            raw.suffix.lower(), ("unsupported", "application/octet-stream")
        )
        timestamp = now()
        manifest = Manifest(
            document_id=document_id,
            original_filename=raw.name,
            source_relative_path=f"source/{raw.name}",
            detected_mime_type=mime,
            source_type=source_type,
            source_sha256=document_id,
            source_size_bytes=before[0],
            received_at=timestamp,
            updated_at=timestamp,
        )
        journal = directory / "receipt.json"
        atomic_json(journal, {"raw_path": str(raw), "manifest": manifest.to_dict()})
        if signature(raw) != before:
            raise ValueError("Input changed before acquisition; receipt retained for inspection")
        # rename is atomic on the same filesystem; cross-volume input is intentionally rejected.
        raw.rename(directory / manifest.source_relative_path)
        sync_directory(raw.parent)
        sync_directory(directory / "source")
        save_manifest(directory, manifest)
        journal.unlink()
        log.info("Received document_id=%s directory=%s", document_id, directory)
        return directory

    def _move_error(self, directory: Path) -> None:
        target = self.config.error_dir / directory.name
        while target.exists():
            target = self.config.error_dir / f"{directory.name}-{uuid.uuid4().hex[:8]}"
        directory.rename(target)
        sync_directory(self.config.in_dir)
        sync_directory(self.config.error_dir)

    def _advance(self, directory: Path) -> bool:
        manifest: Manifest | None = None
        failed_stage = "UNKNOWN"
        try:
            manifest = load_manifest(directory)
            if manifest.current_state == State.ERROR:
                try:
                    self._move_error(directory)
                except Exception:
                    log.exception("Error relocation still unavailable directory=%s", directory)
                return False
            if reached(manifest.last_successful_state, self.config.max_stage):
                return True
            if manifest.last_successful_state == State.RECEIVED:
                failed_stage = State.CONVERTING
                manifest.transition(State.CONVERTING)
                save_manifest(directory, manifest)
                source = source_path(directory, manifest.source_relative_path)
                if sha256(source) != manifest.source_sha256:
                    raise ValueError("Source SHA-256 mismatch")
                if source.suffix.lower() not in FORMATS:
                    raise ValueError(f"Unsupported document format: {source.suffix}")
                output_dir = directory / "JSON"
                if output_dir.is_symlink():
                    raise ValueError("JSON directory must not be a symlink")
                output_dir.mkdir(exist_ok=True)
                manifest.docling_version = self.converter.version
                self.converter.convert(source, output_dir / self.config.docling_json_filename)
                if sha256(source) != manifest.source_sha256:
                    raise ValueError("Source changed during conversion")
                manifest.transition(State.CONVERTED)
                save_manifest(directory, manifest)
                log.info("Converted document_id=%s", manifest.document_id)
            if reached(manifest.last_successful_state, self.config.max_stage):
                return True
            failed_stage = State.CHUNKING
            manifest.transition(State.CHUNKING)
            save_manifest(directory, manifest)
            json_dir = directory / "JSON"
            document_json = json_dir / self.config.docling_json_filename
            if json_dir.is_symlink() or document_json.is_symlink():
                raise ValueError("Docling JSON path must not contain a symlink")
            if document_json.resolve().parent != json_dir.resolve() or not document_json.is_file():
                raise ValueError(f"Missing DoclingDocument JSON: JSON/{document_json.name}")
            chunks = self.chunker.chunks(document_json)
            manifest.chunk_count = self.chunk_writer.write(
                directory, manifest, self.chunker.spec, chunks
            )
            manifest.chunking = self.chunker.spec.manifest_dict()
            manifest.transition(State.CHUNKED)
            save_manifest(directory, manifest)
            log.info("Chunked document_id=%s chunks=%d", manifest.document_id, manifest.chunk_count)
            return True
        except Exception as exc:
            log.exception("Document processing failed directory=%s", directory)
            if manifest is not None:
                try:
                    manifest.attempt_count += 1
                    manifest.failed_stage = str(failed_stage)
                    manifest.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                    manifest.current_state = State.ERROR
                    manifest.updated_at = now()
                    save_manifest(directory, manifest)
                    self._move_error(directory)
                except Exception:
                    log.exception("Could not persist or relocate error; preserved at %s", directory)
            # Invalid manifests cannot safely be rewritten. Preserve and report every cycle.
            return False

    def cycle(self) -> bool:
        with self.lock:
            ok = self._recover_receipts()
            inputs = {
                p
                for p in self.config.in_dir.iterdir()
                if p.is_file() and not p.is_symlink() and not p.name.startswith(".")
            }
            self.stability.prune(inputs)
            for raw in sorted(inputs):
                try:
                    if self.stability.ready(raw):
                        self._register(raw)
                except Exception:
                    log.exception("Cannot acquire input=%s; input/receipt preserved", raw)
                    ok = False
            for directory in sorted(self.config.in_dir.iterdir()):
                if (
                    directory.is_dir()
                    and not directory.is_symlink()
                    and (directory / "manifest.json").exists()
                    and not self._advance(directory)
                ):
                    ok = False
            return ok

    def process(self, path: Path) -> bool:
        """Explicit input is caller-declared complete; no polling delay."""
        with self.lock:
            self._recover_receipts()
            directory = self._register(path)
            return True if directory is None else self._advance(directory)
