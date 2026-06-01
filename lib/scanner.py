"""Build link scanner — searches multiple sources for posts matching
user-defined keywords per build.

Sources:
- Reddit: unauthenticated JSON API
- YouTube: scrape `ytInitialData` JSON blob from results page
- (Maxroll / Mobalytics searches are client-side JS SPAs; their leveling
  content is covered by the dedicated Leveling-library scraper instead.)
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx


REDDIT_UA = "python:poe2-cockpit:v0.1 (by /u/jbaker)"
REDDIT_UA_POOL = [
    REDDIT_UA,
    "poe2cockpit-companion/0.1 (self-hosted)",
    "Mozilla/5.0 (compatible; poe2cockpit/0.1)",
]
_REDDIT_TOKEN: dict[str, Any] = {"token": None, "expires": 0.0}


def _reddit_oauth_token() -> str | None:
    """If data/reddit_oauth.json is present with {client_id, client_secret,
    username, password}, fetch a bearer token (cached until expiry)."""
    import json as _json
    cred_path = Path(__file__).resolve().parent.parent / "data" / "reddit_oauth.json"
    if not cred_path.exists():
        return None
    now = time.time()
    if _REDDIT_TOKEN["token"] and _REDDIT_TOKEN["expires"] > now + 60:
        return _REDDIT_TOKEN["token"]
    try:
        creds = _json.loads(cred_path.read_text())
    except Exception:
        return None
    cid = creds.get("client_id"); sec = creds.get("client_secret")
    usr = creds.get("username"); pw = creds.get("password")
    if not (cid and sec and usr and pw):
        return None
    auth = httpx.BasicAuth(cid, sec)
    data = {"grant_type": "password", "username": usr, "password": pw}
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post("https://www.reddit.com/api/v1/access_token",
                       data=data, auth=auth,
                       headers={"User-Agent": REDDIT_UA})
            if r.status_code != 200:
                return None
            d = r.json()
            _REDDIT_TOKEN["token"] = d.get("access_token")
            _REDDIT_TOKEN["expires"] = now + int(d.get("expires_in", 3600))
            return _REDDIT_TOKEN["token"]
    except Exception:
        return None
CACHE_TTL = 10 * 60  # reddit cache per (sub, query)

_lock = threading.Lock()
_cache: dict[str, dict] = {}  # key -> {fetched_at, items}


DEFAULT_SUBREDDITS = ["PathOfExile2Builds", "pathofexile2"]


def _cache_get(key: str) -> list[dict] | None:
    with _lock:
        e = _cache.get(key)
        if not e:
            return None
        if time.time() - e["fetched_at"] > CACHE_TTL:
            return None
        return e["items"]


def _cache_put(key: str, items: list[dict]) -> None:
    with _lock:
        _cache[key] = {"fetched_at": time.time(), "items": items}


VALID_T = {"hour", "day", "week", "month", "year", "all"}


def _reddit_from_rss(text: str, subreddit: str) -> list[dict]:
    """Parse Reddit search.rss (Atom format) for titles/links/dates."""
    import xml.etree.ElementTree as ET
    out: list[dict] = []
    try:
        root = ET.fromstring(text)
    except Exception:
        return out
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("a:entry", ns):
        title_el = entry.find("a:title", ns)
        link_el = entry.find("a:link", ns)
        updated_el = entry.find("a:updated", ns)
        author_el = entry.find("a:author/a:name", ns)
        title = (title_el.text or "").strip() if title_el is not None else ""
        link = link_el.get("href") if link_el is not None else ""
        updated = (updated_el.text or "").strip() if updated_el is not None else ""
        author = (author_el.text or "").strip() if author_el is not None else ""
        if author.startswith("/u/"): author = author[3:]
        # Convert updated ISO to unix
        created_utc = 0
        if updated:
            try:
                import datetime as _dt
                created_utc = _dt.datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        out.append({
            "source": "reddit",
            "subreddit": subreddit,
            "title": title[:200],
            "author": author,
            "permalink": link,
            "url": link,
            "created_utc": created_utc,
            "num_comments": 0,   # not in RSS
            "score": 0,          # not in RSS
            "selftext": "",
        })
    return out


class RedditBlocked(Exception):
    pass


def _rss_feed_to_posts(xml_text: str, subreddit: str) -> list[dict]:
    """Parse the subreddit's base Atom feed into post-like dicts."""
    import xml.etree.ElementTree as ET
    import datetime as _dt
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("a:entry", ns):
        title_el = entry.find("a:title", ns)
        link_el  = entry.find("a:link", ns)
        upd_el   = entry.find("a:updated", ns)
        auth_el  = entry.find("a:author/a:name", ns)
        cont_el  = entry.find("a:content", ns)

        title = (title_el.text or "").strip() if title_el is not None else ""
        link  = link_el.get("href") if link_el is not None else ""
        author = (auth_el.text or "").strip() if auth_el is not None else ""
        if author.startswith("/u/"): author = author[3:]
        content = (cont_el.text or "")[:400] if cont_el is not None else ""
        created_utc = 0
        if upd_el is not None and upd_el.text:
            try:
                created_utc = _dt.datetime.fromisoformat(upd_el.text.replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        # Extract permalink → normalize for cache-key/dedup
        permalink = link
        if "/comments/" in link:
            # https://www.reddit.com/r/X/comments/abc/slug/ → /r/X/comments/abc/slug/
            try:
                permalink = link.split("reddit.com", 1)[1]
            except Exception:
                permalink = link
        out.append({
            "subreddit": subreddit,
            "title": title,
            "author": author,
            "permalink": permalink,
            "url": link,
            "created_utc": created_utc,
            "num_comments": 0,
            "score": 0,
            "selftext": content,
            "link_flair_text": "",
        })
    return out


def _fetch_reddit_feed(subreddit: str, sort: str = "new",
                      t: str = "all", feed_limit: int = 100) -> list[dict]:
    """Fetch the subreddit's listing, preferring endpoints Reddit gates less.
    Order tried:
      1. old.reddit.com/r/X/{new,hot,top}.json  (richer; often 403)
      2. www.reddit.com/r/X.rss                 (base Atom feed; usually 200)
    """
    ck = f"reddit_feed:{subreddit}:{sort}:{t}:{feed_limit}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    out: list[dict] = []
    last_status = None
    tried_urls: list[str] = []

    # 1: JSON listing
    if sort == "top":
        json_url = f"https://old.reddit.com/r/{quote_plus(subreddit)}/top.json?t={t}&limit={min(feed_limit, 100)}"
    elif sort == "hot":
        json_url = f"https://old.reddit.com/r/{quote_plus(subreddit)}/hot.json?limit={min(feed_limit, 100)}"
    else:
        json_url = f"https://old.reddit.com/r/{quote_plus(subreddit)}/new.json?limit={min(feed_limit, 100)}"
    tried_urls.append(json_url)
    for ua in REDDIT_UA_POOL:
        try:
            # NO custom Accept header — reddit's anti-bot trips on
            # `Accept: application/json`. The `.json` URL suffix is enough.
            with httpx.Client(timeout=15, follow_redirects=True,
                              headers={"User-Agent": ua},
                              transport=httpx.HTTPTransport(local_address="0.0.0.0")) as c:
                r = c.get(json_url)
                last_status = r.status_code
                if r.status_code == 200:
                    try:
                        data = r.json()
                        for kid in data.get("data", {}).get("children", []):
                            p = kid.get("data", {})
                            if p:
                                out.append(p)
                        if out:
                            break
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(0.3)

    # 2: RSS fallback (/r/X.rss)
    if not out:
        rss_url = f"https://www.reddit.com/r/{quote_plus(subreddit)}.rss?limit={min(feed_limit, 100)}"
        tried_urls.append(rss_url)
        for ua in REDDIT_UA_POOL:
            try:
                with httpx.Client(timeout=15, follow_redirects=True,
                                  headers={"User-Agent": ua},
                                  transport=httpx.HTTPTransport(local_address="0.0.0.0")) as c:
                    r = c.get(rss_url)
                    last_status = r.status_code
                    if r.status_code == 200:
                        out = _rss_feed_to_posts(r.text, subreddit)
                        if out:
                            break
            except Exception:
                pass
            time.sleep(0.3)

    if not out and last_status == 403:
        raise RedditBlocked(
            f"Reddit 403 for r/{subreddit} across {len(tried_urls)} endpoints — "
            "IP temporarily anti-bot'd. Retry in a few hours."
        )
    _cache_put(ck, out)
    return out


def _post_matches(post: dict, query: str) -> bool:
    """Phrase-first match. For multi-word queries ('demon form'), require the
    exact consecutive phrase (case-insensitive) — that way 'demon summon'
    mentions don't light up when paired with 'form a group' in another post.
    Single-word queries fall through to substring match.
    """
    q = query.lower().strip()
    if not q:
        return True
    hay = " ".join([
        post.get("title", ""),
        post.get("selftext", ""),
        post.get("link_flair_text", "") or "",
    ]).lower()
    # Collapse whitespace in both sides so 'demon   form' matches 'demon form'
    q_norm = " ".join(q.split())
    hay_norm = " ".join(hay.split())
    return q_norm in hay_norm


def search_reddit(subreddit: str, query: str, limit: int = 25,
                  sort: str = "new", t: str = "all") -> list[dict]:
    """Search r/<subreddit> for <query> by fetching the sub's recent listing
    (which Reddit gates less aggressively than /search) and filtering
    client-side. Multiple keywords hitting the same sub share one fetch via
    a 10-minute cache. Raises RedditBlocked if Reddit returns 403.
    """
    if t not in VALID_T:
        t = "all"

    # `sort` here determines which listing we fetch. For "relevance" or "new"
    # (default), use /new; for "top", use /top with time filter.
    feed_sort = "top" if sort == "top" else ("hot" if sort == "hot" else "new")
    feed_t = t if feed_sort == "top" else "all"

    # One sub-fetch, filter for this keyword. RedditBlocked bubbles up.
    posts = _fetch_reddit_feed(subreddit, sort=feed_sort, t=feed_t,
                               feed_limit=100)

    # Time filter for /new feed (since we asked for "all")
    time_window = _time_window_seconds(t) if feed_sort == "new" else None
    now = time.time()

    out: list[dict] = []
    for p in posts:
        if time_window is not None:
            ts = p.get("created_utc") or 0
            if ts and (now - ts) > time_window:
                continue
        if not _post_matches(p, query):
            continue
        out.append({
            "source": "reddit",
            "subreddit": p.get("subreddit") or subreddit,
            "title": (p.get("title") or "")[:200],
            "author": p.get("author") or "",
            "permalink": "https://www.reddit.com" + (p.get("permalink") or ""),
            "url": p.get("url") or "",
            "created_utc": p.get("created_utc") or 0,
            "num_comments": p.get("num_comments") or 0,
            "score": p.get("score") or 0,
            "selftext": (p.get("selftext") or "")[:400],
        })
        if len(out) >= limit:
            break
    return out


def scan(subreddits: list[str], keywords: list[str],
         per_source_limit: int = 25,
         t: str = "month",
         from_ts: float | None = None,
         to_ts: float | None = None,
         sort: str = "new",
         sources: list[str] | None = None,
         mode: str = "or") -> dict[str, Any]:
    """Run a scan across (subreddit × keyword) combinations.

    Time filtering:
      - `t` is the Reddit-native bucket (hour/day/week/month/year/all) — the
        server pre-filters to that window.
      - `from_ts` / `to_ts` are optional unix timestamps. If provided, the
        fetch widens to `t='all'` and we post-filter by created_utc.

    De-duplicates by permalink, returns sorted-by-created-desc.
    """
    subs = [s.strip() for s in subreddits if s and s.strip()]
    if not subs:
        subs = list(DEFAULT_SUBREDDITS)
    queries = [k.strip() for k in keywords if k and k.strip()]
    if not queries:
        return {"items": [], "errors": ["no keywords provided"]}

    # If custom range set, ignore `t` and fetch wider; otherwise use `t`.
    use_t = "all" if (from_ts or to_ts) else (t if t in VALID_T else "month")

    # Which sources to run
    active_sources = set(sources or ["reddit"])
    seen: dict[str, dict] = {}
    errors: list[str] = []
    searched = 0
    reddit_blocked = False

    # Reddit (needs subreddit × keyword cross-product)
    if "reddit" in active_sources:
        for sub in subs:
            for q in queries:
                searched += 1
                try:
                    items = search_reddit(sub, q, limit=per_source_limit,
                                          sort=sort, t=use_t)
                except RedditBlocked as e:
                    errors.append(str(e))
                    continue
                except Exception as e:
                    errors.append(f"reddit r/{sub} q={q!r}: {e}")
                    continue
                for it in items:
                    ts = it.get("created_utc") or 0
                    if from_ts and ts < from_ts: continue
                    if to_ts and ts > to_ts: continue
                    pk = it["permalink"]
                    if pk not in seen:
                        seen[pk] = {**it, "matched_keywords": [q]}
                    elif q not in seen[pk]["matched_keywords"]:
                        seen[pk]["matched_keywords"].append(q)

    # Other sources (one fetch per keyword, no subreddit cross)
    # Scope the query to PoE 2 since generic terms like "Amazon" or "Witch"
    # are hopelessly ambiguous on YouTube/etc. Wrap multi-word queries in
    # quotes so YouTube's query parser treats them as a phrase.
    def _scope_query(q: str) -> str:
        low = q.lower()
        phrase = q.strip()
        if " " in phrase and not (phrase.startswith('"') and phrase.endswith('"')):
            phrase = f'"{phrase}"'
        if "poe" in low or "path of exile" in low:
            return phrase
        return f"PoE 2 {phrase}"

    for src in ("youtube",):
        if src not in active_sources:
            continue
        for q in queries:
            searched += 1
            try:
                if src == "youtube":
                    items = search_youtube(_scope_query(q), limit=per_source_limit,
                                            t=use_t, from_ts=from_ts, to_ts=to_ts)
                else:
                    items = SOURCE_FUNCTIONS[src](_scope_query(q), limit=per_source_limit)
            except Exception as e:
                errors.append(f"{src} q={q!r}: {e}")
                continue
            for it in items:
                pk = it["permalink"]
                if pk not in seen:
                    seen[pk] = {**it, "matched_keywords": [q]}
                elif q not in seen[pk]["matched_keywords"]:
                    seen[pk]["matched_keywords"].append(q)

    # Apply AND-mode: only keep items that matched EVERY keyword
    total_keywords = len(queries)
    if mode == "and" and total_keywords > 1:
        seen = {
            pk: it for pk, it in seen.items()
            if len(it.get("matched_keywords", [])) == total_keywords
        }

    def _sort_key(it):
        ts = it.get("created_utc") or 0
        return (-ts, it.get("source", "z"), it.get("title", ""))
    results = sorted(seen.values(), key=_sort_key)
    return {
        "items": results,
        "errors": errors,
        "searched": searched,
        "sources_used": sorted(active_sources),
        "time_range": use_t,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "mode": mode,
    }


def youtube_search_url(query: str) -> str:
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}"


