"""HTTP API. Interactive docs at /docs, OpenAPI schema at /openapi.json."""

from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import __version__, service
from .account import Account
from .schemas import AccountInfo, Facets, GenerationCheck, ModelVersion, Page, ProjectDetail, Upload, UploadList, UploadRequest
from .upstream import Upstream, UpstreamError
from .uploads import Uploads, public


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.upstream = Upstream()
    state_dir = Path(os.environ.get("STATE_DIR", "state"))
    app.state.account = Account.from_env(app.state.upstream, state_dir)
    app.state.uploads, renewals = None, None
    if app.state.account:
        app.state.uploads = Uploads(app.state.account, state_dir)
        app.state.uploads.resume()
        renewals = asyncio.create_task(app.state.account.keep_alive())
    yield
    if renewals:
        renewals.cancel()
    if app.state.uploads:
        await app.state.uploads.aclose()
    await app.state.upstream.aclose()


app = FastAPI(
    title="tensorart-api",
    version=__version__,
    description=(
        "Live, cached, normalised search over the Tensor.Art model catalogue. "
        "Every list endpoint supports the same filters and an opaque `cursor` for paging."
    ),
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


@app.exception_handler(UpstreamError)
async def upstream_error(_: Request, exc: UpstreamError):
    return JSONResponse(status_code=exc.status, content={"error": exc.message})


def upstream(request: Request) -> Upstream:
    return request.app.state.upstream


Source = Annotated[service.SourceOpt | None, Query(description="Catalogue to query; defaults to the server's DEFAULT_SOURCE (all).")]
Category = Literal["character", "style", "concept", "clothing", "background", "tool", "base_model"]


class Filters(BaseModel):
    q: str = ""
    source: service.SourceOpt | None = None
    type: list[str] = []
    family: list[str] = []
    category: list[Category] = []
    generation: service.GenerationOpt = "allowed"
    sort: service.SortOpt = "relevance"
    limit: int = 20
    cursor: str | None = None


def filters(
    q: Annotated[str, Query(description="Keywords. Empty browses the catalogue instead.")] = "",
    source: Source = None,
    type: Annotated[list[str], Query(description="lora, checkpoint, lycoris, dora, ... (repeat or comma-separate)")] = [],
    family: Annotated[list[str], Query(description="Base-model family: illustrious, noobai, pony, anima, sdxl, sd15, flux, ...")] = [],
    category: Annotated[list[Category], Query(description="Matches projects tagged with any of these")] = [],
    generation: Annotated[service.GenerationOpt, Query(description="allowed: usable for online generation by anyone. blocked: the rest.")] = "allowed",
    sort: Annotated[service.SortOpt, Query(description="Keyword search honours relevance, trending and most_run.")] = "relevance",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(description="`next_cursor` from the previous page")] = None,
) -> Filters:
    return Filters(q=q, source=source, type=service.split_csv(type), family=service.split_csv(family), category=category,
                   generation=generation, sort=sort, limit=limit, cursor=cursor)


async def run_search(up: Upstream, f: Filters, category: list[str] | None = None) -> Page:
    return await service.search(
        up, q=f.q, source=f.source, types=f.type, families=f.family, categories=category or f.category,
        generation=f.generation, sort=f.sort, limit=f.limit, cursor=f.cursor,
    )


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/docs")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True, "version": __version__}


@app.get("/v1/facets", response_model=Facets, summary="Allowed filter values, for building pickers")
async def get_facets():
    return service.facets()


@app.get("/v1/search", response_model=Page, summary="Search or browse models")
async def search(f: Annotated[Filters, Depends(filters)], up: Annotated[Upstream, Depends(upstream)]):
    return await run_search(up, f)


@app.get("/v1/characters", response_model=Page, summary="Search character models")
async def characters(f: Annotated[Filters, Depends(filters)], up: Annotated[Upstream, Depends(upstream)]):
    return await run_search(up, f, ["character"])


@app.get("/v1/styles", response_model=Page, summary="Search style models")
async def styles(f: Annotated[Filters, Depends(filters)], up: Annotated[Upstream, Depends(upstream)]):
    return await run_search(up, f, ["style"])


@app.get("/v1/projects/{project_id}", response_model=ProjectDetail, summary="A model page with every version")
async def project(project_id: str, up: Annotated[Upstream, Depends(upstream)], source: Source = None):
    return await service.get_project(up, project_id, source)


@app.get("/v1/models/{model_id}", response_model=ModelVersion, summary="One model version: trigger words, base model, generation status")
async def model(model_id: str, up: Annotated[Upstream, Depends(upstream)], source: Source = None):
    return await service.get_model(up, model_id, source)


class CheckRequest(BaseModel):
    model_ids: list[str] = Field(min_length=1, max_length=100, description="Model (version) ids, as passed to generation APIs")
    source: service.SourceOpt | None = None


@app.post("/v1/models/check", response_model=list[GenerationCheck], summary="Check stored model ids are still usable for generation")
async def check(body: CheckRequest, up: Annotated[Upstream, Depends(upstream)]):
    return await service.check(up, body.model_ids, body.source)


# Account ---------------------------------------------------------------------


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    """Account endpoints act as you: when API_TOKEN is set they need `Authorization: Bearer <API_TOKEN>`."""
    token = os.environ.get("API_TOKEN", "").strip()
    if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
        raise UpstreamError("missing or wrong bearer token", 401)


def account(request: Request) -> Account:
    if not request.app.state.account:
        raise UpstreamError("no account configured: set TENSORART_SESSION", 503)
    return request.app.state.account


def uploads(request: Request, _: Annotated[Account, Depends(account)]) -> Uploads:
    return request.app.state.uploads


Authed = [Depends(require_token)]


@app.get("/v1/account", response_model=AccountInfo, dependencies=Authed, summary="Whose session this is and when it lapses")
async def get_account(a: Annotated[Account, Depends(account)]):
    return a.info()


@app.post("/v1/uploads", response_model=Upload, status_code=202, dependencies=Authed,
          summary="Copy a model file from a URL into your account as a private model")
async def create_upload(body: UploadRequest, u: Annotated[Uploads, Depends(uploads)]):
    return public(u.create(body.model_dump()))


@app.get("/v1/uploads", response_model=UploadList, dependencies=Authed, summary="Uploads with a given external_id")
async def list_uploads(u: Annotated[Uploads, Depends(uploads)], external_id: str):
    return {"items": [public(j) for j in u.find(external_id)]}


@app.get("/v1/uploads/{upload_id}", response_model=Upload, dependencies=Authed, summary="Progress and result of one upload")
async def get_upload(upload_id: str, u: Annotated[Uploads, Depends(uploads)]):
    if upload_id not in u.jobs:
        raise UpstreamError("upload not found", 404)
    return public(u.jobs[upload_id])
