"""Astronomical & 3D geometric calculations for Chengdu rocket twilight cloud visibility.

Implements:
1. High-accuracy solar position (Jean Meeus / NOAA algorithm, ~0.01 deg precision).
2. WGS-84 / spherical Earth 3D ECEF -> local East-North-Up (ENU) line-of-sight elevation & azimuth.
3. High-altitude exhaust plume solar illumination above tropospheric screening layer (~12 km).
4. Realistic ascent profiles & downrange azimuth corridors for Chinese spaceports (Xichang, Jiuquan, Taiyuan, Wenchang)
   and general regional launch pads within geometric range of Chengdu.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .models import LaunchEvent, PlumeSample, ProbabilityLevel, VisibilityAssessment

# Default observer: Chengdu downtown (Tianfu Square), Sichuan, China
CHENGDU_LAT = 30.6586
CHENGDU_LON = 104.0648
CHENGDU_ALT_KM = 0.50  # ~500m elevation

EARTH_RADIUS_KM = 6371.0
TROPOSPHERE_SCREEN_KM = 12.0  # Dense clouds/aerosols block grazing rays below ~12 km


@dataclass(frozen=True)
class SpaceportProfile:
    key: str
    name_zh: str
    lat: float
    lon: float
    keywords: Tuple[str, ...]
    # Map orbit hints -> list of (label_zh, launch_azimuth_deg)
    default_corridors: Tuple[Tuple[str, float], ...]


KNOWN_SPACEPORTS: Dict[str, SpaceportProfile] = {
    "xichang": SpaceportProfile(
        key="xichang",
        name_zh="西昌卫星发射中心",
        lat=28.2460,
        lon=102.0266,
        keywords=("xichang", "西昌"),
        default_corridors=(
            ("东南射向（GTO/地球同步/低轨常规航向）", 108.0),
            ("正南偏东射向（极轨/太阳同步/遥感航向）", 172.0),
        ),
    ),
    "jiuquan": SpaceportProfile(
        key="jiuquan",
        name_zh="酒泉卫星发射中心（东风航天城）",
        lat=40.9606,
        lon=100.2914,
        keywords=("jiuquan", "dongfeng", "酒泉", "东风"),
        default_corridors=(
            ("东南射向（空间站/近地倾角轨道，向陕甘川方向飞行）", 128.0),
            ("西南偏南射向（太阳同步轨道 SSO）", 193.0),
        ),
    ),
    "taiyuan": SpaceportProfile(
        key="taiyuan",
        name_zh="太原卫星发射中心",
        lat=38.8489,
        lon=111.6080,
        keywords=("taiyuan", "kelan", "太原", "岢岚"),
        default_corridors=(
            ("南偏西射向（太阳同步轨道 SSO，向晋陕方向南下）", 191.0),
            ("东南射向（中低倾角近地轨道）", 135.0),
        ),
    ),
    "wenchang": SpaceportProfile(
        key="wenchang",
        name_zh="文昌/海南商业航天发射场",
        lat=19.6145,
        lon=110.9510,
        keywords=("wenchang", "hainan", "文昌", "海南商业航天"),
        default_corridors=(
            ("东南射向（GTO/近地轨道，向南海/西太飞行）", 105.0),
            ("正南射向（太阳同步轨道，向南海飞行）", 175.0),
        ),
    ),
    "haiyang": SpaceportProfile(
        key="haiyang",
        name_zh="东方航天港（山东海阳/黄海海上发射）",
        lat=36.6800,
        lon=121.2600,
        keywords=("haiyang", "yellow sea", "oriental spaceport", "海阳", "黄海"),
        default_corridors=(
            ("南偏东射向（海上太阳同步/近地轨道）", 170.0),
        ),
    ),
}

# Typical orbital rocket ascent profile: (flight_time_sec, altitude_km, ground_range_km)
# Covers T+90s (stratosphere/mesosphere plume expansion) through T+510s (second stage high-altitude plume).
ASCENT_PROFILE: Tuple[Tuple[int, float, float], ...] = (
    (90, 38.0, 22.0),
    (120, 62.0, 55.0),
    (150, 88.0, 105.0),
    (180, 112.0, 170.0),
    (210, 135.0, 250.0),
    (240, 156.0, 345.0),
    (270, 175.0, 455.0),
    (300, 192.0, 580.0),
    (360, 220.0, 860.0),
    (420, 242.0, 1160.0),
    (480, 260.0, 1480.0),
)


def julian_day(dt_utc: datetime) -> float:
    """Compute Julian Day for a timezone-aware UTC datetime."""
    utc = dt_utc.astimezone(timezone.utc)
    y = utc.year
    m = utc.month
    d = utc.day + (utc.hour + utc.minute / 60.0 + (utc.second + utc.microsecond / 1e6) / 3600.0) / 24.0
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + (a // 4)
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + b - 1524.5


def solar_alt_az(dt_utc: datetime, lat_deg: float, lon_deg: float) -> Tuple[float, float]:
    """Return (solar_altitude_deg, solar_azimuth_deg) at (lat_deg, lon_deg) using Jean Meeus equations."""
    jd = julian_day(dt_utc)
    t = (jd - 2451545.0) / 36525.0  # Julian centuries since J2000.0

    # Geometric mean longitude & mean anomaly of the Sun (deg)
    l0 = (280.46646 + t * (36000.76983 + 0.0003032 * t)) % 360.0
    m = (357.52911 + t * (35999.05029 - 0.0001537 * t)) % 360.0
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    m_rad = math.radians(m)
    c = (
        math.sin(m_rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m_rad) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m_rad) * 0.000289
    )
    sun_true_lon = l0 + c
    omega = 125.04 - 1934.136 * t
    sun_app_lon = sun_true_lon - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    mean_obliq = 23.0 + (26.0 + (21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    obliq_corr = mean_obliq + 0.00256 * math.cos(math.radians(omega))

    app_lon_rad = math.radians(sun_app_lon)
    obliq_rad = math.radians(obliq_corr)

    # Right ascension and declination
    sin_dec = math.sin(obliq_rad) * math.sin(app_lon_rad)
    dec_rad = math.asin(max(-1.0, min(1.0, sin_dec)))
    ra_deg = math.degrees(
        math.atan2(math.cos(obliq_rad) * math.sin(app_lon_rad), math.cos(app_lon_rad))
    ) % 360.0

    # Greenwich Mean Sidereal Time (deg)
    gmst = (
        280.46061837
        + 360.98564736629 * (jd - 2451545.0)
        + 0.000387933 * t * t
        - (t * t * t) / 38710000.0
    ) % 360.0

    # Local hour angle
    ha_deg = (gmst + lon_deg - ra_deg) % 360.0
    if ha_deg > 180.0:
        ha_deg -= 360.0
    ha_rad = math.radians(ha_deg)
    lat_rad = math.radians(lat_deg)

    sin_alt = math.sin(lat_rad) * math.sin(dec_rad) + math.cos(lat_rad) * math.cos(dec_rad) * math.cos(ha_rad)
    alt_rad = math.asin(max(-1.0, min(1.0, sin_alt)))
    alt_deg = math.degrees(alt_rad)

    # Azimuth from North, clockwise [0, 360)
    y = -math.sin(ha_rad) * math.cos(dec_rad)
    x = math.sin(dec_rad) * math.cos(lat_rad) - math.cos(dec_rad) * math.sin(lat_rad) * math.cos(ha_rad)
    az_deg = math.degrees(math.atan2(y, x)) % 360.0
    return alt_deg, az_deg


def great_circle_destination(lat_deg: float, lon_deg: float, bearing_deg: float, distance_km: float) -> Tuple[float, float]:
    """Compute subpoint (lat, lon) after moving distance_km from (lat_deg, lon_deg) along initial bearing_deg."""
    ang_dist = distance_km / EARTH_RADIUS_KM
    lat1 = math.radians(lat_deg)
    lon1 = math.radians(lon_deg)
    brng = math.radians(bearing_deg)

    sin_lat2 = math.sin(lat1) * math.cos(ang_dist) + math.cos(lat1) * math.sin(ang_dist) * math.cos(brng)
    lat2 = math.asin(max(-1.0, min(1.0, sin_lat2)))
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(ang_dist) * math.cos(lat1),
        math.cos(ang_dist) - math.sin(lat1) * math.sin(lat2),
    )
    lon2_deg = (math.degrees(lon2) + 540.0) % 360.0 - 180.0
    return math.degrees(lat2), lon2_deg


def haversine_km(lat1_deg: float, lon1_deg: float, lat2_deg: float, lon2_deg: float) -> float:
    """Surface great-circle distance in km."""
    r1, r2 = math.radians(lat1_deg), math.radians(lat2_deg)
    dlat = math.radians(lat2_deg - lat1_deg)
    dlon = math.radians(lon2_deg - lon1_deg)
    a = math.sin(dlat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2
    return 2.0 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def observer_look_angles(
    obs_lat_deg: float,
    obs_lon_deg: float,
    obs_alt_km: float,
    target_lat_deg: float,
    target_lon_deg: float,
    target_alt_km: float,
) -> Tuple[float, float, float]:
    """Compute (apparent_elevation_deg, azimuth_deg, slant_distance_km) from observer to 3D target."""
    def to_ecef(lat_d: float, lon_d: float, alt_k: float) -> Tuple[float, float, float]:
        r = EARTH_RADIUS_KM + alt_k
        lat_r = math.radians(lat_d)
        lon_r = math.radians(lon_d)
        return (
            r * math.cos(lat_r) * math.cos(lon_r),
            r * math.cos(lat_r) * math.sin(lon_r),
            r * math.sin(lat_r),
        )

    ox, oy, oz = to_ecef(obs_lat_deg, obs_lon_deg, obs_alt_km)
    tx, ty, tz = to_ecef(target_lat_deg, target_lon_deg, target_alt_km)
    dx, dy, dz = tx - ox, ty - oy, tz - oz
    slant_km = math.sqrt(dx * dx + dy * dy + dz * dz)
    if slant_km < 1e-6:
        return 90.0, 0.0, 0.0

    lat_r = math.radians(obs_lat_deg)
    lon_r = math.radians(obs_lon_deg)

    # Project onto local East-North-Up (ENU) basis at observer
    east = -math.sin(lon_r) * dx + math.cos(lon_r) * dy
    north = (
        -math.sin(lat_r) * math.cos(lon_r) * dx
        - math.sin(lat_r) * math.sin(lon_r) * dy
        + math.cos(lat_r) * dz
    )
    up = (
        math.cos(lat_r) * math.cos(lon_r) * dx
        + math.cos(lat_r) * math.sin(lon_r) * dy
        + math.sin(lat_r) * dz
    )

    geom_el_deg = math.degrees(math.asin(max(-1.0, min(1.0, up / slant_km))))
    # Standard atmospheric refraction near the horizon (~0.35 deg near 0 deg, tapering above 5 deg)
    refraction_deg = 0.0
    if -1.0 <= geom_el_deg <= 15.0:
        refraction_deg = 1.02 / math.tan(math.radians(geom_el_deg + 10.3 / (geom_el_deg + 5.11))) / 60.0
    apparent_el_deg = geom_el_deg + refraction_deg
    az_deg = math.degrees(math.atan2(east, north)) % 360.0
    return apparent_el_deg, az_deg, slant_km


def shadow_horizon_dip_deg(altitude_km: float, screen_height_km: float = TROPOSPHERE_SCREEN_KM) -> float:
    """Maximum solar depression angle (positive deg) at which a plume at altitude_km remains sunlit."""
    if altitude_km <= screen_height_km:
        return 0.0
    ratio = (EARTH_RADIUS_KM + screen_height_km) / (EARTH_RADIUS_KM + altitude_km)
    ratio = max(-1.0, min(1.0, ratio))
    return math.degrees(math.acos(ratio))


def compass_direction_zh(azimuth_deg: float) -> str:
    """Convert azimuth [0, 360) into readable 8/16-point Chinese direction + degree."""
    dirs = (
        "正北", "北偏东", "东北", "东偏北",
        "正东", "东偏南", "东南", "南偏东",
        "正南", "南偏西", "西南", "西偏南",
        "正西", "西偏北", "西北", "北偏西",
    )
    idx = int(((azimuth_deg % 360.0) + 11.25) // 22.5) % 16
    return f"{dirs[idx]}（{azimuth_deg:.0f}°）"


def identify_spaceport(launch: LaunchEvent) -> Optional[SpaceportProfile]:
    """Match a LaunchEvent to one of China's spaceports by name or coordinates."""
    haystack = f"{launch.pad_name} {launch.location_name} {launch.name}".lower()
    for sp in KNOWN_SPACEPORTS.values():
        if any(kw in haystack for kw in sp.keywords):
            return sp
    if launch.latitude is not None and launch.longitude is not None:
        for sp in KNOWN_SPACEPORTS.values():
            if haversine_km(launch.latitude, launch.longitude, sp.lat, sp.lon) < 120.0:
                return sp
    return None


