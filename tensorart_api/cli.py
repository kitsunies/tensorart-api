"""Command line: the same queries as the HTTP API, run in-process (no server needed)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from . import __version__, service
from .upstream import Upstream, UpstreamError


def compact(n: int) -> str:
    for unit, size in (("m", 1_000_000), ("k", 1_000)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(n)


def gen_label(g) -> str:
    return "ok" if g.allowed else f"blocked:{g.reason or 'unavailable'}"


def print_page(page) -> None:
    for p in page.items:
        cats = f" [{','.join(p.categories)}]" if p.categories else ""
        print(f"{p.id}  model={p.model_id}  {p.family}/{p.type}  {p.name}{cats}  "
              f"{compact(p.stats.runs)} runs  {p.source}  {gen_label(p.generation)}")
    if not page.items:
        print("no results")
    if page.total is not None:
        print(f"-- {page.total} upstream matches", file=sys.stderr)
    if page.next_cursor:
        print(f"-- more: --cursor {page.next_cursor}", file=sys.stderr)


def print_version(v, indent: str = "") -> None:
    print(f"{indent}{v.id}  {v.name}  {v.family}/{v.type}  {gen_label(v.generation)}")
    if v.trigger_words:
        print(f"{indent}  triggers: {', '.join(v.trigger_words)}")


def print_project(p) -> None:
    print(f"{p.name}\n  {p.url}\n  {p.family}/{p.type}  base={p.base_model}  {p.source}  {gen_label(p.generation)}")
    if p.categories or p.tags:
        print(f"  categories: {','.join(p.categories) or '-'}  tags: {', '.join(p.tags) or '-'}")
    print(f"  by {p.owner.name}  {compact(p.stats.runs)} runs  {compact(p.stats.likes)} likes")
    print("  versions:")
    for v in p.versions:
        print_version(v, "    ")


def print_checks(checks) -> None:
    for c in checks:
        name = f"  {c.name} ({c.family})" if c.found and c.name else ""
        print(f"{c.model_id}  {'found' if c.found else 'missing'}  {gen_label(c.generation)}{name}")


def add_filters(p: argparse.ArgumentParser, category: bool = True) -> None:
    p.add_argument("q", nargs="?", default="", help="keywords; omit to browse")
    p.add_argument("-t", "--type", action="append", default=[], help="lora, checkpoint, ... (repeat or comma-separate)")
    p.add_argument("-f", "--family", action="append", default=[], help="illustrious, noobai, pony, anima, sdxl, ...")
    if category:
        p.add_argument("-c", "--category", action="append", default=[], help="character, style, concept, ...")
    p.add_argument("-g", "--generation", choices=["allowed", "blocked", "any"], default="allowed")
    p.add_argument("--sort", choices=list(service.SORTS), default="relevance")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("--cursor")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tensorart", description="Search the Tensor.Art model catalogue.")
    ap.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-s", "--source", choices=["tensorart", "tensorhub", "all"],
                        help=f"catalogue to query (default: {service.DEFAULT_SOURCE})")
    common.add_argument("--json", action="store_true", help="print JSON instead of text")
    sub = ap.add_subparsers(dest="cmd", required=True)

    add_filters(sub.add_parser("search", parents=[common], help="search or browse models"))
    add_filters(sub.add_parser("characters", parents=[common], help="search character models"), category=False)
    add_filters(sub.add_parser("styles", parents=[common], help="search style models"), category=False)
    sub.add_parser("project", parents=[common], help="a model page with every version").add_argument("id")
    sub.add_parser("model", parents=[common], help="one model version").add_argument("id")
    sub.add_parser("check", parents=[common], help="check model ids are usable for generation").add_argument("ids", nargs="+")
    sub.add_parser("facets", parents=[common], help="allowed filter values")
    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    return ap


async def run(args) -> object:
    if args.cmd == "facets":
        return service.facets()
    up = Upstream()
    try:
        if args.cmd in ("search", "characters", "styles"):
            category = {"characters": ["character"], "styles": ["style"]}.get(args.cmd) or service.split_csv(getattr(args, "category", []))
            return await service.search(
                up, q=args.q, source=args.source, types=service.split_csv(args.type), families=service.split_csv(args.family),
                categories=category, generation=args.generation, sort=args.sort, limit=args.limit, cursor=args.cursor,
            )
        if args.cmd == "project":
            return await service.get_project(up, args.id, args.source)
        if args.cmd == "model":
            return await service.get_model(up, args.id, args.source)
        if args.cmd == "check":
            return await service.check(up, args.ids, args.source)
    finally:
        await up.aclose()
    raise AssertionError(args.cmd)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("tensorart_api.app:app", host=args.host, port=args.port, proxy_headers=True)
        return 0
    try:
        result = asyncio.run(run(args))
    except UpstreamError as e:
        print(f"error: {e.message}", file=sys.stderr)
        return 1 if e.status >= 500 else 2
    if args.json:
        data = [r.model_dump() for r in result] if isinstance(result, list) else result.model_dump()
        print(json.dumps(data, indent=2, ensure_ascii=False))
    elif args.cmd in ("search", "characters", "styles"):
        print_page(result)
    elif args.cmd == "project":
        print_project(result)
    elif args.cmd == "model":
        print_version(result)
    elif args.cmd == "check":
        print_checks(result)
    else:
        for k, v in result.model_dump().items():
            print(f"{k}: {', '.join(v)}")
    if args.cmd == "check" and not all(c.generation.allowed for c in result):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
