# CEVGE rating sync

Automatically refresh the CEVGE member ratings in Google Sheets.

## What it does

The script reads the `Membres` tab and matches members using stable identifiers:

- `FIDE ID` → official FIDE current player list
- `CODE` → Swiss Chess Federation (FSE/SSB) historical Elo detail page

It updates only:

- `ELO (FIDE)`
- `ELO (FSE)`

It also creates or rebuilds a `Ratings_Current` audit tab with FIDE Standard, Rapid and Blitz ratings, metadata, source URLs, update time and any matching/fetch problems.

## Schedule

The GitHub Action runs on the 2nd of every month at 05:15 UTC and can also be run manually.

## One-time setup

### 1. Create a Google service account

In Google Cloud Console:

1. Create or select a project.
2. Enable the Google Sheets API.
3. Go to IAM & Admin → Service Accounts.
4. Create a service account, for example `cevge-rating-sync`.
5. Create a JSON key for it.

### 2. Share the CEVGE Sheet

Share the Google Sheet with the service-account email as **Editor**.

### 3. Add the GitHub secret

In this repository, go to:

`Settings → Secrets and variables → Actions → Secrets → New repository secret`

Create:

`GOOGLE_SERVICE_ACCOUNT_JSON`

Paste the complete contents of the service-account JSON file as the value.

Do not commit the JSON file to the repository.

### 4. Optional GitHub variable

The CEVGE Sheet ID is already a fallback in the script, but you can also add a repository Actions variable named:

`GOOGLE_SHEET_ID`

with value:

`19xK1KZMzmkHfq4wiw1WKbb1SfgEwIwUkaCx75zC4E3g`

### 5. Dry-run test

Go to:

`Actions → Update CEVGE member ratings → Run workflow`

Enable `dry_run` for the first test. The workflow will fetch and match ratings without changing the Sheet.

Then run it again with `dry_run` disabled to perform the real update.

## Data sources

FIDE current player list:

https://ratings.fide.com/download/players_list.zip

Swiss Chess historical Elo detail pages:

`https://adapter.swisschess.ch/schachsport/fl/detail.php?code=<FSE_CODE>`

## Safety behaviour

- Member names, FIDE IDs, FSE codes and unrelated columns are never overwritten.
- If a FIDE ID cannot be matched, the existing FIDE cell is left unchanged.
- If the Swiss Chess page temporarily fails, the existing FSE Elo is left unchanged.
- Problems are recorded in `Ratings_Current` for review.

## Local testing

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Then set:

```bash
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat /path/to/service-account.json)"
export DRY_RUN=true
python sync_ratings.py
```

Set `DRY_RUN=false` to allow writes.