def select_corridors(launch: LaunchEvent, sp: Optional[SpaceportProfile]) -> List[Tuple[str, float]]:
    """Select plausible launch azimuth corridors based on spaceport and mission orbit."""
    orbit_text = f"{launch.orbit_name} {launch.orbit_abbrev} {launch.mission_name}".lower()
    is_sso = any(k in orbit_text for k in ("sun-synchronous", "sso", "polar", "太阳同步", "极轨"))
    is_gto = any(k in orbit_text for k in ("geostationary", "geosynchronous", "gto", "geo", "beidou", "tjs", "地球同步"))
    is_css = any(k in orbit_text for k in ("shenzhou", "tianzhou", "space station", "tiangong", "神舟", "天舟", "空间站"))

    if sp is not None:
        if sp.key == "xichang":
            if is_sso:
                return [("正南偏东射向（太阳同步轨道）", 172.0)]
            if is_gto:
                return [("东南偏东射向（地球同步转移轨道 GTO）", 104.0)]
            return list(sp.default_corridors)
        if sp.key == "jiuquan":
            if is_css:
                return [("东南射向（空间站载人/货运轨道 ~41.5°倾角）", 128.0)]
            if is_sso:
                return [("西南偏南射向（太阳同步轨道 SSO）", 193.0)]
            return list(sp.default_corridors)
        if sp.key == "taiyuan":
            if is_sso:
                return [("南偏西射向（太阳同步轨道 SSO）", 191.0)]
            return list(sp.default_corridors)
        return list(sp.default_corridors)

    # Generic nearby spaceport (e.g. neighboring regional launch within range)
    return [("东南常规入轨航向", 115.0), ("正南太阳同步航向", 180.0)]


