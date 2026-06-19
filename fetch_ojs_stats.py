#!/usr/bin/env python3
"""
Fetch per-article, per-month OJS usage statistics (abstract views + galley/PDF
downloads) for every published article, and write them to
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

# OJS editorial decision codes (pkp-lib SUBMISSION_EDITOR_DECISION_*).
# Maps the common numeric codes to human-readable labels. Unknown codes
# fall through to an empty label.
DECISION_LABELS = {
    1: "Accept",
    2: "Request revisions (minor)",
    3: "Resubmit for review",
    4: "Decline",
    5: "Send to production",
    7: "Send to external review",
    8: "Send to review (new round)",
    9: "Request revisions (major)",
    14: "Revert decline",
    15: "Accept (skip review)",
    16: "Initial decline",
    17: "Recommend accept",
    18: "Recommend decline",
    19: "Recommend revisions",
    20: "Recommend resubmit",
    21: "Recommend external review",
}

# Date window: from the journal's launch (Oct 2025) through today. The journal
# did not exist before October 2025, so there is nothing to query earlier.
DATE_START = "2025-10-01"
DATE_END = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

# One-time flag so we only dump the first decision object once (debug).
_DEC_DEBUG_DONE = False

# Map common country names (lowercased) to ISO alpha-2 codes, so the CSV can
# be edited with human-readable names ("Germany") and the dashboard still gets
# a code for its map/list. Already-valid 2-letter codes pass through.
_NAME_TO_ISO2 = {
    "argentina": "AR", "australia": "AU", "austria": "AT", "belgium": "BE",
    "brazil": "BR", "canada": "CA", "switzerland": "CH", "chile": "CL",
    "china": "CN", "colombia": "CO", "czechia": "CZ", "czech republic": "CZ",
    "germany": "DE", "denmark": "DK", "estonia": "EE", "egypt": "EG",
    "spain": "ES", "finland": "FI", "france": "FR", "united kingdom": "GB",
    "great britain": "GB", "uk": "GB", "greece": "GR", "hong kong": "HK",
    "croatia": "HR", "hungary": "HU", "indonesia": "ID", "ireland": "IE",
    "israel": "IL", "india": "IN", "iran": "IR", "italy": "IT", "japan": "JP",
    "kenya": "KE", "south korea": "KR", "korea": "KR", "lithuania": "LT",
    "luxembourg": "LU", "latvia": "LV", "mexico": "MX", "malaysia": "MY",
    "nigeria": "NG", "netherlands": "NL", "the netherlands": "NL",
    "norway": "NO", "new zealand": "NZ", "peru": "PE", "philippines": "PH",
    "pakistan": "PK", "poland": "PL", "portugal": "PT", "romania": "RO",
    "serbia": "RS", "russia": "RU", "sweden": "SE", "singapore": "SG",
    "slovenia": "SI", "slovakia": "SK", "thailand": "TH", "turkey": "TR",
    "türkiye": "TR", "taiwan": "TW", "ukraine": "UA", "united states": "US",
    "united states of america": "US", "usa": "US", "us": "US",
    "uruguay": "UY", "vietnam": "VN", "south africa": "ZA",
}


def to_iso2(value):
    """Normalise a country value (name or code) to an ISO alpha-2 code.
    Returns '' if it can't be resolved. Unknown 2-letter inputs pass through.
    """
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return _NAME_TO_ISO2.get(v.lower(), "")


# Manual status overrides. Maps a case-insensitive title substring to a status
# that overrides whatever OJS reports. Use this for editorial states OJS does
# not track (e.g. a paper withdrawn by the authors after submission).
STATUS_OVERRIDES = [
    ("metamotivational beliefs about promotion and prevention focus",
     "Withdrawn"),
]


def apply_status_overrides(details):
    """Force the status of specific submissions, matched by title substring."""
    for d in details:
        title = (d.get("title") or "").lower()
        for needle, new_status in STATUS_OVERRIDES:
            if needle in title:
                d["status"] = new_status
                d["isPublished"] = False
                print("  [override] '%s...' -> status '%s'"
                      % (d.get("title", "")[:40], new_status))
                break


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
    """OJS multilingual fields are {locale: value}; pick a sensible one.
    Always returns a plain string (never a stringified dict)."""
    if isinstance(d, dict):
        val = None
        for key in ("en_US", "en"):
            if d.get(key):
                val = d[key]
                break
        if val is None:
            for v in d.values():
                if v:
                    val = v
                    break
        # Guard: if the chosen value is itself a dict/list, recurse or bail.
        if isinstance(val, dict):
            return first_locale_value(val)
        if val is None:
            return ""
        return str(val)
    if d is None:
        return ""
    return str(d)


# Cache of userId -> country code (alpha-2), to avoid refetching the same
# user across multiple submissions.
_USER_COUNTRY_CACHE = {}
_USER_DEBUG_DONE = False
_PART_DEBUG_DONE = False


def get_user_country(user_id):
    """Fetch a user's profile and return their country as an ISO alpha-2
    code (e.g. 'DE'), or '' if unavailable. Results are cached.
    """
    if not user_id:
        return ""
    if user_id in _USER_COUNTRY_CACHE:
        return _USER_COUNTRY_CACHE[user_id]
    data = api_get("/users/%s" % user_id) or {}
    global _USER_DEBUG_DONE
    if data and not _USER_DEBUG_DONE:
        _USER_DEBUG_DONE = True
        print("  [debug] /users/%s keys: %s" % (user_id, sorted(data.keys())),
              file=sys.stderr)
        print("  [debug] user country=%r userName=%r"
              % (data.get("country"), data.get("userName")), file=sys.stderr)
    c = data.get("country")
    if isinstance(c, dict):
        c = first_locale_value(c)
    c = (c or "").strip().upper()
    if not (len(c) == 2 and c.isalpha()):
        c = ""
    _USER_COUNTRY_CACHE[user_id] = c
    return c


def get_submitter_user_id(submission, sub_id=None):
    """Find the submitting user's ID from a submission object. Tries the
    explicit submitter field first, then the author-role stage assignment,
    then the dedicated participants endpoint.
    """
    # Some OJS versions expose the submitter directly.
    for key in ("submitterId", "userId", "submitter"):
        v = submission.get(key)
        if isinstance(v, int):
            return v
        if isinstance(v, dict) and isinstance(v.get("id"), int):
            return v["id"]

    # stageAssignments embedded on the submission object.
    def scan_assignments(assignments):
        for a in (assignments or []):
            ug = a.get("userGroup") or {}
            role_id = ug.get("roleId") if isinstance(ug, dict) else None
            uid = a.get("userId") or (a.get("user") or {}).get("id")
            if role_id == 65536 and uid:
                return uid
        for a in (assignments or []):
            uid = a.get("userId") or (a.get("user") or {}).get("id")
            if uid:
                return uid
        return None

    uid = scan_assignments(submission.get("stageAssignments"))
    if uid:
        return uid

    # Fall back to the dedicated participants endpoint, which lists everyone
    # assigned to the submission (including the submitting author).
    if sub_id is not None:
        parts = api_get("/submissions/%s/participants" % sub_id)
        global _PART_DEBUG_DONE
        if parts and not _PART_DEBUG_DONE:
            _PART_DEBUG_DONE = True
            p0 = parts[0] if isinstance(parts, list) and parts else parts
            if isinstance(p0, dict):
                print("  [debug] participant[0] keys: %s" % sorted(p0.keys()),
                      file=sys.stderr)
                print("  [debug] participant[0] id=%r country=%r"
                      % (p0.get("id"), p0.get("country")), file=sys.stderr)
        if isinstance(parts, list):
            # Each participant is a user object; prefer one whose stage
            # assignment is author, else take the first with a country.
            for p in parts:
                if p.get("country"):
                    return p.get("id")
            if parts:
                return parts[0].get("id")
    return None


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
    """Fetch every published submission ID from /stats/publications (which we
    know works), then retrieve the DOI for each from /submissions/{id}.

    Returns a list of (submission_id_str, doi_str) tuples sorted by ID.
    Uses the stats list endpoint — no separate /submissions list call needed,
    avoiding the unclear status-filter behaviour of that endpoint.
    """
    # Step 1: collect all submission IDs from the stats endpoint.
    sub_ids = []
    offset = 0
    page_size = 100
    while True:
        data = api_get("/stats/publications", {
            "count": page_size,
            "offset": offset,
            "dateStart": DATE_START,
            "dateEnd": DATE_END,
        })
        if not data:
            break
        items = data.get("items") or []
        for item in items:
            pub = item.get("publication") or {}
            sid = str(pub.get("id") or item.get("submissionId") or "")
            if sid and sid not in sub_ids:
                sub_ids.append(sid)
        total = data.get("itemsMax", 0)
        offset += len(items)
        if not items or offset >= total:
            break
        time.sleep(0.2)

    if not sub_ids:
        return []

    # Step 2: for each submission ID fetch DOI from /submissions/{id}.
    ids = []
    for sid in sub_ids:
        det = get_submission_detail(sid)
        doi = det["doi"] if det else ""
        if doi:
            ids.append((sid, doi))
        time.sleep(0.15)

    ids.sort(key=lambda x: int(x[0]) if x[0].isdigit() else 0)
    return ids



def get_submission_detail(sub_id):
    """Return a per-submission dict in one or two API calls:
    title, doi, dateSubmitted, status (Published/Accepted/Declined/Under
    review), latest decision, and a milestone timeline. Best-effort.
    """
    data = api_get("/submissions/%s" % sub_id)
    if not data:
        return None
    pubs = data.get("publications") or []
    cur_id = data.get("currentPublicationId")
    pub = next((p for p in pubs if p.get("id") == cur_id),
               pubs[0] if pubs else None)

    title, doi = "", ""
    if pub:
        title = str(first_locale_value(pub.get("title") or "")).strip()
        doi_obj = pub.get("doiObject") or {}
        doi = (doi_obj.get("doi") if isinstance(doi_obj, dict) else None) \
            or pub.get("pub-id::doi") or pub.get("doi") or ""
        if not doi:
            url = pub.get("urlPublished") or data.get("_href") or ""
            m = re.search(r"10\.\d{4,}/\S+", url)
            if m:
                doi = m.group(0).rstrip(".,;)")
        doi = re.sub(r"^https?://doi\.org/", "", str(doi), flags=re.I).strip()

    date_submitted = (data.get("dateSubmitted") or "")[:10]

    # ---- Submitter country -----------------------------------------------
    # The reliable country value lives on the submitting user's profile, not
    # on the publication's author records. Find the submitter and look it up.
    submitter_id = get_submitter_user_id(data, sub_id)
    country = get_user_country(submitter_id)

    # Collect ALL author countries from the publication's author records.
    all_countries = []
    if pub:
        for author in (pub.get("authors") or []):
            ac = author.get("country")
            if isinstance(ac, dict):
                ac = first_locale_value(ac)
            ac = (ac or "").strip().upper()
            if len(ac) == 2 and ac.isalpha():
                all_countries.append(ac)

    # Fallback for the primary country: first author with a country.
    if not country and all_countries:
        country = all_countries[0]

    # ---- Status ----------------------------------------------------------
    # OJS submission status: 1=queued, 3=published, 4=declined, 5=scheduled.
    # We collapse everything to four labels: Published, Accepted, Declined,
    # or "Under review" (the catch-all for anything still in the workflow).
    status_code = data.get("status")
    is_published = (status_code == 3) or bool(
        pub and pub.get("datePublished"))
    if is_published:
        status_label = "Published"
    elif status_code == 4:
        status_label = "Declined"
    elif status_code == 5:
        status_label = "Accepted"   # scheduled for publication = accepted
    else:
        status_label = "Under review"

    # ---- Editorial decisions (full list) ---------------------------------
    # The embedded `decisions` array on the submission object is often sparse
    # (known OJS quirk). The dedicated /submissions/{id}/decisions endpoint is
    # the reliable source. Fall back to the embedded array if it's empty.
    decisions = api_get("/submissions/%s/decisions" % sub_id)
    if isinstance(decisions, dict):
        decisions = decisions.get("items") or []
    if not decisions:
        decisions = data.get("decisions") or []
    # Debug dump of the first decision object so we can verify codes/dates.
    global _DEC_DEBUG_DONE
    if decisions and not _DEC_DEBUG_DONE:
        _DEC_DEBUG_DONE = True
        d0 = decisions[0]
        print("  [debug] first decision keys: %s" % sorted(d0.keys()),
              file=sys.stderr)
        print("  [debug] first decision 'decision'=%r dateDecided=%r"
              % (d0.get("decision"), d0.get("dateDecided")), file=sys.stderr)

    decision_label = ""
    decision_date = ""
    if decisions:
        last = decisions[-1]
        decision_date = (last.get("dateDecided") or "")[:10]
        decision_label = DECISION_LABELS.get(last.get("decision"), "")
    if not decision_label and status_label in ("Published", "Declined",
                                               "Accepted"):
        decision_label = status_label

    # ---- Build the per-submission event timeline -------------------------
    # Only four milestone kinds: Submitted, Accepted, Declined, Published.
    # Each event: {date, label, kind}. kind drives the dot colour in the UI.
    events = []
    if date_submitted:
        events.append({"date": date_submitted, "label": "Submitted",
                       "kind": "submitted"})

    # Decision milestones -> collapse to Accepted / Declined only.
    ACCEPT_CODES = {1, 15, 17}         # accept / accept-skip / recommend accept
    DECLINE_CODES = {4, 16, 18}        # decline / initial decline / recommend decline
    for d in decisions:
        code = d.get("decision")
        dd = (d.get("dateDecided") or "")[:10]
        if not dd:
            continue
        if code in ACCEPT_CODES:
            events.append({"date": dd, "label": "Accepted", "kind": "accepted"})
        elif code in DECLINE_CODES:
            events.append({"date": dd, "label": "Declined", "kind": "declined"})

    # Publication.
    date_published = ""
    if pub and pub.get("datePublished"):
        date_published = str(pub.get("datePublished"))[:10]
    if date_published:
        events.append({"date": date_published, "label": "Published",
                       "kind": "published"})

    # Sort chronologically; drop exact duplicate (date,label) pairs.
    seen = set()
    timeline = []
    for ev in sorted(events, key=lambda e: e["date"]):
        key = (ev["date"], ev["label"])
        if key in seen:
            continue
        seen.add(key)
        timeline.append(ev)

    return {
        "id": int(sub_id) if str(sub_id).isdigit() else sub_id,
        "title": title,
        "doi": doi,
        "dateSubmitted": date_submitted,
        "country": country,
        "countries": all_countries,
        "status": status_label,
        "isPublished": is_published,
        "decision": decision_label,
        "decisionDate": decision_date,
        "timeline": timeline,
    }


def fetch_all_submission_ids():
    """Return all submission IDs the manager account can see.

    Tries the /submissions list endpoint with the integer published-status
    filter; if that yields nothing, falls back to the IDs already gathered
    from /stats/publications (published items only).
    """
    ids = []
    offset = 0
    page_size = 100
    # OJS status constants: 1=queued, 3=published, 4=declined, 5=scheduled.
    while True:
        data = api_get("/submissions", {
            "count": page_size,
            "offset": offset,
        })
        if not data:
            break
        items = data.get("items") or []
        for sub in items:
            sid = str(sub.get("id") or "")
            if sid and sid not in ids:
                ids.append(sid)
        total = data.get("itemsMax", 0)
        offset += len(items)
        if not items or offset >= total:
            break
        time.sleep(0.2)
    return ids


COUNTRY_CSV = "submission_countries.csv"


def merge_country_csv(details, path=COUNTRY_CSV):
    """Maintain a human-editable CSV of submitting-author countries, keyed by
    submission ID. Rules:
      * Existing rows are preserved — the `country` you typed is NEVER
        overwritten by an automated run.
      * New submissions are appended with the API-detected country as a
        starting value (often blank); you can correct it on GitHub.
      * Title and submission date are refreshed (safe, non-manual fields).
    Returns a dict {id(str): country} reflecting the merged file, so the
    caller can feed your manual values back into submissions.json.
    Columns: submission_id, date_submitted, title, country
    """
    existing = {}
    order = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                sid = (row.get("submission_id") or "").strip()
                if not sid:
                    continue
                existing[sid] = {
                    "submission_id": sid,
                    "date_submitted": (row.get("date_submitted") or "").strip(),
                    "title": (row.get("title") or "").strip(),
                    # The manually-maintained field — preserved verbatim.
                    "country": (row.get("country") or "").strip(),
                }
                order.append(sid)

    detail_by_id = {str(d["id"]): d for d in details}

    # Update/append from current submissions.
    for sid, d in detail_by_id.items():
        if sid in existing:
            # Preserve the manually-entered country. Refresh title/date only.
            existing[sid]["title"] = d.get("title", existing[sid]["title"])
            existing[sid]["date_submitted"] = (
                d.get("dateSubmitted") or existing[sid]["date_submitted"])
            # If the country cell is still empty and the API now has a guess,
            # seed it (this does not overwrite anything you typed).
            if not existing[sid]["country"] and d.get("country"):
                existing[sid]["country"] = d["country"]
        else:
            existing[sid] = {
                "submission_id": sid,
                "date_submitted": d.get("dateSubmitted", ""),
                "title": d.get("title", ""),
                "country": d.get("country", ""),  # API guess or blank
            }
            order.append(sid)

    # Sort by submission date (oldest first), then ID, for a stable file.
    rows = [existing[s] for s in dict.fromkeys(order) if s in existing]
    rows.sort(key=lambda r: (r["date_submitted"] or "", r["submission_id"]))

    new_content_lines = []
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=["submission_id", "date_submitted", "title",
                         "country"])
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    new_content = buf.getvalue()

    old_content = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            old_content = fh.read()
    if new_content != old_content:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write(new_content)
        print("Wrote %s with %d rows." % (path, len(rows)))
    else:
        print("%s unchanged (%d rows)." % (path, len(rows)))

    # Return merged country map (manual edits take priority).
    return {r["submission_id"]: r["country"] for r in rows if r["country"]}


def write_submissions_json(details, path="submissions.json"):
    """Write submissions.json: list of submissions + monthly counts +
    country totals. Only overwrites when changed.
    """
    import json

    # Drop obvious test/placeholder submissions: empty titles, stringified
    # locale dicts, or trivial one/two-char placeholder titles.
    def is_test(d):
        t = (d.get("title") or "").strip()
        if not t:
            return True
        if t.startswith("{") and "en_US" in t:   # stringified locale dict
            return True
        if t.lower() in ("a", "test", "aa", "abc", "asdf", "xxx"):
            return True
        return False

    before = len(details)
    details = [d for d in details if not is_test(d)]
    dropped = before - len(details)
    if dropped:
        print("  filtered out %d test/placeholder submission(s)." % dropped)

    # Per-month submission counts, broken down by status (YYYY-MM ->
    # {Published, Accepted, "Under review", Declined, Withdrawn}).
    STATUS_KEYS = ["Published", "Accepted", "Under review", "Declined",
                   "Withdrawn"]
    by_month = {}
    for d in details:
        ds = d.get("dateSubmitted") or ""
        if len(ds) < 7:
            continue
        ym = ds[:7]
        st = d.get("status") or "Under review"
        if st not in STATUS_KEYS:
            st = "Under review"
        if ym not in by_month:
            by_month[ym] = {k: 0 for k in STATUS_KEYS}
        by_month[ym][st] += 1

    # Country totals across submissions. Count every distinct author country
    # per submission (so a paper with authors from DE and US adds to both).
    by_country = {}
    for d in details:
        seen = set()
        for c in (d.get("countries") or ([d.get("country")] if d.get("country") else [])):
            c = (c or "").strip().upper()
            if c and c not in seen:
                seen.add(c)
                by_country[c] = by_country.get(c, 0) + 1

    # Aggregate totals for the sidebar.
    totals = {
        "published": sum(1 for d in details if d.get("status") == "Published"),
        "accepted": sum(1 for d in details if d.get("status") == "Accepted"),
        "declined": sum(1 for d in details if d.get("status") == "Declined"),
        "underReview": sum(1 for d in details
                           if d.get("status") == "Under review"),
        "withdrawn": sum(1 for d in details if d.get("status") == "Withdrawn"),
        "countries": len(by_country),
    }

    payload = {
        "generated": datetime.date.today().isoformat(),
        "submissions": sorted(
            [{"id": d["id"], "title": d["title"], "doi": d["doi"],
              "dateSubmitted": d["dateSubmitted"],
              "status": d.get("status", ""),
              "isPublished": d.get("isPublished", False),
              "country": d.get("country", ""),
              "countries": d.get("countries", []),
              "decision": d.get("decision", ""),
              "decisionDate": d.get("decisionDate", ""),
              "timeline": d.get("timeline", [])} for d in details],
            key=lambda x: x.get("dateSubmitted") or "",
        ),
        "byMonth": by_month,
        "byCountry": by_country,
        "totals": totals,
    }
    new_content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            existing = fh.read()
    if new_content == existing:
        print("submissions.json is already up to date "
              "(%d submissions)." % len(details))
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print("Wrote submissions.json with %d submissions, %d months."
          % (len(details), len(by_month)))
    return True


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

    # Fetch the live list of published submissions (ID + DOI) from OJS.
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

    # ---- Submissions tab data: dates, status, decisions, timeline -------
    # Discover the full set of submission IDs the manager account can see
    # (includes non-published if the list endpoint allows it). Fall back to
    # the published set so the tab always has data.
    print("Discovering all submission IDs for the submissions tab...")
    all_ids = fetch_all_submission_ids()
    if not all_ids:
        all_ids = [sid for sid, _ in ids]
        print("  list endpoint returned nothing; using %d published IDs."
              % len(all_ids))
    else:
        print("  found %d submissions via the list endpoint." % len(all_ids))

    submission_details = []
    detail_by_id = {}
    for sid in all_ids:
        det = get_submission_detail(sid)
        if det:
            submission_details.append(det)
            detail_by_id[str(det["id"])] = det
        time.sleep(0.2)
    if submission_details:
        # Maintain the human-editable country CSV and let manual entries
        # take priority over API-detected countries in the dashboard.
        country_map = merge_country_csv(submission_details)
        for d in submission_details:
            # Build the final country list: manual CSV entry (which may be a
            # semicolon-separated list of names/codes) unioned with the
            # author countries detected from the API. Manual entries first.
            codes = []
            manual = country_map.get(str(d["id"]))
            if manual:
                for part in manual.split(";"):
                    code = to_iso2(part)
                    if code and code not in codes:
                        codes.append(code)
            for c in d.get("countries", []):
                if c and c not in codes:
                    codes.append(c)
            d["countries"] = codes
            # Primary country = first in the list (used by byCountry totals).
            d["country"] = codes[0] if codes else ""

        # Apply manual status overrides (e.g. papers withdrawn after the fact).
        apply_status_overrides(submission_details)

        write_submissions_json(submission_details)

    # ---- Per-article monthly usage CSV ---------------------------------
    rows = []
    row_id = 0
    for sub_id, doi in ids:
        # Reuse the title from the detail pass if we already have it.
        det = detail_by_id.get(str(sub_id))
        if not det:
            det = get_submission_detail(sub_id)
        title = det["title"] if det else ""
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
