#!/usr/bin/env python3
"""
Diagnostic probe for the HardcoverSource 0-books bug.

Manually fires three GraphQL queries against Hardcover's API and
reports what comes back, so we can see exactly where the existing
`search_author` path is dropping books for Jim Butcher (whose
hardcover.app/authors/jim-butcher page shows 146 books but our
source returns 0).

Run inside the Seshat container so the API key is reachable:
    docker exec Seshat python /app/scripts/probe_hardcover.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402

from app.secrets import get_secret  # noqa: E402

API = "https://api.hardcover.app/v1/graphql"

# Three queries to compare:
#
# (A) Same SEARCH_QUERY the production source uses.
SEARCH_QUERY = """
query Search($query: String!) {
  search(query: $query, query_type: "Book", per_page: 50) {
    ids
    results
  }
}
"""

# (B) Author-search variant — returns Hardcover author records by name.
SEARCH_AUTHOR_QUERY = """
query SearchAuthor($query: String!) {
  search(query: $query, query_type: "Author", per_page: 20) {
    ids
    results
  }
}
"""

# (C) Direct author-books query — the AUTHOR_BOOKS_QUERY in
# hardcover.py that's defined but never called. Returns the whole
# bibliography in one GraphQL round-trip when we already know the
# author_id.
# (D) Production's actual FIND_BOOKS_BY_IDS — what does
# `contributions` look like on a real book detail page?
FIND_BOOKS_BY_IDS = """
query FindBooksByIds($ids: [Int!]) {
  books(where: {id: {_in: $ids}}) {
    id
    title
    contributions { author { id name } }
  }
}
"""


# Schema introspection: list all fields on the `authors` type so
# we can find the correct name for the book-relation field.
INTROSPECT_AUTHOR = """
query IntrospectAuthor {
  __type(name: "authors") {
    name
    fields {
      name
      type { name kind ofType { name kind } }
    }
  }
}
"""

# Once we know the correct relation name, this is the shape we want.
# Filled in dynamically based on introspection results.
AUTHOR_BOOKS_QUERY = """
query AuthorBooks($id: Int!) {
  authors(where: {id: {_eq: $id}}) {
    id
    name
    bio
    books_count
    contributions(order_by: {book: {release_date: asc}}) {
      book {
        id
        title
        slug
        cached_featured_series
        contributions { author { id name } }
      }
    }
  }
}
"""


async def _post(client: httpx.AsyncClient, query: str, variables: dict) -> dict:
    resp = await client.post(API, json={"query": query, "variables": variables})
    resp.raise_for_status()
    return resp.json()


async def main():
    api_key = await get_secret("hardcover_api_key")
    if not api_key:
        print("FATAL: no hardcover_api_key in secrets store")
        return 1

    token = api_key.strip()
    if " " not in token:
        token = f"Bearer {token}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": token,
        "User-Agent": "Seshat-probe/1.0",
    }

    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        # ── (A) Bare-name book search — current production behavior ──
        print("=" * 70)
        print("(A) SEARCH_QUERY 'Jim Butcher' (current production path)")
        print("=" * 70)
        a = await _post(client, SEARCH_QUERY, {"query": "Jim Butcher"})
        a_search = (a.get("data") or {}).get("search") or {}
        a_ids = a_search.get("ids") or []
        print(f"   ids returned: {len(a_ids)} (first 10: {a_ids[:10]})")
        # `results` may be a JSON-encoded string OR an already-parsed dict
        a_results = a_search.get("results")
        print(f"   results type: {type(a_results).__name__}")
        if isinstance(a_results, str):
            try:
                a_results = json.loads(a_results)
            except (ValueError, TypeError) as e:
                print(f"   results parse failed: {e}")
                a_results = None
        if isinstance(a_results, dict):
            hits = a_results.get("hits", [])
            print(f"   results.hits: {len(hits)}")
            if hits:
                sample = hits[0].get("document", {}) if isinstance(hits[0], dict) else {}
                print(f"   first-hit keys (15 of {len(sample)}): {sorted(sample.keys())[:15]}")
                # Try every contributor-shaped field we can think of
                for key in ("contributions", "author_names", "authors",
                            "cached_contributors", "primary_contributor"):
                    if key in sample:
                        val = sample[key]
                        preview = str(val)[:200]
                        print(f"   first-hit.{key}: {preview}")

        # ── (A2) Fetch actual book details for the search-hit IDs ──
        if a_ids:
            print()
            print(f"   --- fetching detail for first 5 search hits ---")
            sample_ids = [int(x) for x in a_ids[:5]]
            d = await _post(client, FIND_BOOKS_BY_IDS, {"ids": sample_ids})
            books = (d.get("data") or {}).get("books") or []
            print(f"   detail call returned {len(books)} books")
            for b in books:
                contribs = b.get("contributions") or []
                names = [
                    (c.get("author", {}) or {}).get("name") for c in contribs
                ]
                print(f"   book id={b.get('id')} title={b.get('title')!r:<50} contributors={names}")

        # ── (B) Direct author search ──
        print()
        print("=" * 70)
        print("(B) SEARCH_AUTHOR_QUERY 'Jim Butcher'")
        print("=" * 70)
        b = await _post(client, SEARCH_AUTHOR_QUERY, {"query": "Jim Butcher"})
        b_search = (b.get("data") or {}).get("search") or {}
        b_ids = b_search.get("ids") or []
        print(f"   author ids returned: {len(b_ids)} (first 5: {b_ids[:5]})")
        b_results = b_search.get("results")
        if isinstance(b_results, str):
            try:
                parsed = json.loads(b_results)
                hits = parsed.get("hits", []) if isinstance(parsed, dict) else []
                print(f"   results.hits: {len(hits)}")
                for i, h in enumerate(hits[:5]):
                    if isinstance(h, dict):
                        doc = h.get("document", {})
                        print(f"   hit[{i}]: id={doc.get('id')} "
                              f"name={doc.get('name')!r} "
                              f"books_count={doc.get('books_count')}")
            except (ValueError, TypeError) as e:
                print(f"   results parse failed: {e}")

        # ── (B2) Schema introspection on `authors` type ──
        print()
        print("=" * 70)
        print("(B2) INTROSPECT `authors` type to find book-relation field")
        print("=" * 70)
        intro = await _post(client, INTROSPECT_AUTHOR, {})
        author_type = (intro.get("data") or {}).get("__type") or {}
        fields = author_type.get("fields") or []
        # Print only relevant book-ish fields so the output is readable.
        for f in fields:
            name = f.get("name", "")
            if any(s in name.lower() for s in ("book", "contrib", "work")):
                t = f.get("type", {})
                kind = t.get("kind")
                tname = t.get("name") or (t.get("ofType") or {}).get("name")
                print(f"   .{name:<35} {kind}/{tname}")

        # ── (C) Author-books direct query — the AUTHOR_BOOKS_QUERY
        # that's defined but unused in hardcover.py ──
        print()
        print("=" * 70)
        print("(C) AUTHOR_BOOKS_QUERY (unused but defined; the supposed fix)")
        print("=" * 70)
        if b_ids:
            butcher_id = int(b_ids[0])
            print(f"   querying author id={butcher_id}")
            c = await _post(client, AUTHOR_BOOKS_QUERY, {"id": butcher_id})
            authors = (c.get("data") or {}).get("authors") or []
            if authors:
                author = authors[0]
                ba = author.get("contributions") or []
                print(f"   author name: {author.get('name')!r}")
                print(f"   bio length: {len(author.get('bio') or '')}")
                print(f"   books_count field: {author.get('books_count')}")
                print(f"   contributions rows returned: {len(ba)}")
                # First 5 + last 5 books
                sample_titles = []
                for entry in ba[:5]:
                    book = entry.get("book", {})
                    sample_titles.append(book.get("title", "?"))
                print(f"   first 5 titles: {sample_titles}")
                tail = []
                for entry in ba[-5:]:
                    book = entry.get("book", {})
                    tail.append(book.get("title", "?"))
                print(f"   last 5 titles: {tail}")
                # Check contributions structure for the first book
                if ba:
                    first_book = ba[0].get("book", {})
                    contribs = first_book.get("contributions", [])
                    print(f"   first book contributions: {contribs[:3]}")
            else:
                print(f"   NO author records returned for id={butcher_id}")
                print(f"   raw: {json.dumps(c, indent=2)[:500]}")
        else:
            print("   SKIPPED — no author id from query (B)")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