def reddit_search_url(subreddit: str, query: str, t: str = "month") -> str:
    """Generate a clickable Reddit search URL (for link-out fallback)."""
    return (
        f"https://www.reddit.com/r/{quote_plus(subreddit)}/search/"
        f"?q={quote_plus(query)}&restrict_sr=1&sort=new&t={t}"
    )


# -- YouTube scrape ------------------------------------------------------

_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:140.0) "
               "Gecko/20100101 Firefox/140.0")

# YouTube — two paths:
# 1. API key (preferred) — ~/data/youtube_api_key.txt or YOUTUBE_API_KEY env var
# 2. Scrape ytInitialData JSON blob from the results HTML (fallback)

_YT_KEY_FILE = Path(__file__).resolve().parent.parent / "data" / "youtube_api_key.txt"


def _youtube_api_key() -> str:
    import os
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if key:
        return key
    try:
        return _YT_KEY_FILE.read_text().strip()
    except FileNotFoundError:
        return ""


def _walk_for_video_renderers(node, out):
    if isinstance(node, dict):
        if "videoRenderer" in node:
            vr = node["videoRenderer"]
            vid = vr.get("videoId")
            title = ""
            channel = ""
            published = ""
            try: title = vr["title"]["runs"][0]["text"]
            except Exception: pass
            try: channel = vr["longBylineText"]["runs"][0]["text"]
            except Exception: pass
            try: published = vr.get("publishedTimeText", {}).get("simpleText", "")
            except Exception: pass
            if vid and title:
                out.append({"video_id": vid, "title": title,
                            "channel": channel, "published": published})
        for v in node.values():
            _walk_for_video_renderers(v, out)
    elif isinstance(node, list):
        for it in node:
            _walk_for_video_renderers(it, out)


