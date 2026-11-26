"""Multi-source rocket launch data fetchers and deduplicating merger.

Supported public sources (zero API key required by default):
1. The Space Devs — Launch Library 2 API (`ll.thespacedevs.com` with `lldev` fallback)
2. Next Spaceflight (`nextspaceflight.com/launches/`) HTML launch cards
3. RocketLaunch.Live (`fdo.rocketlaunch.live/json/launches/next/5`, or full API if `ROCKETLAUNCH_LIVE_API_KEY` is set)
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

from .astronomy import CHENGDU_LAT, CHENGDU_LON, KNOWN_SPACEPORTS, haversine_km
from .models import LaunchEvent

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 "
    "ChengduRocketCloudMonitor/1.0"
)

CHINA_LOCATION_KEYWORDS = (
    "china",
    "chn",
    "xichang",
    "jiuquan",
    "taiyuan",
    "wenchang",
    "hainan",
    "haiyang",
    "dongfeng",
    "yellow sea",
    "西昌",
    "酒泉",
    "太原",
    "文昌",
    "海南",
    "海阳",
)


def _http_get_text(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 20) -> str:
    req_headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def parse_iso_utc(ts: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 timestamp string (e.g. '2026-11-29T22:45:00Z') to UTC datetime."""
    if not ts or not isinstance(ts, str):
        return None
    cleaned = ts.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _infer_coords_from_text(location_text: str) -> Tuple[Optional[float], Optional[float], str]:
    lower = location_text.lower()
    for sp in KNOWN_SPACEPORTS.values():
        if any(kw in lower for kw in sp.keywords):
            return sp.lat, sp.lon, sp.key
    return None, None, ""


def _is_relevant_region(
    country_code: str, location_text: str, lat: Optional[float], lon: Optional[float]
) -> bool:
    """Keep launches in China or within 2400 km of Chengdu."""
    if (country_code or "").upper() in {"CHN", "CN"}:
        return True
    lower = (location_text or "").lower()
    if any(kw in lower for kw in CHINA_LOCATION_KEYWORDS):
        return True
    if lat is not None and lon is not None:
        return haversine_km(CHENGDU_LAT, CHENGDU_LON, lat, lon) <= 2400.0
    return False


# ---------------------------------------------------------------------------
# Source 1: The Space Devs — Launch Library 2 (LL2)
# ---------------------------------------------------------------------------

