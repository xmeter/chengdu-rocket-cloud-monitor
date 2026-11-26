"""Pure-stdlib renderer that simulates the rocket twilight cloud as seen from Chengdu.

Produces PNG images (no third-party dependencies) showing the sky in the
observer's azimuth/elevation frame at a given moment after liftoff:

* twilight sky gradient (horizon glow towards the Sun, stars when dark enough)
* full exhaust trail, expanding with altitude and age (rarefied upper-atmosphere plume)
* sunlit trail segments glow white/blue, near-terminator segments orange, shadowed parts faint
* "jellyfish" bell around the upper stage when it is high and sunlit
* horizon silhouette, azimuth / elevation ticks, and T+ minute markers

The result is a physically motivated *schematic*, not a photographic prediction.
"""

from __future__ import annotations

import math
import random
import struct
import zlib
from array import array
from dataclasses import dataclass
from datetime import timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from .astronomy import (
    ASCENT_PROFILE,
    CHENGDU_ALT_KM,
    CHENGDU_LAT,
    CHENGDU_LON,
    great_circle_destination,
    observer_look_angles,
    shadow_horizon_dip_deg,
    solar_alt_az,
)
from .models import VisibilityAssessment

WIDTH = 640
HEIGHT = 340

# Full trajectory (time s, altitude km, downrange km), including the low-altitude climb.
_FULL_PROFILE: Tuple[Tuple[float, float, float], ...] = (
    (0.0, 0.0, 0.0),
    (30.0, 5.0, 1.0),
    (60.0, 18.0, 6.0),
) + tuple((float(t), a, r) for t, a, r in ASCENT_PROFILE) + ((540.0, 275.0, 1820.0),)


def trajectory_state(t_sec: float) -> Tuple[float, float]:
    """Linear interpolation of (altitude_km, downrange_km) at flight time t_sec."""
    prof = _FULL_PROFILE
    if t_sec <= prof[0][0]:
        return prof[0][1], prof[0][2]
    for (t0, a0, r0), (t1, a1, r1) in zip(prof, prof[1:]):
        if t_sec <= t1:
            f = (t_sec - t0) / (t1 - t0)
            return a0 + f * (a1 - a0), r0 + f * (r1 - r0)
    (t0, a0, r0), (t1, a1, r1) = prof[-2], prof[-1]
    f = (t_sec - t0) / (t1 - t0)
    return a0 + f * (a1 - a0), r0 + f * (r1 - r0)


@dataclass
class TrailPoint:
    t_sec: float
    alt_km: float
    el_deg: float
    az_deg: float
    slant_km: float
    sun_margin_deg: float  # >0 means sunlit at snapshot time


# ---------------------------------------------------------------------------
# Tiny 3x5 bitmap font for tick labels (digits, cardinal letters, T, +)
# ---------------------------------------------------------------------------
_GLYPHS: Dict[str, Tuple[str, ...]] = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "N": ("101", "111", "111", "111", "101"),
    "E": ("111", "100", "111", "100", "111"),
    "S": ("111", "100", "111", "001", "111"),
    "W": ("101", "101", "111", "111", "101"),
    "T": ("111", "010", "010", "010", "010"),
    "+": ("000", "010", "111", "010", "000"),
    "m": ("000", "000", "111", "111", "101"),
    "o": ("111", "101", "111", "000", "000"),  # degree sign
    " ": ("000", "000", "000", "000", "000"),
}


