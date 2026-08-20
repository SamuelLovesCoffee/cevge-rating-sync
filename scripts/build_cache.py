#!/usr/bin/env python3

import csv
import io
import os
import re
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SHEET_ID = os.getenv(
    "GOOGLE_SHEET_ID",
    "19xK1KZMzmkHfq4wiw1WKbb1SfgEwIwUkaCx75zC4E3g",
)
MEMBERS_TAB = os.getenv("MEMBERS_TAB", "Membres")
FIDE_URL = "https://ratings.fide.com/download/standard_rating_list.zip"
OUTPUT = Path("data/cevge-ratings.csv")
USER_AGENT = "CEVGE-rating-sync/3.0 (https://www.cevge.com/)"


def fetch_bytes(url: str, timeout: int = 180) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def clean_id(value: str) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    return re.sub(r"\D", "", text)


def normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("_", " "))


def find_column(headers, aliases):
    wanted = {normalize_header(alias) for alias in aliases}
    for index, header in enumerate(headers):
        if normalize_header(header) in wanted:
            return index
    raise RuntimeError(f"Missing column {aliases}. Found: {headers}")


def load_member_ids() -> set[str]:
    query = urllib.parse.urlencode({"tqx": "out:csv", "sheet": MEMBERS_TAB})
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?{query}"
    text = fetch_bytes(url, timeout=60).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise RuntimeError("Membres export was empty")

    fide_col = find_column(rows[0], ["FIDE ID", "FIDE code"])
    ids = {
        clean_id(row[fide_col])
        for row in rows[1:]
        if fide_col < len(row) and clean_id(row[fide_col])
    }

    if not ids:
        raise RuntimeError("No FIDE IDs found in Membres")

    print(f"Loaded {len(ids)} FIDE IDs from Google Sheets")
    return ids


def fixed_width_positions(header: str):
    labels = {
        "id": "ID Number",
        "name": "Name",
        "fed": "Fed",
        "sex": "Sex",
        "title": "Tit",
        "wtit": "WTit",
        "otit": "OTit",
        "foa": "FOA",
        "gms": "Gms",
        "bday": "B-day",
        "flag": "Flag",
    }
    positions = {}
    for key, label in labels.items():
        pos = header.find(label)
        if pos < 0:
            raise RuntimeError(f"Unexpected FIDE header; missing {label!r}: {header!r}")
        positions[key] = pos
    return positions


def parse_fixed_width(text: str, wanted_ids: set[str]):
    lines = text.splitlines()
    if not lines:
        raise RuntimeError("FIDE text file is empty")

    header = lines[0]
    pos = fixed_width_positions(header)
    found = {}

    for line in lines[1:]:
        if not line.strip():
            continue

        fide_id = clean_id(line[pos["id"]:pos["name"]])
        if fide_id not in wanted_ids:
            continue

        name = line[pos["name"]:pos["fed"]].strip()
        federation = line[pos["fed"]:pos["sex"]].strip()
        title = line[pos["title"]:pos["wtit"]].strip()
        rating_region = line[pos["foa"]:pos["gms"]]
        rating_candidates = [
            int(value)
            for value in re.findall(r"\b\d{4}\b", rating_region)
            if 1000 <= int(value) <= 3000
        ]
        standard = rating_candidates[-1] if rating_candidates else None
        flag = line[pos["flag"]:].strip()

        found[fide_id] = {
            "fide_id": fide_id,
            "name": name,
            "federation": federation,
            "title": title,
            "standard": standard,
            "inactive": "i" in flag.lower(),
        }

        if len(found) == len(wanted_ids):
            break

    return found


def parse_csv_text(text: str, wanted_ids: set[str]):
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise RuntimeError("FIDE CSV has no header")

    header_map = {normalize_header(name): name for name in reader.fieldnames}

    id_key = header_map.get("id number")
    name_key = header_map.get("name")
    fed_key = header_map.get("fed")
    title_key = header_map.get("title") or header_map.get("tit")
    rating_key = header_map.get("rating") or header_map.get("srtng")
    flag_key = header_map.get("flag")

    if not id_key or not rating_key:
        raise RuntimeError(f"Unexpected FIDE CSV header: {reader.fieldnames}")

    found = {}
    for row in reader:
        fide_id = clean_id(row.get(id_key, ""))
        if fide_id not in wanted_ids:
            continue

        rating_match = re.search(r"\d+", row.get(rating_key, "") or "")
        standard = int(rating_match.group()) if rating_match else None
        flag = row.get(flag_key, "") if flag_key else ""

        found[fide_id] = {
            "fide_id": fide_id,
            "name": row.get(name_key, "") if name_key else "",
            "federation": row.get(fed_key, "") if fed_key else "",
            "title": row.get(title_key, "") if title_key else "",
            "standard": standard,
            "inactive": "i" in str(flag).lower(),
        }

    return found


def load_fide(wanted_ids: set[str]):
    print("Downloading official FIDE Standard rating list…")
    payload = fetch_bytes(FIDE_URL)
    print(f"Downloaded {len(payload) / 1024 / 1024:.1f} MB")

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        candidates = [
            name for name in archive.namelist()
            if name.lower().endswith((".txt", ".csv")) and not name.endswith("/")
        ]
        if not candidates:
            raise RuntimeError("FIDE ZIP contained no TXT or CSV file")

        filename = max(candidates, key=lambda name: archive.getinfo(name).file_size)
        text = archive.read(filename).decode("utf-8-sig", errors="replace")

    first_line = text.splitlines()[0] if text.splitlines() else ""
    if "," in first_line and "ID Number" in first_line:
        found = parse_csv_text(text, wanted_ids)
    else:
        found = parse_fixed_width(text, wanted_ids)

    print(f"Matched {len(found)}/{len(wanted_ids)} FIDE IDs")
    return found


def write_cache(wanted_ids: set[str], found: dict):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    with OUTPUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "updated_at_utc",
                "fide_id",
                "name",
                "federation",
                "title",
                "standard",
                "inactive",
                "status",
                "source",
            ],
        )
        writer.writeheader()

        for fide_id in sorted(wanted_ids, key=lambda value: int(value)):
            record = found.get(fide_id)
            if record:
                writer.writerow({
                    "updated_at_utc": updated_at,
                    "fide_id": fide_id,
                    "name": record["name"],
                    "federation": record["federation"],
                    "title": record["title"],
                    "standard": record["standard"] or "",
                    "inactive": str(bool(record["inactive"])).lower(),
                    "status": "OK" if record["standard"] else "FIDE unrated",
                    "source": FIDE_URL,
                })
            else:
                writer.writerow({
                    "updated_at_utc": updated_at,
                    "fide_id": fide_id,
                    "name": "",
                    "federation": "",
                    "title": "",
                    "standard": "",
                    "inactive": "",
                    "status": "FIDE ID not found",
                    "source": FIDE_URL,
                })

    print(f"Wrote {OUTPUT}")


def main():
    wanted_ids = load_member_ids()
    found = load_fide(wanted_ids)
    write_cache(wanted_ids, found)


if __name__ == "__main__":
    main()
