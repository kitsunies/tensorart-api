"""Query logic shared by the HTTP API and the CLI."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
from typing import Literal

from .normalize import CATEGORIES, FAMILY_BASE_MODELS, model_version, project_summary
from .schemas import Facets, Generation, GenerationCheck, ModelVersion, Page, ProjectDetail, ProjectSummary
from .upstream import SOURCES, Source, Upstream, UpstreamError

SourceOpt = Literal["tensorart", "tensorhub", "all"]
GenerationOpt = Literal["allowed", "blocked", "any"]
SortOpt = Literal["relevance", "trending", "newest", "most_run", "most_downloaded"]

DEFAULT_SOURCE: str = os.environ.get("DEFAULT_SOURCE", "all")
SORTS = {
    "relevance": None,
    "trending": "HOT_TODAY",
    "newest": "NEWEST",
    "most_run": "MOST_RUN",
    "most_downloaded": "MOST_DOWNLOAD",
}
TYPES = ["lora", "checkpoint", "lycoris", "locon", "lokr", "dora", "embedding", "vae", "controlnet"]
FAMILIES = ["illustrious", "noobai", "pony", "anima", "sdxl", "sd15", "flux", "krea", "z-image", "wan", "qwen", "chroma"]
SEARCH_PAGE = 20  # upstream caps keyword search pages at 20
BROWSE_PAGE = 40
MAX_PAGES = int(os.environ.get("MAX_UPSTREAM_PAGES", 5))  # per source per request, bounds sparse filters
SELF_ONLY = "self model"


def split_csv(values: list[str]) -> list[str]:
    """Accept both repeated values and comma-separated ones: ["lora,dora", "vae"] -> ["lora", "dora", "vae"]."""
    return [v.strip().lower() for raw in values for v in raw.split(",") if v.strip()]


def sources_for(source: str | None) -> list[Source]:
    source = source or DEFAULT_SOURCE
    if source == "all":
        return list(SOURCES.values())
    if source not in SOURCES:
        raise UpstreamError(f"unknown source {source!r}; use tensorart, tensorhub or all", 400)
    return [SOURCES[source]]


def facets() -> Facets:
    return Facets(
        sources=[*SOURCES, "all"],
        types=TYPES,
        families=FAMILIES,
        categories=CATEGORIES,
        sorts=list(SORTS),
        generation=["allowed", "blocked", "any"],
    )


# Cursors: base64url JSON of {source: [upstream position, raw items already consumed] | null}.


def encode_cursor(state: dict) -> str | None:
    if all(v is None for v in state.values()):
        return None
    return base64.urlsafe_b64encode(json.dumps(state, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None, sources: list[Source], keyword: bool) -> dict:
    if not cursor:
        start = [0, 0] if keyword else ["0", 0]
        return {s.name: start for s in sources}
    try:
        state = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return {s.name: state.get(s.name) for s in sources}
    except (ValueError, AttributeError) as e:
        raise UpstreamError("invalid cursor", 400) from e


def _matches(p: ProjectSummary, types, families, categories, generation) -> bool:
    if types and p.type not in types:
        return False
    if families and p.family not in families:
        return False
    if categories and not set(p.categories) & set(categories):
        return False
    if generation == "allowed" and not p.generation.allowed:
        return False
    if generation == "blocked" and p.generation.allowed:
        return False
    return True


async def search(
    up: Upstream,
    q: str = "",
    source: str | None = None,
    types: list[str] | None = None,
    families: list[str] | None = None,
    categories: list[str] | None = None,
    generation: GenerationOpt = "allowed",
    sort: SortOpt = "relevance",
    limit: int = 20,
    cursor: str | None = None,
) -> Page:
    """Keyword search when `q` is set, otherwise browse. Filters the catalogue cannot apply are applied here."""
    srcs = sources_for(source)
    keyword = bool(q.strip())
    if sort not in SORTS:
        raise UpstreamError(f"unknown sort {sort!r}", 400)
    upstream_sort = SORTS[sort] or ("HOT_TODAY" if not keyword else None)
    families = [f.lower() for f in families or []]
    base_models = None
    if families and all(f in FAMILY_BASE_MODELS for f in families):
        base_models = sorted({b for f in families for b in FAMILY_BASE_MODELS[f]})
    types = [t.lower() for t in types or []]
    state = decode_cursor(cursor, srcs, keyword)

    async def fetch(src: Source, pos):
        if keyword:
            r = await up.search(src, q.strip(), offset=pos, limit=SEARCH_PAGE, sort=upstream_sort, base_models=base_models)
            nxt = pos + SEARCH_PAGE
            return r["items"], (nxt if r["items"] and nxt < r["total"] else None), r["total"]
        r = await up.browse(src, cursor=pos, size=BROWSE_PAGE, sort=upstream_sort, base_models=base_models,
                            model_types=[t.upper() for t in types] or None)
        nxt = r["cursor"]
        return r["items"], (nxt if r["items"] and nxt not in ("0", "", pos) else None), None

    async def collect(src: Source, want: int):
        pos_skip = state.get(src.name)
        out: list[ProjectSummary] = []
        total = None
        pages = 0
        while pos_skip is not None and len(out) < want and pages < MAX_PAGES:
            pos, skip = pos_skip
            raw, nxt, total = await fetch(src, pos)
            pages += 1
            pos_skip = [nxt, 0] if nxt is not None else None
            for i in range(skip, len(raw)):
                p = project_summary(raw[i], src)
                if _matches(p, types, families, categories, generation):
                    out.append(p)
                if len(out) == want:
                    if i + 1 < len(raw):
                        pos_skip = [pos, i + 1]
                    break
        return src.name, out, pos_skip, total

    want = math.ceil(limit / len(srcs))
    results = await asyncio.gather(*(collect(s, want) for s in srcs))

    # Interleave sources so `all` mixes both catalogues; drop projects listed in both.
    items: list[ProjectSummary] = []
    seen: set[str] = set()
    for row in zip_longest_lists([r[1] for r in results]):
        for p in row:
            if p.id not in seen:
                seen.add(p.id)
                items.append(p)
    next_state = {name: pos for name, _, pos, _ in results}
    totals = [t for *_, t in results if t is not None]
    return Page(items=items[:limit], next_cursor=encode_cursor(next_state), total=sum(totals) if keyword else None)


def zip_longest_lists(lists: list[list]):
    for i in range(max((len(x) for x in lists), default=0)):
        yield [x[i] for x in lists if i < len(x)]


async def _first(srcs: list[Source], fetch):
    """Run `fetch(source)` across sources; return the first hit, else re-raise the most useful error."""
    last: UpstreamError | None = None
    for src in srcs:
        try:
            return src, await fetch(src)
        except UpstreamError as e:
            if e.status != 404 and SELF_ONLY not in e.message:
                raise
            if last is None or last.status == 404:
                last = e
    raise last or UpstreamError("not found", 404)


async def get_model(up: Upstream, model_id: str, source: str | None = None) -> ModelVersion:
    try:
        src, raw = await _first(sources_for(source), lambda s: up.model(s, model_id))
    except UpstreamError as e:
        if SELF_ONLY in e.message:
            raise UpstreamError("model is only accessible by its owner", 403) from e
        raise UpstreamError("model not found", 404) if e.status == 404 else e
    if not raw:
        raise UpstreamError("model not found", 404)
    return model_version(raw, src)


async def check(up: Upstream, model_ids: list[str], source: str | None = None) -> list[GenerationCheck]:
    """Is each model id usable for generation by anyone? Built for validating stored LoRA/checkpoint ids."""

    async def one(mid: str) -> GenerationCheck:
        try:
            v = await get_model(up, mid, source)
        except UpstreamError as e:
            if e.status == 403:
                return GenerationCheck(model_id=mid, found=True, generation=Generation(allowed=False, reason="owner_only"))
            if e.status == 404:
                return GenerationCheck(model_id=mid, found=False, generation=Generation(allowed=False))
            raise
        return GenerationCheck(model_id=mid, found=True, source=v.source, name=v.name, family=v.family,
                               trigger_words=v.trigger_words, generation=v.generation)

    return list(await asyncio.gather(*(one(m) for m in dict.fromkeys(model_ids))))


async def get_project(up: Upstream, project_id: str, source: str | None = None) -> ProjectDetail:
    try:
        src, raw = await _first(sources_for(source), lambda s: up.project(s, project_id))
    except UpstreamError as e:
        raise UpstreamError("project not found", 404) if e.status == 404 or SELF_ONLY in e.message else e
    if not raw:
        raise UpstreamError("project not found", 404)

    async def version(m: dict) -> ModelVersion:
        try:
            return model_version(await up.model(src, str(m["id"])), src, raw.get("name", ""))
        except UpstreamError as e:
            if SELF_ONLY not in e.message:
                raise
            v = model_version({**m, "type": raw.get("type")}, src, raw.get("name", ""))
            return v.model_copy(update={"generation": Generation(allowed=False, reason="owner_only")})

    versions = list(await asyncio.gather(*(version(m) for m in raw.get("relatedModels") or [])))
    latest = versions[0] if versions else None
    # Reuse the list-item normaliser by reshaping the detail payload into its form.
    summary = project_summary(
        {
            "id": raw["id"],
            "name": raw.get("name"),
            "type": raw.get("type"),
            "owner": raw.get("owner"),
            "statisticInfo": raw.get("statisticsInfo"),
            "projectTags": raw.get("relatedTags"),
            "flags": raw.get("flags"),
            "model": {
                "id": latest.id if latest else "",
                "name": latest.name if latest else "",
                "baseModel": latest.base_model if latest else "",
                "cover": {"url": latest.cover_urls[0]} if latest and latest.cover_urls else None,
                "isModelRunSupported": latest.generation.reason != "run_unsupported" if latest else True,
            },
        },
        src,
    )
    if latest and not latest.generation.allowed and summary.generation.allowed:
        summary.generation = latest.generation
    return ProjectDetail(**summary.model_dump(), description=raw.get("description") or "", versions=versions)
