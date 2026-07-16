"""Chargement et résolution des profils de scan du Radar Stage France.

Ce module ne dépend pas de :mod:`scan` afin de pouvoir être testé et utilisé
par les workflows, les benchmarks et le pipeline sans import circulaire.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
PROFILE_FILE = BASE_DIR / "data/profiles.json"
# Alias pluriel conservé pour les intégrations qui nomment le catalogue plutôt
# que le fichier d'un profil individuel.
PROFILES_FILE = PROFILE_FILE
DOMAINS_FILE = BASE_DIR / "data/domains.json"
REGIONS_FILE = BASE_DIR / "data/regions.json"
DEFAULT_PROFILE_ID = "full_general"

# Registre partagé par la configuration. Glassdoor est volontairement absent :
# aucun profil ne doit pouvoir le réactiver par erreur.
KNOWN_SOURCES = (
    "LinkedIn",
    "Indeed",
    "Google Jobs",
    "APEC",
    "Adzuna",
    "France Travail",
    "LinkedIn Posts",
)
JOBSPY_SOURCE_KEYS = {
    "LinkedIn": "linkedin",
    "Indeed": "indeed",
    "Google Jobs": "google",
}

# Valeurs de migration pour un ancien catalogue qui ne déclarait que le
# périmètre métier d'un profil. Le fichier versionné courant explicite toutes
# ces limites ; ces valeurs ne servent donc qu'à charger sans rupture un
# schéma minimal conforme aux premiers exemples de configuration.
DEFAULT_RESULTS_WANTED = {
    "linkedin": 1000,
    "indeed": 1000,
    "google": 900,
}
DEFAULT_MAX_PAGES = {
    "LinkedIn": 40,
    "Indeed": 20,
    "Google Jobs": 30,
    "APEC": 20,
    "Adzuna": 20,
    "France Travail": 20,
    "LinkedIn Posts": 1,
}
DEFAULT_QUERY_BUDGETS = {
    **{source: 1000 for source in KNOWN_SOURCES},
    "LinkedIn Posts": 12,
}
DEFAULT_DETAIL_BUDGETS = {
    **{source: 0 for source in KNOWN_SOURCES},
    "LinkedIn": 3,
    "APEC": 100,
}
DEFAULT_REQUEST_TIMEOUT_S = 20
DEFAULT_MAX_STALE_PAGES = 2
DEFAULT_MAX_PARALLEL_SOURCES = 4
DEFAULT_MAX_PARALLEL_REGIONS = 1


class ProfileConfigError(ValueError):
    """Configuration de profil absente, inconnue ou incohérente."""


def _freeze_int_mapping(
    value: Any,
    *,
    field_name: str,
    allowed_keys: set[str],
    minimum: int,
    defaults: Mapping[str, int] | None = None,
) -> Mapping[str, int]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ProfileConfigError(f"{field_name} doit être un objet JSON")
    result = {
        key: raw for key, raw in (defaults or {}).items() if key in allowed_keys
    }
    for key, raw in value.items():
        if key not in allowed_keys:
            raise ProfileConfigError(f"{field_name}: source inconnue {key!r}")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum:
            qualifier = "positif" if minimum == 1 else "positif ou nul"
            raise ProfileConfigError(
                f"{field_name}.{key} doit être un entier {qualifier}"
            )
        result[key] = raw
    return MappingProxyType(result)


def _unique_strings(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ProfileConfigError(f"{field_name} doit être une liste non vide")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ProfileConfigError(f"{field_name} contient une valeur invalide")
    cleaned = tuple(item.strip() for item in value)
    if len(set(cleaned)) != len(cleaned):
        raise ProfileConfigError(f"{field_name} contient un doublon")
    return cleaned


@dataclass(frozen=True)
class ScanProfile:
    """Configuration métier et limites techniques immuables d'un scan."""

    id: str
    domains: tuple[str, ...]
    regions: tuple[str, ...]
    sources: tuple[str, ...]
    window_hours: int
    results_wanted: Mapping[str, int]
    max_pages: Mapping[str, int]
    query_budgets: Mapping[str, int]
    detail_budgets: Mapping[str, int]
    request_timeout_s: int
    max_stale_pages: int
    max_parallel_sources: int
    max_parallel_regions: int

    def result_limit(self, source: str, default: int | None = None) -> int | None:
        key = JOBSPY_SOURCE_KEYS.get(source, source.casefold())
        return self.results_wanted.get(key, default)

    def page_limit(self, source: str, default: int | None = None) -> int | None:
        return self.max_pages.get(source, default)

    def query_budget(self, source: str, default: int | None = None) -> int | None:
        return self.query_budgets.get(source, default)

    def detail_budget(self, source: str, default: int = 0) -> int:
        return self.detail_budgets.get(source, default)


