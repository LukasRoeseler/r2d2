# R2D2 · Replication Research Dissemination Dashboard

A single-page dashboard that aggregates bibliometrics, altmetrics, and usage statistics for all publications in [Replication Research](https://ejournals.uni-muenster.de/index.php/replicationresearch/index), an open-access journal hosted on OJS at the University of Münster.

Live data is pulled from OJS, OpenAlex, DataCite, Bluesky, and Altmetric on every page load. Usage statistics (abstract views and PDF downloads by month) are fetched from OJS weekly by a GitHub Action and stored as a CSV in this repository.

---

## Repository layout

```
r2d2/
├── index.html               # The dashboard — one self-contained HTML file
├── fetch_ojs_stats.py       # Script run by the GitHub Action each week
├── publications.json        # Auto-generated: [{id, doi}, …] for every published article
├── submissions.json         # Auto-generated: submission dates + author countries (Submissions tab)
├── statistics/
│   └── statistics-YYYYMMDD.csv   # Auto-generated: per-article monthly stats
└── .github/
    └── workflows/
        └── update-ojs-stats.yml  # Weekly GitHub Action
```

`publications.json` and the statistics CSVs are committed automatically by the Action. You do not need to edit them by hand.

---

## How it works

### Dashboard (`index.html`)

On every page load the dashboard:

1. Fetches `publications.json` from this repo to get the list of published articles (submission IDs + DOIs).
2. Calls the public OJS REST API to load title, authors, section, and PDF galley links for each article.
3. Loads the newest `statistics-YYYYMMDD.csv` from the `statistics/` folder.
4. Enriches each article in parallel with:
   - **OpenAlex** — citation count and citations-by-year sparkline
   - **DataCite** — citation count
   - **Bluesky** — post count mentioning the DOI
   - **Altmetric** — donut badge (via their embed script)
5. Renders three tabs: **Published Articles**, **Submissions**, and **R2 Zenodo Community**.

The **Submissions** tab reads `submissions.json` and shows: a per-month submission bar chart, a world choropleth map of author countries (with a ranked country list), and a full submission timeline sorted by submission date. The map uses [world-atlas](https://github.com/topojson/world-atlas) TopoJSON loaded from a CDN at runtime; if the CDN is unreachable it falls back to the ranked list.

The Zenodo tab uses a hardcoded snapshot (Zenodo returns 403 to browser User-Agents) and enriches it live with OpenAlex, DataCite, and Bluesky.

### GitHub Action (`update-ojs-stats.yml`)

Runs every **Monday at 03:17 UTC** and on manual dispatch.

What it does:

1. Calls `fetch_ojs_stats.py` with the `OJS_API_KEY` secret.
2. The script fetches all published submissions from the OJS API and writes `publications.json`.
3. It also fetches every submission's date and author countries and writes `submissions.json` (powers the Submissions tab).
4. For each submission it fetches monthly abstract views and monthly galley (PDF) downloads from the OJS stats API.
5. Writes `statistics/statistics-YYYYMMDD.csv` with one row per (submission, metric, month).
6. Commits any changed files and pushes.

---

## Setup

### 1. Add the OJS API key secret

The Action authenticates with the OJS REST API using a Bearer token tied to a **Journal Manager** or **Admin** account.

1. In OJS, log in as a Journal Manager.
2. Go to **User Profile → API Key**.
3. Generate and copy the key (a long JWT starting with `eyJ…`).
4. In GitHub: **Settings → Secrets and variables → Actions → New repository secret**.
5. Name: `OJS_API_KEY`, value: the key you copied.

### 2. Add the workflow and script

Place these files in your repo at the paths shown above:

| File | Path in repo |
|---|---|
| `fetch_ojs_stats.py` | repo root |
| `update-ojs-stats.yml` | `.github/workflows/` |
| `index.html` | repo root (or wherever your GitHub Pages serves from) |

### 3. Run the Action once manually

Go to **Actions → Update OJS statistics → Run workflow**. This generates the first `publications.json` and `statistics-YYYYMMDD.csv`. After that it runs automatically every Monday.

### 4. Serve `index.html`

The dashboard is a single static HTML file with no build step. Enable **GitHub Pages** on the repo (Settings → Pages → Deploy from branch → `main` / root) and it will be live at `https://<org>.github.io/r2d2/`.

---

## Data sources

| Source | What it provides | Auth required |
|---|---|---|
| OJS REST API | Article metadata (title, authors, PDF links, section) | None (public) |
| OJS stats API | Abstract views and PDF downloads by month | Journal Manager API key (server-side only) |
| OpenAlex | Citation counts, citations-by-year sparkline | None |
| DataCite | Citation counts | None |
| Bluesky | Post count mentioning the DOI | None |
| Altmetric | Donut badge | None (embed script) |
| Zenodo | Community records snapshot | Snapshot embedded in HTML |

The OJS stats API is only called from the GitHub Action (server-side), never from the browser. The `OJS_API_KEY` is stored as a repository secret and never appears in the HTML.

---

## CSV format

`statistics/statistics-YYYYMMDD.csv`:

| Column | Description |
|---|---|
| `ID` | Row number |
| `Submission ID` | OJS submission ID |
| `Title` | Article title |
| `Metric Type` | `abstract` (page views) or `galley` (file downloads) |
| `File Type` | `PDF` for galley rows, empty for abstract rows |
| `Month` | `YYYY-MM` |
| `Count` | Number of views or downloads that month |

The dashboard parser auto-detects this format. Legacy `statistics-*.csv` files in the old OJS report format are still readable by the dashboard for backward compatibility.

---

## Changing the update schedule

Edit the `cron` line in `.github/workflows/update-ojs-stats.yml`:

```yaml
- cron: "17 3 * * 1"   # every Monday at 03:17 UTC
```

Standard cron syntax: `minute hour day-of-month month day-of-week`.

---

## License

MIT — see [opensource.org/licenses/MIT](https://opensource.org/licenses/MIT).
