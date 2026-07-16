"""Écritures atomiques des artefacts persistants du radar."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


def _fsync_directory(directory: Path) -> None:
    """Synchronise le répertoire quand la plateforme l'autorise."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def atomic_write_text(
    path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
) -> Path:
    """Écrit ``content`` sans jamais exposer un fichier partiel.

    Le temporaire est créé dans le même répertoire que la cible afin que
    :func:`os.replace` reste atomique. En cas d'échec, l'ancien fichier est
    intact et le temporaire est supprimé.
    """

    if not isinstance(content, str):
        raise TypeError("content doit être une chaîne")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = (
        stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    )
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding=encoding, newline="") as handle:
            file_descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, previous_mode)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    ensure_ascii: bool = False,
    indent: int | None = 1,
    sort_keys: bool = False,
) -> Path:
    """Sérialise puis écrit atomiquement un document JSON UTF-8."""

    content = json.dumps(
        payload,
        ensure_ascii=ensure_ascii,
        indent=indent,
        sort_keys=sort_keys,
    )
    return atomic_write_text(path, content + "\n")


__all__ = ["atomic_write_json", "atomic_write_text"]
