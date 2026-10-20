import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from hpg_athena_transfer.annotations import AnnotationError, parse_documents
from hpg_athena_transfer.api import APIError, AthenaClient
from hpg_athena_transfer.matching import Slide, build_index, parse_destination, parse_source, slide_basename
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
        return list(self.records.get(item_id, []))

    def create_annotation(self, item_id, annotation):
        if self.fail:
            raise APIError("Athena HTTP 503 on POST /annotation")
        record = {"_id": "annotation%d" % (len(self.created) + 1), "annotation": annotation}
        self.created.append(record)
        self.records.setdefault(item_id, []).append(record)
        return record


class MatchingTests(unittest.TestCase):
    def test_exact_suffixes(self):
        cases = {"a_heatmap.json": "Heatmap", "a_refined_heatmap.json": "Refined Heatmap",
                 "a_mask_gt.json": "Ground Truth Mask", "a_mask_prediction.json": "Prediction Mask"}
        for filename, kind in cases.items():
            with self.subTest(filename=filename):
                source = parse_source(Path(filename))
                self.assertEqual((source.sample, source.kind), ("a", kind))
        self.assertIsNone(parse_source(Path("a_heatmap.JSON")))
        self.assertIsNone(parse_source(Path("a_heatmap_extra.json")))

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

    def test_destination_url(self):
        ident = "6ad6a9d8e27b973811097f02"
        self.assertEqual(parse_destination("https://athena.rc.ufl.edu/#user/aaaaaaaaaaaaaaaaaaaaaaaa/folder/" + ident), ("folder", ident))
        self.assertEqual(parse_destination(ident), (None, ident))


class AnnotationTests(unittest.TestCase):
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

    def test_ambiguous_unmatched_and_duplicate_source(self):
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
        self.assertEqual(result["summary"].get("ambiguous"), 2)

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
