"""Field extraction. Phase 1 provides only canon() and sibling_key(); the
per-field extractors arrive in Phase 2."""

import re

_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")


def canon(name: str) -> str:
    """Lowercase, drop everything except letters, digits, and spaces, and
    collapse whitespace. Used for company/title matching and sibling keys."""
    s = _NON_ALNUM.sub("", (name or "").lower())
    return _WS.sub(" ", s).strip()


def sibling_key(company: str, title: str) -> str:
    """Group key for postings of the same role across cities."""
    return f"{canon(company)}::{canon(title)}"
