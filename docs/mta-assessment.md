# MTA Transit Feature — Code Assessment

**Date:** 2026-03-27
**Reviewed file:** `kiosk-controller.py` (lines 56–760, 808–932)

---

## Architecture Summary

The MTA feature lives entirely inside `kiosk-controller.py` on the kiosk Pi (rpi3b). The Pi fetches four MTA GTFS-Realtime protobuf feeds directly, parses them, caches the result for 20 seconds, and serves a departure board at `http://127.0.0.1:8088/mta`. Chromium is navigated there via CDP — no Windows PC involvement for transit data.

**Data pipeline:**
```
MTA api-endpoint.mta.info (4 feeds)
    ↓ urllib (gzip + protobuf decode)
_build_mta_payload()  (20s TTL cache)
    ↓
GET /api/mta-arrivals  (JSON)
    ↑
MTA_HTML page (fetches every 15s, updates DOM in-place)
```

**Stations covered:**
- 7 Av/53 St — E train only (Penn widget origin)
- 57 St-7 Av — N/Q/R/W
- 50 St (8 Av) — C/E
- 50 St (Broadway-7 Av) — 1
- 59 St-Columbus Circle — A/B/C/D/1
- Optional 6th: Times Sq, Herald Sq, or Lex 59 (user-selectable)

---

## What's Well Done

| Item | Notes |
|------|-------|
| **Graceful degradation on missing dep** | `try: from google.transit import gtfs_realtime_pb2` → `None`, returns `{"ok": False, "error": "missing dependency..."}` if absent. Page shows "Feed unavailable." No crash. |
| **Per-feed error isolation** | Each of the 4 feed fetches is in its own try-except. One bad feed adds a warning but doesn't kill the others. |
| **Cache clearing on settings change** | `mta_cache["fetched_at"] = 0.0` in `/api/mta-settings` forces an immediate fresh fetch after config changes. |
| **Penn ETA widget** | Finds E-train trips that call at both origin (D14) and target (A32) stops, computes total minutes to Penn from now. Smart and practically useful. |
| **De-duplication** | `(route, trip_id, eta_epoch)` triple deduplicates the same arrival appearing in multiple platform/direction updates. |
| **Extra station mechanism** | Clean dict-keyed approach; extra station is injected at payload build time with a namespaced key (`extra:xxx`) to avoid collision. |
| **Validation in /api/mta-settings** | Whitelist checks on `station_key` and `scale` before touching state. |
| **Separate mta_cache_lock** | Not using `state_lock` for cache avoids coupling the cache lifecycle to view/rotation state. |
| **gzip detection fallback** | Checks both `Content-Encoding` header and the `\x1f\x8b` magic bytes — handles servers that omit the header. |

---

## Issues and Suggestions

### 1. No API Key Support — Medium

`api-endpoint.mta.info` is currently open without a key. MTA has historically required keys and has toggled this policy. If they re-enable enforcement, every feed request will 403 and the board will go dark with no way to fix it without a code deploy.

**Suggestion:** Add optional key support now while the code is fresh:
```python
_MTA_API_KEY = os.environ.get("MTA_API_KEY", "")

def _fetch_mta_feed(url):
    headers = {"User-Agent": "rpi-kiosk/1.0", "Accept-Encoding": "gzip"}
    if _MTA_API_KEY:
        headers["x-api-key"] = _MTA_API_KEY
    ...
```
Free keys available at https://api.mta.info. No urgency, but a one-line env var check future-proofs it.

---

### 2. Cache Stampede — Low

`get_mta_payload()` releases `mta_cache_lock` before calling `_build_mta_payload()`, then re-acquires it to write the result. Two concurrent requests that both see a stale cache will both launch a full build (4 feed fetches × 10s timeout = up to 40s each). In practice the 15s JS refresh interval makes this extremely unlikely, but it's a latent issue.

