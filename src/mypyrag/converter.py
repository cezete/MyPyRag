"""Replaceable conversion boundary; only this adapter knows Docling APIs."""

import os
import tempfile
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

from mypyrag.storage import sync_directory

# Explicitly restrict ingestion to the supported stage-one formats.
FORMATS = {
    ".pdf": ("pdf", "application/pdf"),
    ".docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".html": ("html", "text/html"),
    ".htm": ("html", "text/html"),
    ".md": ("md", "text/markdown"),
    ".markdown": ("md", "text/markdown"),
}


class Converter(Protocol):
    @property
    def version(self) -> str: ...

    def convert(self, source: Path, destination: Path) -> None:
        """Convert, persist atomically, and validate by reloading the native document."""
        ...


class DoclingAdapter:
    def __init__(self) -> None:
        self._converter: Any = None

    @property
    def version(self) -> str:
        return version("docling")

    def convert(self, source: Path, destination: Path) -> None:
        from docling.datamodel.base_models import ConversionStatus, DocumentStream, InputFormat
        from docling.document_converter import DocumentConverter
        from docling_core.types.doc.base import ImageRefMode
        from docling_core.types.doc.document import DoclingDocument

        if self._converter is None:
            self._converter = DocumentConverter(
                allowed_formats=[
                    InputFormat.PDF,
                    InputFormat.DOCX,
                    InputFormat.HTML,
                    InputFormat.MD,
                ]
            )
        # Docling recognizes .md, but not the common .markdown extension.
        if source.suffix.lower() == ".markdown":
            result = self._converter.convert(
                DocumentStream(name=f"{source.stem}.md", stream=BytesIO(source.read_bytes()))
            )
            if result.document.origin is not None:
                result.document.origin.filename = source.name
        else:
            result = self._converter.convert(source)
        if result.status != ConversionStatus.SUCCESS:
            raise ValueError(
                f"Docling conversion did not succeed: {result.status}; {result.errors}"
            )
        descriptor, name = tempfile.mkstemp(
            prefix=".docling-", suffix=".json", dir=destination.parent
        )
        os.close(descriptor)
        temporary = Path(name)
        try:
            result.document.save_as_json(
                temporary,
                image_mode=ImageRefMode.EMBEDDED,
                coord_precision=None,
                confid_precision=None,
            )
            DoclingDocument.load_from_json(temporary)
            # Windows FlushFileBuffers requires a writable descriptor.
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            sync_directory(destination.parent)
            DoclingDocument.load_from_json(destination)
        finally:
            temporary.unlink(missing_ok=True)
