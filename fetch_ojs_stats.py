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

BASE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "r2d2-stats-bot/1.0 (+https://github.com/LukasRoeseler/r2d2)",
}

# Auth mode: by default OJS expects the token in the Authorization header.
# Some Apache setups strip that header before it reaches PHP (pkp-lib #9320),
# which makes every call 403 even with a valid token. In that case OJS still
# accepts the token as an `apiToken` query parameter, so we fall back to it
# automatically and remember the choice for the rest of the run.
USE_QUERY_TOKEN = False


def _build_request(path, params):
    import json  # noqa: F401  (used by callers)
    url = OJS_BASE + path
    q = dict(params or {})
    headers = dict(BASE_HEADERS)
    if USE_QUERY_TOKEN:
        q["apiToken"] = API_KEY
    else:
        headers["Authorization"] = "Bearer " + API_KEY
    if q:
        url += "?" + urllib.parse.urlencode(q)
    return urllib.request.Request(url, headers=headers), url


def api_get(path, params=None, retries=3, _allow_fallback=True):
    """GET an OJS API endpoint and return parsed JSON, or None on failure.

    Prints OJS's actual JSON error body on auth failures so the cause is
    visible (e.g. api.403.unauthorized vs api.401.invalidToken), and on a
    header-auth 403 transparently retries once using the apiToken query param.
    """
    global USE_QUERY_TOKEN
    import json
    last_err = None
    for attempt in range(retries):
        req, url = _build_request(path, params)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            last_err = "HTTP %s for %s | %s" % (e.code, url, body)
            if e.code in (401, 403):
                print("AUTH ERROR:", last_err, file=sys.stderr)
                # If the header method was forbidden, try the query-param
                # method once — fixes the Apache header-stripping case.
                if e.code == 403 and not USE_QUERY_TOKEN and _allow_fallback:
                    print("  -> retrying this request with ?apiToken= "
                          "query parameter...", file=sys.stderr)
                    USE_QUERY_TOKEN = True
                    result = api_get(path, params, retries=1,
                                     _allow_fallback=False)
                    if result is not None:
                        print("  -> query-param auth worked; using it for the "
                              "rest of the run.", file=sys.stderr)
                        return result
                    # Didn't help: revert so we don't mask the real problem.
                    USE_QUERY_TOKEN = False
                return None
            time.sleep(2 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last_err = "%s for %s" % (e, url)
            time.sleep(2 * (attempt + 1))
    print("WARN: giving up:", last_err, file=sys.stderr)
    return None


def preflight_check():
    """Hit a manager-only endpoint once to verify the token has access.

    Fails fast with an actionable message instead of 403-ing through every
    DOI. The /stats/publications list endpoint requires the same admin/manager
    role the per-article stats endpoints need, so it's a faithful probe.
    """
    print("Preflight: checking API token access to stats endpoints...")
    data = api_get("/stats/publications", {"count": 1})
    if data is None:
        print(
            "\nPREFLIGHT FAILED. The token reached OJS but was refused.\n"
            "The /submissions and /stats endpoints are restricted to Admin "
            "and Journal Manager accounts.\n"
            "Fix checklist:\n"
            "  1. Generate the API key from a JOURNAL MANAGER (or Admin) "
            "account in Replication Research\n"
            "     (User Profile > API Key), and store it as the OJS_API_KEY "
            "repo secret.\n"
            "  2. Confirm the secret value has no quotes, spaces, or line "
            "breaks around it.\n"
            "  3. If you saw 'api.401' above, api_secret_key may be unset in "
            "the server's config.inc.php (server admin task).\n"
            "  4. The script already auto-tried the ?apiToken= fallback for "
            "the Apache header-stripping case; if that also failed, the cause "
            "is role/permission, not the header.\n",
            file=sys.stderr,
        )
        sys.exit(1)
    print("Preflight OK — token has stats access.\n")


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
    preflight_check()
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
