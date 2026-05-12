#!/usr/bin/env python3
"""Build public Seongnam child allowance merchant data.

Rows are fetched from data.go.kr. Existing coordinates in stores.json/stores.csv
are reused, and only newly missing addresses are geocoded with Kakao Local API.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen

OAS_URL = "https://infuser.odcloud.kr/oas/docs?namespace=15129267/v1"
API_HOST = "https://api.odcloud.kr/api"
LATEST_DATASET_RE = re.compile(r"_(20\d{6})$")

COL_SEQUENCE = "연번"
COL_NAME = "가맹점명"
COL_CATEGORY = "업종명"
COL_POSTAL_CODE = "우편번호"
COL_ADDRESS = "소재지주소"
COL_REFERENCE_DATE = "제공기준일자"
COL_LATITUDE = "위도"
COL_LONGITUDE = "경도"
DATASET_NAME = "경기도 성남시_아동수당 가맹점 현황"
APP_NOTICE = "공공데이터와 지오코딩 결과는 실제 결제 가능 여부와 다를 수 있습니다. 이용 전 가맹점 확인이 필요합니다."


def request_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = Request(url, headers=headers or {})
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def newest_dataset_path(oas: dict[str, Any]) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    for path, methods in oas.get("paths", {}).items():
        summary = methods.get("get", {}).get("summary", "")
        match = LATEST_DATASET_RE.search(summary)
        if match:
            candidates.append((match.group(1), path))
    if not candidates:
        raise RuntimeError("No dated public data endpoint found in OAS document.")
    return max(candidates, key=lambda item: item[0])


def fetch_public_data(service_key: str, per_page: int = 1000) -> tuple[str, str, list[dict[str, Any]]]:
    dataset_date, path = newest_dataset_path(request_json(OAS_URL))
    rows: list[dict[str, Any]] = []
    page = 1
    total_count: int | None = None
    service_key = unquote(service_key)

    while total_count is None or len(rows) < total_count:
        query = urlencode({"page": page, "perPage": per_page, "returnType": "JSON", "serviceKey": service_key})
        payload = request_json(f"{API_HOST}{path}?{query}")
        batch = payload.get("data")
        if "totalCount" not in payload or not isinstance(batch, list):
            raise RuntimeError(f"Unexpected public data response on page {page}: {payload}")
        total_count = int(payload["totalCount"])
        if not batch:
            break
        rows.extend(batch)
        page += 1

    if not rows:
        raise RuntimeError(f"Public data endpoint returned no rows for dataset {dataset_date}.")
    return dataset_date, path, rows


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def stable_id(name: str, category: str, postal_code: str, address: str, sequence: str = "") -> str:
    raw = "|".join([sequence, name, category, postal_code, address])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    sequence = clean_text(row.get(COL_SEQUENCE))
    name = clean_text(row.get(COL_NAME))
    category = clean_text(row.get(COL_CATEGORY))
    postal_code = clean_text(row.get(COL_POSTAL_CODE))
    address = clean_text(row.get(COL_ADDRESS))
    reference_date = clean_text(row.get(COL_REFERENCE_DATE))
    return {
        "id": stable_id(name, category, postal_code, address, sequence),
        "name": name,
        "category": category,
        "postalCode": postal_code,
        "address": address,
        "referenceDate": reference_date,
        "lat": None,
        "lng": None,
        "geocodingStatus": "missing",
    }


def as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def load_coordinate_cache(paths: list[Path]) -> dict[str, tuple[float, float]]:
    cache: dict[str, tuple[float, float]] = {}
    for path in paths:
        if not path.exists():
            continue
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            stores = payload.get("stores", payload if isinstance(payload, list) else [])
            for store in stores:
                address = clean_text(store.get("address") or store.get(COL_ADDRESS))
                lat = as_float(first_present(store.get("lat"), store.get("latitude"), store.get(COL_LATITUDE)))
                lng = as_float(first_present(store.get("lng"), store.get("longitude"), store.get(COL_LONGITUDE)))
                if address and lat is not None and lng is not None:
                    cache[address] = (lat, lng)
        elif path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    address = clean_text(row.get("address") or row.get(COL_ADDRESS))
                    lat = as_float(first_present(row.get("lat"), row.get("latitude"), row.get(COL_LATITUDE)))
                    lng = as_float(first_present(row.get("lng"), row.get("longitude"), row.get(COL_LONGITUDE)))
                    if address and lat is not None and lng is not None:
                        cache[address] = (lat, lng)
    return cache


def geocode_kakao(address: str, rest_api_key: str) -> tuple[float, float] | None:
    query = urlencode({"query": address})
    headers = {"Authorization": f"KakaoAK {rest_api_key}"}
    try:
        payload = request_json(f"https://dapi.kakao.com/v2/local/search/address.json?{query}", headers)
    except HTTPError as exc:
        if exc.code in (400, 404):
            return None
        raise
    documents = payload.get("documents", [])
    if not documents:
        return None
    first = documents[0]
    return float(first["y"]), float(first["x"])


def enrich_coordinates(stores: list[dict[str, Any]], cache: dict[str, tuple[float, float]], delay: float) -> int:
    kakao_rest_api_key = os.getenv("KAKAO_REST_API_KEY", "")
    geocoded = 0
    for store in stores:
        address = store["address"]
        if address in cache:
            store["lat"], store["lng"] = cache[address]
            store["geocodingStatus"] = "cached"
            continue
        if not kakao_rest_api_key:
            store["geocodingStatus"] = "needs_geocoding"
            continue
        result = geocode_kakao(address, kakao_rest_api_key)
        if result:
            store["lat"], store["lng"] = result
            store["geocodingStatus"] = "geocoded"
            cache[address] = result
            geocoded += 1
        else:
            store["geocodingStatus"] = "not_found"
        time.sleep(delay)
    return geocoded


def write_outputs(output_dir: Path, dataset_date: str, endpoint_path: str, stores: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    geocoded_count = sum(1 for store in stores if store["lat"] is not None and store["lng"] is not None)
    categories = sorted({store["category"] for store in stores if store["category"]})
    payload = {
        "metadata": {
            "source": "data.go.kr",
            "provider": "Gyeonggi-do Seongnam-si; original merchant data provided by Shinhan Card",
            "dataset": DATASET_NAME,
            "datasetDate": dataset_date,
            "endpointPath": endpoint_path,
            "generatedAt": generated_at,
            "totalCount": len(stores),
            "geocodedCount": geocoded_count,
            "categories": categories,
            "notice": APP_NOTICE,
        },
        "stores": stores,
    }
    (output_dir / "stores.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    with (output_dir / "stores.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = ["id", "name", "category", "postalCode", "address", "referenceDate", "lat", "lng", "geocodingStatus"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(stores)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Seongnam child allowance merchant data.")
    parser.add_argument("--output-dir", default=".", type=Path)
    parser.add_argument("--geocode-delay", default=0.12, type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    service_key = os.getenv("DATA_GO_KR_API_KEY")
    if not service_key:
        print("DATA_GO_KR_API_KEY is required.", file=sys.stderr)
        return 2
    dataset_date, endpoint_path, raw_rows = fetch_public_data(service_key)
    stores = [normalize_row(row) for row in raw_rows]
    cache = load_coordinate_cache([args.output_dir / "stores.json", args.output_dir / "stores.csv"])
    geocoded = enrich_coordinates(stores, cache, args.geocode_delay)
    write_outputs(args.output_dir, dataset_date, endpoint_path, stores)
    coordinate_count = sum(1 for store in stores if store["lat"] is not None and store["lng"] is not None)
    print(f"Updated {len(stores)} stores from dataset {dataset_date}; {coordinate_count} have coordinates; {geocoded} newly geocoded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
