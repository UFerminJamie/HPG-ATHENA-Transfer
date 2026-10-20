"""Match JSON filename prefixes to slide basenames."""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlsplit


SUFFIXES = {
    "_refined_heatmap.json": "Refined Heatmap",
    "_mask_prediction.json": "Prediction Mask",
    "_mask_gt.json": "Ground Truth Mask",
    "_heatmap.json": "Heatmap",
}
WSI_SUFFIXES = {".ome.tif", ".ome.tiff", ".svs", ".tif", ".tiff", ".ndpi", ".mrxs", ".scn", ".vms", ".vmu", ".bif", ".isyntax", ".svslide", ".czi"}
OBJECT_ID = re.compile(r"^[0-9a-fA-F]{24}$")


@dataclass(frozen=True)
class Source:
    path: Path
    stem: str
    kind: str


@dataclass(frozen=True)
class Slide:
    id: str
    name: str
    basenames: Tuple[str, ...]


def parse_source(path: Path) -> Optional[Source]:
    if path.suffix.lower() != ".json" or not path.stem:
        return None
    for suffix, kind in SUFFIXES.items():
        if path.name.lower().endswith(suffix):
            return Source(path, path.stem, kind)
    return Source(path, path.stem, "Annotation")


def discover(source_dir: Path, recursive: bool = False) -> List[Source]:
    paths = source_dir.rglob("*") if recursive else source_dir.iterdir()
    return sorted((s for p in paths if p.is_file() if (s := parse_source(p))), key=lambda s: str(s.path))


def slide_basename(filename: str, allow_bare: bool = False, known_large_image: bool = False) -> Optional[str]:
    for extension in sorted(WSI_SUFFIXES, key=len, reverse=True):
        if filename.lower().endswith(extension):
            return filename[: -len(extension)]
    suffix = Path(filename).suffix.lower()
    if known_large_image and suffix:
        return filename[: -len(suffix)]
    return filename if allow_bare and not suffix else None


def build_index(slides: Iterable[Slide]) -> Dict[str, List[Slide]]:
    index: Dict[str, List[Slide]] = {}
    for slide in slides:
        for basename in set(slide.basenames):
            existing = index.setdefault(basename, [])
            if not any(other.id == slide.id for other in existing):
                existing.append(slide)
    return index


def match_source(stem: str, index: Dict[str, List[Slide]]) -> Tuple[Optional[str], List[Slide]]:
    """Use the longest slide basename that prefixes a JSON filename stem."""
    names = [name for name in index if stem.startswith(name)]
    if not names:
        return None, []
    longest = max(map(len, names))
    matches = [name for name in names if len(name) == longest]
    slides = {slide.id: slide for name in matches for slide in index[name]}
    return matches[0], list(slides.values())


def parse_destination(value: str) -> Tuple[Optional[str], str]:
    """Return (resource type, object ID); a bare ID is resolved by the API."""
    if OBJECT_ID.fullmatch(value):
        return None, value.lower()
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Destination must be a 24-character ID or Athena collection/folder URL")
    route = unquote(parsed.fragment.split("?", 1)[0] or parsed.path)
    match = re.search(r"(?:^|/)(collection|folder)/([0-9a-fA-F]{24})(?:/|$)", route)
    if not match:
        raise ValueError("Could not find a collection or folder ID in destination URL")
    return match.group(1), match.group(2).lower()
