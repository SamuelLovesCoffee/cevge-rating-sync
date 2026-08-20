#!/usr/bin/env python3

import csv
import io
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SHEET_ID = os.getenv(
    "GOOGLE_SHEET_ID",
    "19xK1KZMzmkHfq4wiw1WKbb1SfgEwIwUkaCx75zC4E3g",
)
MEMBERS_TAB = os.getenv("MEMBERS_TAB", "Membres")

# ratings.fide.com is currently unreachable from GitHub-hosted and Google
# Apps Script infrastructure. This public mirror is generated from FIDE's
# official rating download and split by federation. CEVGE members are SUI.
MIRROR_URL = (
    "https://raw.githubusercontent.com/samuraitruong/"
    "fide-ratings-utils/main/data/SUI/standard/standard.csv"
)
OFFICIAL_SOURCE = "https://ratings.fide.com/download_lists.phtml"

OUTPUT = Path("data/cevge-ratings.csv")
USER_AGENT = "CEVGE-rating-sync/3.1 (https://www.cevge.com/)"


def fetch_bytes(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def clean_id(value: str) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    return re.sub(r"\D", "", text)


def normalize_header(value: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        str(value or "").strip().lower().replace("_", " "),
    )


def find_column(headers, aliases):
    wanted = {normalize_header(alias) for alias in aliases}
    for index, header in enumerate(headers):
        if normalize_header(header) in wanted:
            return index
    raise RuntimeError(f"Missing column {aliases}. Found: {headers}")


def load_member_ids() -> set[str]:
    query = urllib.parse.urlencode({"tqx": "out:csv", "sheet": MEMBERS_TAB})
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?{query}"
    text = fetch_bytes(url).decode("utf-8-sig")
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


def load_fide(wanted_ids: set[str]):
    print("Downloading Swiss Standard rating mirror…")
    payload = fetch_bytes(MIRROR_URL)
    text = payload.decode("utf-8-sig", errors="replace")
    print(f"Downloaded {len(payload) / 1024:.1f} KB")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise RuntimeError("Rating mirror has no CSV header")

    header_map = {normalize_header(name): name for name in reader.fieldnames}

    id_key = header_map.get("id number")
    name_key = header_map.get("name")
    fed_key = header_map.get("fed")
    title_key = header_map.get("title") or header_map.get("tit")
    rating_key = header_map.get("rating")
    month_key = header_map.get("ratingmonth") or header_map.get("rating month")
    flag_key = header_map.get("flag")

    if not id_key or not rating_key:
        raise RuntimeError(f"Unexpected rating mirror header: {reader.fieldnames}")

    found = {}
    rating_months = set()

    for row in reader:
        fide_id = clean_id(row.get(id_key, ""))
        if not fide_id:
            continue

        if month_key and row.get(month_key):
            rating_months.add(str(row[month_key]).strip())

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
            "rating_month": row.get(month_key, "") if month_key else "",
        }

    if rating_months:
        print("Mirror rating month(s): " + ", ".join(sorted(rating_months)))

    print(f"Matched {len(found)}/{len(wanted_ids)} FIDE IDs")

    missing = sorted(wanted_ids.difference(found), key=int)
    if missing:
        print("WARNING: IDs not found in SUI mirror: " + ", ".join(missing))

    return found


def write_cache(wanted_ids: set[str], found: dict):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    with OUTPUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "updated_at_utc",
                "rating_month",
                "fide_id",
                "name",
                "federation",
                "title",
                "standard",
                "inactive",
                "status",
                "source",
                "mirror",
            ],
        )
        writer.writeheader()

        for fide_id in sorted(wanted_ids, key=int):
            record = found.get(fide_id)

            if record:
                writer.writerow({
                    "updated_at_utc": updated_at,
                    "rating_month": record.get("rating_month", ""),
                    "fide_id": fide_id,
                    "name": record["name"],
                    "federation": record["federation"],
                    "title": record["title"],
                    "standard": record["standard"] or "",
                    "inactive": str(bool(record["inactive"])).lower(),
                    "status": "OK" if record["standard"] else "FIDE unrated",
                    "source": OFFICIAL_SOURCE,
                    "mirror": MIRROR_URL,
                })
            else:
                writer.writerow({
                    "updated_at_utc": updated_at,
                    "rating_month": "",
                    "fide_id": fide_id,
                    "name": "",
                    "federation": "",
                    "title": "",
                    "standard": "",
                    "inactive": "",
                    "status": "FIDE ID not found in SUI mirror",
                    "source": OFFICIAL_SOURCE,
                    "mirror": MIRROR_URL,
                })

    print(f"Wrote {OUTPUT}")


def main():
    wanted_ids = load_member_ids()
    found = load_fide(wanted_ids)
    write_cache(wanted_ids, found)


if __name__ == "__main__":
    main()
