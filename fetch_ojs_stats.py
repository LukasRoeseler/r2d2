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
OUT_DIR = "statistics"

# Date window: from the journal's launch (Oct 2025) through today. The journal
# did not exist before October 2025, so there is nothing to query earlier.
DATE_START = "2025-10-01"
DATE_END = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

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
    # Token-shape sanity check. OJS API keys are JWTs: three dot-separated
    # base64url segments (header.payload.signature). A secret that doesn't
    # look like one was almost certainly truncated or mis-pasted.
    if API_KEY.count(".") != 2:
        print("WARNING: OJS_API_KEY does not look like a JWT (expected three "
              "dot-separated parts). It may be truncated or mis-pasted.",
              file=sys.stderr)

    print("Preflight: checking API token access to stats endpoints...")
    data = api_get("/stats/publications", {"count": 1})
    if data is None:
        print(
            "\nPREFLIGHT FAILED. The token reached OJS but the call was "
            "refused.\n"
            "Since the account is confirmed to be a Journal Manager/Editor, "
            "role is NOT the cause.\n"
            "Read the error body printed above and match it below:\n"
            "\n"
            "  * Body says 'api.401...'  -> the server is NOT validating the "
            "token.\n"
            "      Most likely 'api_secret_key' is unset in the server's "
            "config.inc.php.\n"
            "      This is a server-admin task for the team running "
            "ejournals.uni-muenster.de.\n"
            "\n"
            "  * Body says 'api.403...' on BOTH header and ?apiToken= attempts "
            "above ->\n"
            "      the token isn't reaching PHP (Apache strips the "
            "Authorization header, pkp-lib #9320),\n"
            "      OR the secret value is corrupted (quotes/newline/partial "
            "copy in the GitHub secret).\n"
            "      Re-copy the key from User Profile > API Key; if it still "
            "fails, ask the server admin to enable\n"
            "      CGIPassAuth / pass the Authorization header to PHP.\n"
            "\n"
            "  * No body shown / network error -> connectivity or the API is "
            "disabled for this journal.\n",
            file=sys.stderr,
        )
        sys.exit(1)
    print("Preflight OK — token has stats access.\n")


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


def fetch_all_published_dois():
    """Fetch every published submission DOI from the OJS API.

    Uses the /submissions endpoint with status=published, paging through all
    results. Returns a list of (submission_id_str, doi_str) tuples, sorted by
    submission ID ascending. Falls back gracefully if the endpoint is
    unavailable (returns empty list so the caller can fall back to dois.txt).
    """
    ids = []
    offset = 0
    page_size = 100
    while True:
        data = api_get("/submissions", {
            "status": "published",
            "count": page_size,
            "offset": offset,
        })
        if not data:
            break
        items = data.get("items") or []
        for sub in items:
            sub_id = str(sub.get("id", ""))
            if not sub_id:
                continue
            # Prefer the DOI from the current publication.
            doi = None
            pubs = sub.get("publications") or []
            cur_id = sub.get("currentPublicationId")
            pub = next((p for p in pubs if p.get("id") == cur_id),
                       pubs[0] if pubs else None)
            if pub:
                doi = pub.get("doiObject", {}).get("doi") if pub.get("doiObject") else None
                if not doi:
                    doi = pub.get("pub-id::doi") or pub.get("doi") or None
            if not doi:
                # Fall back to extracting from urlPublished / _href
                url = (pub or {}).get("urlPublished") or sub.get("_href") or ""
                m = re.search(r"10\.\d{4,}/\S+", url)
                if m:
                    doi = m.group(0).rstrip(".,;)")
            if doi:
                doi = re.sub(r"^https?://doi\.org/", "", doi, flags=re.I).strip()
                ids.append((sub_id, doi))
        total = data.get("itemsMax", 0)
        offset += len(items)
        if not items or offset >= total:
            break
        time.sleep(0.2)
    ids.sort(key=lambda x: int(x[0]) if x[0].isdigit() else 0)
    return ids


def write_publications_json(ids, path="publications.json"):
    """Write publications.json as [{id, doi}, …] sorted by id.

    Only overwrites if content changed (avoids noisy commits).
    Returns True if the file was written.
    """
    import json
    data = [{"id": int(sid) if sid.isdigit() else sid, "doi": doi}
            for sid, doi in ids]
    new_content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            existing = fh.read()
    if new_content == existing:
        print("publications.json is already up to date (%d entries)." % len(ids))
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print("Wrote publications.json with %d entries." % len(ids))
    return True


def main():
    preflight_check()

    # Fetch the live list of published submissions from OJS.
    print("Fetching published submissions from OJS API...")
    live_ids = fetch_all_published_dois()
    if not live_ids:
        print("ERROR: could not fetch any published submissions from the API.",
              file=sys.stderr)
        sys.exit(1)

    write_publications_json(live_ids)
    ids = live_ids
    print("Found %d published submissions." % len(ids))

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
