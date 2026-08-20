#!/usr/bin/env python3

import io
import os
import re
import time
import zipfile
from datetime import datetime, timezone

import google.auth
import gspread
import requests

DEFAULT_SHEET_ID = "19xK1KZMzmkHfq4wiw1WKbb1SfgEwIwUkaCx75zC4E3g"
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip() or DEFAULT_SHEET_ID
MEMBERS_TAB = os.getenv("MEMBERS_TAB", "").strip() or "Membres"
RATINGS_TAB = os.getenv("RATINGS_TAB", "").strip() or "Ratings_Current"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}

FIDE_URL = "https://ratings.fide.com/download/players_list.zip"
FSE_URL = "https://adapter.swisschess.ch/schachsport/fl/detail.php?code={code}"
USER_AGENT = "CEVGE-rating-sync/1.1 (https://www.cevge.com/)"


def norm(value):
    return str(value or "").strip()


def norm_header(value):
    return re.sub(r"\s+", " ", norm(value).lower().replace("_", " "))


def numeric_id(value):
    text = norm(value)
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return re.sub(r"\D", "", text)


def safe_int(value):
    match = re.search(r"\d+", norm(value))
    if not match:
        return None
    number = int(match.group())
    return number if number > 0 else None


def find_col(headers, aliases):
    wanted = {norm_header(value) for value in aliases}
    for index, header in enumerate(headers):
        if norm_header(header) in wanted:
            return index
    raise RuntimeError(f"Missing column {aliases}. Found: {headers}")


