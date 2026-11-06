# tensorart-api

A live, cached JSON API and CLI over the [Tensor.Art](https://tensor.art) model catalogue.
It searches LoRAs and checkpoints, sorts them into characters, styles and base-model families,
and tells you whether each model can actually be used for online generation.

- **No account needed.** It signs requests the same way the website does, using a key you supply.
- **Two catalogues, one switch.** `tensor.art` and `tensorhub.art` serve different catalogues with
  little overlap. Query one (`source=tensorart` / `source=tensorhub`) or both (`source=all`).
- **Generation switch.** `generation=allowed` (the default) hides models that are owner-only,
  have online generation turned off, or cannot run online. `blocked` shows only those, and `any` shows everything.
- **Normalised output.** One schema across catalogues, with exact cursor paging and filters for
  type, base-model family and category, plus trigger words on every version.

## Quick start

```bash
pip install git+https://github.com/kitsunies/tensorart-api
cp .env.example .env       # then fill in TENSORART_SIGN_KEY
tensorart characters "rebecca" --family illustrious
tensorart serve            # HTTP API on :8000, docs at /docs
```

Or with Docker:

```bash
docker run -p 8000:8000 --env-file .env ghcr.io/kitsunies/tensorart-api
```

## CLI

Every command runs in-process with no server, and takes `--json` for machine-readable output.

```bash
tensorart search "kimono" --type lora --family noobai,illustrious --sort most_run
tensorart search --category style --source tensorhub          # browse (no keywords)
tensorart characters "tifa" -n 5
tensorart styles "watercolor" --generation any
tensorart project 1063432615341616924                         # model page + versions
tensorart model 869608418527192240                            # one version: trigger words, base model
tensorart check 869608418527192240 1000000000000000001        # exit code 3 if any are blocked
tensorart facets                                              # every allowed filter value
```

Text output prints one line per result: `project id`, `model=` version id, `family/type`, name,
categories, runs, catalogue and generation status. Paging hints go to stderr (`-- more: --cursor ...`).

Exit codes: `0` ok, `1` upstream failure, `2` not found or bad input, `3` `check` found a blocked model.

## HTTP API

Interactive docs: `/docs`. OpenAPI schema: `/openapi.json`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/search` | Keyword search, or browse when `q` is empty |
| GET | `/v1/characters` | `/v1/search` limited to character models |
| GET | `/v1/styles` | `/v1/search` limited to style models |
| GET | `/v1/projects/{id}` | A model page with every version |
| GET | `/v1/models/{id}` | One version: trigger words, base model, generation status |
| POST | `/v1/models/check` | Bulk-check stored version ids are still usable |
| GET | `/v1/facets` | Allowed filter values, for building pickers |

### Search parameters

| Param | Values | Default |
|---|---|---|
| `q` | keywords; empty browses | |
| `source` | `tensorart`, `tensorhub`, `all` | `DEFAULT_SOURCE` |
| `type` | `lora`, `checkpoint`, `lycoris`, `dora`, ... | any |
| `family` | `illustrious`, `noobai`, `pony`, `anima`, `sdxl`, `sd15`, `flux`, ... | any |
| `category` | `character`, `style`, `concept`, `clothing`, `background`, `tool`, `base_model` | any |
| `generation` | `allowed`, `blocked`, `any` | `allowed` |
| `sort` | `relevance`, `trending`, `newest`, `most_run`, `most_downloaded` | `relevance` |
| `limit` | 1-100 | 20 |
| `cursor` | `next_cursor` from the previous page | |

List filters accept repeats (`type=lora&type=dora`) or commas (`type=lora,dora`).
Keyword search honours the `relevance`, `trending` and `most_run` sorts; browsing honours all of them.

```bash
curl 'localhost:8000/v1/characters?q=rebecca&source=tensorhub&family=anima&limit=2'
```

```json
{
  "items": [
    {
      "id": "1063432615341616924",
      "name": "Rebecca - Cyberpunk Edgerunners",
      "url": "https://tensorhub.art/models/1063432615341616924",
      "source": "tensorhub",
      "type": "lora",
      "family": "anima",
      "base_model": "Anima",
      "categories": ["character"],
      "tags": ["character", "anime"],
      "model_id": "1063432701240963191",
      "model_name": "Rebecca AN",
      "cover_url": "https://image.tensorartassets.com/...",
      "owner": {"id": "...", "name": "...", "avatar_url": "..."},
      "stats": {"runs": 0, "likes": 0, "downloads": 0},
      "generation": {"allowed": true, "reason": null}
    }
  ],
  "next_cursor": "eyJ0ZW5zb3JodWIiOlswLDJdfQ",
  "total": 3
}
```

`model_id` is the latest version, which is the id that generation APIs (such as TAMS) expect.
`generation.reason` is one of `owner_only`, `online_generation_disabled` or `run_unsupported`.

### Validating stored models

Apps that keep a list of LoRA or checkpoint ids can catch models that were made private or
had generation disabled before a job fails:

```bash
curl -X POST localhost:8000/v1/models/check \
  -H 'content-type: application/json' \
  -d '{"model_ids": ["869608418527192240", "1000000000000000001"]}'
```

```json
[
  {"model_id": "869608418527192240", "found": true, "source": "tensorart", "name": "Illustrious",
   "family": "illustrious", "trigger_words": ["strapless bra", "black hairband", "..."],
   "generation": {"allowed": true, "reason": null}},
  {"model_id": "1000000000000000001", "found": true, "source": null, "name": null,
   "family": null, "trigger_words": [], "generation": {"allowed": false, "reason": "owner_only"}}
]
```

## Building a character or style picker

1. `GET /v1/facets` to fill the family, type and category dropdowns.
2. `GET /v1/characters?q=<typed text>&source=<your catalogue>&family=<your base model>` as the user types.
   Show `name`, `cover_url` and `stats.runs`; follow `next_cursor` for infinite scroll.
3. On selection, `GET /v1/models/{model_id}` to get `trigger_words` for the prompt.
4. Periodically `POST /v1/models/check` with every saved id, and flag any that come back blocked.

Pin `source` to the catalogue your models are tuned for. The two catalogues hold different
models, so `all` gives the widest sweep and a single source gives the most consistent results.

## Configuration

Copy `.env.example` to `.env` (git-ignored); the CLI and server read it from the working directory.
Real environment variables take precedence.

| Env var | Default | Purpose |
|---|---|---|
| `DEFAULT_SOURCE` | `all` | Catalogue used when a request omits `source` |
| `CACHE_TTL` | `300` | Seconds to cache upstream responses |
| `UPSTREAM_CONCURRENCY` | `8` | Max concurrent upstream requests |
| `MAX_UPSTREAM_PAGES` | `5` | Upstream pages scanned per source per request, which bounds sparse filters |
| `TENSORART_SIGN_KEY` | required | Request-signing key (tensor.art, and tensorhub.art unless overridden) |
| `TENSORHUB_SIGN_KEY` | `TENSORART_SIGN_KEY` | Separate request-signing key for tensorhub.art |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | `tensorart serve` bind address |

## How it works

The website's frontend calls a JSON API and signs every request with
`base64(hex(HMAC-SHA256(key, path + sorted query + timestamp)))`. This project sends the same
signed requests from plain HTTP with the key you configure, normalises the responses and caches
them in memory. If requests fail with `rejected the request signature`, the configured key is no
longer accepted: update `TENSORART_SIGN_KEY` / `TENSORHUB_SIGN_KEY`.

Categories come from the catalogue's own tags, so untagged models only match category filters
once their authors tag them. Search by name to find those.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Tests run against an in-memory fake of the catalogue and never touch the network.

## License

MIT. Not affiliated with Tensor.Art. Respect the site's terms and the model authors' licences.