def _twilight_phase_zh(dt_utc: datetime, obs_lat: float, obs_lon: float) -> str:
    """Determine whether twilight is dawn (morning) or dusk (evening) at observer."""
    alt_now, _ = solar_alt_az(dt_utc, obs_lat, obs_lon)
    alt_later, _ = solar_alt_az(dt_utc + timedelta(minutes=15), obs_lat, obs_lon)
    if alt_later > alt_now:
        return "清晨曙光窗口（日出前）"
    return "傍晚暮光窗口（日落后）"


def evaluate_chengdu_visibility(
    launch: LaunchEvent,
    obs_lat: float = CHENGDU_LAT,
    obs_lon: float = CHENGDU_LON,
    obs_alt_km: float = CHENGDU_ALT_KM,
) -> VisibilityAssessment:
    """Evaluate whether `launch` can produce a rocket twilight cloud visible from Chengdu."""
    sp = identify_spaceport(launch)
    if sp is not None:
        launch.site_key = sp.key
        pad_lat = launch.latitude if launch.latitude is not None else sp.lat
        pad_lon = launch.longitude if launch.longitude is not None else sp.lon
    else:
        pad_lat = launch.latitude
        pad_lon = launch.longitude

    liftoff_sun_alt, _ = solar_alt_az(launch.net_utc, obs_lat, obs_lon)
    twilight_phase = _twilight_phase_zh(launch.net_utc, obs_lat, obs_lon)

    if pad_lat is None or pad_lon is None:
        return VisibilityAssessment(
            launch=launch,
            probability=ProbabilityLevel.NONE,
            twilight_phase_zh=twilight_phase,
            trajectory_label_zh="未知发射阵地坐标",
            chengdu_sun_alt_at_liftoff_deg=liftoff_sun_alt,
            best_sample=None,
            visible_start_sec=None,
            visible_end_sec=None,
            azimuth_range_zh="未知",
            max_elevation_deg=0.0,
            reason_zh="缺少发射工位经纬度且未匹配到已知国内发射场。",
            viewing_tips_zh="",
        )

    pad_dist_km = haversine_km(obs_lat, obs_lon, pad_lat, pad_lon)
    if pad_dist_km > 2400.0:
        return VisibilityAssessment(
            launch=launch,
            probability=ProbabilityLevel.NONE,
            twilight_phase_zh=twilight_phase,
            trajectory_label_zh="超出几何目视半径",
            chengdu_sun_alt_at_liftoff_deg=liftoff_sun_alt,
            best_sample=None,
            visible_start_sec=None,
            visible_end_sec=None,
            azimuth_range_zh="-",
            max_elevation_deg=0.0,
            reason_zh=f"发射场距成都约 {pad_dist_km:.0f} km，受地球曲率遮挡无法在成都看到火箭云。",
            viewing_tips_zh="",
        )

    if not launch.has_precise_time:
        return VisibilityAssessment(
            launch=launch,
            probability=ProbabilityLevel.NONE,
            twilight_phase_zh="具体发射时刻待定（TBD）",
            trajectory_label_zh=sp.name_zh if sp else launch.location_name,
            chengdu_sun_alt_at_liftoff_deg=liftoff_sun_alt,
            best_sample=None,
            visible_start_sec=None,
            visible_end_sec=None,
            azimuth_range_zh="-",
            max_elevation_deg=0.0,
            reason_zh="当前仅公布日期占位符，尚未披露精确到小时/分钟的发射窗口，待窗口更新后自动重算。",
            viewing_tips_zh="",
        )

    corridors = select_corridors(launch, sp)

    best_overall_prob = ProbabilityLevel.NONE
    best_corridor_label = corridors[0][0]
    best_corridor_az = corridors[0][1]
    best_corridor_samples: List[PlumeSample] = []
    best_peak_sample: Optional[PlumeSample] = None
    best_score = -1e9

    for corridor_label, az_deg in corridors:
        qualifying: List[PlumeSample] = []
        corridor_best_sample: Optional[PlumeSample] = None
        corridor_best_score = -1e9

        for t_sec, alt_km, downrange_km in ASCENT_PROFILE:
            sample_dt = launch.net_utc + timedelta(seconds=t_sec)
            sub_lat, sub_lon = great_circle_destination(pad_lat, pad_lon, az_deg, downrange_km)
            el_deg, look_az_deg, slant_km = observer_look_angles(
                obs_lat, obs_lon, obs_alt_km, sub_lat, sub_lon, alt_km
            )
            cd_sun_alt, _ = solar_alt_az(sample_dt, obs_lat, obs_lon)
            plume_sun_alt, _ = solar_alt_az(sample_dt, sub_lat, sub_lon)
            dip_deg = shadow_horizon_dip_deg(alt_km)
            # Require at least 0.5 deg clearance above dense tropospheric grazing shadow
            sunlit_margin = plume_sun_alt - (-dip_deg + 0.5)

            sample = PlumeSample(
                flight_time_sec=t_sec,
                timestamp_utc=sample_dt,
                subpoint_lat=sub_lat,
                subpoint_lon=sub_lon,
                altitude_km=alt_km,
                distance_from_chengdu_km=slant_km,
                chengdu_elevation_deg=el_deg,
                chengdu_azimuth_deg=look_az_deg,
                chengdu_sun_alt_deg=cd_sun_alt,
                plume_sun_alt_deg=plume_sun_alt,
                shadow_limit_deg=-dip_deg,
                sunlit_margin_deg=sunlit_margin,
            )

            # Visibility gate:
            # 1) Chengdu sky must be dark enough (Sun <= -2.8 deg; brighter than -2.8 deg washes out plume)
            # 2) High-altitude plume must be sunlit (sunlit_margin > 0)
            # 3) Plume must sit above Chengdu's horizon (>= 1.5 deg apparent elevation)
            if cd_sun_alt <= -2.8 and sunlit_margin > 0.0 and el_deg >= 1.5:
                qualifying.append(sample)
                # Score favors dark sky (-6 to -15 deg), high elevation, and solid sunlight margin
                dark_bonus = 10.0 if -16.0 <= cd_sun_alt <= -5.5 else 4.0
                score = el_deg * 2.0 + min(sunlit_margin, 8.0) + dark_bonus
                if score > corridor_best_score:
                    corridor_best_score = score
                    corridor_best_sample = sample

        # Classify probability for this corridor
        prob = ProbabilityLevel.NONE
        if qualifying and corridor_best_sample is not None:
            max_el = max(s.chengdu_elevation_deg for s in qualifying)
            best_cd_sun = corridor_best_sample.chengdu_sun_alt_deg
            best_margin = corridor_best_sample.sunlit_margin_deg

            if max_el >= 8.0 and -16.5 <= best_cd_sun <= -5.0 and best_margin >= 1.5:
                prob = ProbabilityLevel.HIGH
            elif max_el >= 3.8 and -19.5 <= best_cd_sun <= -3.5 and best_margin >= 0.8:
                prob = ProbabilityLevel.MEDIUM
            else:
                prob = ProbabilityLevel.LOW

        if (prob > best_overall_prob) or (prob == best_overall_prob and corridor_best_score > best_score):
            best_overall_prob = prob
            best_score = corridor_best_score
            best_corridor_label = corridor_label
            best_corridor_az = az_deg
            best_corridor_samples = qualifying
            best_peak_sample = corridor_best_sample

    if best_overall_prob == ProbabilityLevel.NONE or not best_corridor_samples or best_peak_sample is None:
        # Provide clear diagnostic reason why it won't produce a rocket cloud in Chengdu
        if liftoff_sun_alt > -2.8:
            reason = f"发射时成都太阳高度角为 {liftoff_sun_alt:+.1f}°（白昼或民用曙暮光过亮），天空背景太亮无法显现高空火箭云。"
        elif liftoff_sun_alt < -22.0:
            reason = f"发射时成都太阳高度角为 {liftoff_sun_alt:+.1f}°（深夜），即使火箭升至 250 km 高空仍处于地球本影内，羽流无法被阳光照亮。"
        else:
            reason = (
                f"发射时成都太阳高度角为 {liftoff_sun_alt:+.1f}°，虽临近曙暮光时段，"
                "但结合射向轨迹计算，受光高空羽流在成都地平线以下或刚入地影，不满足目击几何条件。"
            )
        return VisibilityAssessment(
            launch=launch,
            probability=ProbabilityLevel.NONE,
            twilight_phase_zh=twilight_phase,
            trajectory_label_zh=best_corridor_label,
            chengdu_sun_alt_at_liftoff_deg=liftoff_sun_alt,
            best_sample=None,
            visible_start_sec=None,
            visible_end_sec=None,
            azimuth_range_zh="-",
            max_elevation_deg=0.0,
            reason_zh=reason,
            viewing_tips_zh="",
        )

    vis_start = best_corridor_samples[0].flight_time_sec
    vis_end = best_corridor_samples[-1].flight_time_sec
    max_el = max(s.chengdu_elevation_deg for s in best_corridor_samples)
    start_dir = compass_direction_zh(best_corridor_samples[0].chengdu_azimuth_deg)
    end_dir = compass_direction_zh(best_corridor_samples[-1].chengdu_azimuth_deg)
    az_summary = start_dir if start_dir == end_dir else f"{start_dir} → {end_dir}"

    site_display = sp.name_zh if sp else launch.location_name
    reason = (
        f"{site_display}发射升空后 T+{vis_start}s 至 T+{vis_end}s 期间，"
        f"成都地面太阳高度角约 {best_peak_sample.chengdu_sun_alt_deg:+.1f}°（天幕已暗），"
        f"而 {best_peak_sample.altitude_km:.0f} km 高空火箭尾迹仍高于地影边界 {best_peak_sample.sunlit_margin_deg:.1f}° 被阳光直射，"
        f"成都视线最高仰角可达 {max_el:.1f}°，具备典型的“暮光水母云/火箭云”光学条件。"
    )

    if max_el >= 12.0:
        tips = (
            f"观测仰角较高（最高约 {max_el:.1f}°），成都市区高层楼顶或视野开阔处即可肉眼及手机广角/中长焦拍摄；"
            f"请提前对准 {az_summary} 天空，重点留意点火后 2~7 分钟的高空扇形/水母状发光云团。"
        )
    elif max_el >= 5.0:
        tips = (
            f"最高仰角约 {max_el:.1f}°，易受盆地低空云雾和城市建筑遮挡；建议前往龙泉山观景台、都江堰/彭州浅山或高层天台，"
            f"朝 {az_summary} 低空方向使用 50mm–200mm 中长焦镜头守候（点火后约 {vis_start // 60}~{max(vis_end // 60, 3)} 分钟）。"
        )
    else:
        tips = (
            f"属于远距离极限异地目击（最高仰角仅约 {max_el:.1f}°），必须在晴朗高通透天气下前往龙泉山或川西高海拔无遮挡机位，"
            f"朝 {az_summary} 贴近地平线方向使用长焦相机拍摄。"
        )

    return VisibilityAssessment(
        launch=launch,
        probability=best_overall_prob,
        twilight_phase_zh=twilight_phase,
        trajectory_label_zh=best_corridor_label,
        chengdu_sun_alt_at_liftoff_deg=liftoff_sun_alt,
        best_sample=best_peak_sample,
        visible_start_sec=vis_start,
        visible_end_sec=vis_end,
        azimuth_range_zh=az_summary,
        max_elevation_deg=max_el,
        reason_zh=reason,
        viewing_tips_zh=tips,
        pad_lat=pad_lat,
        pad_lon=pad_lon,
        corridor_az_deg=best_corridor_az,
    )
