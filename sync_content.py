#!/usr/bin/env python3
"""
Facebook Organic Posts Sync — Supabase edition
-----------------------------------------------
Reads the active Facebook pages (tracker = FB) from the Supabase
`competitors` table, scrapes their recent posts with Apify, keeps the
standout posts, and UPSERTS them into the Supabase `posts` table.

Two Apify steps (same pattern as the Instagram radar):
  1. apify/facebook-pages-scraper  -> follower count of each page
  2. apify/facebook-posts-scraper  -> recent posts of each page

A post is kept when it clears a small noise floor (MIN_ENGAGE) AND passes ANY of:
  1. engagement >= OUTLIER_X x that page's OWN median engagement
  2. shares >= MIN_SHARES
  3. engagement / followers >= MIN_ER
Every page also always contributes its TOP_N_PER_COMP best posts.
engagement = likes + comments + shares. Widening the filter costs nothing
extra: the scrape already pulls every post.

Upsert notes: status / transcript / ai_breakdown / my_script are never in the
payload, so worked rows are never clobbered. engagement_rate and viral_score
are generated columns (see schema_fb.sql) — never written.

Env (required): APIFY_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
Env (tuning, all optional -- GitHub Actions *Variables*):
     MIN_ENGAGE (10)  OUTLIER_X (1.5)  MIN_SHARES (10)  MIN_ER (0.01)
     TOP_N_PER_COMP (3)  MAX_PER_COMP (15)  DAYS_BACK (60)
     RESULTS_LIMIT (30)  MAX_ROWS (2500)
"""

import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TRACKER = "FB"
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")

POLL_INTERVAL = 20
POLL_TIMEOUT = 900
PROTECTED = ("拍摄中", "已处理")


