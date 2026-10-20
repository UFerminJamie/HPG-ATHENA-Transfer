"""Conservative validation of native large_image annotation documents."""

import json
import math
from typing import Any, Dict, List


class AnnotationError(ValueError):
    pass


def _number(value: Any) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _coordinate(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(_number(n) for n in value)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AnnotationError(message)


def validate_annotation(annotation: Any) -> Dict[str, Any]:
    _require(isinstance(annotation, dict), "annotation must be an object")
    _require(set(annotation).issubset({"name", "description", "display", "attributes", "elements"}),
             "annotation has unsupported top-level fields")
    _require(isinstance(annotation.get("name"), str) and bool(annotation["name"].strip()), "annotation needs a nonempty name")
    elements = annotation.get("elements")
    _require(isinstance(elements, list), "annotation needs an elements list")
    if "description" in annotation:
        _require(isinstance(annotation["description"], str), "description must be a string")
    if "attributes" in annotation:
        _require(isinstance(annotation["attributes"], dict), "attributes must be an object")
    if "display" in annotation:
        _require(isinstance(annotation["display"], dict), "display must be an object")
    for i, element in enumerate(elements):
        prefix = "element %d" % i
        _require(isinstance(element, dict), prefix + " must be an object")
        kind = element.get("type")
        _require(isinstance(kind, str) and kind in {"arrow", "point", "circle", "ellipse", "rectangle", "rectanglegrid", "polyline", "heatmap", "griddata", "image", "pixelmap"}, prefix + " has unsupported type")
        if kind in {"point", "circle", "ellipse", "rectangle", "rectanglegrid"}:
            _require(_coordinate(element.get("center")), prefix + " needs a three-number center")
        if kind in {"circle", "ellipse", "rectangle", "rectanglegrid"}:
            for field in (("radius",) if kind == "circle" else ("width", "height")):
                _require(_number(element.get(field)) and element[field] >= 0, prefix + " needs nonnegative " + field)
        if kind in {"ellipse", "rectangle", "rectanglegrid"} and "rotation" in element:
            _require(_number(element["rotation"]), prefix + " has invalid rotation")
        if kind == "rectanglegrid":
            for field in ("widthSubdivisions", "heightSubdivisions"):
                _require(isinstance(element.get(field), int) and not isinstance(element[field], bool) and element[field] > 0, prefix + " needs positive " + field)
        if kind in {"polyline", "arrow"}:
            points = element.get("points")
            _require(isinstance(points, list) and (len(points) == 2 if kind == "arrow" else len(points) >= 2) and all(_coordinate(point) for point in points), prefix + " needs valid three-number points")
            if "holes" in element:
                holes = element["holes"]
                _require(element.get("closed") is True and isinstance(holes, list) and all(isinstance(hole, list) and len(hole) >= 3 and all(_coordinate(point) for point in hole) for hole in holes), prefix + " has invalid polygon holes")
        if kind == "heatmap":
            points = element.get("points")
            _require(isinstance(points, list) and all(isinstance(point, list) and len(point) == 4 and all(_number(n) for n in point) for point in points), prefix + " needs four-number heatmap points")
        if kind == "griddata":
            _require(isinstance(element.get("gridWidth"), int) and not isinstance(element["gridWidth"], bool) and element["gridWidth"] > 0, prefix + " needs positive gridWidth")
            _require(isinstance(element.get("values"), list) and all(_number(n) or n is None for n in element["values"]), prefix + " needs numeric grid values")
        if kind in {"image", "pixelmap"}:
            _require(isinstance(element.get("girderId"), str) and bool(element["girderId"]), prefix + " needs girderId")
        if kind == "pixelmap":
            _require(isinstance(element.get("boundaries"), bool) and isinstance(element.get("categories"), list) and isinstance(element.get("values"), list), prefix + " needs boolean boundaries and categories/values lists")
    return annotation


def parse_documents(raw: bytes) -> List[Dict[str, Any]]:
    try:
        data = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AnnotationError("malformed JSON: " + str(exc)) from None
    if isinstance(data, list):
        _require(bool(data), "annotation list is empty")
        entries = data
    else:
        entries = [data]
    documents = []
    for i, entry in enumerate(entries):
        if isinstance(entry, dict) and "annotation" in entry:
            _require(set(entry) == {"annotation"}, "entry %d has unsupported outer wrapper fields" % i)
            entry = entry["annotation"]
        try:
            documents.append(validate_annotation(entry))
        except AnnotationError as exc:
            raise AnnotationError("entry %d: %s" % (i, exc)) from None
    return documents
