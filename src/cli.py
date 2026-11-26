"""Command-line entry point for the Chengdu Rocket Cloud Monitor."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from .astronomy import KNOWN_SPACEPORTS, evaluate_chengdu_visibility, solar_alt_az
from .models import LaunchEvent, ProbabilityLevel, VisibilityAssessment
from .notifier import CST, SMTPConfig, build_email_content, send_email_notification
from .sources import fetch_all_upcoming_launches
from .state import NotificationStateStore, PendingAlert

DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "notified_state.json"


def _create_simulated_xichang_launch() -> LaunchEvent:
    """Create a realistic dawn twilight launch from Xichang tomorrow morning for end-to-end testing."""
    now_cst = datetime.now(CST)
    target_date = (now_cst + timedelta(days=1)).date()
    # Find timestamp tomorrow morning around 05:30 - 07:30 CST where Chengdu Sun altitude is ~ -10 deg
    best_dt_utc = datetime(target_date.year, target_date.month, target_date.day, 6, 45, tzinfo=CST).astimezone(
        timezone.utc
    )
    best_diff = 999.0
    for minute_of_day in range(4 * 60 + 30, 8 * 60 + 15, 5):
        cand_cst = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            minute_of_day // 60,
            minute_of_day % 60,
            tzinfo=CST,
        )
        cand_utc = cand_cst.astimezone(timezone.utc)
        alt, _ = solar_alt_az(cand_utc, 30.6586, 104.0648)
        diff = abs(alt - (-10.5))
        if diff < best_diff:
            best_diff = diff
            best_dt_utc = cand_utc

    sp = KNOWN_SPACEPORTS["xichang"]
    return LaunchEvent(
        uid="sim:xichang-twilight-demo",
        name="[模拟演练] Long March 3B/E | 测试卫星（成都火箭云通道验证）",
        rocket_name="Long March 3B/E",
        mission_name="测试卫星（邮件通道演练）",
        provider="CASC",
        pad_name="LC-2",
        location_name="Xichang Satellite Launch Center, Sichuan, China",
        country_code="CHN",
        latitude=sp.lat,
        longitude=sp.lon,
        net_utc=best_dt_utc,
        status_abbrev="Go",
        status_name="Go for Launch (Simulated)",
        time_precision="MIN",
        orbit_name="Geostationary Transfer Orbit",
        orbit_abbrev="GTO",
        info_url="https://nextspaceflight.com/launches/",
        sources=["Simulated Test"],
        site_key="xichang",
    )


def _write_github_output(output_path: Optional[str], notified_count: int, state_changed: bool) -> None:
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as fh:
        fh.write(f"notified_count={notified_count}\n")
        fh.write(f"state_changed={'true' if state_changed else 'false'}\n")


def _print_summary_table(assessments: List[VisibilityAssessment]) -> None:
    if not assessments:
        print("当前巡检周期内未检索到中国及周边区域近期发射记录。")
        return

    print(f"\n共评估 {len(assessments)} 条中国及周边区域近期发射计划：")
    print("-" * 104)
    for idx, a in enumerate(assessments, start=1):
        ln = a.launch
        t_cst = ln.net_utc.astimezone(CST).strftime("%Y-%m-%d %H:%M")
        prec_tag = "" if ln.has_precise_time else " [时间待定TBD]"
        srcs = "+".join(ln.sources)
        print(
            f"[{idx:02d}] {t_cst} CST{prec_tag} | {a.probability.badge_zh:<4} | "
            f"成都太阳 {a.chengdu_sun_alt_at_liftoff_deg:+5.1f}° | 最高仰角 {a.max_elevation_deg:4.1f}° | "
            f"{ln.name} ({ln.location_name}) [{srcs}]"
        )
        print(f"     └─ {a.reason_zh}")
    print("-" * 104)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="成都区域火箭云（暮光水母云）多源监测与邮件预警程序")
    parser.add_argument(
        "--days-ahead",
        type=int,
        default=int(os.environ.get("DAYS_AHEAD", "21")),
        help="向前巡检的天数范围（默认 21 天）",
    )
    parser.add_argument(
        "--min-probability",
        type=str,
        default=os.environ.get("MIN_PROBABILITY", "LOW"),
        choices=["LOW", "MEDIUM", "HIGH", "low", "medium", "high"],
        help="触发邮件通知的最低可见概率阈值（默认 LOW，只要存在目击可能即通知）",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help="已通知发射去重状态 JSON 路径",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅抓取评估并打印邮件预览，不发送真实邮件且不修改去重状态文件",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="注入一条西昌清晨曙光模拟发射记录（用于测试天体几何计算及邮箱配置是否畅通）",
    )
    parser.add_argument(
        "--force-notify",
        action="store_true",
        help="忽略本地去重缓存（常与 --simulate 配合测试发信）",
    )
    parser.add_argument(
        "--github-output",
        type=str,
        default=os.environ.get("GITHUB_OUTPUT", ""),
        help="GitHub Actions 输出文件路径",
    )
    parser.add_argument("--verbose", action="store_true", help="输出详细调试日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    min_prob = ProbabilityLevel.from_str(args.min_probability)

    if args.simulate:
        logging.info("已启用 --simulate 模式：生成西昌曙光火箭云模拟发射事件...")
        launches = [_create_simulated_xichang_launch()]
    else:
        launches = fetch_all_upcoming_launches(days_ahead=args.days_ahead)

    assessments = [evaluate_chengdu_visibility(ln) for ln in launches]
    _print_summary_table(assessments)

    qualifying = [a for a in assessments if a.probability >= min_prob]
    store = NotificationStateStore(args.state_file)

    if args.force_notify or args.simulate:
        pending_alerts = [PendingAlert(assessment=a, is_schedule_update=False) for a in qualifying]
    else:
        pending_alerts = store.filter_unsent(qualifying)

    if not pending_alerts:
        if qualifying:
            logging.info("共有 %d 条潜在火箭云发射，但此前均已发送过通知（自动去重跳过）。", len(qualifying))
        else:
            logging.info("暂无满足成都火箭云可见条件的新发射窗口。")
        _write_github_output(args.github_output, notified_count=0, state_changed=False)
        return 0

    subject, plain_body, html_body = build_email_content(pending_alerts)
    print("\n================ 待发送邮件预览 ================")
    print(f"主题: {subject}\n")
    print(plain_body)
    print("================================================\n")

    if args.dry_run:
        logging.info("--dry-run 模式：跳过 SMTP 发送与状态写入。")
        _write_github_output(args.github_output, notified_count=0, state_changed=False)
        return 0

    smtp_cfg = SMTPConfig.from_env()
    send_email_notification(smtp_cfg, subject, plain_body, html_body)
    logging.info("邮件预警发送成功！收件人: %s", ", ".join(smtp_cfg.recipients))

    state_changed = False
    if not args.simulate:
        state_changed = store.mark_notified(pending_alerts)
        logging.info("去重状态文件已更新 (%s, changed=%s)", args.state_file, state_changed)

    _write_github_output(args.github_output, notified_count=len(pending_alerts), state_changed=state_changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
