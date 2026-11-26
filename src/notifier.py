"""SMTP Email notification builder and sender (HTML + Plain Text multipart)."""

from __future__ import annotations

import html
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from typing import List, Optional, Tuple

from .astronomy import KNOWN_SPACEPORTS
from .models import ProbabilityLevel
from .plume_render import describe_shape_zh
from .state import PendingAlert

CST = timezone(timedelta(hours=8))


@dataclass
class SMTPConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipients: List[str]
    security: str = "auto"  # "ssl", "starttls", or "auto"

    @classmethod
    def from_env(cls) -> "SMTPConfig":
        host = os.environ.get("SMTP_HOST", "").strip()
        port_raw = os.environ.get("SMTP_PORT", "465").strip()
        username = os.environ.get("SMTP_USER", "").strip()
        password = os.environ.get("SMTP_PASS", "").strip()
        sender = os.environ.get("NOTIFY_EMAIL_FROM", "").strip() or username
        to_raw = os.environ.get("NOTIFY_EMAIL_TO", "").strip()
        security = os.environ.get("SMTP_SECURITY", "auto").strip().lower()

        missing = []
        if not host:
            missing.append("SMTP_HOST")
        if not username:
            missing.append("SMTP_USER")
        if not password:
            missing.append("SMTP_PASS")
        if not to_raw:
            missing.append("NOTIFY_EMAIL_TO")
        if missing:
            raise ValueError(f"缺少必要的邮件环境变量配置: {', '.join(missing)}")

        recipients = [addr.strip() for addr in to_raw.replace(";", ",").split(",") if addr.strip()]
        return cls(
            host=host,
            port=int(port_raw),
            username=username,
            password=password,
            sender=sender,
            recipients=recipients,
            security=security,
        )


def _prob_colors(prob: ProbabilityLevel) -> Tuple[str, str]:
    if prob == ProbabilityLevel.HIGH:
        return "#dc2626", "#fef2f2"
    if prob == ProbabilityLevel.MEDIUM:
        return "#d97706", "#fffbeb"
    return "#2563eb", "#eff6ff"


def _site_title_zh(site_key: str, fallback: str) -> str:
    if site_key in KNOWN_SPACEPORTS:
        return KNOWN_SPACEPORTS[site_key].name_zh
    return fallback or "区域发射场"


