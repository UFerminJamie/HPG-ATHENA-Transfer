import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from hpg_athena_transfer.annotations import AnnotationError, normalize_scientific_rgba_alpha, parse_documents
from hpg_athena_transfer.api import APIError, AthenaClient
from hpg_athena_transfer.matching import Slide, build_index, match_source, parse_destination, parse_source, slide_basename
from hpg_athena_transfer.transfer import PROVENANCE_KEY, transfer


def document(name="Region"):
    return {"name": name, "attributes": {"study": "synthetic"}, "display": {"visible": True},
            "elements": [{"type": "polyline", "points": [[0, 0, 0], [1, 0, 0], [1, 1, 0]],
                          "closed": True, "holes": [[[0.1, 0.1, 0], [0.2, 0.1, 0], [0.1, 0.2, 0]]]}]}


class FakeClient:
    def __init__(self, slides=None, fail=False):
        self._slides = slides if slides is not None else [Slide("item1", "Exact.SVS", ("Exact",))]
        self.records = {}
        self.fail = fail
        self.created = []

    def slides(self, destination):
        return self._slides

    def annotations(self, item_id):
        return [record for record in self.records.get(item_id, [])
                if record.get("_active") is not False]

    def create_annotation(self, item_id, annotation):
        if self.fail:
            raise APIError("Athena HTTP 503 on POST /annotation")
        record = {"_id": "annotation%d" % (len(self.created) + 1), "annotation": annotation,
                  "_elementCount": len(annotation["elements"])}
        self.created.append(record)
        self.records.setdefault(item_id, []).append(record)
        return record


class MatchingTests(unittest.TestCase):
    def test_any_json_name_and_optional_type_label(self):
        cases = {"a_heatmap.json": "Heatmap", "a_refined_heatmap.json": "Refined Heatmap",
                 "a_mask_gt.json": "Ground Truth Mask", "a_mask_prediction.json": "Prediction Mask"}
        for filename, kind in cases.items():
            with self.subTest(filename=filename):
                source = parse_source(Path(filename))
                self.assertEqual((source.stem, source.kind), (Path(filename).stem, kind))
        self.assertEqual(parse_source(Path("a_heatmap_extra.json")).kind, "Annotation")
        self.assertEqual(parse_source(Path("a_custom.JSON")).stem, "a_custom")
        self.assertIsNone(parse_source(Path("a_custom.txt")))

    def test_wsi_extensions_and_exactness(self):
        self.assertEqual(slide_basename("Case.TIFF"), "Case")
        self.assertEqual(slide_basename("Case.SvS"), "Case")
        self.assertEqual(slide_basename("Case.tif"), "Case")
        self.assertEqual(slide_basename("Case.NDPI"), "Case")
        self.assertEqual(slide_basename("Case.OME.TIFF"), "Case")
        self.assertEqual(slide_basename("Case.custom", known_large_image=True), "Case")
        self.assertIsNone(slide_basename("Case.svs.backup"))
        index = build_index([Slide("1", "Case.SVS", ("Case",)), Slide("2", "CaseExtra.svs", ("CaseExtra",))])
        self.assertEqual([slide.id for slide in index["Case"]], ["1"])
        self.assertNotIn("case", index)

    def test_ambiguous_basename(self):
        index = build_index([Slide("1", "Case.svs", ("Case",)), Slide("2", "Case.tif", ("Case",))])
        self.assertEqual(len(index["Case"]), 2)

    def test_prefix_uses_longest_basename(self):
        index = build_index([Slide("1", "Case.svs", ("Case",)), Slide("2", "Case2.svs", ("Case2",))])
        name, slides = match_source("Case2_any_label", index)
        self.assertEqual(name, "Case2")
        self.assertEqual([slide.id for slide in slides], ["2"])
        self.assertEqual(match_source("case2_any_label", index), (None, []))

    def test_destination_url(self):
        ident = "6ad6a9d8e27b973811097f02"
        self.assertEqual(parse_destination("https://athena.rc.ufl.edu/#user/aaaaaaaaaaaaaaaaaaaaaaaa/folder/" + ident), ("folder", ident))
        self.assertEqual(parse_destination(ident), (None, ident))


