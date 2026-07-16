"""Persistance profile-aware des offres et de la mémoire ``seen``.

Le module est volontairement indépendant de :mod:`scan`.  Il ne réalise
aucune entrée/sortie et expose uniquement des transformations déterministes ;
le point d'entrée choisit ensuite quand écrire les documents atomiquement.

Deux principes guident la réconciliation :

* une absence n'est une preuve que lorsque le profil couvre entièrement
  l'offre et que tous ses connecteurs pertinents ont terminé sainement ;
* une offre n'est jamais masquée après une seule absence. Elle devient
  ``likely_expired`` après le délai ET le nombre d'absences configurés.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


OFFER_STORE_SCHEMA_VERSION = 2
SEEN_SCHEMA_VERSION = 2
DEFAULT_EXPIRATION_HOURS = 72
DEFAULT_SUCCESSFUL_ABSENCES = 3
DEFAULT_SEEN_RETENTION_DAYS = 45
DEFAULT_OFFER_RETENTION_DAYS = 14
RETIRED_SOURCES = frozenset({"Glassdoor"})
RELIABLE_SOURCE_STATUSES = frozenset({"success", "empty"})


def _utc(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat(timespec="seconds")


def parse_store_datetime(value: Any) -> datetime | None:
    """Parse un horodatage du store et le normalise en UTC."""

    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): deepcopy(item) for key, item in value.items()}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, Sequence):
        values = value
    else:
        return []
    return list(dict.fromkeys(
        item.strip() for item in values
        if isinstance(item, str) and item.strip()
    ))


def offer_sources(offer: Mapping[str, Any]) -> tuple[str, ...]:
    """Retourne toutes les sources connues d'une offre, sans doublon."""

    sources = _string_list(offer.get("sources"))
    primary = offer.get("source")
    if isinstance(primary, str) and primary.strip() and primary not in sources:
        sources.insert(0, primary.strip())
    return tuple(sources)


