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
`generation.reason` is one of `owner_only`, `online_generation_disabled`, `run_unsupported` or `no_commercial_use`
(the license forbids commercial use, so commercial generation APIs such as TAMS refuse the model).

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

## Your account: private uploads

Optional. Give the server your website session and it can also act as your account:

- **A session that renews itself.** `TENSORART_SESSION` is the site's login token (the
  `ta_token_prod` cookie). It lasts 30 days; the server swaps it for a fresh one every day and
  keeps the newest in `STATE_DIR`, so it never lapses while the server keeps running. Changing
  your password, logging out everywhere or removing the device ends it.
- **Private uploads from a URL.** `POST /v1/uploads` copies a model file from any URL that
  supports range requests into your account as a private model, marked as a reprint of
  `source_url`. It answers at once and copies in the background; poll `GET /v1/uploads/{id}`.
  The finished upload carries the `model_id` and `access_key` that generation APIs take.
- **Resumable and idempotent.** Files go up in 10 MB parts, straight from the source to the
  model bucket with nothing on disk. A restart resumes from the last finished part, and the
  same `external_id` never uploads twice (a failed upload restarts when you post it again).

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/account` | Whose session this is and when it lapses |
| POST | `/v1/uploads` | Start copying a file into your account (202) |
| GET | `/v1/uploads/{id}` | State (`queued`, `copying`, `publishing`, `ready`, `failed`), progress and result |
| GET | `/v1/uploads?external_id=` | Look an upload up by your own id |

When `API_TOKEN` is set, these endpoints need `Authorization: Bearer <API_TOKEN>`. Set it
whenever the server is reachable by anyone but you: they act as your account.

```bash
curl -X POST localhost:8000/v1/uploads \
  -H "authorization: Bearer $API_TOKEN" -H 'content-type: application/json' \
  -d '{"url": "https://example.com/files/watercolor.safetensors", "name": "Watercolor",
       "type": "lora", "base_model": "SDXL 1.0", "cover_url": "https://example.com/files/watercolor.jpg",
       "trigger_words": ["watercolor style"], "source_url": "https://example.com/watercolor",
       "external_id": "watercolor-v1"}'
```

```json
{"id": "ed9dc79fb0254c8e", "external_id": "watercolor-v1", "state": "ready", "progress": 1.0,
 "bytes_done": 11860808, "bytes_total": 11860808, "project_id": "1063819074351423505",
 "model_id": "1063819077572640710", "access_key": "...", "cover_url": "https://image.tensorartassets.com/...",
 "error": null, "created_at": "...", "updated_at": "..."}
```

`cover_url` is required: the catalogue needs a showcase image and copies it. `type` is `lora`
(also `locon`, `lycoris`, `dora`) or `checkpoint`; `base_model` uses the catalogue's own labels.

## Your account: LoRA training

With a session configured, the server can also train a LoRA from your own images on the
site's online trainer and publish the epoch you choose as a private model.

1. `POST /v1/trainings` `{"name": "Ink Wash", "kind": "style"}` (or `"character"`) creates a draft.
2. `POST /v1/trainings/{id}/images` (multipart, repeatable `files`: jpg, png or webp, up to 20 MB) uploads and
   auto-tags each image with the Danbooru-style `wd-v1-4-vit-tagger-v2`. Call it as many times as you like.
3. `POST /v1/trainings/{id}/start` applies the preset for the kind and starts the run. Training spends the
   site's credits; `credits` on the training shows the cost, and a shortfall answers 402.
4. Poll `GET /v1/trainings/{id}`: `queued`, `training` (with `progress`), then `trained` once every saved
   epoch has sample images. Sample links expire within the hour; re-read the training for fresh ones.
5. `POST /v1/trainings/{id}/publish` `{"epoch": 6}` publishes that epoch privately; the training ends
   `published` with the same `model_id` and `access_key` an upload gets.

Presets: Illustrious v0.1 base (its LoRAs carry over to most Illustrious fine-tunes), rank 32, 10 epochs
with one saved every 2, and repeats scaled so each epoch sees about 300 images. As a guide, 20 to 40
varied images make a good character and 50 to 150 a good style.

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
| `TENSORART_SESSION` | | Your site login token; enables the account endpoints |
| `STATE_DIR` | `./state` | Where the renewed session and upload progress live (keep it on a volume) |
| `API_TOKEN` | | Bearer token required by the account endpoints |
| `ACCOUNT_SOURCE` | `tensorhub` | Which site the account calls go to |
| `SESSION_RENEW_HOURS` | `24` | How often the session is renewed |
| `UPLOAD_CONCURRENCY` / `UPLOAD_PART_CONCURRENCY` | `2` / `4` | Uploads at once / parts in flight per upload |
| `TRAIN_SOURCE` | `tensorart` | Which site runs trainings (each site has its own credits) |
| `TRAIN_UPLOAD_CONCURRENCY` | `4` | Training images uploaded at once |
| `ACCOUNT_TIMEOUT` | `60` | Seconds per account call |

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