@dataclass(frozen=True)
class ScanContext:
    """Contexte temporel unique d'un run.

    Une date naïve est interprétée comme UTC, conformément au comportement
    historique du projet. Toutes les sources partagent ensuite exactement la
    même horloge et la même borne de fraîcheur.
    """

    profile: ScanProfile
    run_at: datetime

    def __post_init__(self) -> None:
        run_at = self.run_at
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "run_at", run_at.astimezone(timezone.utc))

    @property
    def cutoff(self) -> datetime:
        return self.run_at - timedelta(hours=self.profile.window_hours)


@dataclass(frozen=True)
class ProfileCatalog(Mapping[str, ScanProfile]):
    """Catalogue en lecture seule avec profil par défaut versionné."""

    profiles: Mapping[str, ScanProfile]
    default_profile_id: str = DEFAULT_PROFILE_ID
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "profiles", MappingProxyType(dict(self.profiles)))

    def __getitem__(self, key: str) -> ScanProfile:
        return self.profiles[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.profiles)

    def __len__(self) -> int:
        return len(self.profiles)


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProfileConfigError(f"{label} introuvable : {path}") from exc
    except json.JSONDecodeError as exc:
        raise ProfileConfigError(f"{label} contient un JSON invalide : {exc}") from exc
    if not isinstance(payload, dict):
        raise ProfileConfigError(f"{label} doit contenir un objet JSON")
    return payload


def _normalize_profiles_payload(
    payload: dict[str, Any],
) -> tuple[int, str, dict[str, Any]]:
    """Normalise le catalogue enveloppé et l'ancien mapping racine.

    Le premier exemple public de ``profiles.json`` était directement un objet
    ``{profile_id: configuration}``. Accepter cette forme constitue la
    migration automatique ; l'écriture versionnée moderne reste enveloppée
    dans ``schema_version/default_profile/profiles``.
    """

    if "profiles" in payload:
        schema_version = payload.get("schema_version", 1)
        if isinstance(schema_version, bool) or schema_version != 1:
            raise ProfileConfigError(
                f"Version de profils non supportée : {schema_version!r}"
            )
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise ProfileConfigError("profiles doit être un objet JSON non vide")
        default_profile = payload.get("default_profile", DEFAULT_PROFILE_ID)
        if not isinstance(default_profile, str) or not default_profile.strip():
            raise ProfileConfigError("default_profile doit être un identifiant non vide")
        return schema_version, default_profile.strip(), raw_profiles

    if not payload:
        raise ProfileConfigError("Le catalogue de profils ne peut pas être vide")
    # Forme historique : aucune métadonnée, uniquement les profils.
    return 1, DEFAULT_PROFILE_ID, payload


def _positive_config_int(value: Any, *, field_name: str, default: int) -> int:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProfileConfigError(
            f"{field_name} doit être un entier strictement positif"
        )
    return value


