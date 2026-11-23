"""Test helper: a fonts folder filled by hand from this machine's fonts, as a user would."""

from __future__ import annotations

import shutil
from pathlib import Path

from pdf_ar.fonts import default_search_dirs, index_font_files

SYSTEM = index_font_files(default_search_dirs())


def system_has(*names: str) -> bool:
    return all(name in SYSTEM for name in names)


def fonts_folder(folder: Path, *names: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copyfile(SYSTEM[name].path, folder / Path(SYSTEM[name].path).name)
    return folder