def _time_window_seconds(t: str) -> int | None:
    """Map Reddit-style time bucket to seconds back-window."""
    return {
        "hour": 3600, "day": 86400, "week": 7 * 86400,
        "month": 30 * 86400, "year": 365 * 86400, "all": None,
    }.get(t)


def search_youtube(query: str, limit: int = 12, t: str = "all",
                   from_ts: float | None = None,
                   to_ts: float | None = None) -> list[dict]:
    """YouTube search via API if key configured, else scrape ytInitialData.

    Time filter: `t` (hour/day/week/month/year/all) OR explicit from_ts/to_ts
    (unix seconds). Custom range wins over `t`.
    """
    # Derive API-friendly ISO dates
    import datetime as _dt
    published_after = None
    published_before = None
    if from_ts:
        published_after = _dt.datetime.fromtimestamp(from_ts, tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    elif t != "all":
        window = _time_window_seconds(t) or 0
        if window:
            after = _dt.datetime.now(tz=_dt.timezone.utc) - _dt.timedelta(seconds=window)
            published_after = after.isoformat().replace("+00:00", "Z")
    if to_ts:
        published_before = _dt.datetime.fromtimestamp(to_ts, tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")

    key = f"yt:{query}:{limit}:{published_after}:{published_before}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    api_key = _youtube_api_key()
    results_raw: list[dict] = []

    if api_key:
        url = "https://www.googleapis.com/youtube/v3/search"
        transport = httpx.HTTPTransport(local_address="0.0.0.0")
        try:
            params = {
                "key": api_key, "part": "snippet", "q": query,
                "type": "video", "maxResults": min(limit, 25),
                "order": "relevance",
            }
            if published_after:  params["publishedAfter"]  = published_after
            if published_before: params["publishedBefore"] = published_before
            with httpx.Client(timeout=15, transport=transport) as c:
                r = c.get(url, params=params)
                if r.status_code != 200:
                    raise RuntimeError(f"yt api {r.status_code}: {r.text[:200]}")
                d = r.json()
            for item in d.get("items", []):
                s = item.get("snippet", {})
                vid = item.get("id", {}).get("videoId")
                if not vid: continue
                results_raw.append({
                    "video_id": vid,
                    "title": s.get("title", ""),
                    "channel": s.get("channelTitle", ""),
                    "published": s.get("publishedAt", ""),
                })
        except Exception:
            results_raw = []

    if not results_raw:
        # Scrape fallback — has no date-restricted fetch; we post-filter best-effort.
        url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        try:
            with httpx.Client(
                timeout=20, follow_redirects=True,
                headers={"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
            ) as c:
                r = c.get(url)
                if r.status_code != 200:
                    return []
                html_text = r.text
            m = re.search(r"var ytInitialData = ({.+?});</script>", html_text)
            if m:
                data = json.loads(m.group(1))
                _walk_for_video_renderers(data, results_raw)
        except Exception:
            return []

    # Post-filter by published timestamp if we can parse it.
    def _ts_of(published: str) -> float | None:
        if not published: return None
        try:
            import datetime as _dt
            # API returns ISO8601 "2025-10-14T...Z"
            return _dt.datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    # Phrase post-filter: strip the PoE 2 scope prefix + surrounding quotes
    # from the query to recover the user's original phrase, then require it
    # appears in the video title/channel. YouTube's `q` is loose; we tighten.
    def _phrase_in(hay: str, phrase: str) -> bool:
        p = phrase.strip().strip('"').lower()
        if not p: return True
        return p in " ".join(hay.split()).lower()

    raw_phrase = query
    # If the caller passed something like: PoE 2 "demon form"
    m = re.search(r'"([^"]+)"', raw_phrase)
    if m:
        phrase_only = m.group(1)
    elif raw_phrase.lower().startswith("poe 2 "):
        phrase_only = raw_phrase[6:]
    else:
        phrase_only = raw_phrase

    seen = set()
    out: list[dict] = []
    for r in results_raw:
        vid = r["video_id"]
        if vid in seen: continue
        seen.add(vid)
        ts = _ts_of(r.get("published", ""))
        if ts is not None:
            if from_ts and ts < from_ts: continue
            if to_ts and ts > to_ts: continue
            if not (from_ts or to_ts) and t != "all":
                w = _time_window_seconds(t) or 0
                if w and (time.time() - ts) > w: continue
        # Require user's phrase in title or channel
        title = r.get("title", "")
        channel = r.get("channel", "")
        if not (_phrase_in(title, phrase_only) or _phrase_in(channel, phrase_only)):
            continue
        out.append({
            "source": "youtube",
            "video_id": vid,
            "title": title[:200],
            "channel": channel[:120],
            "published": r.get("published", ""),
            "permalink": f"https://www.youtube.com/watch?v={vid}",
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
        if len(out) >= limit:
            break
    _cache_put(key, out)
    return out


# Registry so callers can iterate enabled sources.
SOURCE_FUNCTIONS: dict[str, Any] = {
    "reddit": None,   # special — needs subreddit list
    "youtube": search_youtube,
}