def _catalog_ids(
    value: Mapping[str, Any] | Sequence[str] | None,
    *,
    default_path: Path,
    label: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if value is None:
        catalog: Mapping[str, Any] = _load_object(default_path, label)
    elif isinstance(value, Mapping):
        catalog = value
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        ids = tuple(str(item) for item in value)
        return ids, ids
    else:
        raise ProfileConfigError(f"{label} doit être un catalogue ou une liste")

    ids = tuple(str(item) for item in catalog)
    enabled = tuple(
        item for item in ids
        if not isinstance(catalog[item], Mapping)
        or bool(catalog[item].get("enabled", True))
    )
    return ids, enabled


def _resolve_scope(
    raw: Any,
    *,
    profile_id: str,
    field_name: str,
    all_ids: tuple[str, ...],
    enabled_ids: tuple[str, ...],
) -> tuple[str, ...]:
    if raw == "__enabled__":
        selected = enabled_ids
    else:
        selected = _unique_strings(raw, f"{profile_id}.{field_name}")
    if not selected:
        raise ProfileConfigError(f"{profile_id}.{field_name} ne peut pas être vide")
    unknown = [item for item in selected if item not in all_ids]
    if unknown:
        raise ProfileConfigError(
            f"{profile_id}.{field_name}: valeur(s) inconnue(s) {unknown}"
        )
    return selected


def load_profiles(
    path: str | Path = PROFILE_FILE,
    domains: Mapping[str, Any] | Sequence[str] | None = None,
    regions: Mapping[str, Any] | Sequence[str] | None = None,
    sources: Sequence[str] | None = None,
) -> ProfileCatalog:
    """Charge et valide le catalogue versionné de profils.

    ``"__enabled__"`` est résolu au chargement contre les catalogues fournis,
    ce qui permet à FULL de suivre automatiquement les domaines et régions
    activés sans les dupliquer dans ``profiles.json``.
    """

    profile_path = Path(path)
    payload = _load_object(profile_path, "Catalogue de profils")
    schema_version, default_profile_id, raw_profiles = _normalize_profiles_payload(
        payload
    )

    domain_ids, enabled_domains = _catalog_ids(
        domains, default_path=DOMAINS_FILE, label="Catalogue des domaines"
    )
    region_ids, enabled_regions = _catalog_ids(
        regions, default_path=REGIONS_FILE, label="Catalogue des régions"
    )
    allowed_sources = tuple(KNOWN_SOURCES if sources is None else sources)
    if not allowed_sources or len(set(allowed_sources)) != len(allowed_sources):
        raise ProfileConfigError("Le registre des sources est vide ou contient un doublon")
    source_set = set(allowed_sources)

    parsed: dict[str, ScanProfile] = {}
    for profile_id, raw in raw_profiles.items():
        if (
            not isinstance(profile_id, str)
            or not profile_id
            or profile_id != profile_id.strip()
        ):
            raise ProfileConfigError("Identifiant de profil invalide")
        if not isinstance(raw, dict):
            raise ProfileConfigError(f"Le profil {profile_id!r} doit être un objet")
        # Les quatre champs métier sont le contrat minimal historique. Les
        # limites techniques ajoutées ensuite bénéficient de valeurs de
        # migration déterministes et restent strictement validées si fournies.
        required = {"domains", "regions", "sources", "window_hours"}
        missing = required - set(raw)
        if missing:
            raise ProfileConfigError(
                f"Profil {profile_id}: champs manquants {sorted(missing)}"
            )
        selected_sources = _unique_strings(raw["sources"], f"{profile_id}.sources")
        unknown_sources = [item for item in selected_sources if item not in source_set]
        if unknown_sources:
            raise ProfileConfigError(
                f"{profile_id}.sources: valeur(s) inconnue(s) {unknown_sources}"
            )
        window = _positive_config_int(
            raw["window_hours"], field_name=f"{profile_id}.window_hours", default=24
        )
        timeout = _positive_config_int(
            raw.get("request_timeout_s"),
            field_name=f"{profile_id}.request_timeout_s",
            default=DEFAULT_REQUEST_TIMEOUT_S,
        )
        stale_pages = _positive_config_int(
            raw.get("max_stale_pages"),
            field_name=f"{profile_id}.max_stale_pages",
            default=DEFAULT_MAX_STALE_PAGES,
        )
        parallel_sources = _positive_config_int(
            raw.get("max_parallel_sources"),
            field_name=f"{profile_id}.max_parallel_sources",
            default=DEFAULT_MAX_PARALLEL_SOURCES,
        )
        parallel_regions = _positive_config_int(
            raw.get("max_parallel_regions"),
            field_name=f"{profile_id}.max_parallel_regions",
            default=DEFAULT_MAX_PARALLEL_REGIONS,
        )
        selected_source_set = set(selected_sources)
        selected_jobspy_keys = {
            site_key for source, site_key in JOBSPY_SOURCE_KEYS.items()
            if source in selected_source_set
        }
        parsed[profile_id] = ScanProfile(
            id=profile_id,
            domains=_resolve_scope(
                raw["domains"], profile_id=profile_id, field_name="domains",
                all_ids=domain_ids, enabled_ids=enabled_domains,
            ),
            regions=_resolve_scope(
                raw["regions"], profile_id=profile_id, field_name="regions",
                all_ids=region_ids, enabled_ids=enabled_regions,
            ),
            sources=selected_sources,
            window_hours=window,
            results_wanted=_freeze_int_mapping(
                raw.get("results_wanted"),
                field_name=f"{profile_id}.results_wanted",
                allowed_keys=selected_jobspy_keys,
                minimum=1,
                defaults=DEFAULT_RESULTS_WANTED,
            ),
            max_pages=_freeze_int_mapping(
                raw.get("max_pages"), field_name=f"{profile_id}.max_pages",
                allowed_keys=selected_source_set, minimum=1,
                defaults=DEFAULT_MAX_PAGES,
            ),
            query_budgets=_freeze_int_mapping(
                raw.get("query_budgets"),
                field_name=f"{profile_id}.query_budgets",
                allowed_keys=selected_source_set, minimum=1,
                defaults=DEFAULT_QUERY_BUDGETS,
            ),
            detail_budgets=_freeze_int_mapping(
                raw.get("detail_budgets"),
                field_name=f"{profile_id}.detail_budgets",
                allowed_keys=selected_source_set, minimum=0,
                defaults=DEFAULT_DETAIL_BUDGETS,
            ),
            request_timeout_s=timeout,
            max_stale_pages=stale_pages,
            max_parallel_sources=parallel_sources,
            max_parallel_regions=parallel_regions,
        )

    if default_profile_id not in parsed:
        raise ProfileConfigError(
            f"Profil par défaut inconnu : {default_profile_id!r}"
        )
    return ProfileCatalog(
        parsed,
        default_profile_id=default_profile_id,
        schema_version=schema_version,
    )


def _parse_override(
    raw: str | Sequence[str] | None,
    *,
    field_name: str,
    allowed: set[str],
) -> tuple[str, ...] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        values = tuple(item.strip() for item in raw.split(",") if item.strip())
    elif isinstance(raw, Sequence):
        values = tuple(str(item).strip() for item in raw if str(item).strip())
    else:
        raise ProfileConfigError(f"{field_name} doit être une liste CSV")
    if not values:
        raise ProfileConfigError(f"{field_name} ne peut pas être vide")
    if len(set(values)) != len(values):
        raise ProfileConfigError(f"{field_name} contient un doublon")
    unknown = [item for item in values if item not in allowed]
    if unknown:
        raise ProfileConfigError(f"{field_name}: valeur(s) inconnue(s) {unknown}")
    return values


def _positive_int_override(raw: str | int | None, name: str) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ProfileConfigError(f"{name} doit être un entier strictement positif")
    try:
        result = int(raw)
    except (TypeError, ValueError) as exc:
        raise ProfileConfigError(
            f"{name} doit être un entier strictement positif"
        ) from exc
    if result <= 0 or str(raw).strip() != str(result):
        raise ProfileConfigError(f"{name} doit être un entier strictement positif")
    return result


def _resolved_budget_mapping(
    *,
    attribute: str,
    keys: Sequence[str],
    base: ScanProfile,
    fallback: ScanProfile,
    hard_defaults: Mapping[str, int],
) -> Mapping[str, int]:
    """Projette les budgets lors d'une surcharge de sources.

    Une surcharge peut légitimement ajouter Google Jobs à FAST pour un run de
    diagnostic. Les limites absentes du profil de base sont alors reprises du
    profil par défaut, puis des valeurs de migration. Cela évite un ``KeyError``
    tardif tout en gardant une configuration immuable et déterministe.
    """

    base_values = getattr(base, attribute)
    fallback_values = getattr(fallback, attribute)
    resolved: dict[str, int] = {}
    for key in keys:
        if key in base_values:
            resolved[key] = base_values[key]
        elif key in fallback_values:
            resolved[key] = fallback_values[key]
        elif key in hard_defaults:
            resolved[key] = hard_defaults[key]
        else:
            raise ProfileConfigError(
                f"Aucune limite {attribute} n'est configurée pour {key!r}"
            )
    return MappingProxyType(resolved)


def resolve_profile(
    profiles: ProfileCatalog | Mapping[str, ScanProfile],
    profile_id: str | None = None,
    env: Mapping[str, str] | None = None,
    *,
    cli_domains: str | Sequence[str] | None = None,
    cli_regions: str | Sequence[str] | None = None,
    cli_sources: str | Sequence[str] | None = None,
    cli_window_hours: str | int | None = None,
    default_profile_id: str | None = None,
    domains: Mapping[str, Any] | Sequence[str] | None = None,
    regions: Mapping[str, Any] | Sequence[str] | None = None,
    sources: Sequence[str] | None = None,
) -> ScanProfile:
    """Résout un profil et ses surcharges avec priorité CLI > environnement.

    ``profile_id`` représente l'argument CLI ``--profile``. S'il est absent,
    ``RADAR_PROFILE`` est utilisé, puis le profil par défaut du catalogue.
    Les autres arguments ``cli_*`` suivent la même priorité sur leurs
    variables ``RADAR_*`` respectives.
    """

    environ = os.environ if env is None else env
    catalog_default = getattr(profiles, "default_profile_id", DEFAULT_PROFILE_ID)
    if profile_id is not None:
        if not isinstance(profile_id, str) or not profile_id:
            raise ProfileConfigError("--profile doit être un identifiant non vide")
        selected_id = profile_id
    else:
        selected_id = (
            environ.get("RADAR_PROFILE")
            or default_profile_id
            or catalog_default
        )
    if selected_id not in profiles:
        raise ProfileConfigError(f"Profil inconnu : {selected_id!r}")
    base = profiles[selected_id]

    domain_ids, _ = _catalog_ids(
        domains, default_path=DOMAINS_FILE, label="Catalogue des domaines"
    )
    region_ids, _ = _catalog_ids(
        regions, default_path=REGIONS_FILE, label="Catalogue des régions"
    )
    allowed_sources = set(KNOWN_SOURCES if sources is None else sources)
    if not allowed_sources:
        raise ProfileConfigError("Le registre des sources ne peut pas être vide")
    selected_domains = _parse_override(
        cli_domains if cli_domains is not None else environ.get("RADAR_DOMAINS"),
        field_name="RADAR_DOMAINS", allowed=set(domain_ids),
    )
    selected_regions = _parse_override(
        cli_regions if cli_regions is not None else environ.get("RADAR_REGIONS"),
        field_name="RADAR_REGIONS", allowed=set(region_ids),
    )
    selected_sources = _parse_override(
        cli_sources if cli_sources is not None else environ.get("RADAR_SOURCES"),
        field_name="RADAR_SOURCES", allowed=allowed_sources,
    )
    selected_window = _positive_int_override(
        cli_window_hours
        if cli_window_hours is not None
        else environ.get("RADAR_WINDOW_HOURS"),
        "RADAR_WINDOW_HOURS",
    )
    resolved_sources = selected_sources or base.sources
    fallback = profiles.get(catalog_default, base)
    result_keys = tuple(
        JOBSPY_SOURCE_KEYS[source]
        for source in resolved_sources
        if source in JOBSPY_SOURCE_KEYS
    )
    return replace(
        base,
        domains=selected_domains or base.domains,
        regions=selected_regions or base.regions,
        sources=resolved_sources,
        window_hours=(
            selected_window if selected_window is not None else base.window_hours
        ),
        results_wanted=_resolved_budget_mapping(
            attribute="results_wanted", keys=result_keys, base=base,
            fallback=fallback, hard_defaults=DEFAULT_RESULTS_WANTED,
        ),
        max_pages=_resolved_budget_mapping(
            attribute="max_pages", keys=resolved_sources, base=base,
            fallback=fallback, hard_defaults=DEFAULT_MAX_PAGES,
        ),
        query_budgets=_resolved_budget_mapping(
            attribute="query_budgets", keys=resolved_sources, base=base,
            fallback=fallback, hard_defaults=DEFAULT_QUERY_BUDGETS,
        ),
        detail_budgets=_resolved_budget_mapping(
            attribute="detail_budgets", keys=resolved_sources, base=base,
            fallback=fallback, hard_defaults=DEFAULT_DETAIL_BUDGETS,
        ),
    )


def create_scan_context(
    profiles: ProfileCatalog | Mapping[str, ScanProfile],
    profile_id: str | None = None,
    env: Mapping[str, str] | None = None,
    *,
    run_at: datetime | None = None,
    **overrides: Any,
) -> ScanContext:
    """Construit le contexte partagé d'un run à partir du profil résolu."""

    profile = resolve_profile(
        profiles, profile_id=profile_id, env=env, **overrides
    )
    return ScanContext(profile, run_at or datetime.now(timezone.utc))


__all__ = [
    "DEFAULT_PROFILE_ID",
    "KNOWN_SOURCES",
    "JOBSPY_SOURCE_KEYS",
    "PROFILE_FILE",
    "PROFILES_FILE",
    "ProfileCatalog",
    "ProfileConfigError",
    "ScanContext",
    "ScanProfile",
    "create_scan_context",
    "load_profiles",
    "resolve_profile",
]