class Canvas:
    """Float RGB canvas with additive light buffer."""

    def __init__(self, width: int = WIDTH, height: int = HEIGHT) -> None:
        self.w = width
        self.h = height
        n = width * height
        self.bg_r = array("d", [0.0]) * n
        self.bg_g = array("d", [0.0]) * n
        self.bg_b = array("d", [0.0]) * n
        self.lr = array("d", [0.0]) * n
        self.lg = array("d", [0.0]) * n
        self.lb = array("d", [0.0]) * n
        self.overlay: Dict[int, Tuple[float, float, float]] = {}

    def add_gaussian(self, cx: float, cy: float, sigma: float, color: Tuple[float, float, float], intensity: float) -> None:
        if intensity <= 1e-5:
            return
        sigma = max(0.6, min(sigma, 45.0))
        rad = int(math.ceil(sigma * 2.8))
        x0, x1 = max(0, int(cx) - rad), min(self.w - 1, int(cx) + rad)
        y0, y1 = max(0, int(cy) - rad), min(self.h - 1, int(cy) + rad)
        if x0 > x1 or y0 > y1:
            return
        inv = 1.0 / (2.0 * sigma * sigma)
        wx = [math.exp(-((x - cx) ** 2) * inv) for x in range(x0, x1 + 1)]
        cr, cg, cb = color[0] * intensity, color[1] * intensity, color[2] * intensity
        lr, lg, lb, w = self.lr, self.lg, self.lb, self.w
        for y in range(y0, y1 + 1):
            wy = math.exp(-((y - cy) ** 2) * inv)
            if wy < 0.004:
                continue
            base = y * w + x0
            for i, wxv in enumerate(wx):
                k = wxv * wy
                if k < 0.004:
                    continue
                idx = base + i
                lr[idx] += cr * k
                lg[idx] += cg * k
                lb[idx] += cb * k

    def set_overlay(self, x: int, y: int, color: Tuple[float, float, float]) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self.overlay[y * self.w + x] = color

    def text(self, x: int, y: int, s: str, color: Tuple[float, float, float], scale: int = 2) -> None:
        cx = x
        for ch in s:
            glyph = _GLYPHS.get(ch)
            if glyph is None:
                cx += 4 * scale
                continue
            for gy, row in enumerate(glyph):
                for gx, bit in enumerate(row):
                    if bit == "1":
                        for sy in range(scale):
                            for sx in range(scale):
                                self.set_overlay(cx + gx * scale + sx, y + gy * scale + sy, color)
            cx += 4 * scale

    @staticmethod
    def text_width(s: str, scale: int = 2) -> int:
        return len(s) * 4 * scale - scale

    def to_png(self) -> bytes:
        raw = bytearray()
        w = self.w
        for y in range(self.h):
            raw.append(0)
            base = y * w
            for x in range(w):
                idx = base + x
                ov = self.overlay.get(idx)
                if ov is not None:
                    r, g, b = ov
                else:
                    # Tone-map additive light, then screen-blend over the sky
                    pr = 1.0 - math.exp(-self.lr[idx])
                    pg = 1.0 - math.exp(-self.lg[idx])
                    pb = 1.0 - math.exp(-self.lb[idx])
                    r = 1.0 - (1.0 - self.bg_r[idx]) * (1.0 - pr)
                    g = 1.0 - (1.0 - self.bg_g[idx]) * (1.0 - pg)
                    b = 1.0 - (1.0 - self.bg_b[idx]) * (1.0 - pb)
                raw.append(max(0, min(255, int(r * 255.0 + 0.5))))
                raw.append(max(0, min(255, int(g * 255.0 + 0.5))))
                raw.append(max(0, min(255, int(b * 255.0 + 0.5))))

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        ihdr = struct.pack(">IIBBBBB", w, self.h, 8, 2, 0, 0, 0)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


# ---------------------------------------------------------------------------
# Simulation helpers
# ---------------------------------------------------------------------------

def compute_trail(assessment: VisibilityAssessment, snapshot_sec: float, step_sec: float = 2.0) -> List[TrailPoint]:
    """Sky positions of the exhaust trail (emitted from T+10s up to snapshot) at the snapshot moment."""
    ln = assessment.launch
    if assessment.pad_lat is None or assessment.pad_lon is None or assessment.corridor_az_deg is None:
        return []
    snap_dt = ln.net_utc + timedelta(seconds=snapshot_sec)
    pts: List[TrailPoint] = []
    t = 10.0
    while t <= snapshot_sec + 1e-6:
        alt, rng = trajectory_state(t)
        lat, lon = great_circle_destination(assessment.pad_lat, assessment.pad_lon, assessment.corridor_az_deg, rng)
        el, az, slant = observer_look_angles(CHENGDU_LAT, CHENGDU_LON, CHENGDU_ALT_KM, lat, lon, alt)
        sun_alt, _ = solar_alt_az(snap_dt, lat, lon)
        margin = sun_alt + shadow_horizon_dip_deg(alt) - 0.5
        pts.append(TrailPoint(t, alt, el, az, slant, margin))
        t += step_sec
    return pts


