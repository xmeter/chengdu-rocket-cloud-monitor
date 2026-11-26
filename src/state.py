"""Deduplication state store for notified rocket cloud launches.

Prevents duplicate email notifications across recurring GitHub Actions cron runs,
while re-notifying if an already-notified launch shifts its scheduled T-0 by >= 30 minutes
(and still qualifies for a Chengdu rocket cloud).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import LaunchEvent, VisibilityAssessment
from .sources import parse_iso_utc

SHIFT_RENOTIFY_MINUTES = 30.0
RETENTION_DAYS_PAST = 14


def canonical_launch_key(launch: LaunchEvent) -> str:
    """Build a source-agnostic key so LL2 and Next Spaceflight records share deduplication state."""
    site = launch.site_key or "regional"
    date_bucket = launch.net_utc.strftime("%Y-%m-%d")
    mission_slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", launch.mission_name.lower())[:32]
    if mission_slug:
        return f"{site}:{ mission_slug }"
    rocket_slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", launch.rocket_name.lower())[:24]
    return f"{site}:{date_bucket}:{rocket_slug}"


@dataclass
class PendingAlert:
    assessment: VisibilityAssessment
    is_schedule_update: bool = False
    previous_net_utc: Optional[datetime] = None


class NotificationStateStore:
    """JSON-backed deduplication store saved in the repository (`data/notified_state.json`)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self.entries = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw.get("notified_launches")
            if isinstance(entries, dict):
                self.entries = entries
        except Exception:
            self.entries = {}

    def _find_matching_record(self, launch: LaunchEvent) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        ckey = canonical_launch_key(launch)
        if ckey in self.entries:
            return ckey, self.entries[ckey]

        # Also check uid or same-site close-time match in case mission name was updated from "Unknown Payload"
        for k, rec in self.entries.items():
            uids = rec.get("uids") or []
            if launch.uid in uids:
                return k, rec
            rec_site = rec.get("site_key") or ""
            rec_dt = parse_iso_utc(rec.get("net_utc"))
            if rec_site and rec_site == launch.site_key and rec_dt is not None:
                if abs((launch.net_utc - rec_dt).total_seconds()) <= 6 * 3600:
                    return k, rec
        return None, None

    def filter_unsent(self, assessments: List[VisibilityAssessment]) -> List[PendingAlert]:
        """Return only new qualifying assessments (or >=30 min window shifts)."""
        alerts: List[PendingAlert] = []
        for item in assessments:
            _, existing = self._find_matching_record(item.launch)
            if existing is None:
                alerts.append(PendingAlert(assessment=item, is_schedule_update=False))
                continue

            prev_dt = parse_iso_utc(existing.get("net_utc"))
            if prev_dt is None:
                alerts.append(PendingAlert(assessment=item, is_schedule_update=False))
                continue

            shift_min = abs((item.launch.net_utc - prev_dt).total_seconds()) / 60.0
            if shift_min >= SHIFT_RENOTIFY_MINUTES:
                alerts.append(
                    PendingAlert(
                        assessment=item,
                        is_schedule_update=True,
                        previous_net_utc=prev_dt,
                    )
                )
        return alerts

    def mark_notified(self, alerts: List[PendingAlert], now_utc: Optional[datetime] = None) -> bool:
        """Record newly notified alerts and prune expired past records. Returns True if state changed."""
        now = now_utc or datetime.now(timezone.utc)
        before_snapshot = json.dumps(self.entries, sort_keys=True, ensure_ascii=False)

        for alert in alerts:
            launch = alert.assessment.launch
            existing_key, existing_rec = self._find_matching_record(launch)
            key = existing_key or canonical_launch_key(launch)
            prev_uids = list((existing_rec or {}).get("uids") or [])
            if launch.uid not in prev_uids:
                prev_uids.append(launch.uid)

            self.entries[key] = {
                "name": launch.name,
                "site_key": launch.site_key,
                "net_utc": launch.net_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "probability": alert.assessment.probability.name,
                "uids": prev_uids,
                "notified_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

        # Prune launches more than RETENTION_DAYS_PAST in the past
        cutoff = now - timedelta(days=RETENTION_DAYS_PAST)
        stale_keys = [
            k
            for k, rec in self.entries.items()
            if (dt := parse_iso_utc(rec.get("net_utc"))) is not None and dt < cutoff
        ]
        for k in stale_keys:
            del self.entries[k]

        after_snapshot = json.dumps(self.entries, sort_keys=True, ensure_ascii=False)
        changed = before_snapshot != after_snapshot
        if changed or not self.path.exists():
            self.save(now_utc=now)
        return changed

    def save(self, now_utc: Optional[datetime] = None) -> None:
        now = now_utc or datetime.now(timezone.utc)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "updated_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "notified_launches": dict(sorted(self.entries.items())),
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
