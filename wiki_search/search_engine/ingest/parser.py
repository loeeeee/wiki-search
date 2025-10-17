from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Iterable, List, Tuple
from urllib.parse import unquote

# Try fast path with lxml.html; fallback to Python HTMLParser
try:  # noqa: SIM105
    from lxml import html as _lxml_html  # type: ignore

    def strip_html(value: str) -> str:  # type: ignore[override]
        # lxml's text_content is fast and robust
        if not value:
            return ""
        try:
            return _lxml_html.fromstring(value).text_content()
        except Exception:
            return ""

except Exception:  # pragma: no cover
    class _HTMLStripper(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self._chunks: List[str] = []

        def handle_data(self, data: str) -> None:
            self._chunks.append(data)

        def get_text(self) -> str:
            return "".join(self._chunks)

    def strip_html(value: str) -> str:  # type: ignore[no-redef]
        stripper = _HTMLStripper()
        stripper.feed(value)
        return stripper.get_text()


def extract_plain_paragraphs(text_field: Iterable[Iterable[str]]) -> List[str]:
    """Convert the HotpotQA `text` field into plain text paragraphs.

    The input `text_field` is a list of paragraphs, each paragraph is a list of
    string chunks that may include HTML anchor tags. We join chunks per paragraph
    and strip HTML to produce readable plain text paragraphs.
    """
    paragraphs: List[str] = []
    if text_field is None:
        return paragraphs
    for paragraph_chunks in text_field:
        try:
            if paragraph_chunks is None:
                continue
            joined = "".join(str(chunk) for chunk in paragraph_chunks if chunk)
            plain = strip_html(joined)
            if plain:
                paragraphs.append(plain)
        except Exception:
            # Skip malformed paragraph
            continue
    return paragraphs


def extract_internal_links(text_field: Iterable[Iterable[str]]) -> List[Tuple[str, str]]:
    """Extract internal links from the HotpotQA `text` field.

    The input `text_field` is a list of paragraphs, each paragraph is a list of
    string chunks that may include HTML anchor tags. We extract all internal links
    in the format <a href="target_title">anchor_text</a>.

    Returns:
        List of (target_title, anchor_text) tuples for all internal links found.
    """
    links: List[Tuple[str, str]] = []
    if text_field is None:
        return links
    
    # Regex pattern to match internal links: <a href="target">anchor</a>
    link_pattern = re.compile(r'<a href="([^"]+)">([^<]+)</a>')
    
    for paragraph_chunks in text_field:
        try:
            if paragraph_chunks is None:
                continue
            joined = "".join(str(chunk) for chunk in paragraph_chunks if chunk)
            
            # Find all matches in this paragraph
            for match in link_pattern.finditer(joined):
                href = match.group(1)
                anchor_text = match.group(2).strip()
                
                # URL decode the href (e.g., "Pierre-Joseph%20Proudhon" -> "Pierre-Joseph Proudhon")
                try:
                    target_title = unquote(href)
                    if target_title and anchor_text:
                        links.append((target_title, anchor_text))
                except Exception:
                    # Skip malformed URLs
                    continue
                    
        except Exception:
            # Skip malformed paragraph
            continue
    
    return links


