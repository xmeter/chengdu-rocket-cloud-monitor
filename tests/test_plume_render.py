"""Tests for the rocket cloud shape simulation renderer and inline email images."""

import unittest
from datetime import datetime, timedelta, timezone

from src.astronomy import evaluate_chengdu_visibility
from src.models import LaunchEvent
from src.notifier import SMTPConfig, build_email_content, build_mime_message
from src.plume_render import describe_shape_zh, render_alert_images
from src.state import PendingAlert

CST = timezone(timedelta(hours=8))


class PlumeRenderTests(unittest.TestCase):
    def test_render_png_and_inline_email(self) -> None:
        launch = LaunchEvent(
            uid="t:1", name="Long March 3B | Test", rocket_name="Long March 3B", mission_name="Test",
            provider="CASC", pad_name="LC-2", location_name="Xichang Satellite Launch Center, China",
            country_code="CHN", latitude=28.246, longitude=102.0266,
            net_utc=datetime(2026, 11, 28, 6, 45, tzinfo=CST).astimezone(timezone.utc),
            status_abbrev="Go", orbit_abbrev="GTO", sources=["UnitTest"],
        )
        a = evaluate_chengdu_visibility(launch)
        self.assertIsNotNone(a.corridor_az_deg)
        self.assertTrue(describe_shape_zh(a))

        images = render_alert_images([a])
        self.assertGreaterEqual(len(images[0]), 1)
        cid, _, png = images[0][0]
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

        alerts = [PendingAlert(assessment=a)]
        subject, plain, html_body = build_email_content(alerts, images)
        self.assertIn(f"cid:{cid}", html_body)
        self.assertIn("形状模拟", plain)

        cfg = SMTPConfig("smtp.example.com", 465, "u", "p", "u@example.com", ["me@example.com"])
        msg = build_mime_message(cfg, subject, plain, html_body, images)
        self.assertEqual(msg.get_content_type(), "multipart/related")
        self.assertIn(f"<{cid}>", msg.as_string())


if __name__ == "__main__":
    unittest.main()
