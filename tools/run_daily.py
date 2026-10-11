"""매일 실행 Tool — 랭킹 수집(일간·주간·월간) → 상세정보 → 후기 요약 → 분석 → 리포트.

수집은 매일 세 기간 모두 한다 (29CM은 지난 랭킹을 다시 볼 수 없음).
리포트는 일간은 매일, 주간은 월요일, 월간은 1일에 만든다 (한국 날짜 기준, 무신사 리포트와 같게).

사용법:
  python tools/run_daily.py                  전체 실행
  python tools/run_daily.py --only-missing   재확인: 오늘 수집이 빠진 기간만 받고, 다 있으면 바로 끝
  python tools/run_daily.py --skip-fetch     이미 받은 오늘 랭킹으로 (상세정보 → 분석 → 리포트)
  python tools/run_daily.py --report weekly  리포트 날이 아니어도 해당 기간 리포트를 만듦 (여러 번 가능)

리포트는 기간마다 고정 주소 하나에 날짜별로 쌓이므로 매일 링크를 보내지 않는다.
"""
import argparse
import json
import sys
import traceback
from datetime import date as Date

import analyze_trends
import build_report
import product_details
import review_summary
import twentynine_fetch
from common import DETAILS_ENABLED, PERIODS, REVIEWS_ENABLED, TMP_DIR, BlockedError, history_path, today_kst


def report_periods(date: str, extra: list[str] | None = None) -> list[str]:
    """오늘 리포트를 만들 기간: 일간은 매일, 주간은 월요일, 월간은 1일."""
    d = Date.fromisoformat(date)
    out = ["daily"]
    if d.weekday() == 0:
        out.append("weekly")
    if d.day == 1:
        out.append("monthly")
    return out + [p for p in extra or [] if p not in out]


def collect_missing(date: str, only_missing: bool) -> tuple[list[str], list[str]]:
    """세 기간 랭킹을 받는다. 한 기간이 실패해도 나머지는 계속 (차단이면 즉시 멈춤).
    (새로 받은 기간, 실패한 기간)을 돌려준다."""
    fetched, failed = [], []
    for period in PERIODS:
        if only_missing and history_path(date, period).exists():
            continue
        print(f"랭킹 수집 ({PERIODS[period][1]} · {PERIODS[period][2]})", flush=True)
        try:
            rows = twentynine_fetch.collect(date, period)
            twentynine_fetch.validate(rows)
            twentynine_fetch.save_csv(rows, date, period)
            fetched.append(period)
        except BlockedError:
            raise
        except Exception:
            traceback.print_exc()
            failed.append(period)
    return fetched, failed


def build_period(date: str, period: str) -> None:
    TMP_DIR.mkdir(exist_ok=True)
    result = analyze_trends.analyze(date, period)
    analyze_trends.analysis_path(date, period).write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                                          encoding="utf-8")
    build_report.build(date, period)


def run(date: str, skip_fetch: bool, only_missing: bool, extra_reports: list[str]) -> list[str]:
    """실패한 기간 목록을 돌려준다 (없으면 [])."""
    failed: list[str] = []
    if not skip_fetch:
        print("1/4 랭킹 수집 (일간·주간·월간)")
        fetched, failed = collect_missing(date, only_missing)
        if only_missing and not fetched and not failed:
            print("오늘 수집은 이미 다 되어 있어요 — 갱신 시각 추정용 기록만 남기고 끝")
            twentynine_fetch.probe_update(date)
            return []
    collected = [p for p in PERIODS if history_path(date, p).exists()]
    periods = [p for p in report_periods(date, extra_reports) if p in collected]
    # 상세정보·후기는 보조 자료: 29CM이 거부해도 랭킹 분석·리포트는 만든다 (거부는 failed에 남겨 실패로 알림)
    try:
        if DETAILS_ENABLED:
            print("2/4 상세정보 수집 (처음 보는 상품만)")
            budget = product_details.DAILY_LIMIT  # 세 기간을 합친 하루 한도 (0 = 제한 없음)
            for period in collected:
                if not product_details.DAILY_LIMIT:
                    product_details.update_details(date, period=period)
                elif budget > 0:
                    budget -= product_details.update_details(date, limit=budget, period=period)
        else:
            print("2/4 상세정보 수집 — 꺼져 있음 (common.DETAILS_ENABLED)")
        if REVIEWS_ENABLED:
            print("2-2/4 후기 요약 (리포트 만드는 기간의 종류별 인기 TOP 30, 7일에 한 번)")
            review_summary.update_summaries(date, periods=tuple(periods))
        else:
            print("2-2/4 후기 요약 — 꺼져 있음 (common.REVIEWS_ENABLED)")
    except BlockedError as exc:
        print(f"29CM이 상품 단위 요청을 거부함 — 재시도하지 않고 이 단계만 멈춤: {exc}")
        blocked = True
    else:
        blocked = False
    print("3/4 분석 · 4/4 리포트:", ", ".join(PERIODS[p][1] for p in periods))
    for period in periods:
        build_period(date, period)
    if blocked:
        raise BlockedError("상세정보·후기 요청이 거부됨 (랭킹·리포트는 만들었음)")
    return failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=today_kst())
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--only-missing", action="store_true")
    parser.add_argument("--report", choices=list(PERIODS), action="append", default=[])
    args = parser.parse_args()

    try:
        failed = run(args.date, args.skip_fetch, args.only_missing, args.report)
        if failed:
            raise RuntimeError("랭킹 수집 실패: " + ", ".join(PERIODS[p][1] for p in failed)
                               + " — 재확인 실행(낮 12시·밤 8시)이 다시 받아요")
    except Exception as exc:
        traceback.print_exc()
        if isinstance(exc, BlockedError):
            print("29CM이 데이터 요청을 거부했습니다. 억지로 다시 시도하지 않고 멈췄습니다. 사장님께 보고 후 방향 결정.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