class AnnotationTests(unittest.TestCase):
    def test_scientific_rgba_alpha_is_explicit_and_exact(self):
        original = document()
        original["elements"][0]["fillColor"] = "rgba(255, 105, 180, 4.43172e-07)"
        converted, count = normalize_scientific_rgba_alpha(original)
        self.assertEqual(count, 1)
        self.assertEqual(converted["elements"][0]["fillColor"], "rgba(255, 105, 180, 0.000000443172)")
        self.assertEqual(original["elements"][0]["fillColor"], "rgba(255, 105, 180, 4.43172e-07)")

    def test_native_wrapped_and_list_preserve_geometry(self):
        original = document()
        for payload in (original, {"annotation": original}, [original, document("Second")]):
            parsed = parse_documents(json.dumps(payload).encode())
            self.assertEqual(parsed[0], original)
            self.assertEqual(parsed[0]["elements"][0]["holes"], original["elements"][0]["holes"])

    def test_malformed_and_invalid(self):
        for payload in (b"{broken", b"[]", b'{"annotation": {"name": "x"}}',
                        json.dumps(dict(document(), unsupported=True)).encode(),
                        json.dumps({"name": "x", "elements": [{"type": "point", "center": [1, 2]}]}).encode(),
                        json.dumps({"name": "x", "elements": [{"type": "unknown"}]}).encode()):
            with self.subTest(payload=payload):
                with self.assertRaises(AnnotationError):
                    parse_documents(payload)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.manifest = self.root / "manifest.json"

    def write(self, filename, payload):
        (self.source / filename).write_text(json.dumps(payload), encoding="utf-8")

    def test_dry_run_apply_and_identical_skip(self):
        original = document()
        self.write("Exact_mask_gt.json", [original, document("Second")])
        client = FakeClient()
        preview = transfer(client, self.source, "destination", self.manifest)
        self.assertEqual(preview["summary"], {"planned": 2})
        self.assertEqual(client.created, [])
        applied = transfer(client, self.source, "destination", self.manifest, apply=True)
        self.assertEqual(applied["summary"], {"uploaded": 2})
        self.assertEqual(client.created[0]["annotation"]["elements"], original["elements"])
        self.assertEqual(client.created[0]["annotation"]["attributes"]["study"], "synthetic")
        self.assertEqual(client.created[0]["annotation"]["attributes"][PROVENANCE_KEY]["source_filename"], "Exact_mask_gt.json")
        repeated = transfer(client, self.source, "destination", self.manifest, apply=True)
        self.assertEqual(repeated["summary"], {"skipped-identical": 2})
        self.assertEqual(len(client.created), 2)

    def test_ambiguous_unmatched_and_multiple_files_for_one_sample(self):
        self.write("Exact_heatmap.json", document())
        self.write("Missing_heatmap.json", document())
        client = FakeClient([Slide("a", "Exact.svs", ("Exact",)), Slide("b", "Exact.TIF", ("Exact",))])
        result = transfer(client, self.source, "destination", self.manifest, apply=True)
        self.assertEqual(result["summary"], {"ambiguous": 1, "unmatched": 1})
        self.assertFalse(client.created)
        child = self.source / "child"
        child.mkdir()
        (child / "Exact_heatmap.json").write_text(json.dumps(document()))
        result = transfer(FakeClient(), self.source, "destination", self.manifest, recursive=True)
        self.assertEqual(result["summary"], {"planned": 2, "unmatched": 1})

    def test_arbitrary_json_names_are_uploaded_to_matching_slide(self):
        self.write("Exact_regions_v5.json", document("Regions"))
        self.write("Exact_any_other_file.json", document("Regions"))
        self.write("Exactish_regions.json", document("Different slide"))
        client = FakeClient([Slide("a", "Exact.svs", ("Exact",)),
                             Slide("b", "Exactish.tif", ("Exactish",))])
        result = transfer(client, self.source, "destination", self.manifest, apply=True)
        self.assertEqual(result["summary"], {"uploaded": 3})
        records = json.loads(self.manifest.read_text())["records"]
        self.assertEqual({row["source_path"].split("/")[-1]: row["slide_id"] for row in records},
                         {"Exact_regions_v5.json": "a", "Exact_any_other_file.json": "a",
                          "Exactish_regions.json": "b"})
        self.assertEqual({row["annotation_type"] for row in records}, {"Annotation"})
        self.assertEqual(len(client.created), 3)
        repeated = transfer(client, self.source, "destination", self.manifest, apply=True)
        self.assertEqual(repeated["summary"], {"skipped-identical": 3})

    def test_same_filename_in_different_subdirectories_uploads_both(self):
        for folder in ("first", "second"):
            directory = self.source / folder
            directory.mkdir()
            (directory / "Exact_regions.json").write_text(json.dumps(document()))
        client = FakeClient()
        result = transfer(client, self.source, "destination", self.manifest,
                          recursive=True, apply=True)
        self.assertEqual(result["summary"], {"uploaded": 2})
        self.assertEqual(len(client.created), 2)
        repeated = transfer(client, self.source, "destination", self.manifest,
                            recursive=True, apply=True)
        self.assertEqual(repeated["summary"], {"skipped-identical": 2})

    def test_invalid_and_api_failure(self):
        (self.source / "Exact_heatmap.json").write_text("{broken")
        invalid = transfer(FakeClient(), self.source, "destination", self.manifest, apply=True)
        self.assertEqual(invalid["summary"], {"invalid": 1})
        self.write("Exact_heatmap.json", document())
        failed = transfer(FakeClient(fail=True), self.source, "destination", self.manifest, apply=True)
        self.assertEqual(failed["summary"], {"failed": 1})
        self.assertIn("Athena HTTP 503", json.loads(self.manifest.read_text())["records"][0]["error"])

    def test_partial_failure_can_retry(self):
        self.write("Exact_heatmap.json", [document("First"), document("Second")])
        client = FakeClient()
        real_create = client.create_annotation
        def sometimes_fail(item_id, annotation):
            if annotation["name"] == "Second":
                raise APIError("Athena HTTP 503 on POST /annotation")
            return real_create(item_id, annotation)
        client.create_annotation = sometimes_fail
        first = transfer(client, self.source, "destination", self.manifest, apply=True)
        self.assertEqual(first["summary"], {"uploaded": 1, "failed": 1})
        client.create_annotation = real_create
        second = transfer(client, self.source, "destination", self.manifest, apply=True)
        self.assertEqual(second["summary"], {"skipped-identical": 1, "uploaded": 1})

    def test_color_adaptation_requires_opt_in_and_is_reported(self):
        original = document()
        original["elements"][0]["fillColor"] = "rgba(255, 105, 180, 4.43172e-07)"
        self.write("Exact_heatmap.json", original)
        client = FakeClient()
        without_option = transfer(client, self.source, "destination", self.manifest, apply=True)
        self.assertEqual(without_option["summary"], {"invalid": 1})
        self.assertFalse(client.created)
        with_option = transfer(client, self.source, "destination", self.manifest,
                               apply=True, normalize_rgba_alpha=True)
        self.assertEqual(with_option["summary"], {"uploaded": 1})
        record = json.loads(self.manifest.read_text())["records"][0]
        self.assertEqual(record["color_format_adaptations"], 1)
        self.assertEqual(client.created[0]["annotation"]["elements"][0]["fillColor"],
                         "rgba(255, 105, 180, 0.000000443172)")

    def test_both_classes_are_active_and_complete_after_apply(self):
        cortical = document("cortical heatmap")
        medullary = document("medullary heatmap")
        medullary["elements"][0]["fillColor"] = "rgba(1, 2, 3, 4.43172e-07)"
        self.write("Exact_heatmap.json", [cortical, medullary])
        client = FakeClient()
        result = transfer(client, self.source, "destination", self.manifest,
                          apply=True, normalize_rgba_alpha=True)
        self.assertEqual(result["summary"], {"uploaded": 2})
        self.assertEqual([record["_elementCount"] for record in client.annotations("item1")], [1, 1])
        self.assertEqual([row["element_count"] for row in json.loads(self.manifest.read_text())["records"]], [1, 1])
        client.created[1]["_active"] = False
        repeated = transfer(client, self.source, "destination", self.manifest,
                            apply=True, normalize_rgba_alpha=True)
        self.assertEqual(repeated["summary"], {"skipped-identical": 1, "uploaded": 1})
        self.assertEqual(len(client.annotations("item1")), 2)

    def test_incomplete_existing_annotation_is_reported(self):
        self.write("Exact_heatmap.json", document())
        client = FakeClient()
        transfer(client, self.source, "destination", self.manifest, apply=True)
        client.created[0]["_elementCount"] = 0
        repeated = transfer(client, self.source, "destination", self.manifest, apply=True)
        self.assertEqual(repeated["summary"], {"failed": 1})
        self.assertEqual(len(client.created), 1)


