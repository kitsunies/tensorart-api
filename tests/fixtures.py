"""Minimal payloads in the shape the catalogue returns (trimmed from real responses)."""


def list_item(id="1063432615341616924", name="Rebecca - Cyberpunk Edgerunners", base="Anima",
              tags=("CHARACTER", "ANIME"), allow=True, self_only=False, model_name="v1", type="LORA",
              no_commercial=False):
    return {
        "id": id,
        "name": name,
        "type": type,
        "owner": {"id": "42", "nickname": "maker", "avatar": "https://example.test/a.png"},
        "statisticInfo": {"downloadCount": "3", "runCount": "1200", "likeCount": "88"},
        "projectTags": [{"id": str(i), "name": t, "type": "SYSTEM"} for i, t in enumerate(tags)],
        "model": {
            "id": id[:-1] + "9",
            "name": model_name,
            "cover": {"url": "https://example.test/cover.png", "contentRating": "EVERYONE"},
            "baseModel": base,
            "isModelRunSupported": True,
            "modelFlags": {"noCommercialUse": True if no_commercial else None},
        },
        "flags": {"allowOnlineGenerate": allow, "onlyAccessibleBySelf": self_only},
    }


def model_detail(id="869608418527192240", name="Illustrious", base="Illustrious", can_run=True,
                 words=("strapless bra", " black hairband", ""), no_commercial=False):
    return {
        "id": id,
        "name": name,
        "type": "LORA",
        "baseModel": base,
        "triggerWords": list(words),
        "cover": [{"url": "https://example.test/1.png"}, {"url": "https://example.test/2.png"}],
        "canRun": can_run,
        "isModelRunSupported": True,
        "modelFlags": {"noCommercialUse": True if no_commercial else None},
    }


def project_detail(id="1063432615341616924", model_ids=("869608418527192240",)):
    return {
        "id": id,
        "name": "Rebecca - Cyberpunk Edgerunners",
        "description": "<p>Character LoRA</p>",
        "type": "LORA",
        "relatedTags": [{"id": "1", "name": "CHARACTER", "type": "DEFAULT"}],
        "relatedModels": [{"id": m, "name": f"v{i}", "cover": {"url": "https://example.test/c.png"}} for i, m in enumerate(model_ids)],
        "statisticsInfo": {"runCount": "10", "likeCount": "2", "downloadCount": "1"},
        "owner": {"id": "42", "nickname": "maker"},
        "flags": {"allowOnlineGenerate": True, "onlyAccessibleBySelf": False},
    }