def col_letter(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def google_client():
    """Use Google Application Default Credentials.

    In GitHub Actions these credentials are supplied keylessly by
    google-github-actions/auth through Workload Identity Federation.
    No service-account JSON key is required or supported here.
    """
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials, _ = google.auth.default(scopes=scopes)
    return gspread.authorize(credentials)


def read_members(spreadsheet):
    worksheet = spreadsheet.worksheet(MEMBERS_TAB)
    values = worksheet.get_all_values()
    if not values:
        raise RuntimeError(f"{MEMBERS_TAB} is empty")

    headers = values[0]
    c_name = find_col(headers, ["Nom complet", "Full name"])
    c_code = find_col(headers, ["CODE", "FSE code", "Code FSE"])
    c_fide_id = find_col(headers, ["FIDE ID", "FIDE code"])
    c_fide_elo = find_col(headers, ["ELO (FIDE)", "FIDE Elo"])
    c_fse_elo = find_col(headers, ["ELO (FSE)", "FSE Elo"])

    members = []
    for sheet_row, row in enumerate(values[1:], start=2):
        def cell(index):
            return row[index].strip() if index < len(row) else ""

        name = cell(c_name)
        code = numeric_id(cell(c_code))
        fide_id = numeric_id(cell(c_fide_id))

        if not name and not code and not fide_id:
            continue

        members.append({
            "row": sheet_row,
            "name": name,
            "code": code,
            "fide_id": fide_id,
            "existing_fide": cell(c_fide_elo),
            "existing_fse": cell(c_fse_elo),
            "fide_col": c_fide_elo + 1,
            "fse_col": c_fse_elo + 1,
        })

    return worksheet, members


FIDE_LABELS = [
    ("fide_id", "ID Number"),
    ("name", "Name"),
    ("fed", "Fed"),
    ("title", "Tit"),
    ("standard", "SRtng"),
    ("rapid", "RRtng"),
    ("blitz", "BRtng"),
    ("flag", "Flag"),
]


def fide_spans(header):
    lower = header.lower()
    found = []

    for key, label in FIDE_LABELS:
        position = lower.find(label.lower())
        if position >= 0:
            found.append((key, position))

    found.sort(key=lambda item: item[1])
    keys = {key for key, _ in found}

    if not {"fide_id", "name"}.issubset(keys):
        raise RuntimeError(f"Unexpected FIDE header: {header!r}")

    return {
        key: (start, found[index + 1][1] if index + 1 < len(found) else None)
        for index, (key, start) in enumerate(found)
    }


def fetch_fide(wanted_ids):
    if not wanted_ids:
        return {}

    response = requests.get(
        FIDE_URL,
        timeout=120,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()

    ratings = {}

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        text_files = [
            name for name in archive.namelist()
            if name.lower().endswith(".txt")
        ]
        if not text_files:
            raise RuntimeError("FIDE ZIP contains no TXT file")

        filename = max(text_files, key=lambda name: archive.getinfo(name).file_size)

        with archive.open(filename) as handle:
            header = handle.readline().decode(
                "utf-8-sig", errors="replace"
            ).rstrip("\r\n")
            spans = fide_spans(header)

            def field(line, key):
                span = spans.get(key)
                if not span:
                    return ""
                start, end = span
                return line[start:end].strip() if end is not None else line[start:].strip()

            for raw in handle:
                line = raw.decode("utf-8-sig", errors="replace").rstrip("\r\n")
                fide_id = numeric_id(field(line, "fide_id"))

                if fide_id not in wanted_ids:
                    continue

                flag = field(line, "flag")
                ratings[fide_id] = {
                    "name": field(line, "name"),
                    "fed": field(line, "fed"),
                    "title": field(line, "title").upper(),
                    "standard": safe_int(field(line, "standard")),
                    "rapid": safe_int(field(line, "rapid")),
                    "blitz": safe_int(field(line, "blitz")),
                    "inactive": "i" in flag.lower(),
                }

                if len(ratings) == len(wanted_ids):
                    break

    return ratings


FSE_ELO_RE = re.compile(r"\bElo\s*:\s*([0-9]{3,4})\b", re.I)


def fetch_fse(codes):
    ratings = {}
    session = requests.Session()

    for index, code in enumerate(sorted(codes), start=1):
        url = FSE_URL.format(code=code)

        try:
            response = session.get(
                url,
                timeout=30,
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            text = re.sub(r"<[^>]+>", " ", response.text)
            match = FSE_ELO_RE.search(text)

            ratings[code] = {
                "ok": True,
                "elo": int(match.group(1)) if match else None,
                "source": url,
            }
            print(f"FSE {index}/{len(codes)} {code}: {ratings[code]['elo'] or '—'}")
        except Exception as exc:
            ratings[code] = {
                "ok": False,
                "elo": None,
                "source": url,
                "error": str(exc),
            }
            print(f"FSE {index}/{len(codes)} {code}: ERROR {exc}")

        time.sleep(0.15)

    return ratings


def write_members(worksheet, members, fide, fse):
    updates = []

    for member in members:
        fide_row = fide.get(member["fide_id"])
        if fide_row:
            updates.append({
                "range": f"{col_letter(member['fide_col'])}{member['row']}",
                "values": [[fide_row.get("standard") or ""]],
            })

        fse_row = fse.get(member["code"])
        if fse_row and fse_row.get("ok"):
            updates.append({
                "range": f"{col_letter(member['fse_col'])}{member['row']}",
                "values": [[fse_row.get("elo") or ""]],
            })

    if DRY_RUN:
        print(f"DRY RUN: would update {len(updates)} cells in {MEMBERS_TAB}")
        return

    if updates:
        worksheet.batch_update(updates, value_input_option="RAW")

    print(f"Updated {len(updates)} cells in {MEMBERS_TAB}")


def write_audit(spreadsheet, members, fide, fse):
    if DRY_RUN:
        print("DRY RUN: skipping Ratings_Current write")
        return

    try:
        worksheet = spreadsheet.worksheet(RATINGS_TAB)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            RATINGS_TAB,
            rows=max(100, len(members) + 10),
            cols=15,
        )

    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rows = [[
        "updated_at_utc",
        "nom_complet",
        "fse_code",
        "fide_id",
        "fide_name",
        "fide_federation",
        "fide_title",
        "fide_standard",
        "fide_rapid",
        "fide_blitz",
        "fide_inactive",
        "fse_elo_historical",
        "fide_source",
        "fse_source",
        "status",
    ]]

    for member in members:
        fide_row = fide.get(member["fide_id"])
        fse_row = fse.get(member["code"])
        status = []

        if member["fide_id"] and not fide_row:
            status.append("FIDE ID not found")
        if member["code"] and fse_row and not fse_row.get("ok"):
            status.append("FSE fetch failed")
        if not member["fide_id"]:
            status.append("No FIDE ID")
        if not member["code"]:
            status.append("No FSE code")
        if not status:
            status.append("OK")

        rows.append([
            stamp,
            member["name"],
            member["code"],
            member["fide_id"],
            fide_row.get("name", "") if fide_row else "",
            fide_row.get("fed", "") if fide_row else "",
            fide_row.get("title", "") if fide_row else "",
            fide_row.get("standard", "") if fide_row else "",
            fide_row.get("rapid", "") if fide_row else "",
            fide_row.get("blitz", "") if fide_row else "",
            bool(fide_row.get("inactive")) if fide_row else "",
            (fse_row.get("elo") or "") if fse_row and fse_row.get("ok") else member["existing_fse"],
            FIDE_URL if member["fide_id"] else "",
            fse_row.get("source", "") if fse_row else "",
            "; ".join(status),
        ])

    worksheet.clear()
    worksheet.update(rows, f"A1:O{len(rows)}", value_input_option="RAW")
    worksheet.freeze(rows=1)


def main():
    client = google_client()
    spreadsheet = client.open_by_key(SHEET_ID)
    members_ws, members = read_members(spreadsheet)

    print(f"Read {len(members)} member rows")

    fide_ids = {member["fide_id"] for member in members if member["fide_id"]}
    fse_codes = {member["code"] for member in members if member["code"]}

    print(f"Fetching FIDE data for {len(fide_ids)} IDs")
    fide = fetch_fide(fide_ids)
    print(f"Matched {len(fide)}/{len(fide_ids)} FIDE IDs")

    print(f"Fetching FSE data for {len(fse_codes)} codes")
    fse = fetch_fse(fse_codes)

    write_members(members_ws, members, fide, fse)
    write_audit(spreadsheet, members, fide, fse)

    missing = sorted(fide_ids.difference(fide))
    if missing:
        print("WARNING missing FIDE IDs:", ", ".join(missing))

    print("Rating sync complete")


if __name__ == "__main__":
    main()
