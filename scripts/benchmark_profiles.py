#!/usr/bin/env python3
"""Exécute les profils du radar en mode benchmark isolé.

Le script ne mesure pas lui-même le réseau : il délègue à ``radar.py`` qui
produit le rapport machine lisible. Le drapeau ``--benchmark`` garantit que le
store, la mémoire des offres vues, le dashboard et les emails de production ne
sont jamais modifiés.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


PROFILE_IDS = ("fast_data_ia", "full_general")


def benchmark_command(
    *, python: str, radar: Path, profile_id: str, output: Path
) -> list[str]:
    return [
        python,
        str(radar),
        "--profile",
        profile_id,
        "--benchmark",
        "--benchmark-output",
        str(output),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmarke FAST et/ou FULL sans toucher aux données de production."
    )
    parser.add_argument(
        "--profiles",
        nargs="+",
        choices=PROFILE_IDS,
        default=list(PROFILE_IDS),
        help="Profils à exécuter séquentiellement (FAST puis FULL par défaut).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "radar-stage-benchmarks",
        help="Répertoire distinct des sorties de production (temporaire par défaut).",
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche les commandes sans lancer de collecte.",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Exécute le profil suivant même si le précédent échoue.",
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    radar = root / "radar.py"
    if not radar.exists() and not args.dry_run:
        parser.error(f"point d'entrée introuvable : {radar}")

    output_dir = args.output_dir
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    failures = 0

    for profile_id in args.profiles:
        output = output_dir / f"{profile_id}-{stamp}.json"
        command = benchmark_command(
            python=args.python,
            radar=radar,
            profile_id=profile_id,
            output=output,
        )
        print("Commande :", " ".join(command), flush=True)
        if args.dry_run:
            continue
        output_dir.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(command, cwd=root, check=False)
        if completed.returncode:
            failures += 1
            if not args.keep_going:
                break

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
