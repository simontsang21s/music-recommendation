import math
import os
import re
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

APP_NAME = "record2nd-python-recommender"
DISCOGS_BASE = os.getenv("DISCOGS_API_BASE_URL", "https://api.discogs.com").rstrip("/")
DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN", "").strip()
DISCOGS_USER_AGENT = os.getenv(
    "DISCOGS_USER_AGENT",
    "Record2ndRecommender/1.0 (+https://main.better-discogs-web.pages.dev)",
)
RECS_API_KEY = os.getenv("PY_RECS_API_KEY", "").strip()

app = FastAPI(title=APP_NAME, version="1.0.0")


class ReleaseSeed(BaseModel):
    id: str
    artist: str = ""
    title: str = ""
    year: int | None = None
    released: str | None = None
    country: str | None = None
    format: str | None = None
    label: str | None = None
    genres: list[str] = Field(default_factory=list)
    styles: list[str] = Field(default_factory=list)


class RecommendRequest(BaseModel):
    release: ReleaseSeed
    limit: int = 8


def _auth_headers() -> dict[str, str]:
    headers = {"User-Agent": DISCOGS_USER_AGENT, "Accept": "application/json"}
    if DISCOGS_TOKEN:
        headers["Authorization"] = f"Discogs token={DISCOGS_TOKEN}"
    return headers


def _normalize_tokens(value: str) -> set[str]:
    clean = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    return {x for x in clean.split() if len(x) >= 2}


def _to_release_id(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.startswith("discogs_r_"):
        return s
    if s.isdigit():
        return f"discogs_r_{s}"
    return s


def _year_of(row: dict[str, Any]) -> int | None:
    year = row.get("year")
    if isinstance(year, int) and year > 0:
        return year
    try:
        parsed = int(str(year))
        return parsed if parsed > 0 else None
    except Exception:
        return None


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    denom = max(len(a), len(b))
    if denom <= 0:
        return 0.0
    return len(a.intersection(b)) / denom


def _score(seed: ReleaseSeed, row: dict[str, Any]) -> tuple[float, str]:
    seed_artist = _normalize_tokens(seed.artist)
    seed_title = _normalize_tokens(seed.title)
    seed_genres = {x.strip().lower() for x in seed.genres if x.strip()}
    seed_styles = {x.strip().lower() for x in seed.styles if x.strip()}
    seed_format = (seed.format or "").strip().lower()
    seed_country = (seed.country or "").strip().lower()
    seed_year = seed.year

    row_artist = _normalize_tokens(row.get("artist", ""))
    row_title = _normalize_tokens(row.get("title", ""))
    row_genres = {x.strip().lower() for x in row.get("genres", []) if str(x).strip()}
    row_styles = {x.strip().lower() for x in row.get("styles", []) if str(x).strip()}
    row_format = str(row.get("format") or "").strip().lower()
    row_country = str(row.get("country") or "").strip().lower()
    row_year = _year_of(row)

    artist_overlap = _overlap(seed_artist, row_artist)
    title_overlap = _overlap(seed_title, row_title)
    genre_overlap = _overlap(seed_genres, row_genres)
    style_overlap = _overlap(seed_styles, row_styles)

    score = 0.0
    score += artist_overlap * 38.0
    score += title_overlap * 16.0
    score += genre_overlap * 24.0
    score += style_overlap * 30.0

    if seed_format and row_format and seed_format in row_format:
        score += 10.0
    if seed_country and row_country and seed_country == row_country:
        score += 5.0
    if seed_year and row_year:
        diff = abs(seed_year - row_year)
        score += max(0.0, 9.0 - min(9.0, diff))

    have = max(0, int(row.get("have", 0) or 0))
    want = max(0, int(row.get("want", 0) or 0))
    score += min(7.0, math.log10(1 + have + want) * 2.2)

    if style_overlap > 0.0:
        reason = "Shares style profile"
    elif genre_overlap > 0.0:
        reason = "Shares genre profile"
    elif artist_overlap > 0.0:
        reason = "Similar artist signal"
    elif seed_year and row_year and abs(seed_year - row_year) <= 2:
        reason = "Close release era"
    else:
        reason = "Closest metadata similarity"
    return score, reason


async def _discogs_search(client: httpx.AsyncClient, query: str, per_page: int = 20) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    response = await client.get(
        f"{DISCOGS_BASE}/database/search",
        params={"q": query, "type": "release", "per_page": per_page, "page": 1},
        headers=_auth_headers(),
        timeout=9.5,
    )
    response.raise_for_status()
    payload = response.json() or {}
    items = payload.get("results") or []
    out: list[dict[str, Any]] = []
    for row in items:
        artist = ""
        title = str(row.get("title") or "").strip()
        if " - " in title:
            parts = title.split(" - ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()
        out.append(
            {
                "id": _to_release_id(row.get("id")),
                "artist": artist,
                "title": title,
                "year": _year_of(row),
                "country": row.get("country"),
                "format": (row.get("format") or [None])[0],
                "label": (row.get("label") or [None])[0],
                "cover_image_url": row.get("cover_image") or row.get("thumb"),
                "genres": row.get("genre") or [],
                "styles": row.get("style") or [],
                "have": (row.get("community") or {}).get("have", 0),
                "want": (row.get("community") or {}).get("want", 0),
            }
        )
    return out


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": APP_NAME}


@app.post("/recommend")
async def recommend(payload: RecommendRequest, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    if RECS_API_KEY and x_api_key != RECS_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid recommender API key.")

    seed = payload.release
    if not seed.id:
        raise HTTPException(status_code=400, detail="release.id is required.")

    queries: list[str] = []
    if seed.artist and seed.title:
        queries.append(f"{seed.artist} {seed.title}")
    if seed.artist:
        queries.append(seed.artist)
    if seed.styles:
        queries.extend(seed.styles[:3])
    if seed.genres:
        queries.extend(seed.genres[:3])
    queries = [q.strip() for q in queries if q and q.strip()]
    # keep order, remove dupes
    deduped_queries = list(dict.fromkeys(queries))[:6]

    if not deduped_queries:
        return {"recommendations": []}

    candidates: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient() as client:
        for query in deduped_queries:
            try:
                rows = await _discogs_search(client, query, per_page=24)
            except Exception:
                continue
            for row in rows:
                rid = _to_release_id(row.get("id"))
                if not rid or rid == seed.id:
                    continue
                if rid not in candidates:
                    candidates[rid] = row
            if len(candidates) >= 120:
                break

    scored: list[dict[str, Any]] = []
    for row in candidates.values():
        score, reason = _score(seed, row)
        if score <= 0:
            continue
        scored.append(
            {
                "id": row.get("id"),
                "artist": row.get("artist") or "Unknown Artist",
                "title": row.get("title") or "Unknown Title",
                "year": row.get("year"),
                "country": row.get("country"),
                "format": row.get("format"),
                "label": row.get("label"),
                "cover_image_url": row.get("cover_image_url"),
                "score": round(score, 2),
                "reason": reason,
            }
        )

    scored.sort(key=lambda x: float(x.get("score", 0)), reverse=True)
    limit = max(1, min(15, int(payload.limit or 8)))
    return {"recommendations": scored[:limit]}
