from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import HTTPException, status
from pydantic import BaseModel, Field, HttpUrl, field_validator


class SourceType(StrEnum):
    HTML = "html"
    RSS = "rss"


class SourceStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    FAILED = "failed"


def normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL must use http or https and include a host.")
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if not hostname:
        raise ValueError("URL must include a valid host.")
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/") or "/"
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith(("utm_", "fbclid", "gclid"))
        ]
    )
    return urlunsplit((parsed.scheme.lower(), f"{hostname}{port}", path, query, ""))


class CompetitorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    canonical_domain: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class CompetitorResponse(CompetitorCreate):
    model_config = {"from_attributes": True}

    id: str
    workspace_id: str
    active: bool


class SourceCreate(BaseModel):
    competitor_id: str
    source_type: SourceType
    url: HttpUrl
    feed_url: HttpUrl | None = None
    crawl_interval_minutes: int = Field(default=1440, ge=15, le=10080)
    parser_key: str = Field(default="default", pattern=r"^[a-z0-9_]{1,64}$")

    @field_validator("url", "feed_url")
    @classmethod
    def validate_public_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is None:
            return value
        normalize_url(str(value))
        return value


class SourceUpdate(BaseModel):
    crawl_interval_minutes: int | None = Field(default=None, ge=15, le=10080)
    parser_key: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")
    status: SourceStatus | None = None


class SourceResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    workspace_id: str
    competitor_id: str
    source_type: SourceType
    url: str
    feed_url: str | None
    normalized_url: str
    crawl_interval_minutes: int
    parser_key: str
    status: SourceStatus
    last_success_at: datetime | None
    last_failure_code: str | None


@dataclass
class Competitor:
    id: str
    workspace_id: str
    name: str
    canonical_domain: str
    description: str | None
    active: bool = True


@dataclass
class Source:
    id: str
    workspace_id: str
    competitor_id: str
    source_type: SourceType
    url: str
    feed_url: str | None
    normalized_url: str
    crawl_interval_minutes: int
    parser_key: str
    status: SourceStatus = SourceStatus.ACTIVE
    last_success_at: datetime | None = None
    last_failure_code: str | None = None


class MonitoringRepository:
    """Replaceable repository used for the first vertical slice.

    The SQL models/migrations define the durable target. This repository keeps the
    API slice deterministic until database session wiring is introduced.
    """

    def __init__(self) -> None:
        self.competitors: dict[str, Competitor] = {}
        self.sources: dict[str, Source] = {}

    def create_competitor(self, workspace_id: str, data: CompetitorCreate) -> Competitor:
        competitor = Competitor(
            id=str(uuid4()),
            workspace_id=workspace_id,
            name=data.name,
            canonical_domain=data.canonical_domain.lower().strip(),
            description=data.description,
        )
        self.competitors[competitor.id] = competitor
        return competitor

    def list_competitors(self, workspace_id: str) -> list[Competitor]:
        return [item for item in self.competitors.values() if item.workspace_id == workspace_id]

    def get_competitor(self, workspace_id: str, competitor_id: str) -> Competitor:
        competitor = self.competitors.get(competitor_id)
        if competitor is None or competitor.workspace_id != workspace_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "COMPETITOR_NOT_FOUND", "message": "Competitor was not found."},
            )
        return competitor

    def create_source(self, workspace_id: str, data: SourceCreate) -> Source:
        self.get_competitor(workspace_id, data.competitor_id)
        normalized_url = normalize_url(str(data.url))
        duplicate = next(
            (
                source
                for source in self.sources.values()
                if source.workspace_id == workspace_id and source.normalized_url == normalized_url
            ),
            None,
        )
        if duplicate is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "DUPLICATE_SOURCE",
                    "message": "A source with this normalized URL already exists in the workspace.",
                },
            )
        source = Source(
            id=str(uuid4()),
            workspace_id=workspace_id,
            competitor_id=data.competitor_id,
            source_type=data.source_type,
            url=normalized_url,
            feed_url=normalize_url(str(data.feed_url)) if data.feed_url else None,
            normalized_url=normalized_url,
            crawl_interval_minutes=data.crawl_interval_minutes,
            parser_key=data.parser_key,
        )
        self.sources[source.id] = source
        return source

    def list_sources(self, workspace_id: str) -> list[Source]:
        return [item for item in self.sources.values() if item.workspace_id == workspace_id]

    def get_source(self, workspace_id: str, source_id: str) -> Source:
        source = self.sources.get(source_id)
        if source is None or source.workspace_id != workspace_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "SOURCE_NOT_FOUND", "message": "Source was not found."},
            )
        return source

    def update_source(self, workspace_id: str, source_id: str, data: SourceUpdate) -> Source:
        source = self.get_source(workspace_id, source_id)
        changes = data.model_dump(exclude_unset=True)
        for key, value in changes.items():
            setattr(source, key, value)
        return source

    def save_source(self, source: Source) -> Source:
        """Persist a source mutated by a crawl without changing the public API."""
        self.sources[source.id] = source
        return source


def competitor_response(item: Competitor) -> CompetitorResponse:
    return CompetitorResponse.model_validate(item, from_attributes=True)


def source_response(item: Source) -> SourceResponse:
    return SourceResponse.model_validate(item, from_attributes=True)