def describe_shape_zh(assessment: VisibilityAssessment) -> str:
    """Qualitative prediction of the rocket cloud shape from the sky-track geometry."""
    trail = compute_trail(assessment, float(assessment.visible_end_sec or 360), step_sec=10.0)
    lit = [p for p in trail if p.sun_margin_deg > 0 and p.el_deg > 0]
    if len(lit) < 2:
        return "受光尾迹较短，预计为一段短促发光的斜向光带。"
    a, b = lit[0], lit[-1]
    daz = ((b.az_deg - a.az_deg + 540.0) % 360.0) - 180.0
    d_horiz = abs(daz) * math.cos(math.radians((a.el_deg + b.el_deg) / 2.0))
    d_vert = b.el_deg - a.el_deg
    span = math.hypot(d_horiz, d_vert)
    receding = lit[-1].slant_km > lit[0].slant_km * 1.3
    if span < 12.0 or (abs(d_vert) > d_horiz and receding):
        return (
            "火箭大致沿视线方向远离成都，高空羽流会在同一片天区持续膨胀，"
            "预计呈现典型的“水母 / 伞罩”正面形态：明亮的箭体光点外围包裹一圈逐渐扩大的半透明钟形光晕，下方拖着发光“触须”。"
        )
    if d_horiz > 2.5 * abs(d_vert):
        direction = "由右向左" if daz < 0 else "由左向右"
        return (
            f"火箭在成都视野中{direction}横向掠过天空，预计呈现一条长长的弧形发光尾迹，"
            "前端箭体附近出现侧向张开的扇形 / 彗尾状羽流，越往高空越蓬松宽大。"
        )
    return (
        "火箭斜向爬升穿过天空，预计先看到一条斜挂的金色光带，随后在高空端逐渐膨胀成扇形 / 半个水母状的光罩，"
        "下段低空尾迹因处于地影中颜色偏暗或呈橙红色。"
    )


def _sky_background(canvas: Canvas, to_az, to_el, sun_alt: float, sun_az: float, seed: int) -> None:
    glow_strength = max(0.0, min(1.0, (sun_alt + 18.0) / 15.0)) ** 2
    zen = 0.015 + 0.20 * glow_strength
    w, h = canvas.w, canvas.h
    for y in range(h):
        el = to_el(y)
        for x in range(w):
            idx = y * w + x
            if el < 0.0:
                continue
            az = to_az(x)
            daz = abs(((az - sun_az + 540.0) % 360.0) - 180.0)
            dir_w = 0.30 + 0.70 * math.exp(-((daz / 55.0) ** 2))
            hz = math.exp(-el / 7.0) * glow_strength * dir_w
            mid = math.exp(-el / 22.0) * glow_strength * dir_w * 0.35
            r = zen * 0.25 + hz * 0.95 + mid * 0.55
            g = zen * 0.40 + hz * 0.48 + mid * 0.45
            b = zen * 1.00 + hz * 0.22 + mid * 0.60 + 0.03
            canvas.bg_r[idx] = min(1.0, r)
            canvas.bg_g[idx] = min(1.0, g)
            canvas.bg_b[idx] = min(1.0, b)

    # Stars once the sky is dark enough
    star_vis = max(0.0, min(1.0, (-sun_alt - 8.0) / 8.0))
    if star_vis > 0:
        rng = random.Random(seed)
        for _ in range(int(260 * star_vis)):
            x, y = rng.randrange(w), rng.randrange(h)
            if to_el(y) < 1.5:
                continue
            idx = y * w + x
            s = rng.random() ** 3 * 0.75 * star_vis
            canvas.bg_r[idx] = min(1.0, canvas.bg_r[idx] + s)
            canvas.bg_g[idx] = min(1.0, canvas.bg_g[idx] + s)
            canvas.bg_b[idx] = min(1.0, canvas.bg_b[idx] + s * 1.05)


def _plume_color(margin: float, alt: float) -> Tuple[float, float, float]:
    if margin <= 0:
        return (0.30, 0.32, 0.38)
    if margin < 1.5:
        return (1.00, 0.52, 0.25)  # reddened grazing sunlight
    if margin < 3.5:
        return (1.00, 0.80, 0.55)  # golden
    if alt > 120:
        return (0.75, 0.88, 1.00)  # rarefied high plume: bluish white
    return (1.00, 0.95, 0.85)