def parse_ll2_payload(payload: Dict[str, Any], source_label: str = "Launch Library 2") -> List[LaunchEvent]:
    """Parse JSON response from Launch Library 2 `/2.2.0/launch/upcoming/`."""
    events: List[LaunchEvent] = []
    results = payload.get("results") or []
    for item in results:
        net_utc = parse_iso_utc(item.get("net"))
        if net_utc is None:
            continue

        pad = item.get("pad") or {}
        loc = pad.get("location") or {}
        pad_name = pad.get("name") or ""
        loc_name = loc.get("name") or ""
        country_code = loc.get("country_code") or pad.get("country_code") or ""

        lat = _safe_float(pad.get("latitude"))
        lon = _safe_float(pad.get("longitude"))
        site_key = ""
        if lat is None or lon is None:
            inferred_lat, inferred_lon, site_key = _infer_coords_from_text(f"{pad_name} {loc_name}")
            lat = lat if lat is not None else inferred_lat
            lon = lon if lon is not None else inferred_lon
        else:
            _, _, site_key = _infer_coords_from_text(f"{pad_name} {loc_name}")

        if not _is_relevant_region(country_code, f"{pad_name} {loc_name}", lat, lon):
            continue

        status_obj = item.get("status") or {}
        status_abbrev = status_obj.get("abbrev") or "Unknown"
        status_name = status_obj.get("name") or "Unknown"

        net_prec_obj = item.get("net_precision") or {}
        time_precision = net_prec_obj.get("abbrev") or ("TBD" if status_abbrev == "TBD" else "MIN")

        rocket_cfg = ((item.get("rocket") or {}).get("configuration")) or {}
        mission = item.get("mission") or {}
        orbit = mission.get("orbit") or {}
        lsp = item.get("launch_service_provider") or {}

        full_name = item.get("name") or "Unknown Launch"
        parts = [p.strip() for p in full_name.split("|", 1)]
        rocket_name = rocket_cfg.get("full_name") or rocket_cfg.get("name") or parts[0]
        mission_name = mission.get("name") or (parts[1] if len(parts) > 1 else full_name)

        info_urls = item.get("infoURLs") or []
        info_url = ""
        if info_urls and isinstance(info_urls[0], dict):
            info_url = info_urls[0].get("url") or ""
        if not info_url:
            slug = item.get("slug") or ""
            info_url = f"https://nextspaceflight.com/launches/" if not slug else f"https://thespacedevs.com"

        events.append(
            LaunchEvent(
                uid=f"ll2:{item.get('id')}",
                name=full_name,
                rocket_name=rocket_name,
                mission_name=mission_name,
                provider=lsp.get("name") or "",
                pad_name=pad_name,
                location_name=loc_name,
                country_code=country_code,
                latitude=lat,
                longitude=lon,
                net_utc=net_utc,
                window_start_utc=parse_iso_utc(item.get("window_start")),
                window_end_utc=parse_iso_utc(item.get("window_end")),
                status_abbrev=status_abbrev,
                status_name=status_name,
                time_precision=time_precision,
                orbit_name=orbit.get("name") or "",
                orbit_abbrev=orbit.get("abbrev") or "",
                info_url=info_url,
                sources=[source_label],
                site_key=site_key,
            )
        )
    return events


