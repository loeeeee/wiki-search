from __future__ import annotations

from html.parser import HTMLParser
from typing import Iterable, List


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: List[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks)


def strip_html(value: str) -> str:
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
    for paragraph_chunks in text_field:
        joined = "".join(paragraph_chunks)
        paragraphs.append(strip_html(joined))
    return paragraphs


