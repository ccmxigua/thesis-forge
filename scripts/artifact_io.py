"""Filesystem safety helpers for generated artifacts."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable


def paths_alias(paths: Iterable[Path]) -> bool:
    items = [path.expanduser().resolve() for path in paths]
    if len(set(items)) != len(items):
        return True
    existing = [path for path in items if path.exists()]
    for index, left in enumerate(existing):
        for right in existing[index + 1:]:
            try:
                if os.path.samefile(left, right):
                    return True
            except FileNotFoundError:
                pass
    return False


def sibling_temp(path: Path, suffix: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=suffix or path.suffix,
                                        dir=path.parent)
    os.close(descriptor)
    return Path(name)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    temporary = sibling_temp(path)
    try:
        temporary.write_text(text, encoding=encoding)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def commit_files(staged_to_final: list[tuple[Path, Path]]) -> None:
    """Replace several outputs with rollback of all pre-existing destinations."""
    backups: dict[Path, Path] = {}
    committed: list[Path] = []
    try:
        for _, destination in staged_to_final:
            if destination.exists():
                backup = sibling_temp(destination, suffix=".backup")
                backup.unlink()
                os.replace(destination, backup)
                backups[destination] = backup
        for staged, destination in staged_to_final:
            os.replace(staged, destination)
            committed.append(destination)
    except Exception:
        for destination in reversed(committed):
            destination.unlink(missing_ok=True)
        for destination, backup in backups.items():
            if backup.exists():
                os.replace(backup, destination)
        raise
    finally:
        for backup in backups.values():
            backup.unlink(missing_ok=True)
