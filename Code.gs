const CEVGE_CONFIG = {
  membersSheet: "Membres",
  auditSheet: "Ratings_Current",
  fideUrl: "https://ratings.fide.com/download/standard_rating_list.zip",
  monthlyDay: 2,
  monthlyHour: 6,
  timeZone: "Europe/Zurich",
}

const FIDE_FIELDS = [
  ["fide_id", "ID Number"],
  ["name", "Name"],
  ["fed", "Fed"],
  ["sex", "Sex"],
  ["title", "Tit"],
  ["w_title", "WTit"],
  ["o_title", "OTit"],
  ["foa", "FOA"],
  ["standard", "SRtng"],
  ["standard_games", "SGm"],
  ["standard_k", "SK"],
  ["birth_year", "B-day"],
  ["flag", "Flag"],
]

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("CEVGE Ratings")
    .addItem("Update FIDE ratings now", "updateFideRatings")
    .addSeparator()
    .addItem("Install monthly update", "installMonthlyTrigger")
    .addItem("Remove monthly update", "removeMonthlyTrigger")
    .addToUi()
}

function installMonthlyTrigger() {
  removeMonthlyTrigger_(false)

  ScriptApp.newTrigger("updateFideRatings")
    .timeBased()
    .onMonthDay(CEVGE_CONFIG.monthlyDay)
    .atHour(CEVGE_CONFIG.monthlyHour)
    .inTimezone(CEVGE_CONFIG.timeZone)
    .create()

  SpreadsheetApp.getActive().toast(
    "Monthly FIDE update installed for the 2nd of each month.",
    "CEVGE Ratings",
    6
  )
}

function removeMonthlyTrigger() {
  removeMonthlyTrigger_(true)
}

function removeMonthlyTrigger_(showToast) {
  ScriptApp.getProjectTriggers().forEach((trigger) => {
    if (trigger.getHandlerFunction() === "updateFideRatings") {
      ScriptApp.deleteTrigger(trigger)
    }
  })

  if (showToast) {
    SpreadsheetApp.getActive().toast(
      "Monthly FIDE update removed.",
      "CEVGE Ratings",
      5
    )
  }
}

function updateFideRatings() {
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet()
  const membersSheet = spreadsheet.getSheetByName(CEVGE_CONFIG.membersSheet)

  if (!membersSheet) {
    throw new Error(`Sheet '${CEVGE_CONFIG.membersSheet}' not found.`)
  }

  const values = membersSheet.getDataRange().getDisplayValues()
  if (values.length < 2) {
    throw new Error("The Membres sheet contains no member rows.")
  }

  const headers = values[0]
  const nameCol = findColumn_(headers, ["Nom complet", "Full name"])
  const fideIdCol = findColumn_(headers, ["FIDE ID", "FIDE code"])
  const fideEloCol = findColumn_(headers, ["ELO (FIDE)", "FIDE Elo"])
  const fseEloCol = findColumnOptional_(headers, ["ELO (FSE)", "FSE Elo"])
  const fseCodeCol = findColumnOptional_(headers, ["CODE", "FSE code", "Code FSE"])

  const members = []
  const wantedIds = new Set()

  for (let rowIndex = 1; rowIndex < values.length; rowIndex++) {
    const row = values[rowIndex]
    const name = cleanCell_(row[nameCol])
    const fideId = cleanId_(row[fideIdCol])

    if (!name && !fideId) continue

    const member = {
      sheetRow: rowIndex + 1,
      arrayRow: rowIndex,
      name,
      fideId,
      fseCode: fseCodeCol >= 0 ? cleanId_(row[fseCodeCol]) : "",
      historicalFse: fseEloCol >= 0 ? cleanCell_(row[fseEloCol]) : "",
    }

    members.push(member)
    if (fideId) wantedIds.add(fideId)
  }

  if (!wantedIds.size) {
    throw new Error("No FIDE IDs were found in the Membres sheet.")
  }

  spreadsheet.toast(
    `Downloading the FIDE Standard list for ${wantedIds.size} member IDs…`,
    "CEVGE Ratings",
    5
  )

  const response = UrlFetchApp.fetch(CEVGE_CONFIG.fideUrl, {
    muteHttpExceptions: true,
    followRedirects: true,
    headers: {
      "User-Agent": "CEVGE-rating-sync/2.0 (https://www.cevge.com/)",
    },
  })

  const status = response.getResponseCode()
  if (status < 200 || status >= 300) {
    throw new Error(`FIDE download failed with HTTP ${status}.`)
  }

  const blobs = Utilities.unzip(response.getBlob())
  if (!blobs.length) {
    throw new Error("The FIDE ZIP archive was empty.")
  }

  const textBlob = blobs.find((blob) => /\.txt$/i.test(blob.getName())) || blobs[0]
  const text = textBlob.getDataAsString("UTF-8")
  const fide = parseFideStandardList_(text, wantedIds)

  const existingEloRange = membersSheet.getRange(2, fideEloCol + 1, values.length - 1, 1)
  const fideEloValues = existingEloRange.getValues()

  let updatedCount = 0
  let notFoundCount = 0

  members.forEach((member) => {
    if (!member.fideId) return

    const record = fide[member.fideId]
    if (!record) {
      notFoundCount++
      return
    }

    const relativeIndex = member.sheetRow - 2
    fideEloValues[relativeIndex][0] = record.standard || ""
    updatedCount++
  })

  existingEloRange.setValues(fideEloValues)
  writeAuditSheet_(spreadsheet, members, fide)

  const now = Utilities.formatDate(
    new Date(),
    CEVGE_CONFIG.timeZone,
    "yyyy-MM-dd HH:mm:ss"
  )

  spreadsheet.toast(
    `Updated ${updatedCount} FIDE ratings${notFoundCount ? `; ${notFoundCount} IDs not found` : ""}.`,
    "CEVGE Ratings",
    8
  )

  console.log(`FIDE rating sync complete at ${now}`)
  console.log(`Members: ${members.length}`)
  console.log(`Matched: ${updatedCount}/${wantedIds.size}`)

  if (notFoundCount) {
    const missing = members
      .filter((member) => member.fideId && !fide[member.fideId])
      .map((member) => `${member.name || "Unnamed"} (${member.fideId})`)

    console.warn(`FIDE IDs not found: ${missing.join(", ")}`)
  }
}