def render_snapshot_png(assessment: VisibilityAssessment, snapshot_sec: float) -> Optional[bytes]:
    """Render one simulated sky view (PNG bytes) at T+snapshot_sec as seen from Chengdu."""
    trail = compute_trail(assessment, snapshot_sec)
    if not trail:
        return None
    ln = assessment.launch
    snap_dt = ln.net_utc + timedelta(seconds=snapshot_sec)
    sun_alt, sun_az = solar_alt_az(snap_dt, CHENGDU_LAT, CHENGDU_LON)

    # Field of view: equal angular scale on both axes, framing the visible trail
    vis = [p for p in trail if p.el_deg > -1.0] or trail
    ref_az = vis[len(vis) // 2].az_deg
    rel = [((p.az_deg - ref_az + 540.0) % 360.0) - 180.0 for p in vis]
    az_lo, az_hi = min(rel) - 12.0, max(rel) + 12.0
    el_top = max(p.el_deg for p in vis) + 10.0
    el_bottom = -3.0
    span_az = max(az_hi - az_lo, 50.0)
    span_el = max(el_top - el_bottom, 22.0)
    aspect = WIDTH / HEIGHT
    if span_az / span_el < aspect:
        span_az = span_el * aspect
    else:
        span_el = span_az / aspect
    az_center = (az_lo + az_hi) / 2.0
    az_min = az_center - span_az / 2.0
    scale = WIDTH / span_az  # px per degree

    def to_x(az: float) -> float:
        r = ((az - ref_az + 540.0) % 360.0) - 180.0
        return (r - az_min) * scale

    def to_y(el: float) -> float:
        return HEIGHT - (el - el_bottom) * scale

    def x_to_az(x: float) -> float:
        return (ref_az + az_min + x / scale) % 360.0

    def y_to_el(y: float) -> float:
        return el_bottom + (HEIGHT - y) / scale

    canvas = Canvas()
    seed = int(ln.net_utc.timestamp()) // 60
    _sky_background(canvas, x_to_az, y_to_el, sun_alt, sun_az, seed)

    # --- exhaust trail -------------------------------------------------------
    prev_xy: Optional[Tuple[float, float]] = None
    for p in trail:
        x, y = to_x(p.az_deg), to_y(p.el_deg)
        if prev_xy is None:
            prev_xy = (x, y)
            continue
        d_px = max(0.6, math.hypot(x - prev_xy[0], y - prev_xy[1]))
        prev_xy = (x, y)
        if p.el_deg < -0.5:
            continue
        age = snapshot_sec - p.t_sec
        expand_rate = 0.004 + 0.9 * (max(0.0, p.alt_km - 25.0) / 150.0) ** 1.6  # km/s, rarefied air expands fast
        width_km = 0.4 + 0.015 * p.alt_km + expand_rate * age
        sigma = max(0.8, math.degrees(width_km / p.slant_km) * scale * 0.5)
        airmass_ext = math.exp(-0.12 / max(math.sin(math.radians(max(p.el_deg, 0.5))), 0.06))
        if p.sun_margin_deg > 0:
            surf = 1.6 * (1.0 + min(p.alt_km, 200.0) / 120.0) / (1.0 + sigma / 30.0)
        else:
            surf = 0.16 if p.alt_km < 20 else 0.09
        inten = surf * airmass_ext * d_px / (sigma * 2.2)
        canvas.add_gaussian(x, y, sigma, _plume_color(p.sun_margin_deg, p.alt_km), inten)

    # --- jellyfish bell around upper stage ----------------------------------
    head = trail[-1]
    hx, hy = to_x(head.az_deg), to_y(head.el_deg)
    if head.sun_margin_deg > 0 and head.alt_km >= 90 and head.el_deg > -0.5 and len(trail) > 8:
        back = trail[-8]
        bx, by = to_x(back.az_deg) - hx, to_y(back.el_deg) - hy
        norm = math.hypot(bx, by)
        if norm < 1e-3:
            bx, by, norm = 0.0, 1.0, 1.0  # heading straight away: bell opens downward
        ux, uy = bx / norm, by / norm
        bell_km = 25.0 + 0.45 * head.alt_km
        bell_len = min(110.0, max(14.0, math.degrees(bell_km / head.slant_km) * scale))
        half_open = math.radians(42.0 if norm > 20 else 70.0)
        col = _plume_color(head.sun_margin_deg, head.alt_km)
        n_rays = 15
        for i in range(n_rays):
            frac = i / (n_rays - 1)
            ang = -half_open + 2 * half_open * frac
            ca, sa = math.cos(ang), math.sin(ang)
            rx, ry = ux * ca - uy * sa, ux * sa + uy * ca
            limb = 0.55 + 1.2 * abs(2 * frac - 1) ** 2  # limb-brightened edges of the bell
            steps = 16
            for s in range(1, steps + 1):
                f = s / steps
                dist = bell_len * f * (0.85 + 0.15 * math.cos(ang * 2))
                sig = 1.2 + 4.0 * f
                canvas.add_gaussian(hx + rx * dist, hy + ry * dist, sig, col, 0.10 * limb * (1.0 - 0.45 * f))
        # bright skirt edge of the bell
        for i in range(40):
            ang = -half_open + 2 * half_open * i / 39
            ca, sa = math.cos(ang), math.sin(ang)
            rx, ry = ux * ca - uy * sa, ux * sa + uy * ca
            dist = bell_len * (0.85 + 0.15 * math.cos(ang * 2))
            canvas.add_gaussian(hx + rx * dist, hy + ry * dist, 3.5, col, 0.12)
    # rocket point itself
    if head.el_deg > -0.5:
        canvas.add_gaussian(hx, hy, 1.6, (1.0, 1.0, 1.0), 2.5 if head.sun_margin_deg > 0 else 0.8)

    # --- ground silhouette ---------------------------------------------------
    w, h = canvas.w, canvas.h
    y_hor = to_y(0.0)
    for x in range(w):
        bump = (1.3 * math.sin(x * 0.021 + 1.7) + 0.8 * math.sin(x * 0.067 + 0.3) + 0.5 * math.sin(x * 0.13)) * scale * 0.12
        top = int(y_hor - max(0.0, bump + 0.6 * scale * 0.3))
        for y in range(max(0, top), h):
            idx = y * w + x
            canvas.bg_r[idx], canvas.bg_g[idx], canvas.bg_b[idx] = 0.018, 0.022, 0.030
            canvas.lr[idx] = canvas.lg[idx] = canvas.lb[idx] = 0.0

    # --- axis ticks & labels ------------------------------------------------
    grid = (0.55, 0.60, 0.70)
    tick_step = 10 if span_az <= 90 else 20
    start = int(math.floor((ref_az + az_min) / tick_step)) * tick_step
    a = start
    while a <= ref_az + az_min + span_az + tick_step:
        az = a % 360
        x = int(round(to_x(az)))
        if 0 <= x < w:
            for yy in range(h - 9, h - 4):
                canvas.set_overlay(x, yy, grid)
            cardinal = {0: "N", 90: "E", 180: "S", 270: "W"}.get(az)
            label = cardinal if cardinal else f"{az}o"
            tw = Canvas.text_width(label)
            canvas.text(x - tw // 2, h - 22, label, (0.95, 0.85, 0.45) if cardinal else grid)
        a += tick_step
    for el_tick in range(10, int(el_bottom + span_el), 10):
        y = int(round(to_y(el_tick)))
        if 34 <= y < h - 26:
            for xx in range(0, 6):
                canvas.set_overlay(xx, y, grid)
            canvas.text(9, y - 4, f"{el_tick}o", grid)

    # --- T+ minute markers along the trail -----------------------------------
    for minute in range(2, int(snapshot_sec // 60) + 1):
        t_mark = minute * 60.0
        p = min(trail, key=lambda q: abs(q.t_sec - t_mark))
        if abs(p.t_sec - t_mark) > 3 or p.el_deg < 0.5:
            continue
        mx, my = int(round(to_x(p.az_deg))), int(round(to_y(p.el_deg)))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                canvas.set_overlay(mx + dx, my + dy, (1.0, 0.35, 0.35))
        canvas.text(mx + 5, my + 4, f"T+{minute}m", (1.0, 0.55, 0.55))

    # snapshot time label (top-left)
    canvas.text(10, 10, f"T+{int(snapshot_sec // 60)}m{int(snapshot_sec % 60):02d}".replace("m00", "m"), (1.0, 1.0, 1.0), scale=3)
    return canvas.to_png()


def choose_snapshot_times(assessment: VisibilityAssessment) -> List[float]:
    """Pick two representative moments: early golden trail and late full 'jellyfish' stage."""
    start = float(assessment.visible_start_sec or 150)
    end = float(assessment.visible_end_sec or 420)
    first = min(max(start + 30.0, 180.0), end)
    second = min(max(end, first + 90.0), 540.0)
    return [first, second] if second - first >= 45 else [second]


def render_alert_images(assessments: Sequence[VisibilityAssessment]) -> List[List[Tuple[str, float, bytes]]]:
    """For each assessment return a list of (content_id, snapshot_sec, png_bytes)."""
    out: List[List[Tuple[str, float, bytes]]] = []
    for idx, a in enumerate(assessments, start=1):
        imgs: List[Tuple[str, float, bytes]] = []
        for j, t in enumerate(choose_snapshot_times(a), start=1):
            try:
                png = render_snapshot_png(a, t)
            except Exception:  # never let rendering break the alert email
                png = None
            if png:
                imgs.append((f"plume-{idx}-{j}@rocketcloud", t, png))
        out.append(imgs)
    return out
