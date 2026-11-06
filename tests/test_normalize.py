from tensorart_api.normalize import categories, family, model_version, project_summary
from tensorart_api.upstream import SOURCES

from .fixtures import list_item, model_detail


def test_family():
    assert family("Illustrious") == "illustrious"
    assert family("Illustrious", "NoobAI EPS 1.1") == "noobai"
    assert family("NoobAI") == "noobai"
    assert family("Pony") == "pony"
    assert family("SDXL 1.0") == "sdxl"
    assert family("SD 1.5") == "sd15"
    assert family("FLUX.1 Krea Dev") == "flux"
    assert family("KREA_2") == "krea"
    assert family("WAN_2_2_A14B") == "wan"
    assert family("MOSS-Image-Base") == "moss-image-base"
    assert family("") == "unknown"


def test_categories():
    assert categories(["CHARACTER", "ANIME"]) == ["character"]
    assert categories(["style", "Game Character"]) == ["character", "style"]
    assert categories(["CUTE"]) == []


def test_project_summary_allowed():
    p = project_summary(list_item(), SOURCES["tensorhub"])
    assert p.url == "https://tensorhub.art/models/1063432615341616924"
    assert (p.type, p.family, p.categories) == ("lora", "anima", ["character"])
    assert p.stats.runs == 1200 and p.owner.name == "maker"
    assert p.generation.allowed and p.generation.reason is None


def test_project_summary_blocked_reasons():
    assert project_summary(list_item(allow=False), SOURCES["tensorart"]).generation.reason == "online_generation_disabled"
    assert project_summary(list_item(self_only=True), SOURCES["tensorart"]).generation.reason == "owner_only"


def test_model_version():
    v = model_version(model_detail(), SOURCES["tensorhub"])
    assert v.trigger_words == ["strapless bra", "black hairband"]
    assert v.cover_urls == ["https://example.test/1.png", "https://example.test/2.png"]
    assert v.generation.allowed
    assert model_version(model_detail(can_run=False), SOURCES["tensorhub"]).generation.allowed is False
