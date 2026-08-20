const CEVGE_CONFIG = {
  membersSheet: "Membres",
  auditSheet: "Ratings_Current",
  cacheUrl: "https://raw.githubusercontent.com/SamuelLovesCoffee/cevge-rating-sync/main/data/cevge-ratings.csv",
  monthlyDay: 3,
  monthlyHour: 6,
  timeZone: "Europe/Zurich",
}

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
    "Monthly FIDE update installed for the 3rd of each month.",
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

  spreadsheet.toast(
    "Loading the latest CEVGE FIDE cache from GitHub…",
    "CEVGE Ratings",
    5
  )

  const cache = loadRatingCache_()
  const cacheById = cache.byId

  const members = []
  for (let rowIndex = 1; rowIndex < values.length; rowIndex++) {
    const row = values[rowIndex]
    const name = cleanCell_(row[nameCol])
    const fideId = cleanId_(row[fideIdCol])

    if (!name && !fideId) continue

    members.push({
      sheetRow: rowIndex + 1,
      name,
      fideId,
      fseCode: fseCodeCol >= 0 ? cleanId_(row[fseCodeCol]) : "",
      historicalFse: fseEloCol >= 0 ? cleanCell_(row[fseEloCol]) : "",
    })
  }

  const eloRange = membersSheet.getRange(2, fideEloCol + 1, values.length - 1, 1)
  const eloValues = eloRange.getValues()

  let updatedCount = 0
  let missingCount = 0

  members.forEach((member) => {
    if (!member.fideId) return

    const record = cacheById[member.fideId]
    if (!record) {
      missingCount++
      return
    }

    if (record.standard) {
      eloValues[member.sheetRow - 2][0] = Number(record.standard)
      updatedCount++
    } else if (
      record.status === "FIDE unrated" ||
      record.status === "No published Standard rating"
    ) {
      eloValues[member.sheetRow - 2][0] = ""
      updatedCount++
    } else {
      missingCount++
    }
  })

  eloRange.setValues(eloValues)
  writeAuditSheet_(spreadsheet, members, cache)

  spreadsheet.toast(
    `Updated ${updatedCount} FIDE ratings${missingCount ? `; ${missingCount} IDs need checking` : ""}.`,
    "CEVGE Ratings",
    8
  )

  console.log(`Cache updated: ${cache.updatedAt || "unknown"}`)
  console.log(`Matched/updated: ${updatedCount}`)
  console.log(`Missing/needs checking: ${missingCount}`)
}

function loadRatingCache_() {
  const response = UrlFetchApp.fetch(CEVGE_CONFIG.cacheUrl, {
    muteHttpExceptions: true,
    followRedirects: true,
  })

  const status = response.getResponseCode()
  if (status < 200 || status >= 300) {
    throw new Error(
      `Could not load the GitHub rating cache (HTTP ${status}). ` +
      "Make sure the cevge-rating-sync repository is public and the cache workflow has run."
    )
  }

  const rows = Utilities.parseCsv(response.getContentText("UTF-8"))
  if (rows.length < 2) {
    throw new Error("The GitHub rating cache is empty.")
  }

  const headers = rows[0]
  const fideIdCol = findColumn_(headers, ["fide_id"])
  const nameCol = findColumn_(headers, ["name"])
  const federationCol = findColumn_(headers, ["federation"])
  const titleCol = findColumn_(headers, ["title"])
  const standardCol = findColumn_(headers, ["standard"])
  const inactiveCol = findColumn_(headers, ["inactive"])
  const statusCol = findColumn_(headers, ["status"])
  const updatedCol = findColumn_(headers, ["updated_at_utc"])
  const sourceCol = findColumn_(headers, ["source"])

  const byId = {}
  let updatedAt = ""

  rows.slice(1).forEach((row) => {
    const fideId = cleanId_(row[fideIdCol])
    if (!fideId) return

    updatedAt = updatedAt || cleanCell_(row[updatedCol])

    byId[fideId] = {
      fideId,
      name: cleanCell_(row[nameCol]),
      federation: cleanCell_(row[federationCol]),
      title: cleanCell_(row[titleCol]),
      standard: cleanCell_(row[standardCol]),
      inactive: cleanCell_(row[inactiveCol]).toLowerCase() === "true",
      status: cleanCell_(row[statusCol]),
      source: cleanCell_(row[sourceCol]),
    }
  })

  return { byId, updatedAt }
}

function writeAuditSheet_(spreadsheet, members, cache) {
  let sheet = spreadsheet.getSheetByName(CEVGE_CONFIG.auditSheet)
  if (!sheet) {
    sheet = spreadsheet.insertSheet(CEVGE_CONFIG.auditSheet)
  }

  const rows = [[
    "cache_updated_at_utc",
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
    const record = member.fideId ? cache.byId[member.fideId] : null

    let status = "OK"
    if (!member.fideId) status = "No FIDE ID"
    else if (!record) status = "Not in cache"
    else status = record.status || "OK"

    rows.push([
      cache.updatedAt,
      member.name,
      member.fseCode,
      member.fideId,
      record ? record.name : "",
      record ? record.federation : "",
      record ? record.title : "",
      record ? record.standard : "",
      record ? record.inactive : "",
      member.historicalFse,
      status,
      record ? record.source : "",
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
