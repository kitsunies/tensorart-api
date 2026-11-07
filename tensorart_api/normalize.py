"""Map raw catalogue payloads onto the public schema."""

from __future__ import annotations

import re

from .schemas import Generation, ModelVersion, Owner, ProjectSummary, Stats
from .upstream import Source

# Substring of the lower-cased raw base model -> family. First match wins.
FAMILY_RULES = [
    ("noob", "noobai"),
    ("illustrious", "illustrious"),
    ("pony", "pony"),
    ("anima", "anima"),
    ("sdxl", "sdxl"),
    ("sd 1.5", "sd15"),
    ("sd1.5", "sd15"),
    ("flux", "flux"),
    ("krea", "krea"),
    ("z-image", "z-image"),
    ("wan_", "wan"),
    ("qwen", "qwen"),
    ("chroma", "chroma"),
    ("hunyuan", "hunyuan"),
]

# Families the upstream base-model filter can narrow to; the rest are filtered locally.
FAMILY_BASE_MODELS = {
    "illustrious": ["Illustrious"],
    "noobai": ["NoobAI", "Illustrious", "SDXL 1.0"],
    "pony": ["Pony"],
    "anima": ["Anima"],
    "sdxl": ["SDXL 1.0"],
    "sd15": ["SD 1.5"],
}

CATEGORY_TAGS = {
    "character": {"CHARACTER", "CHARACTERS", "GAME CHARACTER", "OC"},
    "style": {"STYLE", "ART STYLE", "ARTSTYLE", "ARTIST", "ARTIST STYLE"},
    "concept": {"CONCEPT", "POSE", "POSES"},
    "clothing": {"CLOTHING", "OUTFIT", "COSTUME"},
    "background": {"BACKGROUND", "SCENERY", "SCENE"},
    "tool": {"TOOL", "SLIDER"},
    "base_model": {"BASE MODEL"},
}
CATEGORIES = list(CATEGORY_TAGS)

# Families also inferred from version names, because NoobAI models are often labelled Illustrious/SDXL.
_NOOB_NAME = re.compile(r"noob", re.I)


def family(base_model: str, *names: str) -> str:
    raw = (base_model or "").lower()
    if raw in ("illustrious", "sdxl 1.0") and any(_NOOB_NAME.search(n or "") for n in names):
        return "noobai"
    for needle, fam in FAMILY_RULES:
        if needle in raw:
            return fam
    return re.sub(r"[^a-z0-9]+", "-", raw).strip("-") or "unknown"


def categories(tag_names: list[str]) -> list[str]:
    upper = {t.strip().upper() for t in tag_names}
    return [cat for cat, names in CATEGORY_TAGS.items() if upper & names]


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _no_commercial_use(model: dict) -> bool:
    # Generation APIs such as TAMS are commercial use, so they refuse these models.
    return bool((model.get("modelFlags") or {}).get("noCommercialUse"))


def project_generation(project_flags: dict, model: dict) -> Generation:
    flags = project_flags or {}
    if flags.get("onlyAccessibleBySelf"):
        return Generation(allowed=False, reason="owner_only")
    if not flags.get("allowOnlineGenerate", False):
        return Generation(allowed=False, reason="online_generation_disabled")
    if model.get("isModelRunSupported") is False:
        return Generation(allowed=False, reason="run_unsupported")
    if _no_commercial_use(model):
        return Generation(allowed=False, reason="no_commercial_use")
    return Generation(allowed=True)


def model_generation(model: dict) -> Generation:
    if model.get("canRun") is False:
        return Generation(allowed=False, reason="online_generation_disabled")
    if model.get("isModelRunSupported") is False:
        return Generation(allowed=False, reason="run_unsupported")
    if _no_commercial_use(model):
        return Generation(allowed=False, reason="no_commercial_use")
    return Generation(allowed=True)


def _owner(raw: dict | None) -> Owner:
    raw = raw or {}
    return Owner(id=str(raw.get("id") or ""), name=raw.get("nickname") or "", avatar_url=raw.get("avatar") or None)


def project_summary(raw: dict, source: Source) -> ProjectSummary:
    """Normalise a search/browse list item."""
    model = raw.get("model") or {}
    stats = raw.get("statisticInfo") or {}
    tag_names = [t.get("name", "") for t in raw.get("projectTags") or []]
    base = model.get("baseModel") or ""
    return ProjectSummary(
        id=str(raw["id"]),
        name=raw.get("name") or "",
        url=f"{source.site}/models/{raw['id']}",
        source=source.name,
        type=(raw.get("type") or "").lower(),
        family=family(base, raw.get("name", ""), model.get("name", "")),
        base_model=base,
        categories=categories(tag_names),
        tags=[t.lower() for t in tag_names if t],
        model_id=str(model.get("id") or ""),
        model_name=model.get("name") or "",
        cover_url=(model.get("cover") or {}).get("url") or None,
        owner=_owner(raw.get("owner")),
        stats=Stats(runs=_int(stats.get("runCount")), likes=_int(stats.get("likeCount")), downloads=_int(stats.get("downloadCount"))),
        generation=project_generation(raw.get("flags") or {}, model),
    )


def model_version(raw: dict, source: Source, project_name: str = "") -> ModelVersion:
    """Normalise a model (version) detail payload."""
    base = raw.get("baseModel") or ""
    covers = raw.get("cover") or raw.get("coverShowcases") or []
    if isinstance(covers, dict):
        covers = [covers]
    return ModelVersion(
        id=str(raw["id"]),
        name=raw.get("name") or "",
        type=(raw.get("type") or "").lower(),
        family=family(base, raw.get("name", ""), project_name),
        base_model=base,
        trigger_words=[w.strip() for w in raw.get("triggerWords") or [] if w and w.strip()],
        cover_urls=[c["url"] for c in covers if c.get("url")],
        source=source.name,
        generation=model_generation(raw),
    )
