"""Offline regression tests; no API keys or network requests are used."""
import argparse
import copy
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("updater", Path(__file__).with_name("update_stores.py"))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class SourceChecks(unittest.TestCase):
    def setUp(self):
        self.row = {updater.COL_SEQUENCE: "1", updater.COL_NAME: "Test Mart",
                    updater.COL_CATEGORY: "market", updater.COL_POSTAL_CODE: "13437",
                    updater.COL_ADDRESS: "Test address", updater.COL_REFERENCE_DATE: "2025-11-23"}
        self.store = updater.normalize_row(self.row)
        self.store.update(lat=37.42, lng=127.12, geocodingStatus="geocoded")
        self.previous = {"metadata": {"datasetDate": "20251123"}, "stores": [self.store]}

    def status(self, previous, date, stores):
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
            return updater.report_source_check(previous, date, "/test", stores)

    def test_order_and_geocoding_status_do_not_change_source(self):
        second = dict(self.store, id="second", name="Second")
        changed = dict(self.store, geocodingStatus="cached")
        self.assertEqual(updater.row_signature([self.store, second], updater.SOURCE_FIELDS),
                         updater.row_signature([second, changed], updater.SOURCE_FIELDS))

    def test_same_date_content_change_is_detected(self):
        self.assertEqual(self.status(self.previous, "20251123", [dict(self.store, name="Changed")]),
                         "same_date_source_changed")

    def test_new_date_and_first_snapshot(self):
        self.assertEqual(self.status(self.previous, "20251124", [self.store]), "new_reference_date")
        self.assertEqual(self.status(None, "20251123", [self.store]), "first_snapshot")

    def test_legacy_date_does_not_look_like_a_new_release(self):
        previous = copy.deepcopy(self.previous)
        previous["metadata"]["datasetDate"] = "2025-11-23"
        self.assertEqual(self.status(previous, "20251123", [self.store]), "source_unchanged")
        with self.assertRaises(RuntimeError):
            self.status(previous, "20251122", [self.store])

    def test_invalid_and_future_dates_fail(self):
        for value in ("20250230", "2025112", "not-a-date"):
            with self.assertRaises(ValueError):
                updater.parse_reference_date(value)
        with self.assertRaises(RuntimeError):
            self.status(self.previous, "20990101", [self.store])

    def test_incomplete_or_changing_pagination_fails(self):
        oas = {"paths": {"/test": {"get": {"summary": "test_20251123"}}}}
        for pages in ([{"totalCount": 2, "data": [self.row]}, {"totalCount": 2, "data": []}],
                      [{"data": [self.row]}],
                      [{"totalCount": 2, "data": [self.row]}, {"totalCount": 3, "data": [self.row]}]):
            with patch.object(updater, "request_json", side_effect=[oas] + pages):
                with self.assertRaises(RuntimeError):
                    updater.fetch_public_data("dummy", per_page=1)

    def run_refresh(self, coordinate_improved=False):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            old_store = dict(self.store)
            if coordinate_improved:
                old_store.update(lat=None, lng=None)
            updater.write_outputs(output, "20251123", "/test", [old_store])
            paths = [output / "stores.json", output / "stores.csv"]
            before = [p.read_bytes() for p in paths]
            args = argparse.Namespace(output_dir=output, legacy_csv=output / "absent.csv",
                                      geocode_delay=0, max_geocode=0)

            def enrich(stores, *unused):
                stores[0].update(lat=37.42, lng=127.12, geocodingStatus="cached")
                return 0

            with patch.dict(os.environ, {"DATA_GO_KR_API_KEY": "dummy",
                                         "GITHUB_STEP_SUMMARY": str(output / "summary.md")}, clear=True), \
                    patch.object(updater, "parse_args", return_value=args), \
                    patch.object(updater, "fetch_public_data", return_value=("20251123", "/test", [self.row])), \
                    patch.object(updater, "enrich_coordinates", side_effect=enrich), \
                    patch.object(updater, "MIN_COORDINATE_CACHE", 0, create=True), redirect_stdout(io.StringIO()):
                self.assertEqual(updater.main(), 0)
            after = [p.read_bytes() for p in paths]
            summary = (output / "summary.md").read_text()
            self.assertIn("source_unchanged", summary)
            if coordinate_improved:
                self.assertNotEqual(before, after)
                self.assertIn("Publication: rebuilt", summary)
            else:
                self.assertEqual(before, after)
                self.assertIn("generatedAt preserved", summary)

    def test_noop_preserves_json_csv_and_generated_at(self):
        self.run_refresh()

    def test_coordinate_improvement_still_publishes(self):
        self.run_refresh(coordinate_improved=True)

    def test_failed_geocodes_count_toward_request_limit(self):
        stores = [dict(self.store, address=str(i), lat=None, lng=None) for i in range(4)]
        with patch.dict(os.environ, {"KAKAO_REST_API_KEY": "dummy"}, clear=True), \
                patch.object(updater, "geocode_kakao", return_value=None) as lookup:
            self.assertEqual(updater.enrich_coordinates(stores, {}, 0, 2), 0)
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(stores[-1]["geocodingStatus"], "needs_geocoding")

    def test_zero_request_budget_still_reuses_cached_coordinates(self):
        stores = [dict(self.store, lat=None, lng=None)]
        with patch.dict(os.environ, {"KAKAO_REST_API_KEY": "dummy"}, clear=True), \
                patch.object(updater, "geocode_kakao") as lookup:
            updater.enrich_coordinates(stores, {self.store["address"]: (37.42, 127.12)}, 0, 0)
        lookup.assert_not_called()
        self.assertEqual(stores[0]["lat"], 37.42)


if __name__ == "__main__":
    unittest.main()