def build_email_content(
    alerts: List[PendingAlert],
    images: Optional[List[List[Tuple[str, float, bytes]]]] = None,
) -> Tuple[str, str, str]:
    """Return `(subject, plain_text_body, html_body)` for one or more pending rocket cloud alerts.

    `images` (optional) is aligned with `alerts`; each entry is a list of
    `(content_id, snapshot_sec, png_bytes)` simulated sky views referenced via `cid:` in the HTML.
    """
    if not alerts:
        raise ValueError("alerts must not be empty")

    top_alert = max(alerts, key=lambda a: (a.assessment.probability, a.assessment.max_elevation_deg))
    first = alerts[0]
    first_cst = first.assessment.launch.net_utc.astimezone(CST)
    site_short = _site_title_zh(first.assessment.launch.site_key, first.assessment.launch.location_name).replace(
        "卫星发射中心", ""
    )
    prefix = "【窗口更新】" if any(a.is_schedule_update for a in alerts) else ""

    if len(alerts) == 1:
        subject = (
            f"🚀 {prefix}成都火箭云预警【{first.assessment.probability.badge_zh}】"
            f"{site_short} · {first.assessment.launch.rocket_name} "
            f"({first_cst.strftime('%m月%d日 %H:%M')} 北京时间)"
        )
    else:
        subject = (
            f"🚀 {prefix}成都火箭云预警：发现 {len(alerts)} 次潜在可见发射窗口"
            f"（最高：{top_alert.assessment.probability.badge_zh}）"
        )

    text_lines: List[str] = [
        "成都区域“火箭云（暮光水母云）”自动监测预警",
        "=" * 52,
        f"本次共检测到 {len(alerts)} 个满足“成都地面入暗 + 高空羽流迎光 + 地平线以上可见”条件的发射计划：",
        "",
    ]

    cards_html: List[str] = []

    for idx, alert in enumerate(alerts, start=1):
        a = alert.assessment
        ln = a.launch
        t0_cst = ln.net_utc.astimezone(CST)
        t0_utc = ln.net_utc.astimezone(timezone.utc)
        vis_start_cst = (ln.net_utc + timedelta(seconds=a.visible_start_sec or 120)).astimezone(CST)
        vis_end_cst = (ln.net_utc + timedelta(seconds=a.visible_end_sec or 360)).astimezone(CST)
        site_zh = _site_title_zh(ln.site_key, ln.location_name)
        fg_color, bg_color = _prob_colors(a.probability)
        try:
            shape_zh = describe_shape_zh(a)
        except Exception:
            shape_zh = ""
        alert_imgs = images[idx - 1] if images and len(images) >= idx else []
        sim_html = ""
        if alert_imgs or shape_zh:
            figs = "".join(
                f'<div style="margin-top:8px;"><img src="cid:{html.escape(cid)}" width="600" '
                f'alt="T+{int(t // 60)}分{int(t % 60):02d}秒 成都视角火箭云模拟" '
                f'style="display:block;width:100%;max-width:600px;height:auto;border-radius:8px;border:1px solid #1f2937;">'
                f'<div style="font-size:12px;color:#6b7280;margin-top:4px;">'
                f'▲ 点火后 T+{int(t // 60)}分{int(t % 60):02d}秒（北京时间 '
                f'{(ln.net_utc + timedelta(seconds=t)).astimezone(CST).strftime("%H:%M:%S")}）成都视角模拟</div></div>'
                for cid, t, _ in alert_imgs
            )
            sim_html = (
                '<div style="margin-top:14px;padding:12px;background:#0b1220;border-radius:8px;color:#e5e7eb;">'
                '<div style="font-size:14px;font-weight:700;margin-bottom:6px;">🪼 火箭云形状模拟</div>'
                f'<div style="font-size:13px;line-height:1.6;color:#cbd5e1;">{html.escape(shape_zh)}</div>'
                f"{figs}"
                '<div style="font-size:11px;color:#94a3b8;margin-top:8px;line-height:1.5;">'
                "图例：横轴为成都观测方位角（S=正南），纵轴为仰角；白/蓝白色为受阳光照射的高空羽流，"
                "金/橙色为贴近地影边界的尾迹，暗灰为处于地影中的低空尾迹，红点为各分钟火箭位置，亮点及钟形光罩为二级箭体与膨胀羽流。"
                "基于典型弹道与几何光照推演的示意图，实际形态受具体弹道、发动机工况与高空风影响。</div></div>"
            )

        update_note_txt = ""
        update_banner_html = ""
        if alert.is_schedule_update and alert.previous_net_utc is not None:
            prev_cst = alert.previous_net_utc.astimezone(CST).strftime("%Y-%m-%d %H:%M")
            update_note_txt = f"  [发射窗口变更] 原计划北京时间 {prev_cst} 已更新为 {t0_cst.strftime('%Y-%m-%d %H:%M')}\n"
            update_banner_html = (
                f'<div style="background:#fef3c7;color:#92400e;padding:8px 12px;border-radius:6px;'
                f'margin-bottom:12px;font-size:13px;">⚠️ <strong>发射时间更新：</strong>'
                f"原定北京时间 {html.escape(prev_cst)} 已调整为最新窗口，请以新时间为准。</div>"
            )

        sources_str = " / ".join(ln.sources) if ln.sources else "Public Launch Feed"
        sun_margin_str = f"+{a.best_sample.sunlit_margin_deg:.1f}°" if a.best_sample else "-"
        plume_alt_str = f"{a.best_sample.altitude_km:.0f} km" if a.best_sample else "-"

        text_lines.extend(
            [
                f"【{idx}】{ln.name} — {a.probability.label_zh}",
                update_note_txt.rstrip() if update_note_txt else f"  时段类型：{a.twilight_phase_zh}",
                f"  发射阵地：{site_zh} ({ln.pad_name})",
                f"  点火时间：{t0_cst.strftime('%Y-%m-%d %H:%M:%S')} 北京时间 ({t0_utc.strftime('%H:%M')} UTC)",
                f"  最佳观测：{vis_start_cst.strftime('%H:%M')} ~ {vis_end_cst.strftime('%H:%M')} 北京时间 (T+{a.visible_start_sec}s ~ T+{a.visible_end_sec}s)",
                f"  成都方位：{a.azimuth_range_zh}",
                f"  最高仰角：{a.max_elevation_deg:.1f}°",
                f"  光学参数：成都太阳高度角 {a.chengdu_sun_alt_at_liftoff_deg:+.1f}° | 峰值羽流高度 {plume_alt_str} | 日照余量 {sun_margin_str}",
                f"  判定说明：{a.reason_zh}",
                f"  观测建议：{a.viewing_tips_zh}",
                f"  形状模拟：{shape_zh}" if shape_zh else "  形状模拟：-",
                f"  数据来源：{sources_str} ({ln.info_url})",
                "-" * 52,
            ]
        )

        cards_html.append(
            f"""
            <div style="border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin-bottom:20px;background:#ffffff;box-shadow:0 1px 3px rgba(0,0,0,0.04);">
              {update_banner_html}
              <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px;">
                <span style="display:inline-block;background:{bg_color};color:{fg_color};border:1px solid {fg_color};font-weight:700;font-size:13px;padding:4px 10px;border-radius:999px;">
                  {html.escape(a.probability.label_zh)}
                </span>
                <span style="font-size:13px;color:#6b7280;">{html.escape(a.twilight_phase_zh)}</span>
              </div>
              <h2 style="margin:0 0 12px 0;font-size:18px;color:#111827;">{html.escape(ln.name)}</h2>
              <table style="width:100%;border-collapse:collapse;font-size:14px;color:#374151;">
                <tr>
                  <td style="padding:6px 0;width:96px;color:#6b7280;">发射场工位</td>
                  <td style="padding:6px 0;font-weight:600;">{html.escape(site_zh)} · {html.escape(ln.pad_name)}</td>
                </tr>
                <tr>
                  <td style="padding:6px 0;color:#6b7280;">点火时间</td>
                  <td style="padding:6px 0;font-weight:700;color:#111827;">
                    {t0_cst.strftime('%Y-%m-%d %H:%M:%S')} 北京时间
                    <span style="font-weight:400;color:#6b7280;">({t0_utc.strftime('%Y-%m-%d %H:%M')} UTC)</span>
                  </td>
                </tr>
                <tr>
                  <td style="padding:6px 0;color:#6b7280;">最佳观测窗</td>
                  <td style="padding:6px 0;color:#b45309;font-weight:600;">
                    {vis_start_cst.strftime('%H:%M:%S')} – {vis_end_cst.strftime('%H:%M:%S')} 北京时间
                    <span style="font-weight:400;color:#6b7280;">(升空后 T+{a.visible_start_sec}s ~ T+{a.visible_end_sec}s)</span>
                  </td>
                </tr>
                <tr>
                  <td style="padding:6px 0;color:#6b7280;">成都视线方位</td>
                  <td style="padding:6px 0;font-weight:600;">{html.escape(a.azimuth_range_zh)}</td>
                </tr>
                <tr>
                  <td style="padding:6px 0;color:#6b7280;">成都最高仰角</td>
                  <td style="padding:6px 0;font-weight:600;">{a.max_elevation_deg:.1f}°（推演航向：{html.escape(a.trajectory_label_zh)}）</td>
                </tr>
                <tr>
                  <td style="padding:6px 0;color:#6b7280;">天体几何参数</td>
                  <td style="padding:6px 0;">成都太阳高度角 {a.chengdu_sun_alt_at_liftoff_deg:+.1f}° ｜ 峰值羽流高度 {plume_alt_str} ｜ 高空阳光余量 {sun_margin_str}</td>
                </tr>
              </table>
              <div style="margin-top:14px;padding:12px;background:#f9fafb;border-radius:8px;font-size:13px;line-height:1.6;color:#374151;">
                <div><strong>🔭 物理判定：</strong>{html.escape(a.reason_zh)}</div>
                <div style="margin-top:6px;"><strong>📸 成都观测建议：</strong>{html.escape(a.viewing_tips_zh)}</div>
              </div>
              {sim_html}
              <div style="margin-top:12px;font-size:12px;color:#9ca3af;">
                数据来源：{html.escape(sources_str)} ·
                <a href="{html.escape(ln.info_url)}" style="color:#2563eb;text-decoration:none;">查看发射详情 →</a>
              </div>
            </div>
            """
        )

    text_lines.append("提示：实际能否目击取决于当日成都及目标方位天空云量与大气通透度，出发前请结合卫星云图确认天气。")
    plain_body = "\n".join(text_lines)

    html_body = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px 12px;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;">
  <div style="max-width:660px;margin:0 auto;">
    <div style="background:linear-gradient(135deg,#0f172a,#1e3a8a);color:#ffffff;padding:22px 24px;border-radius:14px;margin-bottom:20px;">
      <div style="font-size:12px;letter-spacing:1px;opacity:0.8;text-transform:uppercase;">Chengdu Twilight Plume Alert</div>
      <h1 style="margin:6px 0 6px 0;font-size:21px;">🚀 成都区域“火箭云”观测预警</h1>
      <div style="font-size:13px;opacity:0.9;">基准坐标：成都市区（30.66°N, 104.06°E）｜ 综合 Next Spaceflight / Launch Library 2 等多源数据</div>
    </div>
    {''.join(cards_html)}
    <div style="text-align:center;font-size:12px;color:#6b7280;line-height:1.6;padding:8px 16px;">
      实际目击效果受四川盆地云层遮挡及能见度影响较大，建议在发射前 2 小时结合气象云图（如 Windy / 天文通）确认观测方位无低云遮挡。<br>
      由 GitHub Actions 自动巡检发送（同一发射窗口已自动去重）。
    </div>
  </div>