def fetch_ll2_launches(days_ahead: int = 30) -> List[LaunchEvent]:
    """Fetch upcoming regional launches from Launch Library 2 API (with dev mirror fallback)."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urllib.parse.urlencode(
        {
            "limit": 100,
            "mode": "normal",
            "net__gte": now_iso,
            "net__lte": end_iso,
        }
    )
    endpoints = [
        f"https://ll.thespacedevs.com/2.2.0/launch/upcoming/?{query}",
        f"https://lldev.thespacedevs.com/2.2.0/launch/upcoming/?{query}",
    ]
    for url in endpoints:
        try:
            raw = _http_get_text(url)
            data = json.loads(raw)
            events = parse_ll2_payload(data)
            logger.info("Launch Library 2 returned %d regional upcoming launches (%s)", len(events), url)
            return events
        except Exception as exc:
            logger.warning("Launch Library 2 endpoint failed (%s): %s", url, exc)
    return []


# ---------------------------------------------------------------------------
# Source 2: Next Spaceflight (`nextspaceflight.com/launches/`) HTML parser
# ---------------------------------------------------------------------------

class _NextSpaceflightHTMLParser(HTMLParser):
    """Lightweight HTML parser for Next Spaceflight MDL launch cards."""

    def __init__(self) -> None:
        super().__init__()
        self.cards: List[Dict[str, str]] = []
        self._card_depth = 0
        self._current: Optional[Dict[str, Any]] = None
        self._in_header = False
        self._in_title = False
        self._in_support = False
        self._support_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr_map = {k: (v or "") for k, v in attrs}
        classes = set(attr_map.get("class", "").split())

        if tag == "div" and "mdl-card" in classes and self._card_depth == 0:
            self._card_depth = 1
            self._current = {"provider": [], "title": [], "support": [], "detail_href": ""}
            return

        if self._card_depth > 0 and self._current is not None:
            if tag == "div":
                self._card_depth += 1
            if "header-style" in classes:
                self._in_header = True
            if "mdl-card__title" in classes or "mdl-card__title-text" in classes or tag == "h5":
                self._in_title = True
            if "mdl-card__supporting-text" in classes:
                self._in_support = True
                self._support_depth = 1
            elif self._in_support and tag == "div":
                self._support_depth += 1
            if tag in {"br", "p"} and self._in_support:
                self._current["support"].append("\n")
            if tag == "a":
                href = attr_map.get("href", "")
                if "/launches/details/" in href and not self._current["detail_href"]:
                    self._current["detail_href"] = href

    def handle_endtag(self, tag: str) -> None:
        if self._card_depth == 0 or self._current is None:
            return
        if tag == "h5":
            self._in_title = False
        if tag == "div":
            if self._in_support:
                self._support_depth -= 1
                if self._support_depth <= 0:
                    self._in_support = False
            self._in_header = False
            self._card_depth -= 1
            if self._card_depth == 0:
                self.cards.append(
                    {
                        "provider": " ".join("".join(self._current["provider"]).split()),
                        "title": " ".join("".join(self._current["title"]).split()),
                        "support": "".join(self._current["support"]),
                        "detail_href": self._current["detail_href"],
                    }
                )
                self._current = None

    def handle_data(self, data: str) -> None:
        if self._card_depth == 0 or self._current is None:
            return
        if self._in_title:
            self._current["title"].append(data)
        elif self._in_support:
            self._current["support"].append(data)
        elif self._in_header:
            self._current["provider"].append(data)


# Matches e.g. "Sun Nov 29, 2026 22:45 UTC" or "Nov 29, 2026 22:45:00 UTC"
_NSF_DATETIME_RE = re.compile(
    r"(?:[A-Za-z]{3}\s+)?([A-Za-z]{3}\s+\d{1,2},\s+\d{4})\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*(UTC|GMT|CST)?",
    re.IGNORECASE,
)


def parse_nextspaceflight_datetime(text: str) -> Tuple[Optional[datetime], str]:
    """Extract (utc_datetime, precision) from Next Spaceflight card text."""
    m = _NSF_DATETIME_RE.search(text)
    if not m:
        return None, "TBD"
    date_part, time_part, tz_label = m.group(1), m.group(2), (m.group(3) or "UTC").upper()
    fmt = "%b %d, %Y %H:%M:%S" if time_part.count(":") == 2 else "%b %d, %Y %H:%M"
    try:
        dt = datetime.strptime(f"{date_part} {time_part}", fmt)
    except ValueError:
        return None, "TBD"
    if tz_label == "CST":
        # China Standard Time (+08:00) if ever localized, otherwise UTC
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt, "MIN"


def parse_nextspaceflight_html(html: str, days_ahead: int = 30) -> List[LaunchEvent]:
    """Parse Next Spaceflight HTML page into regional `LaunchEvent` objects."""
    parser = _NextSpaceflightHTMLParser()
    parser.feed(html)

    cutoff = datetime.now(timezone.utc) + timedelta(days=days_ahead)
    events: List[LaunchEvent] = []

    for idx, card in enumerate(parser.cards):
        title = card["title"].strip()
        support_lines = [ln.strip() for ln in card["support"].splitlines() if ln.strip()]
        if not title or not support_lines:
            continue

        support_joined = " | ".join(support_lines)
        dt_utc, precision = parse_nextspaceflight_datetime(support_joined)
        if dt_utc is None or dt_utc > cutoff:
            continue

        # Usually the line after the date is the pad & location string
        loc_line = support_lines[-1]
        lat, lon, site_key = _infer_coords_from_text(f"{loc_line} {title}")
        if not _is_relevant_region("", f"{loc_line} {title}", lat, lon):
            continue

        parts = [p.strip() for p in title.split("|", 1)]
        rocket_name = parts[0]
        mission_name = parts[1] if len(parts) > 1 else title

        pad_parts = [p.strip() for p in loc_line.split(",", 1)]
        pad_name = pad_parts[0]
        location_name = pad_parts[1] if len(pad_parts) > 1 else loc_line

        href = card["detail_href"]
        detail_id = href.rstrip("/").split("/")[-1] if href else f"card-{idx}"
        info_url = f"https://nextspaceflight.com{href}" if href.startswith("/") else (href or "https://nextspaceflight.com/launches/")

        events.append(
            LaunchEvent(
                uid=f"nsf:{detail_id}",
                name=title,
                rocket_name=rocket_name,
                mission_name=mission_name,
                provider=card["provider"],
                pad_name=pad_name,
                location_name=location_name,
                country_code="CHN" if "china" in loc_line.lower() else "",
                latitude=lat,
                longitude=lon,
                net_utc=dt_utc,
                status_abbrev="Go" if precision == "MIN" else "TBD",
                status_name="Scheduled",
                time_precision=precision,
                info_url=info_url,
                sources=["Next Spaceflight"],
                site_key=site_key,
            )
        )
    return events


def fetch_nextspaceflight_launches(days_ahead: int = 30) -> List[LaunchEvent]:
    """Fetch and parse upcoming launches from nextspaceflight.com."""
    url = "https://nextspaceflight.com/launches/?search=China"
    fallback_url = "https://nextspaceflight.com/launches/"
    for target in (url, fallback_url):
        try:
            html = _http_get_text(target)
            events = parse_nextspaceflight_html(html, days_ahead=days_ahead)
            logger.info("Next Spaceflight returned %d regional upcoming launches (%s)", len(events), target)
            if events or target == fallback_url:
                return events
        except Exception as exc:
            logger.warning("Next Spaceflight fetch failed (%s): %s", target, exc)
    return []


# ---------------------------------------------------------------------------
# Source 3: RocketLaunch.Live JSON API
# ---------------------------------------------------------------------------

def parse_rocketlaunch_live_payload(payload: Dict[str, Any]) -> List[LaunchEvent]:
    """Parse RocketLaunch.Live `/json/launches/next/5` (or authenticated `/json/launches`) JSON."""
    events: List[LaunchEvent] = []
    for item in payload.get("result") or []:
        t0_str = item.get("t0") or item.get("win_open")
        net_utc = parse_iso_utc(t0_str)
        if net_utc is None:
            continue

        pad = item.get("pad") or {}
        loc = pad.get("location") or {}
        pad_name = pad.get("name") or ""
        loc_name = " ".join(filter(None, [loc.get("name"), loc.get("statename"), loc.get("country")]))
        country = loc.get("country") or ""
        lat, lon, site_key = _infer_coords_from_text(f"{pad_name} {loc_name}")

        if not _is_relevant_region(country, f"{pad_name} {loc_name}", lat, lon):
            continue

        vehicle = (item.get("vehicle") or {}).get("name") or ""
        mission_name = item.get("name") or "Unknown"
        provider = (item.get("provider") or {}).get("name") or ""
        full_name = f"{vehicle} | {mission_name}" if vehicle else mission_name
        slug = item.get("slug") or ""

        events.append(
            LaunchEvent(
                uid=f"rll:{item.get('id')}",
                name=full_name,
                rocket_name=vehicle or mission_name,
                mission_name=mission_name,
                provider=provider,
                pad_name=pad_name,
                location_name=loc_name,
                country_code="CHN" if "china" in country.lower() else country,
                latitude=lat,
                longitude=lon,
                net_utc=net_utc,
                window_start_utc=parse_iso_utc(item.get("win_open")),
                window_end_utc=parse_iso_utc(item.get("win_close")),
                status_abbrev="Go",
                status_name="Scheduled",
                time_precision="MIN",
                orbit_name=" ".join(t.get("text", "") for t in (item.get("tags") or []) if isinstance(t, dict)),
                info_url=f"https://www.rocketlaunch.live/launch/{slug}" if slug else "https://www.rocketlaunch.live/",
                sources=["RocketLaunch.Live"],
                site_key=site_key,
            )
        )
    return events


def fetch_rocketlaunch_live_launches() -> List[LaunchEvent]:
    """Fetch upcoming launches from RocketLaunch.Live (uses API key if configured, else free `/next/5`)."""
    api_key = os.environ.get("ROCKETLAUNCH_LIVE_API_KEY", "").strip()
    headers: Dict[str, str] = {}
    if api_key:
        url = "https://fdo.rocketlaunch.live/json/launches?limit=25"
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        url = "https://fdo.rocketlaunch.live/json/launches/next/5"

    try:
        raw = _http_get_text(url, headers=headers)
        data = json.loads(raw)
        events = parse_rocketlaunch_live_payload(data)
        logger.info("RocketLaunch.Live returned %d regional upcoming launches", len(events))
        return events
    except Exception as exc:
        logger.warning("RocketLaunch.Live fetch failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Cross-source fusion & deduplication
# ---------------------------------------------------------------------------

def _same_physical_launch(a: LaunchEvent, b: LaunchEvent) -> bool:
    """Return True if two LaunchEvent records from different sites describe the same physical launch."""
    if a.uid == b.uid:
        return True
    time_diff_hours = abs((a.net_utc - b.net_utc).total_seconds()) / 3600.0
    same_site = bool(a.site_key and b.site_key and a.site_key == b.site_key)
    if not same_site and a.latitude is not None and b.latitude is not None and a.longitude is not None and b.longitude is not None:
        same_site = haversine_km(a.latitude, a.longitude, b.latitude, b.longitude) < 100.0

    if same_site and time_diff_hours <= 4.0:
        return True

    # Fallback: same UTC calendar date and matching normalized mission name
    norm_a = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", a.mission_name.lower())
    norm_b = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", b.mission_name.lower())
    if norm_a and norm_a == norm_b and time_diff_hours <= 18.0:
        return True
    return False


def merge_launch_events(event_lists: List[List[LaunchEvent]]) -> List[LaunchEvent]:
    """Merge launches across multiple sources, preserving richest metadata & combined source list."""
    merged: List[LaunchEvent] = []
    for events in event_lists:
        for candidate in events:
            matched: Optional[LaunchEvent] = None
            for existing in merged:
                if _same_physical_launch(existing, candidate):
                    matched = existing
                    break

            if matched is None:
                merged.append(candidate)
                continue

            # Enrich existing event with missing metadata from candidate
            for src in candidate.sources:
                if src not in matched.sources:
                    matched.sources.append(src)
            if not matched.has_precise_time and candidate.has_precise_time:
                matched.net_utc = candidate.net_utc
                matched.time_precision = candidate.time_precision
                matched.status_abbrev = candidate.status_abbrev
            if not matched.orbit_name and candidate.orbit_name:
                matched.orbit_name = candidate.orbit_name
            if not matched.orbit_abbrev and candidate.orbit_abbrev:
                matched.orbit_abbrev = candidate.orbit_abbrev
            if (matched.latitude is None or matched.longitude is None) and candidate.latitude is not None:
                matched.latitude = candidate.latitude
                matched.longitude = candidate.longitude
            if not matched.site_key and candidate.site_key:
                matched.site_key = candidate.site_key
            if "nextspaceflight.com/launches/details/" in candidate.info_url:
                matched.info_url = candidate.info_url

    merged.sort(key=lambda e: e.net_utc)
    return merged


def fetch_all_upcoming_launches(days_ahead: int = 30) -> List[LaunchEvent]:
    """Fetch and merge upcoming China/regional launches from all configured public sources."""
    ll2 = fetch_ll2_launches(days_ahead=days_ahead)
    nsf = fetch_nextspaceflight_launches(days_ahead=days_ahead)
    rll = fetch_rocketlaunch_live_launches()
    return merge_launch_events([ll2, nsf, rll])
