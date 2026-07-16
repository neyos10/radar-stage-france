#!/usr/bin/env python3
"""Reconstitue un baseline lisible depuis un ``source_status.json`` existant."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from radar_io import atomic_write_json


COUNT_FIELDS = (
    "network_request_count",
    "pages_fetched",
    "raw_count",
    "accepted_count",
    "rejected_count",
    "duplicate_count",
    "timeout_count",
    "blocked_count",
    "detail_request_count",
)


def aggregate_status(payload: dict, *, offers: Path, seen: Path) -> dict:
    sources: dict[str, dict] = defaultdict(
        lambda: {field: 0 for field in COUNT_FIELDS}
    )
    unavailable_fields: set[str] = set()
    for status in payload.get("sources", []):
        source = sources[str(status.get("source") or "inconnue")]
        source["duration_s"] = round(
            float(source.get("duration_s", 0))
            + float(status.get("duration_s") or 0),
            1,
        )
        for field in COUNT_FIELDS:
            if field in status:
                source[field] += int(status.get(field) or 0)
            else:
                unavailable_fields.add(field)
        error_text = f"{status.get('http_error', '')} {status.get('note', '')}"
        source["http_403_429_count"] = int(
            source.get("http_403_429_count", 0)
        ) + int("403" in error_text or "429" in error_text)

    totals: dict[str, int | None] = {}
    for field in COUNT_FIELDS:
        totals[field] = (
            None
            if field in unavailable_fields
            else sum(int(source[field]) for source in sources.values())
        )
    totals["http_403_429_count"] = sum(
        int(source.get("http_403_429_count", 0))
        for source in sources.values()
    )

    for source in sources.values():
        for field in unavailable_fields:
            source[field] = None

    active_domains = payload.get("active_domains", [])
    active_regions = payload.get("active_regions", [])
    return {
        "schema_version": 1,
        "measurement_type": "reconstructed_from_status",
        "profile_id": "legacy_full_reference",
        "observed_at": payload.get("generated_at"),
        "duration_total_s": payload.get("scan_duration_s"),
        "active_domains": active_domains,
        "active_regions": active_regions,
        "domain_count": len(active_domains),
        "region_count": len(active_regions),
        "source_count": len(sources),
        "query_count": payload.get("query_count"),
        "combination_count": payload.get("combination_count"),
        "totals": totals,
        "sources": [
            {"source": name, **metrics}
            for name, metrics in sorted(sources.items())
        ],
        "artifacts": {
            "offers_json_bytes": offers.stat().st_size if offers.exists() else None,
            "seen_offers_json_bytes": seen.stat().st_size if seen.exists() else None,
        },
        "limitations": [
            "Les champs absents de l'ancien schéma SourceStatus valent null.",
            "Les durées par source se chevauchent car les sources étaient parallèles.",
            "Ce fichier reconstitue un run historique ; ce n'est pas un nouveau test réseau.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=Path, default=Path("data/source_status.json"))
    parser.add_argument("--offers", type=Path, default=Path("data/offers.json"))
    parser.add_argument("--seen", type=Path, default=Path("data/seen_offers.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    payload = json.loads(args.status.read_text(encoding="utf-8"))
    baseline = aggregate_status(payload, offers=args.offers, seen=args.seen)
    if args.output:
        atomic_write_json(args.output, baseline)
    else:
        print(json.dumps(baseline, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
