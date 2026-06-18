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

# One-time flags so we only dump the first object of each kind once (debug).
_AUTHOR_DEBUG_DONE = False
_RA_DEBUG_DONE = False
_DEC_DEBUG_DONE = False

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


def get_submission_meta(sub_id):
    """Return (title, doi) for a submission in a single API call."""
    data = api_get("/submissions/%s" % sub_id)
    if not data:
        return "", ""
    pubs = data.get("publications") or []
    cur_id = data.get("currentPublicationId")
    pub = next((p for p in pubs if p.get("id") == cur_id),
               pubs[0] if pubs else None)
    if not pub:
        return "", ""
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
    return title, doi


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
    # (Title is fetched in the same call inside the main loop via
    # get_submission_meta, so we just collect DOIs here.)
    ids = []
    for sid in sub_ids:
        _, doi = get_submission_meta(sid)
        if doi:
            ids.append((sid, doi))
        time.sleep(0.15)

    ids.sort(key=lambda x: int(x[0]) if x[0].isdigit() else 0)
    return ids



def get_submission_detail(sub_id):
    """Return a rich per-submission dict in a single API call:
    title, doi, dateSubmitted, author countries, review counts,
    latest editorial decision, and whether it's published. Best-effort;
    missing fields come back empty/zero.
    """
    data = api_get("/submissions/%s" % sub_id)
    if not data:
        return None
    pubs = data.get("publications") or []
    cur_id = data.get("currentPublicationId")
    pub = next((p for p in pubs if p.get("id") == cur_id),
               pubs[0] if pubs else None)

    title, doi = "", ""
    countries = []
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

        # Author countries. OJS may expose the ISO code under different keys
        # depending on version: 'country' (alpha-2), sometimes nested, or via
        # the author's affiliation. Try the common ones.
        authors_list = pub.get("authors") or []
        # One-time diagnostic: dump the keys + country of the first author we
        # ever see, so it's clear what the API actually returns.
        global _AUTHOR_DEBUG_DONE
        if authors_list and not _AUTHOR_DEBUG_DONE:
            _AUTHOR_DEBUG_DONE = True
            a0 = authors_list[0]
            print("  [debug] first author keys: %s" % sorted(a0.keys()),
                  file=sys.stderr)
            print("  [debug] first author 'country' value: %r"
                  % a0.get("country"), file=sys.stderr)
        for author in authors_list:
            c = author.get("country")
            if isinstance(c, dict):
                c = first_locale_value(c)
            c = (c or "").strip().upper()
            if not c:
                alt = author.get("countryLocalized") or ""
                c = str(alt).strip().upper()[:2] if alt else ""
            if c and len(c) == 2 and c.isalpha():
                countries.append(c)

    date_submitted = (data.get("dateSubmitted") or "")[:10]

    # ---- Published status -------------------------------------------------
    # OJS submission status: 1=queued, 3=published, 4=declined, 5=scheduled.
    status_code = data.get("status")
    status_label = {
        1: "In progress",
        3: "Published",
        4: "Declined",
        5: "Scheduled",
    }.get(status_code, "Unknown")
    is_published = (status_code == 3) or bool(
        pub and pub.get("datePublished"))

    # ---- Review assignments ----------------------------------------------
    # reviewAssignments is returned to editors/managers. Each entry is one
    # invited reviewer. We count invited, completed, and declined.
    # Review-assignment status constants (pkp-lib ReviewAssignment):
    #   0 = awaiting response, 1 = declined, 4 = accepted (response received),
    #   5 = received (review submitted), 6 = complete (confirmed by editor),
    #   7 = thanked, 8 = cancelled, 9 = request resent, 10/11 = overdue.
    # A review counts as "completed" when dateCompleted is set OR the status
    # is one of the submitted/complete/thanked states.
    ras = data.get("reviewAssignments") or []
    global _RA_DEBUG_DONE
    if ras and not _RA_DEBUG_DONE:
        _RA_DEBUG_DONE = True
        r0 = ras[0]
        print("  [debug] first reviewAssignment keys: %s" % sorted(r0.keys()),
              file=sys.stderr)
        print("  [debug] first reviewAssignment status=%r dateCompleted=%r "
              "dateConfirmed=%r"
              % (r0.get("status"), r0.get("dateCompleted"),
                 r0.get("dateConfirmed")), file=sys.stderr)
    COMPLETED_STATES = (5, 6, 7)
    DECLINED_STATES = (1, 8)
    invited = 0
    completed = 0
    declined = 0
    for ra in ras:
        invited += 1
        st = ra.get("status")
        is_done = bool(ra.get("dateCompleted")) or \
            (isinstance(st, int) and st in COMPLETED_STATES)
        is_declined = (isinstance(st, int) and st in DECLINED_STATES) or \
            bool(ra.get("dateDeclined"))
        if is_done:
            completed += 1
        elif is_declined:
            declined += 1

    # ---- Review rounds ----------------------------------------------------
    review_rounds = len(data.get("reviewRounds") or [])

    # ---- Editorial decisions (full list) ---------------------------------
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
                                               "Scheduled"):
        decision_label = status_label

    # ---- Build the per-submission event timeline -------------------------
    # Each event: {date, label, kind}. kind drives the dot colour in the UI.
    events = []
    if date_submitted:
        events.append({"date": date_submitted, "label": "Submitted",
                       "kind": "submitted"})

    # Review-assignment milestones: earliest "sent to review" = earliest
    # request date; "reviews received" = each completed review's date.
    request_dates = []
    for ra in ras:
        rq = (ra.get("dateAssigned") or ra.get("dateRequested") or "")[:10]
        if rq:
            request_dates.append(rq)
        dc = (ra.get("dateCompleted") or "")[:10]
        st = ra.get("status")
        if dc or (isinstance(st, int) and st in COMPLETED_STATES):
            if dc:
                events.append({"date": dc, "label": "Review received",
                               "kind": "review"})
    if request_dates:
        events.append({"date": min(request_dates), "label": "Sent to review",
                       "kind": "sent"})

    # Decision milestones from the decisions array.
    REVISION_CODES = {2, 9, 19}        # minor/major/recommend revisions
    ACCEPT_CODES = {1, 15, 17}         # accept / accept-skip / recommend accept
    DECLINE_CODES = {4, 16, 18}        # decline / initial decline / recommend decline
    REVIEW_CODES = {7, 8, 21}          # send to (external) review
    for d in decisions:
        code = d.get("decision")
        dd = (d.get("dateDecided") or "")[:10]
        if not dd:
            continue
        if code in REVISION_CODES:
            lab = "Revisions requested"
            kind = "revision"
        elif code in ACCEPT_CODES:
            lab = "Accepted"
            kind = "accepted"
        elif code in DECLINE_CODES:
            lab = "Declined"
            kind = "declined"
        elif code in REVIEW_CODES:
            lab = "Sent to review"
            kind = "sent"
        else:
            lab = DECISION_LABELS.get(code, "Decision")
            kind = "decision"
        events.append({"date": dd, "label": lab, "kind": kind})

    # Publication.
    date_published = ""
    if pub and pub.get("datePublished"):
        date_published = str(pub.get("datePublished"))[:10]
    if date_published:
        events.append({"date": date_published, "label": "Published",
                       "kind": "published"})

    # Sort chronologically; drop exact duplicate (date,label) pairs and
    # collapse repeated "Sent to review" events to the earliest one.
    seen = set()
    sent_seen = False
    timeline = []
    for ev in sorted(events, key=lambda e: e["date"]):
        if ev["label"] == "Sent to review":
            if sent_seen:
                continue
            sent_seen = True
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
        "countries": countries,
        "status": status_label,
        "isPublished": is_published,
        "reviewsInvited": invited,
        "reviewsCompleted": completed,
        "reviewsDeclined": declined,
        "reviewRounds": review_rounds,
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

    # Per-month submission counts (YYYY-MM -> n).
    by_month = {}
    for d in details:
        ds = d.get("dateSubmitted") or ""
        if len(ds) >= 7:
            ym = ds[:7]
            by_month[ym] = by_month.get(ym, 0) + 1

    # Country totals across all authors of all submissions (ISO code -> n).
    by_country = {}
    for d in details:
        for c in d.get("countries", []):
            by_country[c] = by_country.get(c, 0) + 1

    # Aggregate editorial totals for the sidebar.
    totals = {
        "published": sum(1 for d in details if d.get("isPublished")),
        "reviewsInvited": sum(d.get("reviewsInvited", 0) for d in details),
        "reviewsCompleted": sum(d.get("reviewsCompleted", 0) for d in details),
    }

    payload = {
        "generated": datetime.date.today().isoformat(),
        "submissions": sorted(
            [{"id": d["id"], "title": d["title"], "doi": d["doi"],
              "dateSubmitted": d["dateSubmitted"],
              "status": d.get("status", ""),
              "isPublished": d.get("isPublished", False),
              "reviewsInvited": d.get("reviewsInvited", 0),
              "reviewsCompleted": d.get("reviewsCompleted", 0),
              "reviewsDeclined": d.get("reviewsDeclined", 0),
              "reviewRounds": d.get("reviewRounds", 0),
              "decision": d.get("decision", ""),
              "decisionDate": d.get("decisionDate", ""),
              "timeline": d.get("timeline", []),
              "countries": d.get("countries", [])} for d in details],
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
    print("Wrote submissions.json with %d submissions, %d months, "
          "%d countries." % (len(details), len(by_month), len(by_country)))
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

    # ---- Submissions tab data: dates + author countries ----------------
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
        write_submissions_json(submission_details)

    # ---- Per-article monthly usage CSV ---------------------------------
    rows = []
    row_id = 0
    for sub_id, doi in ids:
        # Reuse the title from the detail pass if we already have it.
        det = detail_by_id.get(str(sub_id))
        title = det["title"] if det else get_submission_meta(sub_id)[0]
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
