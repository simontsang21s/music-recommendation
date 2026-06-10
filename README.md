# Python Recommender Service

This service provides release recommendations for Record2nd using a Python scoring algorithm.

## What it does

- Receives a seed release from Record2nd backend.
- Queries Discogs release search using artist/title/style/genre signals.
- Scores candidate releases by metadata similarity:
  - artist/title token overlap
  - genre/style overlap
  - format/country/year proximity
  - community popularity signal
- Returns top similar releases.

## Run locally

```bash
cd python-recommender
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DISCOGS_TOKEN="<your-discogs-token>"
export DISCOGS_USER_AGENT="Record2ndRecommender/1.0 (+https://main.better-discogs-web.pages.dev)"
export PY_RECS_API_KEY="optional-shared-key"
uvicorn app:app --host 0.0.0.0 --port 8081
```

Health check:

```bash
curl http://127.0.0.1:8081/health
```

## Connect from Record2nd

Set in `.dev.vars` or Cloudflare Pages env vars:

- `PY_RECS_API_BASE_URL` (example: `http://127.0.0.1:8081` locally, or your hosted URL)
- `PY_RECS_API_KEY` (optional, if enabled on the Python service)

The frontend calls `/api/releases/recommendations?id=<releaseId>`, and Cloudflare Functions proxy to this service.