</body>
</html>"""

    return subject, plain_body, html_body


def build_mime_message(
    config: SMTPConfig,
    subject: str,
    plain_body: str,
    html_body: str,
    images: Optional[List[List[Tuple[str, float, bytes]]]] = None,
) -> MIMEMultipart:
    """Build multipart/related (alternative text+html, inline PNG simulations)."""
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain_body, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))

    flat = [img for group in (images or []) for img in group]
    if flat:
        msg = MIMEMultipart("related")
        msg.attach(alt)
        for cid, t, png in flat:
            part = MIMEImage(png, _subtype="png")
            part.add_header("Content-ID", f"<{cid}>")
            part.add_header("Content-Disposition", "inline", filename=f"rocket-cloud-T{int(t)}s.png")
            msg.attach(part)
    else:
        msg = alt

    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("成都火箭云监测助手", "utf-8")), config.sender))
    msg["To"] = ", ".join(config.recipients)
    msg["Date"] = formatdate(localtime=True)
    return msg


def send_email_notification(
    config: SMTPConfig,
    subject: str,
    plain_body: str,
    html_body: str,
    images: Optional[List[List[Tuple[str, float, bytes]]]] = None,
) -> None:
    """Send the alert via SMTP SSL (465) or STARTTLS (587)."""
    msg = build_mime_message(config, subject, plain_body, html_body, images)

    use_ssl = config.security == "ssl" or (config.security == "auto" and config.port == 465)
    context = ssl.create_default_context()

    if use_ssl:
        with smtplib.SMTP_SSL(config.host, config.port, context=context, timeout=25) as server:
            server.login(config.username, config.password)
            server.sendmail(config.sender, config.recipients, msg.as_string())
    else:
        with smtplib.SMTP(config.host, config.port, timeout=25) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(config.username, config.password)
            server.sendmail(config.sender, config.recipients, msg.as_string())
