"""HTML to readable plain text, on the stdlib alone.

Email bodies are converted to text **on the way in**, not on the way out. Two
reasons, and the second is the one that decides it:

- The SPA would otherwise need an HTML sanitizer on a path that renders content
  from an arbitrary third party. Storing text means the renderer is a
  ``whitespace-pre-wrap`` div and there is no XSS surface to get wrong.
- ``thread_emails.body`` is read by whatever reads the row, and SQLite reads
  whole rows. Markup is typically several times the size of its own text.

Deliberately not a dependency. html2text and friends do this better, but this
would be the repo's first non-stdlib text package, and ``html.parser`` plus 40
lines covers what mail actually contains. Gmail's ``get_email_body`` prefers
``text/plain`` and only falls back to ``text/html``, so this runs on the
minority of messages.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

__all__ = ["looks_like_html", "html_to_text"]

# Content that is markup-only: never emit its text.
_DROP = {"script", "style", "head", "title", "meta", "link", "noscript"}

# Tags that end a line. <br> is a break; the rest are blocks whose boundaries
# are the only paragraph structure a converted body keeps.
_BREAK = {"br"}
_BLOCK = {
    "p", "div", "tr", "li", "ul", "ol", "table", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "section", "article",
    "header", "footer", "figure", "form", "fieldset", "dl", "dt", "dd",
}
# Cells separate horizontally, not vertically. Without this a table row renders
# as "AB" -- and mail is full of layout tables, so two adjacent cells running
# together is the common case rather than an exotic one.
_CELL = {"td", "th"}

_SPACES = re.compile(r"[ \t ]+")
_BLANK_RUN = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+\n")

# Enough of a signal to be worth parsing. Deliberately strict, because the two
# ways of being wrong are not symmetrical: mistaking markup for plain text merely
# leaves tags visible, while mistaking plain text for markup silently *deletes*
# characters out of somebody's message.
#
# A well-formed tag: a name, optional attributes, no nested angle brackets.
_TAG = re.compile(r"<\s*/?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")
# ...and the unambiguous document markers, which need no corroboration.
_DOCUMENT = re.compile(r"<\s*(?:!doctype\s+html|/?(?:html|body|head)\b)", re.IGNORECASE)


def _tag_count(text: str) -> int:
    return len(_TAG.findall(text))


def looks_like_html(text: str | None) -> bool:
    """Whether a body is worth running through the converter.

    Providers do not reliably say. Gmail's extractor returns ``text/plain`` when
    it can and raw ``text/html`` when that is all there is, with no flag, and
    Zoho's content field is documented inconsistently. So the shape of the text
    is the only thing to go on -- and guessing wrong towards "plain" merely
    leaves markup visible, while guessing wrong towards "html" would delete
    real angle brackets out of a plain-text message.
    """
    if not text:
        return False
    if _DOCUMENT.search(text):
        return True
    # Two or more tags, because one is what plain prose produces by accident.
    # "a<b and b>c" is a comparison, not bold text; "3 < 5" and
    # "reply to <priya@acme.com>" must survive untouched. Real markup
    # essentially always closes something, so it clears this trivially, and a
    # lone <br> in an otherwise plain body loses nothing by being left alone.
    return _tag_count(text) >= 2


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _DROP:
            self._suppress += 1
            return
        if tag in _CELL:
            self._out.append(" ")
        elif tag in _BREAK or tag in _BLOCK:
            self._out.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in _BREAK or tag in _BLOCK:
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP:
            # Clamped at zero: a stray </style> with no opener is common in
            # forwarded mail and must not make the rest of the body vanish.
            self._suppress = max(0, self._suppress - 1)
            return
        if tag in _BLOCK:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._suppress:
            return
        self._out.append(data)

    def text(self) -> str:
        return "".join(self._out)


def html_to_text(raw: str | None) -> str:
    """Readable text from an HTML fragment or document.

    Never raises: a malformed body still has to produce *something*, because the
    alternative is an email that displays as empty. ``HTMLParser`` in non-strict
    mode already tolerates unclosed and unknown tags; the try/except is for the
    genuinely pathological input.
    """
    if not raw:
        return ""

    parser = _Extractor()
    try:
        parser.feed(raw)
        parser.close()
        text = parser.text()
    except Exception:  # noqa: BLE001 - see docstring: empty output is worse
        text = re.sub(r"<[^>]*>", " ", raw)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of spaces but not newlines: the block-level breaks above are
    # the only paragraph structure that survived, so they have to be kept.
    text = _SPACES.sub(" ", text)
    text = _TRAILING_WS.sub("\n", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def to_plain_text(raw: str | None) -> str:
    """Convert if it looks like markup, otherwise normalise newlines only."""
    if not raw:
        return ""
    if looks_like_html(raw):
        return html_to_text(raw)
    return raw.replace("\r\n", "\n").replace("\r", "\n").strip()
