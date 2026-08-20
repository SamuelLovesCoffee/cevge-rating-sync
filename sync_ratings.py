#!/usr/bin/env python3

import io
import json
import os
import re
import time
import zipfile
from datetime import datetime, timezone

import gspread
import requests

DEFAULT_SHEET_ID = "19xK1KZMzmkHfq4wiw1WKbb1SfgEwIwUkaCx75zC4E3g"
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip() or DEFAULT_SHEET_ID
MEMBERS_TAB = os.getenv("MEMBERS_TAB", "").strip() or "Membres"
RATINGS_TAB = os.getenv("RATINGS_TAB", "").strip() or "Ratings_Current"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}

FIDE_URL = "https://ratings.fide.com/download/players_list.zip"
FSE_URL = "https://adapter.swisschess.ch/schachsport/fl/detail.php?code={code}"
USER_AGENT = "CEVGE-rating-sync/1.0 (https://www.cevge.com/)"


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
    m = re.search(r"\d+", norm(value))
    if not m:
        return None
    value = int(m.group())
    return value if value > 0 else None


def find_col(headers, aliases):
    wanted = {norm_header(x) for x in aliases}
    for i, header in enumerate(headers):
        if norm_header(header) in wanted:
            return i
    raise RuntimeError(f"Missing column {aliases}. Found: {headers}")


def col_letter(n):
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def google_client():
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON GitHub secret")
    return gspread.service_account_from_dict(json.loads(raw))


def read_members(spreadsheet):
    ws = spreadsheet.worksheet(MEMBERS_TAB)
    values = ws.get_all_values()
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
        def cell(i):
            return row[i].strip() if i < len(row) else ""

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

    return ws, members


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
    low = header.lower()
    found = []
    for key, label in FIDE_LABELS:
        pos = low.find(label.lower())
        if pos >= 0:
            found.append((key, pos))
    found.sort(key=lambda x: x[1])
    keys = {k for k, _ in found}
    if not {"fide_id", "name"}.issubset(keys):
        raise RuntimeError(f"Unexpected FIDE header: {header!r}")
    return {
        key: (start, found[i + 1][1] if i + 1 < len(found) else None)
        for i, (key, start) in enumerate(found)
    }


def fetch_fide(wanted_ids):
    if not wanted_ids:
        return {}

    r = requests.get(FIDE_URL, timeout=120, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    out = {}

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        txts = [x for x in z.namelist() if x.lower().endswith(".txt")]
        if not txts:
            raise RuntimeError("FIDE ZIP contains no TXT file")
        filename = max(txts, key=lambda x: z.getinfo(x).file_size)

        with z.open(filename) as fh:
            header = fh.readline().decode("utf-8-sig", errors="replace").rstrip("\r\n")
            spans = fide_spans(header)

            def field(line, key):
                span = spans.get(key)
                if not span:
                    return ""
                start, end = span
                return line[start:end].strip() if end is not None else line[start:].strip()

            for raw in fh:
                line = raw.decode("utf-8-sig", errors="replace").rstrip("\r\n")
                fide_id = numeric_id(field(line, "fide_id"))
                if fide_id not in wanted_ids:
                    continue
                flag = field(line, "flag")
                out[fide_id] = {
                    "name": field(line, "name"),
                    "fed": field(line, "fed"),
                    "title": field(line, "title").upper(),
                    "standard": safe_int(field(line, "standard")),
                    "rapid": safe_int(field(line, "rapid")),
                    "blitz": safe_int(field(line, "blitz")),
                    "inactive": "i" in flag.lower(),
                }
                if len(out) == len(wanted_ids):
                    break

    return out


FSE_ELO_RE = re.compile(r"\bElo\s*:\s*([0-9]{3,4})\b", re.I)


def fetch_fse(codes):
    out = {}
    session = requests.Session()
    for i, code in enumerate(sorted(codes), start=1):
        url = FSE_URL.format(code=code)
        try:
            r = session.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            text = re.sub(r"<[^>]+>", " ", r.text)
            m = FSE_ELO_RE.search(text)
            out[code] = {"ok": True, "elo": int(m.group(1)) if m else None, "source": url}
            print(f"FSE {i}/{len(codes)} {code}: {out[code]['elo'] or '—'}")
        except Exception as exc:
            out[code] = {"ok": False, "elo": None, "source": url, "error": str(exc)}
            print(f"FSE {i}/{len(codes)} {code}: ERROR {exc}")
        time.sleep(0.15)
    return out


def write_members(ws, members, fide, fse):
    updates = []
    for m in members:
        if m["fide_id"] in fide:
            updates.append({
                "range": f"{col_letter(m['fide_col'])}{m['row']}",
                "values": [[fide[m["fide_id"]].get("standard") or ""]],
            })

        fse_row = fse.get(m["code"])
        if fse_row and fse_row.get("ok"):
            updates.append({
                "range": f"{col_letter(m['fse_col'])}{m['row']}",
                "values": [[fse_row.get("elo") or ""]],
            })

    if DRY_RUN:
        print(f"DRY RUN: would update {len(updates)} cells in {MEMBERS_TAB}")
        return

    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    print(f"Updated {len(updates)} cells in {MEMBERS_TAB}")


def write_audit(spreadsheet, members, fide, fse):
    if DRY_RUN:
        print("DRY RUN: skipping Ratings_Current write")
        return

    try:
        ws = spreadsheet.worksheet(RATINGS_TAB)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(RATINGS_TAB, rows=max(100, len(members) + 10), cols=15)

    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rows = [[
        "updated_at_utc", "nom_complet", "fse_code", "fide_id", "fide_name",
        "fide_federation", "fide_title", "fide_standard", "fide_rapid",
        "fide_blitz", "fide_inactive", "fse_elo_historical", "fide_source",
        "fse_source", "status",
    ]]

    for m in members:
        fr = fide.get(m["fide_id"])
        sr = fse.get(m["code"])
        status = []
        if m["fide_id"] and not fr:
            status.append("FIDE ID not found")
        if m["code"] and sr and not sr.get("ok"):
            status.append("FSE fetch failed")
        if not m["fide_id"]:
            status.append("No FIDE ID")
        if not m["code"]:
            status.append("No FSE code")
        if not status:
            status.append("OK")

        rows.append([
            stamp,
            m["name"],
            m["code"],
            m["fide_id"],
            fr.get("name", "") if fr else "",
            fr.get("fed", "") if fr else "",
            fr.get("title", "") if fr else "",
            fr.get("standard", "") if fr else "",
            fr.get("rapid", "") if fr else "",
            fr.get("blitz", "") if fr else "",
            bool(fr.get("inactive")) if fr else "",
            (sr.get("elo") or "") if sr and sr.get("ok") else m["existing_fse"],
            FIDE_URL if m["fide_id"] else "",
            sr.get("source", "") if sr else "",
            "; ".join(status),
        ])

    ws.clear()
    ws.update(rows, f"A1:O{len(rows)}", value_input_option="RAW")
    ws.freeze(rows=1)


def main():
    gc = google_client()
    spreadsheet = gc.open_by_key(SHEET_ID)
    members_ws, members = read_members(spreadsheet)
    print(f"Read {len(members)} member rows")

    fide_ids = {m["fide_id"] for m in members if m["fide_id"]}
    fse_codes = {m["code"] for m in members if m["code"]}

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
