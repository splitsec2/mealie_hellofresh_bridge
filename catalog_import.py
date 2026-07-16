"""Import HelloFresh public-catalog recipes matching a keyword into Mealie.

Companion to recipe_bridge.py. Where recipe_bridge imports your own order
history (and needs a HelloFresh token), this reads the public recipe sitemap
(no HelloFresh token needed), keeps the URLs whose slug matches a keyword,
dedupes to distinct dishes, and imports the ones you don't already have via
Mealie's URL scraper, applying one or more tags.

Only MEALIE_TOKEN is required (same env var as recipe_bridge):

    export MEALIE_TOKEN="Bearer xxx"
    python catalog_import.py --keyword salmon --mealie-url https://<mealie_server> \
        --tag Salmon --tag Pescatarian

Dedupe is by base slug (the recipe name with its trailing HelloFresh id and
any ?locale= query stripped), checked against everything already in Mealie, so
it's idempotent and resumable. Use --limit to import in batches; Mealie holds
memory per import, so for large keywords restart Mealie between batches.
"""
import argparse
import logging
import os
import re

import requests

from recipe_bridge import Mealie

DEFAULT_SITEMAP_TEMPLATE = "https://www.hellofresh.{country}/sitemap_recipe_pages.xml"
ID_SUFFIX = re.compile(r"-[0-9a-f]{24}$")
# Internal (non-consumer) catalog entries HelloFresh keeps around.
NOISE = re.compile(r"gap-fill|^\d{4}-w\d|^\d{3,4}-|premium-feast")

eligible_countries = [
    "at", "ch", "fr", "lu", "au", "de", "gb", "nl", "se",
    "be", "dk", "ie", "no", "us", "ca", "es", "it", "nz",
]


def base_slug(url):
    """Recipe slug with the ?query and trailing -<24hex> id removed."""
    path = url.split("?", 1)[0].rstrip("/")
    slug = path.split("/recipes/", 1)[-1]
    return ID_SUFFIX.sub("", slug)


def enumerate_sitemap(xml_text, keyword):
    """base_slug -> canonical (query-less) recipe URL for keyword matches.

    Deduping by base slug collapses per-week id variants and per-locale
    (?locale=...) duplicates onto a single canonical URL.
    """
    kw = keyword.lower()
    out = {}
    for loc in re.findall(r"<loc>(.*?)</loc>", xml_text):
        loc = loc.strip()
        base = base_slug(loc)
        if kw not in base.lower() or NOISE.search(base):
            continue
        out.setdefault(base, loc.split("?", 1)[0])
    return out


def resolve_tag(mealie, name):
    res = mealie.json_request(
        url=f"{mealie.base_url}/api/organizers/tags",
        method="get",
        headers=mealie.headers,
        params={"search": name},
    )
    for tag in res["items"]:
        if tag["name"].lower() == name.lower():
            return tag
    logging.info(f"Creating tag {name}")
    return mealie.json_request(
        url=f"{mealie.base_url}/api/organizers/tags",
        method="post",
        headers=mealie.headers,
        data={"name": name},
    )


def existing_base_slugs(mealie):
    """Base slugs of every recipe already in Mealie (the dedupe key)."""
    total = mealie.json_request(
        url=f"{mealie.base_url}/api/recipes",
        method="get",
        headers=mealie.headers,
        params={"perPage": 0},
    )["total"]
    res = mealie.json_request(
        url=f"{mealie.base_url}/api/recipes",
        method="get",
        headers=mealie.headers,
        params={"perPage": total},
    )
    return {base_slug(r["orgURL"]) for r in res["items"] if r.get("orgURL")}


def apply_tags(mealie, slug, tags):
    body = mealie.get_mealie_recipe(slug)
    have = body.get("tags") or []
    have_ids = {t.get("id") for t in have}
    for tag in tags:
        if tag["id"] not in have_ids:
            have.append(tag)
    body["tags"] = have
    mealie.json_request(
        url=f"{mealie.base_url}/api/recipes/{slug}",
        method="patch",
        headers=mealie.headers,
        data=body,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--keyword", "-k", required=True,
        help="Keyword that must appear in the recipe slug, e.g. salmon",
    )
    parser.add_argument(
        "--mealie-url", "-u", required=True,
        help="Base URL of your Mealie instance, e.g. https://mealie.example.com",
    )
    parser.add_argument(
        "--country", "-c", choices=eligible_countries, default="ca",
        help="HelloFresh country site whose sitemap to read (default: ca)",
    )
    parser.add_argument(
        "--tag", "-t", action="append", dest="tags",
        help="Tag to apply to imported recipes (repeatable). Default: the keyword, capitalised",
    )
    parser.add_argument(
        "--sitemap", default=None,
        help="Override the sitemap URL (default: derived from --country)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Import at most N new recipes this run (0 = all)",
    )
    parser.add_argument(
        "--dry-run", "-d", action="store_true",
        help="List what would be imported without writing anything",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    mealie_token = os.environ.get("MEALIE_TOKEN")
    if mealie_token is None:
        logging.error("Could not load required env var: MEALIE_TOKEN")
        exit(1)

    tags_wanted = args.tags or [args.keyword.capitalize()]
    sitemap = args.sitemap or DEFAULT_SITEMAP_TEMPLATE.format(
        country=args.country.lower()
    )
    mealie = Mealie(args.mealie_url.rstrip("/"), mealie_token)

    logging.info(f"Fetching sitemap {sitemap}")
    xml = requests.get(sitemap, timeout=30).text
    catalog = enumerate_sitemap(xml, args.keyword)
    logging.info(f"{len(catalog)} distinct '{args.keyword}' recipes in catalog")

    have = existing_base_slugs(mealie)
    new = [url for base, url in sorted(catalog.items()) if base not in have]
    logging.info(f"{len(new)} not yet in Mealie")
    if args.limit and len(new) > args.limit:
        new = new[: args.limit]
        logging.info(f"Limiting to {args.limit} this run")

    if args.dry_run:
        logging.info(f"[dry-run] would import {len(new)} recipe(s)")
        for url in new[:10]:
            logging.info(f"  {url}")
        return

    if not new:
        logging.info("Nothing new to import.")
        return

    tags = [resolve_tag(mealie, tag_name) for tag_name in tags_wanted]
    imported = 0
    for url in new:
        slug = mealie.add_mealie_recipe(url)
        apply_tags(mealie, slug, tags)
        imported += 1
    logging.info(f"Imported {imported} recipe(s).")


if __name__ == "__main__":
    main()
