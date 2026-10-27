#!/usr/bin/env python3
"""Copy every memory from one Hindsight bank into another (operator tool).

Used once for the hermes -> ceo bank rename (docs/HINDSIGHT-BANKS.md). Read-only
on the source bank; safe to re-run (re-retained items use a stable document_id,
so Hindsight upserts instead of duplicating). The API key comes from the
HINDSIGHT_API_KEY environment variable and is never printed.

    HINDSIGHT_URL=http://hindsight:8888 HINDSIGHT_API_KEY=... \
        python3 scripts/hindsight-copy-bank.py hermes ceo [--dry-run]
"""
import json
import os
import sys
import urllib.request

PAGE = 100


def call(url, key, path, body=None):
    """GET when body is None (list endpoint), POST otherwise (retain)."""
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        return json.loads(resp.read().decode() or "{}")


def copy_bank(url, key, src, dst, dry_run=False):
    offset, copied = 0, 0
    while True:
        page = call(url, key, f"/v1/default/banks/{src}/memories/list?limit={PAGE}&offset={offset}&state=valid")
        items = page.get("items") or []
        if not items:
            return copied
        batch = []
        for m in items:
            text = m.get("text")
            if not text:
                continue
            batch.append(
                {
                    "content": text,
                    "context": m.get("context") or f"copied from bank {src}",
                    "tags": list(m.get("tags") or []),
                    "document_id": f"copy-{src}-{m.get('id')}",
                }
            )
        if batch and not dry_run:
            call(url, key, f"/v1/default/banks/{dst}/memories", {"items": batch, "async": True})
        copied += len(batch)
        offset += len(items)
        if len(items) < PAGE:
            return copied


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__)
        return 2
    key = os.environ.get("HINDSIGHT_API_KEY")
    if not key:
        print("HINDSIGHT_API_KEY is required", file=sys.stderr)
        return 2
    url = os.environ.get("HINDSIGHT_URL", "http://hindsight:8888")
    n = copy_bank(url, key, args[0], args[1], dry_run="--dry-run" in argv)
    print(f"{'would copy' if '--dry-run' in argv else 'copied'} {n} memories {args[0]} -> {args[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
