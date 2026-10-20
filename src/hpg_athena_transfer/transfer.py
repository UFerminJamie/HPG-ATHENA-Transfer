"""Restart-safe transfer workflow and manifest writing."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .annotations import AnnotationError, parse_documents
from .api import APIError, AthenaClient
from .matching import Source, build_index, discover


PROVENANCE_KEY = "hpg_athena_transfer"


def _provenance(record: Dict[str, Any]) -> Dict[str, Any]:
    document = record.get("annotation", record)
    if not isinstance(document, dict):
        return {}
    attrs = document.get("attributes", {})
    value = attrs.get(PROVENANCE_KEY, {}) if isinstance(attrs, dict) else {}
    return value if isinstance(value, dict) and value.get("tool") == PROVENANCE_KEY else {}


def _identical(records: List[Dict[str, Any]], sha256: str, index: int) -> Optional[Dict[str, Any]]:
    for record in records:
        provenance = _provenance(record)
        if provenance.get("sha256") == sha256 and provenance.get("document_index") == index:
            return record
    return None


def _file_summary(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    grouped: Dict[str, set] = {}
    for row in rows:
        grouped.setdefault(row["source_path"], set()).add(row["status"])
    precedence = ("failed", "invalid", "ambiguous", "unmatched", "uploaded", "skipped-identical", "planned")
    return dict(Counter(next(status for status in precedence if status in statuses)
                        for statuses in grouped.values()))


def _write_manifest(path: Path, rows: List[Dict[str, Any]], dry_run: bool,
                    duplicate_slide_basenames: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"dry_run": dry_run,
               "summary": dict(Counter(row["status"] for row in rows)),
               "file_summary": _file_summary(rows),
               "duplicate_slide_basenames": duplicate_slide_basenames,
               "records": rows}
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _row(source: Source) -> Dict[str, Any]:
    return {"source_path": str(source.path.resolve()), "sample_name": source.sample,
            "annotation_type": source.kind, "slide_name": None, "slide_id": None,
            "sha256": None, "document_index": None, "annotation_id": None,
            "annotation_name": None, "status": "", "error": None}


def transfer(client: AthenaClient, source_dir: Path, destination: str, manifest: Path,
             recursive: bool = False, apply: bool = False) -> Dict[str, Any]:
    if not source_dir.is_dir():
        raise ValueError("Source directory does not exist or is not readable")
    sources = discover(source_dir, recursive)
    if not sources:
        raise ValueError("No JSON files with recognized suffixes found")
    slides = client.slides(destination)
    slide_index = build_index(slides)
    duplicate_slide_basenames = sum(len(candidates) > 1 for candidates in slide_index.values())
    source_counts = Counter((source.sample, source.kind) for source in sources)
    rows: List[Dict[str, Any]] = []
    annotation_cache: Dict[str, List[Dict[str, Any]]] = {}

    for source in sources:
        row = _row(source)
        candidates = slide_index.get(source.sample, [])
        if source_counts[(source.sample, source.kind)] > 1:
            row.update(status="ambiguous", error="duplicate source sample and annotation type")
            rows.append(row)
        elif not candidates:
            row.update(status="unmatched", error="no exact WSI item or image filename match")
            rows.append(row)
        elif len(candidates) > 1:
            row.update(status="ambiguous", error="multiple WSI items have this exact basename")
            rows.append(row)
        else:
            slide = candidates[0]
            row.update(slide_name=slide.name, slide_id=slide.id)
            try:
                raw = source.path.read_bytes()
                sha256 = hashlib.sha256(raw).hexdigest()
                row["sha256"] = sha256
                documents = parse_documents(raw)
                if any(PROVENANCE_KEY in document.get("attributes", {}) for document in documents):
                    raise AnnotationError("source attributes already use the reserved provenance key")
                if slide.id not in annotation_cache:
                    annotation_cache[slide.id] = client.annotations(slide.id)
                for index, original in enumerate(documents):
                    document_row = dict(row, document_index=index, annotation_name=original["name"])
                    records = annotation_cache[slide.id]
                    found = _identical(records, sha256, index)
                    if found:
                        document_row.update(status="skipped-identical", annotation_id=found.get("_id"))
                    elif not apply:
                        document_row["status"] = "planned"
                    else:
                        # Add only provenance. All existing document fields and element
                        # structures are copied intact, with no geometry transformation.
                        document = dict(original)
                        document["attributes"] = dict(original.get("attributes", {}))
                        document["attributes"][PROVENANCE_KEY] = {
                            "tool": PROVENANCE_KEY, "source_filename": source.path.name,
                            "sha256": sha256, "document_index": index,
                            "annotation_type": source.kind,
                        }
                        try:
                            created = client.create_annotation(slide.id, document)
                            refreshed = client.annotations(slide.id)
                            annotation_cache[slide.id] = refreshed
                            verified = next((a for a in refreshed if a.get("_id") == created["_id"]
                                             and a.get("annotation", {}).get("name") == original["name"]), None)
                            if verified is None:
                                raise APIError("created annotation could not be verified on the item")
                            document_row.update(status="uploaded", annotation_id=created["_id"])
                        except APIError as exc:
                            # The request may have committed before a timeout. Re-query
                            # before declaring failure, so the next run can safely resume.
                            try:
                                refreshed = client.annotations(slide.id)
                                annotation_cache[slide.id] = refreshed
                                recovered = _identical(refreshed, sha256, index)
                            except APIError:
                                recovered = None
                            if recovered:
                                document_row.update(status="uploaded", annotation_id=recovered.get("_id"))
                            else:
                                document_row.update(status="failed", error=str(exc))
                    rows.append(document_row)
                    _write_manifest(manifest, rows, not apply, duplicate_slide_basenames)
            except (AnnotationError, OSError, ValueError) as exc:
                row.update(status="invalid", error=str(exc) if isinstance(exc, AnnotationError) else type(exc).__name__)
                rows.append(row)
            except APIError as exc:
                row.update(status="failed", error=str(exc))
                rows.append(row)
        _write_manifest(manifest, rows, not apply, duplicate_slide_basenames)
    return {"summary": dict(Counter(row["status"] for row in rows)),
            "file_summary": _file_summary(rows), "duplicate_slide_basenames": duplicate_slide_basenames,
            "files": len(sources), "records": len(rows), "slides": len(slides), "manifest": str(manifest)}
