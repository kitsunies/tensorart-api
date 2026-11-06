"""An in-memory stand-in for the catalogue API, served through httpx.MockTransport."""

import json

import httpx

from tensorart_api.upstream import Upstream

from .fixtures import list_item, model_detail, project_detail


def catalogue(prefix: str, n: int) -> list[dict]:
    # Every third item has generation disabled; ids are unique per prefix.
    return [list_item(id=f"{prefix}{i:04d}0", name=f"{prefix} item {i}", allow=i % 3 != 0) for i in range(n)]


def handler(catalogues: dict[str, list[dict]], models: dict[str, dict] | None = None):
    models = models or {}

    def ok(data):
        return httpx.Response(200, json={"code": "0", "data": data})

    def handle(request: httpx.Request) -> httpx.Response:
        items = catalogues.get(request.url.host, [])
        path = request.url.path
        if path.endswith("/search/general/v2"):
            body = json.loads(request.content)
            off, lim = body["offset"], body["limit"]
            return ok({"projectData": {"count": str(len(items)), "projects": items[off:off + lim]}})
        if path.endswith("/portal/list/v3"):
            body = json.loads(request.content)
            off, size = int(body["cursor"]), int(body["size"])
            page = items[off:off + size]
            return ok({"items": page, "cursor": str(off + size) if off + size < len(items) else "0"})
        if path.endswith("/model/detail"):
            mid = request.url.params["modelId"]
            if mid in models:
                return ok({"model": models[mid]})
            if mid.startswith("self"):
                return httpx.Response(200, json={"code": "-1", "message": "sorry, self model :(", "data": None})
            return httpx.Response(200, json={"code": "-1", "message": "rpc error: code = NotFound desc = project not found"})
        if path.endswith("/project/detail"):
            pid = request.url.params["projectId"]
            if pid == "p1":
                return ok({"project": project_detail(id="p1", model_ids=("m1", "self1"))})
            return httpx.Response(200, json={"code": "-1", "message": "rpc error: code = NotFound desc = project not found"})
        return httpx.Response(405)

    return handle


def fake_upstream(n_main=50, n_hub=50, models=None) -> Upstream:
    cats = {"api.tensor.art": catalogue("main", n_main), "api.tensorhub.art": catalogue("hub", n_hub)}
    return Upstream(transport=httpx.MockTransport(handler(cats, models or {"m1": model_detail(id="m1")})))
