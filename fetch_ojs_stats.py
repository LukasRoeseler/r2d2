#!/usr/bin/env python3
"""
Fetch per-article, per-month OJS usage statistics (abstract views + galley/PDF
downloads) for every DOI listed in dois.txt, and write them to
statistics/statistics-YYYYMMDD.csv in the format the R2D2 dashboard reads:

    ID,Submission ID,Title,Metric Type,File Type,Month,Count

- Metric Type is "abstract" (abstract-page views) or "galley" (file downloads).
- Month is YYYY-MM.
- One row per (submission, metric, month) with a non-zero count.

Auth: requires the OJS_API_KEY environment variable (an OJS API token for a
user with stats access). It is sent as `Authorization: Bearer <token>`.
"""

import os
import sys
import csv
import time
import re
import datetime
import urllib.request
import urllib.parse
import urllib.error

OJS_BASE = "https://ejournals.uni-muenster.de/index.php/replicationresearch/api/v1"
DOIS_FILE = "dois.txt"
OUT_DIR = "statistics"

# Date window: from the journal's launch (Oct 2025) through today. The journal
# did not exist before October 2025, so there is nothing to query earlier.
DATE_START = "2025-10-01"
DATE_END = datetime.date.today().isoformat()

API_KEY = os.environ.get("OJS_API_KEY", "").strip()
if not API_KEY:
    print("ERROR: OJS_API_KEY environment variable is not set.", file=sys.stderr)
    sys.exit(1)

HEADERS = {
    "Authorization": "Bearer " + API_KEY,
    "Accept": "application/json",
    "User-Agent": "r2d2-stats-bot/1.0 (+https://github.com/LukasRoeseler/r2d2)",
}


def api_get(path, params=None, retries=3):
    """GET an OJS API endpoint and return parsed JSON, or None on failure."""
    url = OJS_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                import json
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s for %s" % (e.code, url)
            # 401/403 are auth problems — no point retrying.
            if e.code in (401, 403):
                print("AUTH ERROR:", last_err, file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last_err = "%s for %s" % (e, url)
            time.sleep(2 * (attempt + 1))
    print("WARN: giving up:", last_err, file=sys.stderr)
    return None


def submission_ids_from_dois(path):
    """Map the trailing integer of each DOI to a submission ID.

    The dashboard derives the OJS submission id from the digits at the end of
    the DOI (e.g. 10.5281/.../12345 -> 12345). We do the same here so the CSV
    keys line up with what the front-end expects.
    """
    ids = []
    if not os.path.exists(path):
        print("ERROR: %s not found." % path, file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            doi = line.strip()
            if not doi:
                continue
            doi = re.sub(r"^https?://doi\.org/", "", doi, flags=re.I)
            m = re.search(r"(\d+)$", doi)
            if m:
                ids.append((m.group(1), doi))
    return ids


def first_locale_value(d):
    """OJS multilingual fields are {locale: value}; pick a sensible one."""
    if isinstance(d, dict):
        for key in ("en_US", "en"):
            if d.get(key):
                return d[key]
        for v in d.values():
            if v:
                return v
    return d or ""


def get_title(sub_id):
    """Fetch a human-readable title for a submission (best effort)."""
    data = api_get("/submissions/%s" % sub_id)
    if not data:
        return ""
    pubs = data.get("publications") or []
    cur_id = data.get("currentPublicationId")
    pub = next((p for p in pubs if p.get("id") == cur_id), pubs[0] if pubs else None)
    if not pub:
        return ""
    return str(first_locale_value(pub.get("title"))).strip()


def monthly_timeline(sub_id, metric):
    """Return {YYYY-MM: count} for a metric ('abstract' or 'galley')."""
    path = "/stats/publications/%s/%s" % (sub_id, metric)
    data = api_get(path, {
        "timelineInterval": "month",
        "dateStart": DATE_START,
        "dateEnd": DATE_END,
    })
    out = {}
    if not data:
        return out
    # OJS returns a list of {date, label, value}. `date` is YYYY-MM-DD for a
    # monthly interval (first of the month) or YYYY-MM; normalise to YYYY-MM.
    rows = data if isinstance(data, list) else data.get("items", [])
    for row in rows:
        date = str(row.get("date") or row.get("label") or "")
        value = row.get("value")
        if value in (None, ""):
            continue
        m = re.match(r"(\d{4})-(\d{2})", date)
        if not m:
            continue
        ym = "%s-%s" % (m.group(1), m.group(2))
        try:
            out[ym] = out.get(ym, 0) + int(value)
        except (TypeError, ValueError):
            continue
    return out


def main():
    ids = submission_ids_from_dois(DOIS_FILE)
    print("Found %d DOIs in %s" % (len(ids), DOIS_FILE))

    os.makedirs(OUT_DIR, exist_ok=True)
    today = datetime.date.today().strftime("%Y%m%d")
    out_path = os.path.join(OUT_DIR, "statistics-%s.csv" % today)

    rows = []
    row_id = 0
    for sub_id, doi in ids:
        title = get_title(sub_id)
        abstract = monthly_timeline(sub_id, "abstract")
        galley = monthly_timeline(sub_id, "galley")

        for ym in sorted(abstract):
            if abstract[ym] <= 0:
                continue
            row_id += 1
            rows.append([row_id, sub_id, title, "abstract", "", ym, abstract[ym]])
        for ym in sorted(galley):
            if galley[ym] <= 0:
                continue
            row_id += 1
            rows.append([row_id, sub_id, title, "galley", "PDF", ym, galley[ym]])

        print("  sub %-8s abstract-months=%-3d galley-months=%-3d  %s"
              % (sub_id, len(abstract), len(galley), title[:50]))
        time.sleep(0.3)  # be polite to the API

    if not rows:
        print("ERROR: no stats rows produced — aborting so we don't commit an "
              "empty CSV.", file=sys.stderr)
        sys.exit(1)

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ID", "Submission ID", "Title", "Metric Type",
                    "File Type", "Month", "Count"])
        w.writerows(rows)

    print("Wrote %d rows to %s" % (len(rows), out_path))


if __name__ == "__main__":
    main()
