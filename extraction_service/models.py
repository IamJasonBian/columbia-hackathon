"""Generic extraction output shapes shared by every extractor."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, computed_field


class DocumentKind(str, Enum):
    email = "email"
    pdf = "pdf"
    spreadsheet = "spreadsheet"
    image = "image"
    text = "text"
    unknown = "unknown"


class ExtractedPage(BaseModel):
    index: int = Field(description="0-based page index")
    text: str = ""
    ocr: bool = Field(False, description="True when text came from Tesseract, not a text layer")


class ExtractedTable(BaseModel):
    name: str | None = Field(None, description="Sheet name, or 'page N table M' for PDFs")
    page: int | None = None
    headers: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.headers, row)) for row in self.rows]


class ExtractedDocument(BaseModel):
    filename: str
    kind: DocumentKind
    content_type: str | None = None
    size_bytes: int = 0
    sha256: str | None = None
    text: str = Field("", description="All text, pages joined with form feeds")
    pages: list[ExtractedPage] = Field(default_factory=list)
    tables: list[ExtractedTable] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    attachments: list[ExtractedDocument] = Field(
        default_factory=list, description="Email attachments, extracted recursively"
    )
    warnings: list[str] = Field(default_factory=list)
