from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SourceName = Literal["tensorart", "tensorhub"]


class Generation(BaseModel):
    """Whether the model can be used for online generation by anyone, not just its owner."""

    allowed: bool
    reason: Literal["online_generation_disabled", "run_unsupported", "owner_only"] | None = Field(
        None, description="Why generation is blocked; null when allowed."
    )


class Owner(BaseModel):
    id: str
    name: str
    avatar_url: str | None = None


class Stats(BaseModel):
    runs: int = 0
    likes: int = 0
    downloads: int = 0


class ProjectSummary(BaseModel):
    """A model page (project). `model_id` is the latest version, the id generation APIs expect."""

    id: str
    name: str
    url: str
    source: SourceName
    type: str = Field(description="lora, checkpoint, lycoris, dora, ...")
    family: str = Field(description="Normalised base-model family: illustrious, noobai, pony, anima, sdxl, ...")
    base_model: str = Field(description="Raw base-model label from the catalogue")
    categories: list[str] = Field(description="character, style, concept, clothing, background, tool, base_model")
    tags: list[str]
    model_id: str
    model_name: str
    cover_url: str | None = None
    owner: Owner
    stats: Stats
    generation: Generation


class ModelVersion(BaseModel):
    id: str
    name: str
    type: str
    family: str
    base_model: str
    trigger_words: list[str]
    cover_urls: list[str]
    source: SourceName
    generation: Generation


class ProjectDetail(ProjectSummary):
    description: str = ""
    versions: list[ModelVersion]


class Page(BaseModel):
    items: list[ProjectSummary]
    next_cursor: str | None = Field(None, description="Pass back as `cursor` for the next page; null at the end.")
    total: int | None = Field(None, description="Upstream match count for keyword searches; null when browsing.")


class GenerationCheck(BaseModel):
    model_id: str
    found: bool
    source: SourceName | None = None
    name: str | None = None
    family: str | None = None
    trigger_words: list[str] = []
    generation: Generation


class Facets(BaseModel):
    sources: list[str]
    types: list[str]
    families: list[str]
    categories: list[str]
    sorts: list[str]
    generation: list[str]