def _envf(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


def _envi(name, default):
    try:
        return int(float(os.environ.get(name) or default))
    except ValueError:
        return int(default)


MIN_ENGAGE     = _envi("MIN_ENGAGE", 10)
OUTLIER_X      = _envf("OUTLIER_X", 1.5)
MIN_SHARES     = _envi("MIN_SHARES", 10)
MIN_ER         = _envf("MIN_ER", 0.01)
TOP_N_PER_COMP = _envi("TOP_N_PER_COMP", 3)
MAX_PER_COMP   = _envi("MAX_PER_COMP", 15)
DAYS_BACK      = _envi("DAYS_BACK", 60)
RESULTS_LIMIT  = _envi("RESULTS_LIMIT", 30)   # posts per page (costs $)
MAX_ROWS       = _envi("MAX_ROWS", 2500)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _request(url, method="GET", data=None, headers=None, timeout=60):
    hdrs = dict(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return raw
    except urllib.error.HTTPError as exc:
        # never print the full URL: Apify calls carry the token in the query
        print(f"  HTTP {exc.code} for {method} {url.split('?')[0]}")
        print(f"  Response: {exc.read().decode('utf-8', errors='replace')[:400]}")
        raise


def sb(path, method="GET", data=None, prefer=None):
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key}  # new-style keys are not JWTs: apikey header only
    if prefer:
        headers["Prefer"] = prefer
    return _request(f"{SUPABASE_URL}/rest/v1/{path}", method, data, headers, 60)


# ---------------------------------------------------------------------------
# Competitors (self-service list in Supabase)
# ---------------------------------------------------------------------------


def fb_slug(raw):
    """Whatever was typed -> page slug (healingmuseumhk) or numeric page id.
    Returns '' for unusable input."""
    n = (raw or "").strip()
    if "facebook.com" in n or "fb.com" in n:
        u = urllib.parse.urlparse(n if "://" in n else "https://" + n)
        parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
        if parts and parts[0] == "profile.php":
            q = urllib.parse.parse_qs(u.query)
            n = (q.get("id") or [""])[0]
        elif parts and parts[0] == "p" and len(parts) > 1:
            n = parts[1].split("-")[-1]            # /p/Name-61551041647516/
        elif parts:
            n = parts[0]
        else:
            return ""
    return n.lstrip("@").strip().strip("/")


def page_url(slug):
    if slug.isdigit():
        return f"https://www.facebook.com/profile.php?id={slug}"
    return f"https://www.facebook.com/{urllib.parse.quote(slug)}"


def fetch_competitors():
    rows = sb(f"competitors?select=username&tracker=eq.{TRACKER}&active=is.true")
    slugs = []
    for r in rows:
        s = fb_slug(r.get("username"))
        if s and s.lower() not in [x.lower() for x in slugs]:
            slugs.append(s)
    return slugs


def slug_from_item(item, slugs):
    """Find which tracked page an Apify item belongs to."""
    texts = []
    for k in ("inputUrl", "facebookUrl", "pageUrl", "url", "topLevelUrl", "link"):
        v = item.get(k)
        if isinstance(v, str):
            texts.append(v.lower())
    user = item.get("user") or item.get("page") or {}
    if isinstance(user, dict):
        for k in ("url", "profileUrl"):
            if isinstance(user.get(k), str):
                texts.append(user[k].lower())
    blob = " ".join(texts)
    for s in slugs:
        sl = s.lower()
        if re.search(r"facebook\.com/(?:profile\.php\?id=)?" + re.escape(sl) + r"(?:[/?&#]|$)", blob):
            return s
    for s in slugs:
        if s.lower() in blob:
            return s
    return None


# ---------------------------------------------------------------------------
# Apify
# ---------------------------------------------------------------------------


def run_actor(token, actor_id, actor_input, label):
    print(f"[{label}] {actor_id}...")
    result = _request(f"https://api.apify.com/v2/acts/{actor_id}/runs?token={token}",
                      "POST", actor_input, timeout=120)
    run_id = result["data"]["id"]
    print(f"  run {run_id}")
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        st = _request(f"https://api.apify.com/v2/actor-runs/{run_id}?token={token}",
                      timeout=30)["data"]
        print(f"  {st['status']}")
        if st["status"] == "SUCCEEDED":
            items = _request(
                f"https://api.apify.com/v2/datasets/{st['defaultDatasetId']}/items"
                f"?token={token}&clean=true", timeout=120)
            return items if isinstance(items, list) else []
        if st["status"] in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run ended: {st['status']}")
    try:  # abort so a run nobody will fetch stops billing
        _request(f"https://api.apify.com/v2/actor-runs/{run_id}/abort?token={token}",
                 "POST", timeout=30)
        print(f"  aborted {run_id} to stop billing")
    except Exception as exc:
        print(f"  WARNING: could not abort {run_id}: {exc}")
    raise TimeoutError(f"run {run_id} exceeded {POLL_TIMEOUT}s")


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------


def num(*vals):
    for v in vals:
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and re.fullmatch(r"[\d,]+", v.strip()):
            return int(v.replace(",", ""))
    return 0


def parse_date(item):
    for k in ("time", "timestamp", "date", "createdTime"):
        v = item.get(k)
        if isinstance(v, (int, float)):
            ts = v / 1000 if v > 1e11 else v
            return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00")).date().isoformat()
            except ValueError:
                pass
    return None


def media_info(item):
    """(is_video, thumbnail_url)."""
    media = item.get("media") or item.get("attachments") or []
    if not isinstance(media, list):
        media = []
    is_video, thumb = False, None
    for m in media:
        if not isinstance(m, dict):
            continue
        kind = str(m.get("__typename") or m.get("type") or "").lower()
        if "video" in kind or m.get("video_url") or m.get("videoUrl"):
            is_video = True
        if not thumb:
            cand = m.get("thumbnail") or m.get("image") or m.get("photo_image")
            if isinstance(cand, dict):
                cand = cand.get("uri") or cand.get("url")
            if isinstance(cand, str):
                thumb = cand
    return is_video, thumb


def to_rows(posts, followers_map, slugs, today):
    by_comp, nofol, nomatch, broken = {}, 0, 0, 0
    for p in posts:
        if p.get("error"):
            broken += 1
            continue
        slug = slug_from_item(p, slugs)
        if not slug:
            nomatch += 1
            continue
        fol = followers_map.get(slug.lower(), 0)
        if not fol:
            nofol += 1
            continue
        pid = str(p.get("postId") or p.get("id") or "").strip()
        url = p.get("postUrl") or p.get("url") or ""
        if not pid:
            m = re.search(r"(?:posts|videos|reel)/(?:pfbid)?([\w-]+)", url)
            pid = m.group(1) if m else ""
        if not pid or "facebook.com" not in url:
            broken += 1
            continue
        likes = num(p.get("likes"), p.get("reactionsCount"), p.get("likesCount"))
        comments = num(p.get("comments"), p.get("commentsCount"))
        shares = num(p.get("shares"), p.get("sharesCount"))
        views = num(p.get("viewsCount"), p.get("videoViewCount"), p.get("playCount"))
        text = (p.get("text") or p.get("message") or "").strip()
        is_video, thumb = media_info(p)
        eng = likes + comments + shares
        by_comp.setdefault(slug, []).append({
            "row": {
                "post_id": "fb_" + pid,
                "tracker": TRACKER,
                "competitor": slug,
                "caption": text[:5000],
                "post_type": "Video" if is_video else "Post",
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "views": views,
                "followers": fol,
                "post_date": parse_date(p),
                "post_url": url,
                "thumbnail_url": thumb,
                "hashtags": ", ".join(re.findall(r"#([\w一-鿿]+)", text)[:15]),
                "is_video": is_video,
                "last_synced": today,
            },
            "eng": eng,
            "shares": shares,
            "er": eng / fol,
            "score": (likes + comments * 3 + shares * 5) / fol * 100,
        })

    rows, below_total, rescued_total = [], 0, 0
    print(f"  {'專頁':<26}{'抓到':>5}{'入選':>5}{'補位':>5}  基準")
    for slug in sorted(by_comp):
        recs = by_comp[slug]
        med = statistics.median([r["eng"] for r in recs]) if len(recs) >= 3 else 0
        keep_ids, kept = set(), []
        for r in recs:
            if r["eng"] < MIN_ENGAGE:
                continue
            if ((med > 0 and r["eng"] >= OUTLIER_X * med)
                    or r["shares"] >= MIN_SHARES or r["er"] >= MIN_ER):
                keep_ids.add(r["row"]["post_id"])
                kept.append(r)
        passed = len(kept)
        rescued = 0
        if passed < TOP_N_PER_COMP:
            for r in sorted(recs, key=lambda r: -r["score"]):
                if r["row"]["post_id"] in keep_ids:
                    continue
                keep_ids.add(r["row"]["post_id"])
                kept.append(r)
                rescued += 1
                if len(kept) >= TOP_N_PER_COMP:
                    break
        capped = 0
        if len(kept) > MAX_PER_COMP:
            kept.sort(key=lambda r: -r["score"])
            capped = len(kept) - MAX_PER_COMP
            kept = kept[:MAX_PER_COMP]
        below_total += len(recs) - passed
        rescued_total += rescued
        rows.extend(r["row"] for r in kept)
        note = f" · 超額截掉 {capped}" if capped else ""
        print(f"  {slug:<26}{len(recs):>5}{len(kept):>5}{rescued:>5}  "
              f"中位 {int(med)} 互動 · {recs[0]['row']['followers']:,} 粉絲{note}")

    print(f"  ── 合計 {len(rows)} 條入庫 ({len(rows) - rescued_total} 達標 + "
          f"{rescued_total} 補位) | {below_total} 條未達標 | {nofol} 條缺粉絲數 | "
          f"{nomatch} 條對不上專頁 | {broken} 條無法解讀")
    return rows


# ---------------------------------------------------------------------------
# Cleanup (dashboard weight, not Supabase limits)
# ---------------------------------------------------------------------------


def cleanup():
    rows = sb(f"posts?select=post_id,status,last_synced&tracker=eq.{TRACKER}"
              "&order=last_synced.asc.nullsfirst")
    if len(rows) <= MAX_ROWS:
        print(f"[Cleanup] {len(rows)} rows <= {MAX_ROWS}, nothing removed")
        return
    overflow = len(rows) - MAX_ROWS
    victims = [r["post_id"] for r in rows if r.get("status") not in PROTECTED][:overflow]
    if not victims:
        print(f"[Cleanup] WARNING: {overflow} over cap but all rows carry work")
        return
    for i in range(0, len(victims), 100):
        chunk = ",".join(victims[i:i + 100])
        sb(f"posts?post_id=in.({chunk})", method="DELETE", prefer="return=minimal")
    print(f"[Cleanup] removed {len(victims)} oldest unworked rows")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    token = (os.environ.get("APIFY_TOKEN") or "").strip()
    if not token or not SUPABASE_URL or not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        print("ERROR: APIFY_TOKEN / SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY required")
        sys.exit(1)

    slugs = fetch_competitors()
    if not slugs:
        print(f"No active {TRACKER} pages in Supabase — nothing to do.")
        return
    print(f"[{TRACKER}] {len(slugs)} pages: {', '.join(slugs[:8])}"
          + (" ..." if len(slugs) > 8 else ""))
    print(f"  filter: engagement>={OUTLIER_X}x own median OR shares>={MIN_SHARES} OR "
          f"ER>={MIN_ER:.1%} | floor {MIN_ENGAGE} | keep {TOP_N_PER_COMP}-{MAX_PER_COMP} "
          f"per page | last {DAYS_BACK} days")

    start_urls = [{"url": page_url(s)} for s in slugs]

    # Step 1: followers
    pages = run_actor(token, "apify~facebook-pages-scraper",
                      {"startUrls": start_urls}, "Step 1 pages")
    followers = {}
    for pg in pages:
        s = slug_from_item(pg, slugs)
        c = num(pg.get("followers"), pg.get("followersCount"), pg.get("likes"))
        if s and c:
            followers[s.lower()] = c
    print(f"  followers for {len(followers)}/{len(slugs)} pages")
    if pages and len(followers) < len(slugs):
        print(f"  (sample page item keys: {sorted(pages[0].keys())[:25]})")

    # Step 2: posts
    posts = run_actor(token, "apify~facebook-posts-scraper",
                      {"startUrls": start_urls, "resultsLimit": RESULTS_LIMIT,
                       "onlyPostsNewerThan": f"{DAYS_BACK} days"}, "Step 2 posts")
    print(f"  scraped {len(posts)} posts (last {DAYS_BACK} days, <= {RESULTS_LIMIT}/page)")
    errs = [p for p in posts if p.get("error")]
    for p in errs[:6]:
        print(f"  Apify error item: {str(p.get('url'))[:80]} -> "
              f"{str(p.get('error'))[:60]} / {str(p.get('errorDescription'))[:160]}")
    perr = [p for p in pages if p.get("error")]
    for p in perr[:6]:
        print(f"  Apify page error: {str(p.get('url'))[:80]} -> "
              f"{str(p.get('error'))[:60]} / {str(p.get('errorDescription'))[:160]}")
    if posts:
        print(f"  (sample post item keys: {sorted(posts[0].keys())[:30]})")

    today = datetime.now(timezone.utc).date().isoformat()
    rows = to_rows(posts, followers, slugs, today)
    if rows:
        for i in range(0, len(rows), 100):
            sb("posts?on_conflict=post_id", method="POST", data=rows[i:i + 100],
               prefer="resolution=merge-duplicates,return=minimal")
        print(f"[Upsert] {len(rows)} rows written")
    else:
        print("[Upsert] nothing to write — 看上面的逐個專頁表格：『缺粉絲數』或"
              "『對不上專頁』偏多代表欄位對不上，把這段日誌給 Claude 看；"
              "『未達標』偏多就放寬 Variables（MIN_ENGAGE / OUTLIER_X / MIN_SHARES）")

    try:
        cleanup()
    except Exception as exc:
        print(f"[Cleanup] skipped ({exc})")
    print("Done.")


if __name__ == "__main__":
    main()
