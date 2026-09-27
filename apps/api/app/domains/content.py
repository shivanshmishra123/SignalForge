import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, NavigableString, Tag

_WHITESPACE = re.compile(r"\s+")
_TIMESTAMP = re.compile(
    r"\b(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}"
    r"|\d{1,2}[-/]\d{1,2}[-/](?:20)?\d{2}"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"\s+\d{1,2},?\s+20\d{2})\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>\"]+")


@dataclass(frozen=True)
class NormalizedSection:
    key: str
    heading: str
    text: str
    content_hash: str


@dataclass(frozen=True)
class NormalizedDocument:
    text: str
    content_hash: str
    sections: tuple[NormalizedSection, ...]

    @property
    def section_hashes(self) -> dict[str, str]:
        return {section.key: section.content_hash for section in self.sections}

    @property
    def serialized_sections(self) -> list[dict[str, str]]:
        return [
            {
                "key": section.key,
                "heading": section.heading,
                "text": section.text,
                "content_hash": section.content_hash,
            }
            for section in self.sections
        ]


@dataclass(frozen=True)
class SectionChange:
    key: str
    change_type: str
    before: NormalizedSection | None
    after: NormalizedSection | None


def normalize_content(content: str, content_type: str) -> NormalizedDocument:
    """Convert fetched HTML/XML into stable text and heading-based sections."""
    soup = BeautifulSoup(content, "html.parser")
    for element in soup.find_all(["script", "style", "noscript", "template"]):
        element.decompose()

    if "html" in content_type.lower():
        sections = _html_sections(soup)
    else:
        sections = _feed_sections(soup)

    if not sections:
        text = _clean_text(soup.get_text(" ", strip=True))
        if text:
            sections = (NormalizedSection("body", "", text, _hash(text)),)
        else:
            sections = ()

    document_text = "\n\n".join(
        f"{section.heading}\n{section.text}".strip() if section.heading else section.text
        for section in sections
    )
    return NormalizedDocument(
        text=document_text,
        content_hash=_hash(document_text),
        sections=sections,
    )


def diff_sections(
    before: NormalizedDocument, after: NormalizedDocument
) -> tuple[SectionChange, ...]:
    before_by_key = {section.key: section for section in before.sections}
    after_by_key = {section.key: section for section in after.sections}
    changes: list[SectionChange] = []
    for key in sorted(before_by_key.keys() | after_by_key.keys()):
        previous = before_by_key.get(key)
        current = after_by_key.get(key)
        if previous is None:
            changes.append(SectionChange(key, "added", None, current))
        elif current is None:
            changes.append(SectionChange(key, "removed", previous, None))
        elif previous.content_hash != current.content_hash:
            changes.append(SectionChange(key, "modified", previous, current))
    return tuple(changes)


def _html_sections(soup: BeautifulSoup) -> tuple[NormalizedSection, ...]:
    headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    if not headings:
        return ()
    sections: list[NormalizedSection] = []
    for index, heading in enumerate(headings):
        heading_text = _clean_text(heading.get_text(" ", strip=True))
        section_text = _clean_text(_following_text_until_heading(heading))
        combined = " ".join(part for part in (heading_text, section_text) if part)
        if combined:
            key = f"{index}:{_slug(heading_text) or 'section'}"
            sections.append(NormalizedSection(key, heading_text, section_text, _hash(combined)))
    return tuple(sections)


def _feed_sections(soup: BeautifulSoup) -> tuple[NormalizedSection, ...]:
    items = soup.find_all(["item", "entry"])
    sections: list[NormalizedSection] = []
    for index, item in enumerate(items):
        title = item.find(["title", "name"])
        heading = _clean_text(title.get_text(" ", strip=True)) if title else ""
        text_parts = [
            child.get_text(" ", strip=True)
            for child in item.find_all(["description", "summary", "content", "value"])
        ]
        text = _clean_text(" ".join(text_parts or [item.get_text(" ", strip=True)]))
        combined = " ".join(part for part in (heading, text) if part)
        if combined:
            sections.append(NormalizedSection(f"entry:{index}", heading, text, _hash(combined)))
    return tuple(sections)


def _following_text_until_heading(heading: Tag) -> str:
    text_nodes: list[str] = []
    for node in heading.next_siblings:
        if isinstance(node, Tag) and node.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            break
        if isinstance(node, Tag):
            text_nodes.append(node.get_text(" ", strip=True))
        elif isinstance(node, NavigableString) and node.strip():
            text_nodes.append(str(node))
    return " ".join(text_nodes)


def _clean_text(value: str) -> str:
    value = _URL.sub(_without_tracking_parameters, value)
    value = _TIMESTAMP.sub("", value)
    return _WHITESPACE.sub(" ", value).strip()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _without_tracking_parameters(match: re.Match[str]) -> str:
    parsed = urlsplit(match.group(0))
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith(("utm_", "fbclid", "gclid"))
        ]
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))
