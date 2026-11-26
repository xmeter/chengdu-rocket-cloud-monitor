"""Unit tests for astronomical geometry and Chengdu rocket cloud probability classification."""

import unittest
from datetime import datetime, timedelta, timezone

from src.astronomy import CHENGDU_LAT, CHENGDU_LON, evaluate_chengdu_visibility, solar_alt_az
from src.models import LaunchEvent, ProbabilityLevel

CST = timezone(timedelta(hours=8))


def _make_launch(
    site_name: str,
    lat: float,
    lon: float,
    dt_cst: datetime,
    orbit_abbrev: str = "GTO",
    precision: str = "MIN",
    status: str = "Go",
) -> LaunchEvent:
    return LaunchEvent(
        uid="test:1",
        name=f"Long March | Test ({site_name})",
        rocket_name="Long March",
        mission_name="Test Payload",
        provider="CASC",
        pad_name="LC-2",
        location_name=site_name,
        country_code="CHN",
        latitude=lat,
        longitude=lon,
        net_utc=dt_cst.astimezone(timezone.utc),
        status_abbrev=status,
        status_name=status,
        time_precision=precision,
        orbit_name=orbit_abbrev,
        orbit_abbrev=orbit_abbrev,
        sources=["UnitTest"],
    )


class AstronomyVisibilityTests(unittest.TestCase):
    def test_chengdu_solar_altitude_sanity(self) -> None:
        # Late Nov in Chengdu: solar noon ~13:00 CST (~38 deg), midnight ~01:00 CST (~-61 deg)
        noon_utc = datetime(2026, 11, 28, 13, 0, tzinfo=CST).astimezone(timezone.utc)
        midnight_utc = datetime(2026, 11, 28, 1, 0, tzinfo=CST).astimezone(timezone.utc)
        alt_noon, _ = solar_alt_az(noon_utc, CHENGDU_LAT, CHENGDU_LON)
        alt_mid, _ = solar_alt_az(midnight_utc, CHENGDU_LAT, CHENGDU_LON)
        self.assertGreater(alt_noon, 30.0)
        self.assertLess(alt_noon, 45.0)
        self.assertLess(alt_mid, -50.0)

    def test_xichang_dawn_twilight_is_high_probability(self) -> None:
        # 06:45 CST in late Nov (~55 min before Chengdu sunrise) -> golden dawn rocket cloud window
        launch = _make_launch(
            "Xichang Satellite Launch Center, China",
            28.2460,
            102.0266,
            datetime(2026, 11, 28, 6, 45, tzinfo=CST),
            orbit_abbrev="GTO",
        )
        res = evaluate_chengdu_visibility(launch)
        self.assertEqual(res.probability, ProbabilityLevel.HIGH)
        self.assertGreater(res.max_elevation_deg, 15.0)
        self.assertIn("清晨曙光", res.twilight_phase_zh)

    def test_xichang_dusk_twilight_is_high_probability(self) -> None:
        # 18:28 CST in late Nov (~25 min after Chengdu sunset, before SE plume enters Earth shadow)
        launch = _make_launch(
            "Xichang Satellite Launch Center, China",
            28.2460,
            102.0266,
            datetime(2026, 11, 28, 18, 28, tzinfo=CST),
            orbit_abbrev="GTO",
        )
        res = evaluate_chengdu_visibility(launch)
        self.assertEqual(res.probability, ProbabilityLevel.HIGH)
        self.assertIn("傍晚暮光", res.twilight_phase_zh)

    def test_jiuquan_dusk_and_summer_corridor_is_visible(self) -> None:
        # Winter dusk SE corridor (~18:25 CST) and Summer solstice dusk (~20:45 CST) from Jiuquan
        winter_launch = _make_launch(
            "Jiuquan Satellite Launch Center, China",
            40.9606,
            100.2914,
            datetime(2026, 11, 28, 18, 25, tzinfo=CST),
            orbit_abbrev="Tianzhou Space Station",
        )
        summer_launch = _make_launch(
            "Jiuquan Satellite Launch Center, China",
            40.9606,
            100.2914,
            datetime(2026, 6, 21, 20, 45, tzinfo=CST),
            orbit_abbrev="Tianzhou Space Station",
        )
        res_winter = evaluate_chengdu_visibility(winter_launch)
        res_summer = evaluate_chengdu_visibility(summer_launch)
        self.assertGreaterEqual(res_winter.probability, ProbabilityLevel.MEDIUM)
        self.assertGreater(res_winter.max_elevation_deg, 5.0)
        self.assertGreaterEqual(res_summer.probability, ProbabilityLevel.MEDIUM)

    def test_taiyuan_dawn_sso_corridor_is_visible(self) -> None:
        # Taiyuan SSW SSO corridor (~191 deg) around 06:25 CST in late Nov
        launch = _make_launch(
            "Taiyuan Satellite Launch Center, China",
            38.8489,
            111.6080,
            datetime(2026, 11, 28, 6, 25, tzinfo=CST),
            orbit_abbrev="SSO",
        )
        res = evaluate_chengdu_visibility(launch)
        self.assertGreaterEqual(res.probability, ProbabilityLevel.MEDIUM)
        self.assertGreater(res.max_elevation_deg, 5.0)

    def test_daytime_and_deep_night_are_rejected(self) -> None:
        day_launch = _make_launch(
            "Xichang Satellite Launch Center, China",
            28.2460,
            102.0266,
            datetime(2026, 11, 28, 12, 30, tzinfo=CST),
        )
        night_launch = _make_launch(
            "Xichang Satellite Launch Center, China",
            28.2460,
            102.0266,
            datetime(2026, 11, 28, 2, 0, tzinfo=CST),
        )
        self.assertEqual(evaluate_chengdu_visibility(day_launch).probability, ProbabilityLevel.NONE)
        self.assertEqual(evaluate_chengdu_visibility(night_launch).probability, ProbabilityLevel.NONE)

    def test_date_only_placeholder_is_ignored(self) -> None:
        # Many unannounced launches default to 00:00 UTC (08:00 CST) with DAY/TBD precision
        tbd_launch = _make_launch(
            "Xichang Satellite Launch Center, China",
            28.2460,
            102.0266,
            datetime(2026, 11, 28, 6, 45, tzinfo=CST),
            precision="DAY",
            status="TBD",
        )
        res = evaluate_chengdu_visibility(tbd_launch)
        self.assertEqual(res.probability, ProbabilityLevel.NONE)
        self.assertIn("待定", res.twilight_phase_zh)


if __name__ == "__main__":
    unittest.main()
