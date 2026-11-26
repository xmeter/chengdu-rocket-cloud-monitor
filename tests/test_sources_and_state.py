"""Unit tests for multi-source parsers, cross-source merging, state deduplication, and email formatting."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.astronomy import evaluate_chengdu_visibility
from src.notifier import build_email_content
from src.sources import (
    merge_launch_events,
    parse_ll2_payload,
    parse_nextspaceflight_html,
    parse_rocketlaunch_live_payload,
)
from src.state import NotificationStateStore

SAMPLE_LL2_JSON = {
    "results": [
        {
            "id": "abc-xichang-1",
            "name": "Long March 3B/E | TJS-99",
            "net": "2026-12-02T22:45:00Z",
            "window_start": "2026-12-02T22:40:00Z",
            "window_end": "2026-12-02T23:00:00Z",
            "status": {"abbrev": "Go", "name": "Go for Launch"},
            "net_precision": {"abbrev": "MIN"},
            "launch_service_provider": {"name": "China Aerospace Science and Technology Corporation"},
            "rocket": {"configuration": {"full_name": "Long March 3B/E"}},
            "mission": {"name": "TJS-99", "orbit": {"name": "Geostationary Transfer Orbit", "abbrev": "GTO"}},
            "pad": {
                "name": "Launch Complex 2",
                "latitude": "28.2460",
                "longitude": "102.0266",
                "location": {"name": "Xichang Satellite Launch Center, People's Republic of China", "country_code": "CHN"},
            },
        },
        {
            "id": "usa-ksc-1",
            "name": "Falcon 9 Block 5 | Starlink",
            "net": "2026-12-02T22:45:00Z",
            "status": {"abbrev": "Go", "name": "Go for Launch"},
            "net_precision": {"abbrev": "MIN"},
            "pad": {
                "name": "SLC-40",
                "latitude": "28.5619",
                "longitude": "-80.577",
                "location": {"name": "Cape Canaveral SFS, FL, USA", "country_code": "USA"},
            },
        },
    ]
}

SAMPLE_NSF_HTML = """
<html><body>
  <div class="mdl-card mdl-shadow--2dp">
    <div class="header-style">CASC</div>
    <div class="mdl-card__title"><h5 class="mdl-card__title-text">Long March 3B/E | TJS-99</h5></div>
    <div class="mdl-card__supporting-text">
      <span>Wed Dec 02, 2026 22:45 UTC</span><br>
      LC-2, Xichang Satellite Launch Center, China
    </div>
    <a href="/launches/details/7788">Details</a>
  </div>
  <div class="mdl-card mdl-shadow--2dp">
    <div class="header-style">CASC</div>
    <div class="mdl-card__title"><h5 class="mdl-card__title-text">Long March 2D | Unannounced</h5></div>
    <div class="mdl-card__supporting-text">
      <span>NET Dec 2026</span><br>
      LC-3, Xichang Satellite Launch Center, China
    </div>
  </div>
</body></html>
"""

SAMPLE_RLL_JSON = {
    "result": [
        {
            "id": 9901,
            "name": "TJS-99",
            "slug": "tjs-99",
            "t0": "2026-12-02T22:45Z",
            "provider": {"name": "CASC"},
            "vehicle": {"name": "Long March 3B/E"},
            "pad": {
                "name": "LC-2",
                "location": {"name": "Xichang Satellite Launch Center", "country": "China"},
            },
            "tags": [{"text": "Geostationary Transfer Orbit"}],
        }
    ]
}


class SourcesAndStateTests(unittest.TestCase):
    def test_multi_source_parsing_and_cross_source_deduplication(self) -> None:
        ll2_events = parse_ll2_payload(SAMPLE_LL2_JSON)
        # Cape Canaveral should be filtered out, keeping only Xichang
        self.assertEqual(len(ll2_events), 1)
        self.assertEqual(ll2_events[0].site_key, "xichang")

        nsf_events = parse_nextspaceflight_html(SAMPLE_NSF_HTML, days_ahead=3650)
        # Only the card with an exact UTC timestamp should be kept ("NET Dec 2026" skipped)
        self.assertEqual(len(nsf_events), 1)
        self.assertEqual(nsf_events[0].site_key, "xichang")
        self.assertEqual(nsf_events[0].info_url, "https://nextspaceflight.com/launches/details/7788")

        rll_events = parse_rocketlaunch_live_payload(SAMPLE_RLL_JSON)
        self.assertEqual(len(rll_events), 1)

        merged = merge_launch_events([ll2_events, nsf_events, rll_events])
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            merged[0].sources,
            ["Launch Library 2", "Next Spaceflight", "RocketLaunch.Live"],
        )

    def test_state_deduplication_and_window_shift_renotify(self) -> None:
        ll2_events = parse_ll2_payload(SAMPLE_LL2_JSON)
        assessment = evaluate_chengdu_visibility(ll2_events[0])

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "notified_state.json"
            store = NotificationStateStore(state_path)

            # 1) First check: unsent -> returns 1 PendingAlert
            pending1 = store.filter_unsent([assessment])
            self.assertEqual(len(pending1), 1)
            self.assertFalse(pending1[0].is_schedule_update)

            # Build email and ensure key fields render cleanly
            subject, plain_txt, html_txt = build_email_content(pending1)
            self.assertIn("成都火箭云预警", subject)
            self.assertIn("TJS-99", plain_txt)
            self.assertIn("最佳观测窗", html_txt)

            # Mark notified and reload from disk
            changed = store.mark_notified(pending1, now_utc=datetime(2026, 12, 1, tzinfo=timezone.utc))
            self.assertTrue(changed)

            store_reloaded = NotificationStateStore(state_path)
            # 2) Same launch time -> deduplicated (0 alerts)
            self.assertEqual(len(store_reloaded.filter_unsent([assessment])), 0)

            # 3) Minor 10-minute tweak -> still deduplicated
            minor_shift_event = parse_ll2_payload(SAMPLE_LL2_JSON)[0]
            minor_shift_event.net_utc += timedelta(minutes=10)
            minor_assessment = evaluate_chengdu_visibility(minor_shift_event)
            self.assertEqual(len(store_reloaded.filter_unsent([minor_assessment])), 0)

            # 4) Major 40-minute shift -> triggers [窗口更新] re-notification
            major_shift_event = parse_ll2_payload(SAMPLE_LL2_JSON)[0]
            major_shift_event.net_utc -= timedelta(minutes=40)
            major_assessment = evaluate_chengdu_visibility(major_shift_event)
            pending_shift = store_reloaded.filter_unsent([major_assessment])
            self.assertEqual(len(pending_shift), 1)
            self.assertTrue(pending_shift[0].is_schedule_update)

            subj_shift, _, _ = build_email_content(pending_shift)
            self.assertIn("【窗口更新】", subj_shift)


if __name__ == "__main__":
    unittest.main()
