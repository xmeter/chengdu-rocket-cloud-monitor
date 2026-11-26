"""Data models for rocket launches and Chengdu rocket-cloud visibility assessments."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import List, Optional


class ProbabilityLevel(IntEnum):
    """Probability tiers for observing a rocket twilight cloud from Chengdu."""

    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3

    @property
    def label_zh(self) -> str:
        labels = {
            ProbabilityLevel.NONE: "无可见可能",
            ProbabilityLevel.LOW: "低概率（极限异地目击）",
            ProbabilityLevel.MEDIUM: "中等概率（晴好天气可见）",
            ProbabilityLevel.HIGH: "高概率（强烈推荐守候）",
        }
        return labels[self]

    @property
    def badge_zh(self) -> str:
        badges = {
            ProbabilityLevel.NONE: "无",
            ProbabilityLevel.LOW: "低概率",
            ProbabilityLevel.MEDIUM: "中等概率",
            ProbabilityLevel.HIGH: "高概率",
        }
        return badges[self]

    @classmethod
    def from_str(cls, value: str) -> "ProbabilityLevel":
        normalized = (value or "").strip().upper()
        mapping = {
            "NONE": cls.NONE,
            "LOW": cls.LOW,
            "MEDIUM": cls.MEDIUM,
            "HIGH": cls.HIGH,
        }
        if normalized not in mapping:
            raise ValueError(f"Unknown probability level: {value!r} (expected LOW, MEDIUM, or HIGH)")
        return mapping[normalized]


@dataclass
class LaunchEvent:
    """Normalized upcoming rocket launch event merged across data sources."""

    uid: str
    name: str
    rocket_name: str
    mission_name: str
    provider: str
    pad_name: str
    location_name: str
    country_code: str
    latitude: Optional[float]
    longitude: Optional[float]
    net_utc: datetime
    window_start_utc: Optional[datetime] = None
    window_end_utc: Optional[datetime] = None
    status_abbrev: str = "Unknown"
    status_name: str = "Unknown"
    time_precision: str = "MIN"  # SEC, MIN, HOUR, DAY, MONTH, TBD
    orbit_name: str = ""
    orbit_abbrev: str = ""
    info_url: str = ""
    sources: List[str] = field(default_factory=list)
    site_key: str = ""  # xichang, jiuquan, taiyuan, wenchang, or other

    @property
    def has_precise_time(self) -> bool:
        """Return True if the launch timestamp has at least hour/minute precision (not a date-only placeholder)."""
        if self.status_abbrev.upper() in {"TBD"}:
            return False
        if self.time_precision.upper() in {"DAY", "MONTH", "QUARTER", "YEAR", "TBD"}:
            return False
        return True


@dataclass
class PlumeSample:
    """Single trajectory sample point evaluated for sunlight illumination & Chengdu line-of-sight."""

    flight_time_sec: int
    timestamp_utc: datetime
    subpoint_lat: float
    subpoint_lon: float
    altitude_km: float
    distance_from_chengdu_km: float
    chengdu_elevation_deg: float
    chengdu_azimuth_deg: float
    chengdu_sun_alt_deg: float
    plume_sun_alt_deg: float
    shadow_limit_deg: float
    sunlit_margin_deg: float

    @property
    def is_sunlit(self) -> bool:
        return self.sunlit_margin_deg > 0.0


@dataclass
class VisibilityAssessment:
    """Full evaluation of whether a launch can produce a visible rocket cloud over Chengdu."""

    launch: LaunchEvent
    probability: ProbabilityLevel
    twilight_phase_zh: str  # e.g. "清晨曙光（日出前）" or "傍晚暮光（日落后）"
    trajectory_label_zh: str
    chengdu_sun_alt_at_liftoff_deg: float
    best_sample: Optional[PlumeSample]
    visible_start_sec: Optional[int]
    visible_end_sec: Optional[int]
    azimuth_range_zh: str
    max_elevation_deg: float
    reason_zh: str
    viewing_tips_zh: str
    pad_lat: Optional[float] = None
    pad_lon: Optional[float] = None
    corridor_az_deg: Optional[float] = None