**Suggestion:** A simple "in-flight" flag:
```python
mta_cache = {"fetched_at": 0.0, "data": None, "error": "", "fetching": False}

def get_mta_payload():
    now = time.time()
    with mta_cache_lock:
        if mta_cache["data"] and (now - mta_cache["fetched_at"]) < MTA_CACHE_SECONDS:
            return mta_cache["data"]
        if mta_cache["fetching"] and mta_cache["data"]:
            return mta_cache["data"]  # return stale rather than double-fetch
        mta_cache["fetching"] = True
    try:
        payload = _build_mta_payload()
    ...
    with mta_cache_lock:
        mta_cache["fetching"] = False
        ...
```

---

### 3. Dead Code: `mins < 0` Check — Low

**Line 202–203:**
```python
mins = max(0, int(math.ceil((eta - now) / 60.0)))
if mins < 0 or mins > MTA_MAX_MINUTES:   # mins < 0 can never be True
    continue
```
`max(0, ...)` guarantees `mins >= 0`. The `mins < 0` branch is unreachable. Either remove `max(0, ...)` and keep the check (so trains already departed show as negative and get filtered), or drop `mins < 0` from the condition. The latter is cleaner since a negative ETA shouldn't appear on a departure board:
```python
if mins > MTA_MAX_MINUTES:
    continue
```

---

### 4. "0 min" for Imminent Trains — UX

When a train's ETA rounds to 0, the chip displays "0 min". A departure board convention is to show "Due" or "Arr" for trains within ~1 minute.

**Suggestion** (in the JS `render()` function):
```javascript
chip.appendChild(el('div','mins', a.minutes <= 0 ? 'Due' : a.minutes+' min'));
```
Could also color it red to catch the eye.

---

### 5. Stale Tooltip Text — Cosmetic

**Line 544:**
```html
<label class="tg" title="When enabled, cycles Dakboard → Camera → Backyard">
```
Should include MTA: `"cycles Dakboard → Camera → Backyard → MTA"`.

---

### 6. Redundant `route_colors` in API Response — Minor

`_build_mta_payload()` returns the full `MTA_ROUTE_COLORS` dict in every `/api/mta-arrivals` response, but the HTML never reads `d.route_colors` — each arrival object already has its `color` field pre-populated. The key can be removed from the payload, saving a small amount of bandwidth on every 15s refresh.

---

### 7. No Cache Pre-Warming — Minor

On first startup, the initial kiosk navigation to `/mta` triggers a cold fetch. With 4 feeds at up to 10s timeout each, the page could show "Loading..." for up to 40 seconds. A background thread started at boot would warm the cache before Chromium gets there.

**Suggestion:** In `main()`:
```python
threading.Thread(target=get_mta_payload, daemon=True).start()
```
One line, runs once, warms the cache in the background while the rest of startup completes.

---

### 8. Penn Widget `min()` is Redundant — Minor

```python
penn_candidates.sort(key=lambda x: x["origin_eta_epoch"])
best3 = penn_candidates[:3]
penn = {
    "minutes": min((x["total_minutes"] for x in best3), default=None),
    "wait_minutes": min((x["wait_minutes"] for x in best3), default=None),
}
```
`best3` is sorted by boarding time (ascending). Since the E train run time is fixed, `best3[0]` will almost always have the minimum `total_minutes`. The `min()` generator scan is harmless but could just be `best3[0]["total_minutes"]` (with a None check). Not worth changing unless you want the code to reflect intent more clearly.

---

## Summary Table

| # | Issue | Severity | Fix effort |
|---|-------|----------|-----------|
| 1 | No API key support | Medium | ~5 lines |
| 2 | Cache stampede | Low | ~10 lines |
| 3 | Dead `mins < 0` check | Low | 1 line |
| 4 | "0 min" instead of "Due" | UX | 1 line (JS) |
| 5 | Stale tooltip text | Cosmetic | 1 line |
| 6 | Redundant `route_colors` in response | Minor | 1 line |
| 7 | No cache pre-warming | Minor | 1 line |
| 8 | Redundant `min()` in Penn widget | Minor | cleanup only |

Items 1 and 7 are the only ones worth prioritizing. The rest are low/cosmetic.
