"""RFC 822 (.eml) extraction: headers, body text, and attachments.

Gmail's ``users.messages.get(format="raw")`` returns exactly this format
(base64url-encoded), as do "Download message" / "Show original" exports, so
one parser covers live mailbox pulls and manual uploads alike.
"""

from __future__ import annotations

import email
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser


@dataclass
class FetchedAttachment:
    filename: str
    data: bytes
    content_type: str = "application/octet-stream"


@dataclass
class FetchedMessage:
    """One parsed email: envelope, body, and raw attachments."""

    message_id: str
    subject: str
    sender: str
    internal_date_ms: int
    body_text: str
    attachments: list[FetchedAttachment] = field(default_factory=list)
    recipients: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    date_iso: str | None = None
    body_html: str | None = None


class _HtmlToText(HTMLParser):
    _BLOCK_TAGS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"}
    _SKIP_TAGS = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")
        elif tag == "td":
            self._parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self._parts).splitlines())
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    """Flatten an HTML email body to plain text.

    Args:
        html: The HTML body.

    Returns:
        Whitespace-normalized text, one block element per line.
    """
    parser = _HtmlToText()
    parser.feed(html)
    return parser.text()


def _decoded_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def parse_eml(data: bytes, fallback_id: str = "") -> FetchedMessage:
    """Parse raw RFC 822 bytes into a FetchedMessage.

    Prefers the text/plain body; falls back to flattened text/html. Every
    non-body part with a filename (or a non-text content type) is returned
    as an attachment, including parts nested in forwarded messages.

    Args:
        data: Raw message bytes.
        fallback_id: Message ID to use when the Message-ID header is absent.

    Returns:
        The parsed message.
    """
    msg: EmailMessage = email.message_from_bytes(data, policy=policy.default)  # type: ignore[assignment]

    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[FetchedAttachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if filename or disposition == "attachment" or not content_type.startswith("text/"):
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            attachments.append(
                FetchedAttachment(
                    filename=filename or f"attachment-{len(attachments) + 1}",
                    data=payload,
                    content_type=content_type,
                )
            )
        elif content_type == "text/plain":
            plain_parts.append(_decoded_text(part))
        elif content_type == "text/html":
            html_parts.append(_decoded_text(part))

    body_html = "\n".join(html_parts) or None
    body_text = "\n".join(plain_parts).strip() or (html_to_text(body_html) if body_html else "")

    date_iso: str | None = None
    internal_date_ms = 0
    if msg["Date"]:
        try:
            parsed = parsedate_to_datetime(str(msg["Date"]))
            date_iso = parsed.isoformat()
            internal_date_ms = int(parsed.timestamp() * 1000)
        except (TypeError, ValueError):
            pass

    def _addresses(header: str) -> list[str]:
        return [addr for _, addr in getaddresses(msg.get_all(header, [])) if addr]

    return FetchedMessage(
        message_id=str(msg["Message-ID"] or fallback_id).strip("<> "),
        subject=str(msg["Subject"] or ""),
        sender=str(msg["From"] or ""),
        internal_date_ms=internal_date_ms,
        body_text=body_text,
        attachments=attachments,
        recipients=_addresses("To"),
        cc=_addresses("Cc"),
        date_iso=date_iso,
        body_html=body_html,
    )


def looks_like_eml(data: bytes) -> bool:
    """Heuristic: the first non-blank line is an RFC 822 header we expect.

    Args:
        data: Candidate file bytes.

    Returns:
        True when the bytes look like a raw email.
    """
    head = data[:2048].lstrip().decode("latin-1", errors="replace")
    first = head.split("\n", 1)[0].lower()
    return first.startswith(
        ("from:", "received:", "return-path:", "mime-version:", "delivered-to:",
         "message-id:", "subject:", "date:", "to:", "x-")
    ) and ":" in first
