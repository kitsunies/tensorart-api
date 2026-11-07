from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SourceName = Literal["tensorart", "tensorhub"]


class Generation(BaseModel):
    """Whether the model can be used for online generation by anyone, not just its owner."""

    allowed: bool
    reason: Literal["online_generation_disabled", "run_unsupported", "owner_only", "no_commercial_use"] | None = Field(
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


class AccountInfo(BaseModel):
    user_id: str
    token_expires_at: str | None = Field(None, description="When the current session token lapses; renewal pushes it out")
    renewed_at: str | None = None


class UploadRequest(BaseModel):
    """Copy a model file from `url` into your account as a private model."""

    url: str = Field(description="Direct file URL that supports HTTP range requests (presigned links are fine)")
    name: str = Field(min_length=1, max_length=120, description="Model page name")
    type: Literal["lora", "locon", "lycoris", "dora", "checkpoint"]
    base_model: str = Field(description="Base model label as the catalogue spells it, e.g. Illustrious, NoobAI, SDXL 1.0")
    cover_url: str = Field(description="Showcase image URL; required by the catalogue and copied by it")
    version_name: str = "v1"
    trigger_words: list[str] = []
    description: str = ""
    source_url: str = Field("", description="Where the model comes from; stored as the reprint's original link")
    file_name: str | None = Field(None, description="Defaults to the name the source reports")
    channel: str = Field("anime", description="Channel by name: anime, character, illustration, realistic, ...")
    external_id: str | None = Field(None, max_length=200, description="Idempotency key: the same value returns the same upload")


class Upload(BaseModel):
    id: str
    external_id: str | None = None
    state: Literal["queued", "copying", "publishing", "ready", "failed"]
    bytes_done: int = 0
    bytes_total: int = 0
    progress: float = 0.0
    project_id: str | None = None
    model_id: str | None = Field(None, description="Model (version) id for generation APIs")
    access_key: str | None = Field(None, description="The private model's access key for generation APIs")
    cover_url: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class UploadList(BaseModel):
    items: list[Upload]


class TrainingRequest(BaseModel):
    """Start a LoRA training: then add images, then start it."""

    name: str = Field(min_length=1, max_length=120)
    kind: Literal["style", "character"]
    trigger_word: str | None = Field(None, max_length=60, description="Defaults to a slug of the name")
    external_id: str | None = Field(None, max_length=200, description="Idempotency key: the same value returns the same training")


class Epoch(BaseModel):
    epoch: int
    samples: list[str] = Field(description="Sample images; links expire within the hour, so re-read the training for fresh ones")


class Published(BaseModel):
    epoch: int
    model_id: str
    access_key: str | None = None
    cover_url: str | None = None


class Training(BaseModel):
    id: str
    external_id: str | None = None
    name: str
    kind: Literal["style", "character"]
    trigger_word: str
    state: Literal["draft", "queued", "training", "trained", "publishing", "published", "failed"]
    images: int = 0
    progress: float = 0.0
    credits: float | None = Field(None, description="Credits charged for the run")
    epochs: list[Epoch] = []
    published: Published | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class TrainingList(BaseModel):
    items: list[Training]


class PublishRequest(BaseModel):
    epoch: int = Field(ge=1)
