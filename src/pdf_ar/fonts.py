"""The fonts folder: the full font files the user puts there, never fetched automatically.

PDFs embed only subsets of their fonts: glyph outlines without the shaping tables
(GSUB, GPOS, GDEF) that say which glyph is which letter. The rebuild therefore
needs the full font files. This module indexes the files in the fonts folder by
their names and versions. It never copies or downloads a font: which fonts and
versions a set of PDFs needs is reported by ``required_fonts``, and the user adds
them. The open-font registry and this machine's font folders are consulted to say
where a missing font can be found; the ``auto`` engine also reads this machine's
installed files of a font the folder lacks, whatever their version.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import cache
from importlib import resources
from pathlib import Path

from fontTools.ttLib import TTCollection, TTFont

FONT_SUFFIXES = (".ttf", ".otf", ".ttc", ".otc")


def font_key(basefont: str) -> str:
    """Lookup key of a PDF font name: subset prefix and spaces removed ("ABCDEF+Sakkal Majalla")."""
    return basefont.split("+", 1)[-1].replace(" ", "")


def is_arabic_code(code: int) -> bool:
    return 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F or 0xFB50 <= code <= 0xFEFF


def default_search_dirs() -> list[Path]:
    """This machine's font folders."""
    dirs = []
    windir = os.environ.get("WINDIR")
    if windir:
        dirs.append(Path(windir) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    dirs += [Path("/Library/Fonts"), Path.home() / "Library" / "Fonts",
             Path("/usr/share/fonts"), Path.home() / ".local" / "share" / "fonts"]
    return [d for d in dirs if d.is_dir()]


# Font files -----------------------------------------------------------------


@dataclass(frozen=True)
class FontFile:
    path: str
    number: int  # index inside a .ttc/.otc collection, else 0


def font_version(font: TTFont) -> str | None:
    """The font's version number: from the name table ("Version 6.86;O365" -> "6.86"),
    else the head table's revision. None when the font records neither."""
    try:
        text = font["name"].getDebugName(5) if "name" in font else None
    except Exception:  # noqa: BLE001
        text = None
    if text:
        match = re.search(r"\d+\.\d+", text)
        if match:
            return match.group(0)
    try:
        return f"{font['head'].fontRevision:.3f}" if "head" in font else None
    except Exception:  # noqa: BLE001
        return None


def same_version(a: str | None, b: str | None) -> bool:
    """Versions as numbers: "1.1", "1.10" and "1.100" are the same version."""
    if not a or not b:
        return False
    try:
        return Decimal(a).normalize() == Decimal(b).normalize()
    except InvalidOperation:
        return a == b


def _names(font: TTFont) -> set[str]:
    table = font["name"]
    out = set()
    for name_id in (6, 4):
        value = table.getDebugName(name_id)
        if value:
            out.add(value.replace(" ", ""))
    family, style = table.getDebugName(1), table.getDebugName(2)
    if family:
        compact = family.replace(" ", "")
        out.add(compact if style in (None, "Regular") else f"{compact}-{style.replace(' ', '')}")
    return out


def _fonts_in(path: Path) -> list[TTFont]:
    if path.suffix.lower() in (".ttc", ".otc"):
        return list(TTCollection(str(path), lazy=True).fonts)
    return [TTFont(str(path), lazy=True)]


def index_font_files(dirs: Iterable[Path]) -> dict[str, FontFile]:
    """PostScript and full names (spaces removed) of every font file in ``dirs``."""
    return {name: files[0] for name, files in index_all_font_files(dirs).items()}


def index_all_font_files(dirs: Iterable[Path]) -> dict[str, list[FontFile]]:
    """Every font file for each name: one font can be present in several versions."""
    index: dict[str, list[FontFile]] = {}
    for folder in dirs:
        for path in sorted(folder.rglob("*")):
            if path.suffix.lower() not in FONT_SUFFIXES or not path.is_file():
                continue
            try:
                fonts = _fonts_in(path)
            except Exception:  # noqa: BLE001 - unreadable files are skipped
                continue
            for number, font in enumerate(fonts):
                try:
                    names = _names(font)
                except Exception:  # noqa: BLE001
                    continue
                for name in names:
                    files = index.setdefault(name, [])
                    if FontFile(str(path), number) not in files:
                        files.append(FontFile(str(path), number))
    return index


@cache
def installed_font_index() -> dict[str, list[FontFile]]:
    """Every font file in this machine's font folders, by name."""
    return index_all_font_files(default_search_dirs())


@cache
def file_version(file: FontFile) -> str | None:
    try:
        return font_version(TTFont(file.path, fontNumber=file.number, lazy=True))
    except Exception:  # noqa: BLE001
        return None


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _license_of(path: Path, number: int) -> str | None:
    try:
        font = TTFont(str(path), fontNumber=number, lazy=True)
        text = font["name"].getDebugName(13) or font["name"].getDebugName(0)
    except Exception:  # noqa: BLE001
        return None
    return text.replace("\r\n", "\n").split("\n\n")[0].strip() if text else None


def name_candidates(index: dict[str, list[FontFile]], key: str) -> list[FontFile]:
    """The files in ``index`` for a PDF font name, every version of it."""
    key = font_key(key)
    found = list(index.get(key, []))
    if "," in key:
        # "Name,Bold": Word 2019's name for the bold face, or a bold it simulated
        # on the plain face; both files are candidates.
        base, style = key.split(",", 1)
        for name in (f"{base}-{style}", base):
            found += [f for f in index.get(name, []) if f not in found]
    return found


# Where a font can be found (hints only) ----------------------------------------


def open_font_registry() -> dict[str, list[dict]]:
    """Pinned versions of open-licensed fonts per font key, preferred first."""
    text = resources.files("pdf_ar").joinpath("data/open_fonts.toml").read_text("utf-8")
    fonts = tomllib.loads(text)["fonts"]
    return {key: value if isinstance(value, list) else [value] for key, value in fonts.items()}


def pinned_file_name(pinned: dict) -> str:
    return pinned.get("file") or Path(pinned["url"]).name


# The fonts folder -----------------------------------------------------------


class FontLibrary:
    """The font files in the fonts folder, indexed by name; nothing is copied into it.

    ``use_installed`` adds this machine's own files of named fonts, read in place.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._index: dict[str, list[FontFile]] | None = None
        self.installed: dict[str, list[FontFile]] = {}

    @property
    def index(self) -> dict[str, list[FontFile]]:
        if self._index is None:
            self._index = index_all_font_files([self.folder]) if self.folder.is_dir() else {}
        return self._index

    def files(self) -> list[FontFile]:
        """Every font in the folder, and the installed files in use, once."""
        return sorted({f for files in [*self.index.values(), *self.installed.values()]
                       for f in files}, key=lambda f: (f.path, f.number))

    def candidates(self, key: str) -> list[FontFile]:
        """Every file for a PDF font name: the folder's first, then installed ones."""
        found = name_candidates(self.index, key)
        return found + [f for f in self.installed.get(font_key(key), []) if f not in found]

    def use_installed(self, key: str) -> list[FontFile]:
        """Also read this machine's files of a font, every version; returns them."""
        files = [f for f in name_candidates(installed_font_index(), key)
                 if f not in name_candidates(self.index, key)]
        if files:
            self.installed[font_key(key)] = files
        return files

    def describe(self) -> list[dict]:
        """The files in use with version and SHA-256, for the run record."""
        installed = {f for files in self.installed.values() for f in files}
        out = []
        for file in self.files():
            path = Path(file.path)
            entry = {"file": path.name, "number": file.number,
                     "version": file_version(file), "sha256": sha256_of(path),
                     "license": _license_of(path, file.number)}
            if file in installed:
                entry["installed"] = str(path)
            out.append(entry)
        return out