function parseFideStandardList_(text, wantedIds) {
  const headerEnd = text.indexOf("\n")
  if (headerEnd < 0) {
    throw new Error("Could not read the FIDE list header.")
  }

  const header = text.slice(0, headerEnd).replace(/\r$/, "")
  const spans = fideFieldSpans_(header)
  const results = {}

  const lineRegex = /[^\r\n]+/g
  lineRegex.lastIndex = headerEnd + 1

  let match
  while ((match = lineRegex.exec(text)) !== null) {
    const line = match[0]
    const fideId = cleanId_(fideField_(line, spans, "fide_id"))

    if (!fideId || !wantedIds.has(fideId)) continue

    const ratingText = fideField_(line, spans, "standard")
    const ratingMatch = ratingText.match(/\d+/)
    const flag = fideField_(line, spans, "flag")

    results[fideId] = {
      fideId,
      name: fideField_(line, spans, "name"),
      federation: fideField_(line, spans, "fed"),
      title: fideField_(line, spans, "title").toUpperCase(),
      standard: ratingMatch ? Number(ratingMatch[0]) : null,
      inactive: /i/i.test(flag),
      flag,
    }

    if (Object.keys(results).length === wantedIds.size) break
  }

  return results
}

function fideFieldSpans_(header) {
  const lowerHeader = header.toLowerCase()
  const positions = []

  FIDE_FIELDS.forEach(([key, label]) => {
    const position = lowerHeader.indexOf(label.toLowerCase())
    if (position >= 0) positions.push({ key, position })
  })

  positions.sort((a, b) => a.position - b.position)

  const keys = new Set(positions.map((item) => item.key))
  if (!keys.has("fide_id") || !keys.has("name") || !keys.has("standard")) {
    throw new Error(`Unexpected FIDE list format. Header: ${header}`)
  }

  const spans = {}
  positions.forEach((item, index) => {
    spans[item.key] = {
      start: item.position,
      end: index + 1 < positions.length ? positions[index + 1].position : null,
    }
  })

  return spans
}

function fideField_(line, spans, key) {
  const span = spans[key]
  if (!span) return ""

  return (
    span.end === null
      ? line.slice(span.start)
      : line.slice(span.start, span.end)
  ).trim()
}

function writeAuditSheet_(spreadsheet, members, fide) {
  let sheet = spreadsheet.getSheetByName(CEVGE_CONFIG.auditSheet)

  if (!sheet) {
    sheet = spreadsheet.insertSheet(CEVGE_CONFIG.auditSheet)
  }

  const updatedAt = Utilities.formatDate(
    new Date(),
    CEVGE_CONFIG.timeZone,
    "yyyy-MM-dd HH:mm:ss"
  )

  const rows = [[
    "updated_at",
    "nom_complet",
    "fse_code",
    "fide_id",
    "fide_name",
    "federation",
    "title",
    "fide_standard",
    "inactive",
    "fse_elo_historical",
    "status",
    "source",
  ]]

  members.forEach((member) => {
    const record = member.fideId ? fide[member.fideId] : null

    let status = "OK"
    if (!member.fideId) status = "No FIDE ID"
    else if (!record) status = "FIDE ID not found"
    else if (!record.standard) status = "FIDE unrated"

    rows.push([
      updatedAt,
      member.name,
      member.fseCode,
      member.fideId,
      record ? record.name : "",
      record ? record.federation : "",
      record ? record.title : "",
      record ? record.standard || "" : "",
      record ? record.inactive : "",
      member.historicalFse,
      status,
      CEVGE_CONFIG.fideUrl,
    ])
  })

  sheet.clearContents()
  sheet.getRange(1, 1, rows.length, rows[0].length).setValues(rows)
  sheet.setFrozenRows(1)

  sheet.getRange(1, 1, 1, rows[0].length)
    .setFontWeight("bold")
    .setBackground("#1F1F1F")
    .setFontColor("#FFFFFF")

  sheet.autoResizeColumns(1, rows[0].length)
}

function findColumn_(headers, aliases) {
  const index = findColumnOptional_(headers, aliases)
  if (index < 0) {
    throw new Error(`Missing required column: ${aliases.join(" / ")}`)
  }
  return index
}

function findColumnOptional_(headers, aliases) {
  const wanted = new Set(aliases.map(normalizeHeader_))
  return headers.findIndex((header) => wanted.has(normalizeHeader_(header)))
}

function normalizeHeader_(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/_/g, " ")
    .replace(/\s+/g, " ")
}

function cleanCell_(value) {
  return String(value == null ? "" : value).trim()
}

function cleanId_(value) {
  return cleanCell_(value)
    .replace(/\.0$/, "")
    .replace(/\D/g, "")
}
