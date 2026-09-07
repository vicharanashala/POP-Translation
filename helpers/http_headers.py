"""HTTP header helpers.

HTTP header values are latin-1 encoded (RFC 7230). A filename containing any
character outside that range -- an en-dash, a rupee sign, Devanagari, Kannada --
raises UnicodeEncodeError when the response is serialised, which surfaces as a
500 rather than a download. That is not hypothetical here: this corpus is full
of Indian-language documents and typographic dashes.
"""
from __future__ import annotations

from urllib.parse import quote


def content_disposition(filename: str, *, inline: bool = False) -> str:
    """Build a Content-Disposition value that is safe for any filename.

    Emits both forms defined by RFC 6266:
      - `filename="..."`  an ASCII-only fallback for old clients
      - `filename*=UTF-8''...`  percent-encoded UTF-8, which every current
        browser prefers and which preserves the real name

    Quotes and backslashes are stripped from the fallback so they cannot
    terminate the quoted string early.
    """
    disposition = "inline" if inline else "attachment"
    name = (filename or "download").replace("\r", "").replace("\n", "")

    ascii_fallback = name.encode("ascii", "ignore").decode("ascii")
    ascii_fallback = ascii_fallback.replace("\\", "").replace('"', "")
    if not ascii_fallback.strip():
        ascii_fallback = "download"

    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(name, safe='')}"