class ApiTests(unittest.TestCase):
    def test_paginated_discovery_and_file_basename(self):
        client = AthenaClient("https://example.org/api/v1", "synthetic-test-key")
        calls = []
        def request(method, path, params=None, body=None, authenticated=True):
            calls.append((path, params))
            if path == "/folder/folderid":
                return {"_id": "folderid"}
            if path == "/item":
                return [{"_id": "item1", "name": "display name"}] if params["offset"] == 0 else []
            if path == "/item/item1/files":
                return [{"name": "Exact.TIFF"}]
            if path == "/folder":
                return []
            raise AssertionError(path)
        client._request = request
        # A real destination URL uses a 24-character Girder ID.
        ident = "a" * 24
        def request_with_id(method, path, params=None, body=None, authenticated=True):
            if path == "/folder/" + ident:
                return {"_id": ident}
            return request(method, path, params, body, authenticated)
        client._request = request_with_id
        slides = client.slides("https://example.org/#folder/" + ident)
        self.assertEqual(slides[0].basenames, ("Exact",))
        self.assertTrue(any(path == "/item/item1/files" for path, _ in calls))

    def test_pagination_fetches_second_page(self):
        client = AthenaClient("https://example.org/api/v1", "synthetic-test-key")
        offsets = []
        def request(method, path, params=None, body=None, authenticated=True):
            offsets.append(params["offset"])
            return [{"_id": str(i)} for i in range(100)] if params["offset"] == 0 else [{"_id": "last"}]
        client._request = request
        self.assertEqual(len(list(client.paged("/item"))), 101)
        self.assertEqual(offsets, [0, 100])

    def test_http_error_never_exposes_key(self):
        secret = "synthetic-secret-for-redaction-test"
        client = AthenaClient("https://example.org/api/v1", secret)
        error = HTTPError("https://example.org/api/v1/api_key/token?key=" + secret,
                          401, "Unauthorized", {}, None)
        with patch("hpg_athena_transfer.api.urlopen", side_effect=error):
            with self.assertRaises(APIError) as context:
                client.authenticate()
        self.assertNotIn(secret, str(context.exception))


if __name__ == "__main__":
    unittest.main()