def _normalize_timestamp_mapping(value: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, raw in _mapping(value).items():
        parsed = parse_store_datetime(raw)
        if parsed is not None:
            result[key] = _iso(parsed)
    return result


def _normalize_count_mapping(value: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for key, raw in _mapping(value).items():
        if isinstance(raw, bool):
            continue
        try:
            count = int(raw)
        except (TypeError, ValueError):
            continue
        result[key] = max(0, count)
    return result


def _entry_timestamp(entry: Mapping[str, Any], fallback: datetime) -> datetime:
    for field in (
        "last_seen_at", "last_seen_run", "last_seen_success", "discovered_at",
        "first_seen", "sort_date", "date",
    ):
        parsed = parse_store_datetime(entry.get(field))
        if parsed is not None:
            return parsed
    return fallback


def _is_pure_retired_offer(
    offer: Mapping[str, Any], retired_sources: frozenset[str] = RETIRED_SOURCES,
) -> bool:
    sources = offer_sources(offer)
    return bool(sources) and all(source in retired_sources for source in sources)


def migrate_offer_store(
    payload: Mapping[str, Any] | None,
    *,
    run_at: datetime | None = None,
    retired_sources: Iterable[str] = RETIRED_SOURCES,
) -> dict[str, dict[str, Any]]:
    """Migre un ancien ``offers.json`` sans réindexer ni supprimer ses IDs.

    Le format historique (objet ``id -> offre``) reste le format de sortie.
    Un document versionné ``{"schema_version": 2, "offers": {...}}`` est
    également accepté afin qu'une migration future reste non bloquante.
    """

    now = _utc(run_at)
    now_iso = _iso(now)
    retired = frozenset(str(source) for source in retired_sources)
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise ValueError("offers.json doit contenir un objet JSON")
    raw_store: Any = payload.get("offers") if (
        "offers" in payload and isinstance(payload.get("offers"), Mapping)
        and "schema_version" in payload
    ) else payload
    if not isinstance(raw_store, Mapping):
        raise ValueError("offers.json.offers doit contenir un objet JSON")

    migrated: dict[str, dict[str, Any]] = {}
    for raw_id, raw_offer in raw_store.items():
        if not isinstance(raw_offer, Mapping):
            continue
        offer_id = str(raw_id)
        entry = deepcopy(dict(raw_offer))
        try:
            old_schema = int(entry.get("store_schema_version") or 1)
        except (TypeError, ValueError):
            old_schema = 1
        embedded_id = str(entry.get("id") or offer_id)
        legacy_ids = _string_list(entry.get("legacy_ids"))
        if embedded_id != offer_id and embedded_id not in legacy_ids:
            legacy_ids.append(embedded_id)
        entry["id"] = offer_id
        if legacy_ids:
            entry["legacy_ids"] = legacy_ids

        timestamp = _entry_timestamp(entry, now)
        first_seen = (
            parse_store_datetime(entry.get("first_seen"))
            or parse_store_datetime(entry.get("discovered_at"))
            or timestamp
        )
        last_seen = (
            parse_store_datetime(entry.get("last_seen_at"))
            or parse_store_datetime(entry.get("last_seen_run"))
            or parse_store_datetime(entry.get("last_seen_success"))
            or timestamp
        )
        entry["first_seen"] = _iso(first_seen)
        entry["discovered_at"] = _iso(
            parse_store_datetime(entry.get("discovered_at")) or first_seen
        )
        entry["last_seen_at"] = _iso(last_seen)
        # Alias lu par les versions antérieures de scan.py.
        entry["last_seen_run"] = _iso(last_seen)
        entry["last_seen_by_profile"] = _normalize_timestamp_mapping(
            entry.get("last_seen_by_profile")
        )
        entry["missing_since_by_profile"] = _normalize_timestamp_mapping(
            entry.get("missing_since_by_profile")
        )
        entry["successful_absence_count_by_profile"] = _normalize_count_mapping(
            entry.get("successful_absence_count_by_profile")
        )
        entry["last_absence_run_by_profile"] = _normalize_timestamp_mapping(
            entry.get("last_absence_run_by_profile")
        )
        entry["seen_profiles"] = _string_list(entry.get("seen_profiles"))
        entry["sources"] = list(offer_sources(entry))

        pure_retired = _is_pure_retired_offer(entry, retired)
        retired_evidence = [
            source for source in offer_sources(entry) if source in retired
        ]
        entry["retired_sources"] = list(dict.fromkeys(
            _string_list(entry.get("retired_sources")) + retired_evidence
        ))
        entry["source_retired"] = pure_retired
        if pure_retired:
            entry["active"] = False
            entry["likely_expired"] = False
            entry["retired_at"] = (
                _iso(parse_store_datetime(entry.get("retired_at")) or now)
            )
        else:
            # Les anciennes versions rendaient une offre inactive à la première
            # absence. Sans les preuves v2, la migration choisit la sécurité.
            legacy_single_absence = old_schema < OFFER_STORE_SCHEMA_VERSION
            if legacy_single_absence and entry.get("active") is False:
                entry["active"] = True
            else:
                entry["active"] = bool(entry.get("active", True))
            entry["likely_expired"] = bool(entry.get("likely_expired", False))
            if entry["likely_expired"] and not entry.get("likely_expired_at"):
                entry["likely_expired_at"] = now_iso
        entry["store_schema_version"] = OFFER_STORE_SCHEMA_VERSION
        migrated[offer_id] = entry
    return migrated


def _value(item: Any, field: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(field, default)
    return getattr(item, field, default)


@dataclass(frozen=True)
class ProfileScope:
    """Périmètre effectivement couvert par un run."""

    profile_id: str
    domains: frozenset[str]
    regions: frozenset[str]
    sources: frozenset[str]
    window_hours: int
    run_at: datetime

    @property
    def cutoff(self) -> datetime:
        return self.run_at - timedelta(hours=self.window_hours)


def profile_scope(context: Any) -> ProfileScope:
    """Adapte un ``ScanContext`` ou un mapping équivalent."""

    profile = _value(context, "profile", context)
    profile_id = str(_value(profile, "id", _value(context, "profile_id", "")))
    if not profile_id:
        raise ValueError("Le contexte doit identifier son profil")
    run_at = _utc(_value(context, "run_at"))
    window = int(_value(profile, "window_hours", _value(context, "window_hours", 0)))
    if window <= 0:
        raise ValueError("window_hours doit être strictement positif")
    return ProfileScope(
        profile_id=profile_id,
        domains=frozenset(_string_list(_value(profile, "domains", []))),
        regions=frozenset(_string_list(_value(profile, "regions", []))),
        sources=frozenset(_string_list(_value(profile, "sources", []))),
        window_hours=window,
        run_at=run_at,
    )


def _status_is_reliable(status: Any, profile_id: str) -> bool:
    status_profile = str(_value(status, "profile_id", "") or "")
    if status_profile and status_profile != profile_id:
        return False
    if _value(status, "status") not in RELIABLE_SOURCE_STATUSES:
        return False
    if bool(_value(status, "interrupted", False)):
        return False
    if not bool(_value(status, "coverage_complete", True)):
        return False
    for field in ("timeout_count", "failed_queries", "blocked_count"):
        try:
            if int(_value(status, field, 0) or 0) > 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _successful_source_coverage(
    offer: Mapping[str, Any], statuses: Sequence[Any], scope: ProfileScope,
    retired_sources: frozenset[str],
) -> bool:
    sources = {
        source for source in offer_sources(offer) if source not in retired_sources
    }
    regions = set(_string_list(offer.get("regions")))
    if not sources or not regions:
        return False
    # Une preuve partielle ne peut pas expirer une offre multi-source/région.
    if not sources.issubset(scope.sources) or not regions.issubset(scope.regions):
        return False
    for source in sources:
        for region in regions:
            matching = [
                status for status in statuses
                if _value(status, "source") == source
                and _value(status, "region_id", region) == region
            ]
            if not matching or not any(
                _status_is_reliable(status, scope.profile_id)
                for status in matching
            ):
                return False
    return True


def offer_is_pertinent_absence(
    offer: Mapping[str, Any], statuses: Sequence[Any], context: Any,
    *, retired_sources: Iterable[str] = RETIRED_SOURCES,
) -> bool:
    """Indique si l'absence peut être comptée pour ce profil précis."""

    scope = profile_scope(context)
    domains = set(_string_list(offer.get("domains")))
    regions = set(_string_list(offer.get("regions")))
    if not domains or not domains.issubset(scope.domains):
        return False
    if not regions or not regions.issubset(scope.regions):
        return False
    retired = frozenset(str(source) for source in retired_sources)
    if not _successful_source_coverage(offer, statuses, scope, retired):
        return False

    last_by_profile = _normalize_timestamp_mapping(
        offer.get("last_seen_by_profile")
    )
    if scope.profile_id in last_by_profile:
        return True
    # Première évaluation par ce profil : elle n'est pertinente que si son
    # horizon couvrait encore la dernière observation réelle de l'offre.
    last_seen = _entry_timestamp(offer, scope.run_at)
    return last_seen >= scope.cutoff


def _identity_tokens(offer: Mapping[str, Any]) -> set[str]:
    tokens = {f"id:{offer.get('id')}"} if offer.get("id") else set()
    tokens.update(f"legacy:{item}" for item in _string_list(offer.get("legacy_ids")))
    tokens.update(f"identity:{item}" for item in _string_list(offer.get("identity_keys")))
    for source, external_id in _mapping(offer.get("external_ids")).items():
        if external_id:
            tokens.add(f"external:{source}:{external_id}")
    for source, url in _mapping(offer.get("source_urls")).items():
        if isinstance(url, str) and url:
            tokens.add(f"url:{source}:{url}")
    return tokens


def find_existing_offer_id(
    store: Mapping[str, Mapping[str, Any]], offer: Mapping[str, Any],
) -> str | None:
    """Trouve uniquement une preuve d'identité exacte, jamais probabiliste."""

    candidate_id = str(offer.get("id") or "")
    if candidate_id and candidate_id in store:
        return candidate_id
    candidate_tokens = _identity_tokens(offer)
    if candidate_id:
        candidate_tokens.add(f"legacy:{candidate_id}")
    for existing_id, existing in store.items():
        tokens = _identity_tokens(existing)
        tokens.add(f"legacy:{existing_id}")
        if candidate_tokens & tokens:
            return existing_id
    return None


def _merge_unique(existing: Any, incoming: Any) -> list[str]:
    return list(dict.fromkeys(_string_list(existing) + _string_list(incoming)))


def _merge_offer(existing_id: str, existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    first_seen = existing.get("first_seen")
    discovered_at = existing.get("discovered_at")
    primary_source = existing.get("source")
    result = deepcopy(dict(existing))
    result.update(deepcopy(dict(incoming)))
    result["id"] = existing_id
    result["first_seen"] = first_seen
    result["discovered_at"] = discovered_at
    if primary_source:
        result["source"] = primary_source
    # Une redécouverte sans date ne doit jamais rajeunir l'offre : la date de
    # première découverte reste la référence de fraîcheur. Une vraie date de
    # publication, elle, remplace toujours une inconnue.
    incoming_date_known = bool(
        incoming.get("date_known", bool(incoming.get("date")))
    )
    if not incoming_date_known:
        for field in ("date", "date_known", "date_raw", "date_status",
                      "sort_date"):
            if field in existing:
                result[field] = deepcopy(existing[field])
    # Traçabilité X-ray des redécouvertes : first_discovered_at est immuable,
    # last_discovered_at suit le run courant, et chaque run redécouvrant le
    # post incrémente le compteur d'une seule unité.
    if existing.get("first_discovered_at") or incoming.get("first_discovered_at"):
        firsts = [value for value in (existing.get("first_discovered_at"),
                                      incoming.get("first_discovered_at")) if value]
        lasts = [value for value in (existing.get("last_discovered_at"),
                                     incoming.get("last_discovered_at")) if value]
        result["first_discovered_at"] = min(firsts)
        result["last_discovered_at"] = max(lasts)
        if existing.get("first_discovered_at") and incoming.get("first_discovered_at"):
            result["discovery_count"] = (
                int(existing.get("discovery_count") or 1)
                + int(incoming.get("discovery_count") or 1)
            )
        else:
            result["discovery_count"] = int(
                existing.get("discovery_count")
                or incoming.get("discovery_count") or 1
            )
    merged_queries = _merge_unique(
        existing.get("queries_that_found_it"),
        incoming.get("queries_that_found_it"),
    )
    if merged_queries:
        result["queries_that_found_it"] = merged_queries
    incoming_id = str(incoming.get("id") or "")
    result["legacy_ids"] = _merge_unique(
        existing.get("legacy_ids"),
        list(_string_list(incoming.get("legacy_ids")))
        + ([incoming_id] if incoming_id and incoming_id != existing_id else []),
    )
    for field in ("sources", "domains", "regions", "identity_keys"):
        result[field] = _merge_unique(existing.get(field), incoming.get(field))
    if result.get("source") and result["source"] not in result["sources"]:
        result["sources"].insert(0, result["source"])
    for field in ("source_urls", "external_ids"):
        merged = _mapping(existing.get(field))
        merged.update(_mapping(incoming.get(field)))
        result[field] = merged
    scores = _mapping(existing.get("domain_scores"))
    for domain, value in _mapping(incoming.get("domain_scores")).items():
        try:
            previous = scores.get(domain, value)
            scores[domain] = value if float(value) > float(previous) else previous
        except (TypeError, ValueError):
            scores[domain] = value
    if scores:
        result["domain_scores"] = scores
    keywords = _mapping(existing.get("matched_keywords"))
    for domain, values in _mapping(incoming.get("matched_keywords")).items():
        keywords[domain] = _merge_unique(keywords.get(domain), values)
    if keywords:
        result["matched_keywords"] = keywords
    return result


def reconcile_offer_store(
    found_offers: Sequence[Mapping[str, Any]],
    statuses: Sequence[Any],
    context: Any,
    *,
    store: Mapping[str, Any] | None = None,
    expiration_hours: int = DEFAULT_EXPIRATION_HOURS,
    successful_absences: int = DEFAULT_SUCCESSFUL_ABSENCES,
    retired_sources: Iterable[str] = RETIRED_SOURCES,
) -> dict[str, dict[str, Any]]:
    """Fusionne un run sans laisser un profil étroit invalider le général."""

    if expiration_hours <= 0 or successful_absences <= 0:
        raise ValueError("Les seuils d'expiration doivent être positifs")
    scope = profile_scope(context)
    retired = frozenset(str(source) for source in retired_sources)
    merged = migrate_offer_store(store, run_at=scope.run_at, retired_sources=retired)
    found_ids: set[str] = set()
    run_iso = _iso(scope.run_at)

    for incoming_raw in found_offers:
        if not isinstance(incoming_raw, Mapping) or not incoming_raw.get("id"):
            continue
        incoming = deepcopy(dict(incoming_raw))
        existing_id = find_existing_offer_id(merged, incoming)
        if existing_id is None:
            existing_id = str(incoming["id"])
            entry = incoming
            entry["id"] = existing_id
            entry["first_seen"] = run_iso
            entry["discovered_at"] = (
                _iso(parse_store_datetime(entry.get("discovered_at")) or scope.run_at)
            )
            entry["last_seen_by_profile"] = {}
            entry["missing_since_by_profile"] = {}
            entry["successful_absence_count_by_profile"] = {}
            entry["last_absence_run_by_profile"] = {}
            entry["seen_profiles"] = []
        else:
            entry = _merge_offer(existing_id, merged[existing_id], incoming)

        seen_profiles = _merge_unique(entry.get("seen_profiles"), [scope.profile_id])
        last_by_profile = _normalize_timestamp_mapping(entry.get("last_seen_by_profile"))
        last_by_profile[scope.profile_id] = run_iso
        # Toute redécouverte, même par une autre source/profil, invalide les
        # preuves d'absence accumulées jusque-là.
        entry["missing_since_by_profile"] = {}
        entry["successful_absence_count_by_profile"] = {}
        entry["last_absence_run_by_profile"] = {}
        entry["last_seen_by_profile"] = last_by_profile
        entry["seen_profiles"] = seen_profiles
        entry["last_seen_at"] = run_iso
        entry["last_seen_run"] = run_iso
        entry["active"] = True
        entry["likely_expired"] = False
        entry.pop("likely_expired_at", None)
        entry["sources"] = list(offer_sources(entry))
        entry["retired_sources"] = list(dict.fromkeys(
            _string_list(entry.get("retired_sources"))
            + [source for source in offer_sources(entry) if source in retired]
        ))
        entry["source_retired"] = _is_pure_retired_offer(entry, retired)
        if entry["source_retired"]:
            entry["active"] = False
            entry["retired_at"] = entry.get("retired_at") or run_iso
        entry["store_schema_version"] = OFFER_STORE_SCHEMA_VERSION
        merged[existing_id] = entry
        found_ids.add(existing_id)

    for offer_id, entry in merged.items():
        if offer_id in found_ids or entry.get("source_retired"):
            continue
        if not offer_is_pertinent_absence(
            entry, statuses, context, retired_sources=retired
        ):
            continue
        missing = _normalize_timestamp_mapping(entry.get("missing_since_by_profile"))
        counts = _normalize_count_mapping(
            entry.get("successful_absence_count_by_profile")
        )
        last_absence = _normalize_timestamp_mapping(
            entry.get("last_absence_run_by_profile")
        )
        # Une réconciliation rejouée avec le même run ne double pas le compteur.
        if last_absence.get(scope.profile_id) == run_iso:
            continue
        missing.setdefault(scope.profile_id, run_iso)
        counts[scope.profile_id] = counts.get(scope.profile_id, 0) + 1
        last_absence[scope.profile_id] = run_iso
        entry["missing_since_by_profile"] = missing
        entry["successful_absence_count_by_profile"] = counts
        entry["last_absence_run_by_profile"] = last_absence
        missing_at = parse_store_datetime(missing[scope.profile_id])
        if (
            missing_at is not None
            and scope.run_at - missing_at >= timedelta(hours=expiration_hours)
            and counts[scope.profile_id] >= successful_absences
        ):
            entry["likely_expired"] = True
            entry["likely_expired_at"] = entry.get("likely_expired_at") or run_iso
        # ``active`` reste vrai : le dashboard place ces offres dans une
        # section dédiée au lieu de les supprimer.
        entry["active"] = bool(entry.get("active", True))
    return merged


DEFAULT_UNKNOWN_DATE_VISIBILITY_HOURS = 72


def unknown_date_is_stale(
    offer: Mapping[str, Any],
    *,
    now: datetime | None = None,
    visibility_hours: int = DEFAULT_UNKNOWN_DATE_VISIBILITY_HOURS,
) -> bool:
    """Vrai quand un post sans date ne doit plus passer pour récent.

    La référence est la première découverte, jamais la dernière : une
    redécouverte ne remet pas le compteur à zéro. Sans aucune référence
    exploitable, l'offre est considérée ancienne par prudence.
    """
    if visibility_hours <= 0:
        raise ValueError("visibility_hours doit être strictement positif")
    if bool(offer.get("date_known", bool(offer.get("date")))):
        return False
    checked_at = _utc(now)
    reference = None
    for field in ("first_discovered_at", "discovered_at", "first_seen"):
        reference = parse_store_datetime(offer.get(field))
        if reference is not None:
            break
    if reference is None:
        return True
    return checked_at - reference >= timedelta(hours=visibility_hours)


def active_dashboard_offers(store: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Offres actives, non retirées et non probablement pourvues."""

    return [
        deepcopy(dict(offer)) for offer in store.values()
        if bool(offer.get("active", True))
        and not bool(offer.get("source_retired", False))
        and not bool(offer.get("likely_expired", False))
    ]


def expired_dashboard_offers(store: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Offres probablement pourvues à rendre dans la section repliée."""

    return [
        deepcopy(dict(offer)) for offer in store.values()
        if bool(offer.get("active", True))
        and not bool(offer.get("source_retired", False))
        and bool(offer.get("likely_expired", False))
    ]


def dashboard_offer_sections(
    store: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return active_dashboard_offers(store), expired_dashboard_offers(store)


def purge_offer_store(
    store: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime | None = None,
    retention_days: int = DEFAULT_OFFER_RETENTION_DAYS,
) -> dict[str, dict[str, Any]]:
    """Purge seulement les offres déjà retirées, inactives ou expirées."""

    if retention_days <= 0:
        raise ValueError("retention_days doit être strictement positif")
    checked_at = _utc(now)
    cutoff = checked_at - timedelta(days=retention_days)
    result: dict[str, dict[str, Any]] = {}
    for offer_id, raw in store.items():
        offer = deepcopy(dict(raw))
        purgeable = (
            bool(offer.get("source_retired"))
            or not bool(offer.get("active", True))
            or bool(offer.get("likely_expired"))
        )
        reference = None
        for field in (
            "retired_at", "likely_expired_at", "last_seen_at", "last_seen_run",
            "first_seen",
        ):
            reference = parse_store_datetime(offer.get(field))
            if reference is not None:
                break
        if purgeable and reference is not None and reference < cutoff:
            continue
        result[str(offer_id)] = offer
    return result


def migrate_seen_memory(
    payload: Any,
    *,
    run_at: datetime | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Accepte le tableau historique et produit le schéma v2 horodaté."""

    checked_at = _utc(run_at)
    now_iso = _iso(checked_at)
    profiles = [profile_id] if profile_id else []
    if payload is None:
        raw_offers: Any = {}
    elif isinstance(payload, (list, tuple, set)):
        raw_offers = {
            str(offer_id): {
                "first_seen": now_iso,
                "last_seen": now_iso,
                "profiles": profiles,
            }
            for offer_id in payload
            if str(offer_id)
        }
    elif isinstance(payload, Mapping):
        raw_offers = payload.get("offers", {}) if "offers" in payload else payload
    else:
        raise ValueError("seen_offers.json doit contenir un tableau ou un objet")
    if not isinstance(raw_offers, Mapping):
        raise ValueError("seen_offers.json.offers doit contenir un objet")

    offers: dict[str, dict[str, Any]] = {}
    for raw_id, raw_meta in raw_offers.items():
        offer_id = str(raw_id)
        if not offer_id:
            continue
        meta = dict(raw_meta) if isinstance(raw_meta, Mapping) else {}
        first = parse_store_datetime(meta.get("first_seen"))
        last = parse_store_datetime(meta.get("last_seen"))
        first_iso = _iso(first or last or checked_at)
        last_iso = _iso(last or first or checked_at)
        offers[offer_id] = {
            "first_seen": first_iso,
            "last_seen": last_iso,
            "profiles": _string_list(meta.get("profiles")) or list(profiles),
        }
    return {"schema_version": SEEN_SCHEMA_VERSION, "offers": offers}


def purge_seen_memory(
    payload: Any,
    *,
    now: datetime | None = None,
    retention_days: int = DEFAULT_SEEN_RETENTION_DAYS,
) -> dict[str, Any]:
    """Purge par ``last_seen`` et jamais par l'ordre des hashes."""

    if retention_days <= 0:
        raise ValueError("retention_days doit être strictement positif")
    checked_at = _utc(now)
    document = migrate_seen_memory(payload, run_at=checked_at)
    cutoff = checked_at - timedelta(days=retention_days)
    document["offers"] = {
        offer_id: meta
        for offer_id, meta in document["offers"].items()
        if (parse_store_datetime(meta.get("last_seen")) or checked_at) >= cutoff
    }
    return document


def update_seen_memory(
    payload: Any,
    offer_ids: Iterable[str],
    *,
    run_at: datetime | None = None,
    profile_id: str,
    retention_days: int = DEFAULT_SEEN_RETENTION_DAYS,
) -> tuple[dict[str, Any], set[str]]:
    """Met à jour seen v2 et retourne les vrais nouveaux IDs.

    Les IDs du tableau historique sont connus avant sa migration ; ils ne
    provoquent donc jamais une rafale de fausses notifications.
    """

    checked_at = _utc(run_at)
    document = migrate_seen_memory(
        payload, run_at=checked_at, profile_id=profile_id
    )
    known = set(document["offers"])
    current = {str(offer_id) for offer_id in offer_ids if str(offer_id)}
    new_ids = current - known
    now_iso = _iso(checked_at)
    for offer_id in current:
        meta = document["offers"].setdefault(offer_id, {
            "first_seen": now_iso,
            "last_seen": now_iso,
            "profiles": [],
        })
        meta["first_seen"] = meta.get("first_seen") or now_iso
        meta["last_seen"] = now_iso
        meta["profiles"] = _merge_unique(meta.get("profiles"), [profile_id])
    return (
        purge_seen_memory(
            document, now=checked_at, retention_days=retention_days
        ),
        new_ids,
    )


def seen_offer_ids(payload: Any, *, run_at: datetime | None = None) -> set[str]:
    return set(migrate_seen_memory(payload, run_at=run_at)["offers"])


__all__ = [
    "DEFAULT_EXPIRATION_HOURS",
    "DEFAULT_OFFER_RETENTION_DAYS",
    "DEFAULT_UNKNOWN_DATE_VISIBILITY_HOURS",
    "DEFAULT_SEEN_RETENTION_DAYS",
    "DEFAULT_SUCCESSFUL_ABSENCES",
    "OFFER_STORE_SCHEMA_VERSION",
    "ProfileScope",
    "RELIABLE_SOURCE_STATUSES",
    "RETIRED_SOURCES",
    "SEEN_SCHEMA_VERSION",
    "active_dashboard_offers",
    "dashboard_offer_sections",
    "expired_dashboard_offers",
    "find_existing_offer_id",
    "migrate_offer_store",
    "migrate_seen_memory",
    "offer_is_pertinent_absence",
    "offer_sources",
    "parse_store_datetime",
    "profile_scope",
    "purge_offer_store",
    "purge_seen_memory",
    "reconcile_offer_store",
    "seen_offer_ids",
    "unknown_date_is_stale",
    "update_seen_memory",
]
