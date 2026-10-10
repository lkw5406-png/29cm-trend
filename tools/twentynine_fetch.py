"""29CM 의류 랭킹 수집 Tool.

29CM 웹사이트가 베스트 화면을 그릴 때 쓰는 공개 데이터 주소(recommend-api.29cm.co.kr)에서
남성의류·여성의류 랭킹(일간·주간·월간 중 하나)을 받아 CSV로 저장한다.
저장 위치: 일간 data/history/, 주간 data/history_weekly/, 월간 data/history_monthly/ (common.history_path)

- 29CM은 카테고리마다 100위까지만 공개한다(다음 페이지 없음). 그래서 성별마다
  ① 의류 전체 TOP 100 ② 종류(상의·니트웨어·아우터·바지·스커트·원피스)별 TOP 100 을 받아 합친다
  (2026-10-11 사장님 결정: "전체 100 + 종류별 100").
- 랭킹 응답에 카테고리·가격·좋아요·후기 수가 들어 있어 의류 판별용 상세 요청은 필요 없다.
- 29CM은 랭킹 갱신 시각을 알려주지 않는다 → 받은 시각과 상위 10개 상품번호를 data/ranking_updates.json에
  남겨, 며칠 치를 비교해 갱신 시각을 추정한다.
- 봇 위장 없음: 정직한 User-Agent, 요청 사이 고정 간격.
- 29CM이 차단(403/429)하면 재시도하지 않고 BlockedError로 멈춘다.

CSV의 순위 세 가지
- rank          = 종류(mid_name) 안 순위 1~100. 모든 상품에 있음 → 순위 변화(급상승)는 이걸로 비교
- overall_rank  = 성별 의류 전체 TOP 100 순위. 100위 밖이면 빈칸
- clothing_rank = 분석 가중치용 합친 순위: 전체 TOP 100 먼저, 그 뒤는 종류 안 순위 순으로 번갈아

사용법: python tools/twentynine_fetch.py [--date YYYY-MM-DD] [--period daily|weekly|monthly]
"""
import argparse
import csv
import json
import sys
import time
import urllib.parse
from datetime import datetime

from common import (CLOTHING_CATEGORIES, GENDER_CATEGORY, IMAGE_BASE, KST, MID_CATEGORIES, PERIODS, PRODUCT_URL,
                    UPDATES_PATH, get_json, history_path, today_kst)

RANK_URL = "https://recommend-api.29cm.co.kr/api/v4/best/items"
REQUEST_INTERVAL_SEC = 4
PAGE_LIMIT = 100       # 29CM이 한 번에 주는 최대 개수 (더 달라고 해도 100개, 다음 페이지 없음 — 2026-10-11 확인)
MIN_OVERALL = 50       # 전체 TOP 100이 이보다 적으면 구조 변경을 의심하고 멈춤
MIN_PER_MID = 30       # 종류별 랭킹이 이보다 적으면 〃
GENDERS = {"M": "남성", "F": "여성"}

CSV_COLUMNS = [
    "date", "period", "gender_filter", "rank", "clothing_rank", "overall_rank", "category_code", "category_name",
    "mid_code", "mid_name", "item_type_raw",
    "product_id", "brand", "product_name", "final_price", "original_price",
    "discount_rate", "sales_label", "viewers", "flag", "sold_out", "hearts", "reviews", "review_point",
    "image_url", "product_url",
]


def build_url(category: int, period: str = "daily") -> str:
    params = {"categoryList": category, "periodSort": PERIODS[period][0], "limit": PAGE_LIMIT, "offset": 0}
    return RANK_URL + "?" + urllib.parse.urlencode(params)


def fetch_list(category: int, period: str) -> list[dict]:
    data = get_json(build_url(category, period))
    time.sleep(REQUEST_INTERVAL_SEC)
    if (data or {}).get("result") != "SUCCESS":
        raise RuntimeError(f"29CM 랭킹 응답이 이상함 (카테고리 {category}): {str(data)[:200]}")
    return (data.get("data") or {}).get("content") or []


def item_type_raw(item: dict, mid_code: int) -> str:
    """이 종류(중분류)에 해당하는 29CM 소분류 이름 (예: '긴소매 티셔츠'). 없으면 ''."""
    for c in item.get("frontCategoryInfo") or []:
        if c.get("category2Code") == mid_code:
            return c.get("category3Name") or ""
    return ""


def parse_product(item: dict) -> dict:
    pid = str(item.get("itemNo", ""))
    hearts, reviews = item.get("heartCount") or 0, item.get("reviewCount") or 0
    label = [f"좋아요 {hearts:,}"] if hearts else []
    if reviews:
        label.append(f"후기 {reviews:,}")
    image = item.get("imageUrl") or ""
    return {
        "product_id": pid,
        "brand": item.get("frontBrandNameKor") or item.get("frontBrandNameEng") or "",
        "product_name": item.get("itemName", ""),
        "final_price": item.get("lastSalePrice", ""),
        "original_price": item.get("consumerPrice", ""),
        "discount_rate": item.get("lastSalePercent", ""),
        "sales_label": " · ".join(label),
        "viewers": "",
        "flag": "NEW" if item.get("isNew") else "",
        "sold_out": bool(item.get("isSoldOut")),
        "hearts": hearts,
        "reviews": reviews,
        "review_point": item.get("reviewAveragePoint") or "",
        "image_url": (IMAGE_BASE + image) if image.startswith("/") else image,
        "product_url": PRODUCT_URL.format(id=pid),
    }


def record_update(period: str, date: str, gender: str, top_ids: list[str]) -> None:
    """받은 시각 + 전체 랭킹 상위 20개 상품번호. 같은 날 여러 번 받은 기록을 비교해 갱신 시각을 추정한다.
    (칸 이름은 처음 만든 대로 top10)"""
    log = json.loads(UPDATES_PATH.read_text(encoding="utf-8")) if UPDATES_PATH.exists() else {}
    entries = log.setdefault(period, {}).setdefault(date, {}).setdefault(gender, [])
    entries.append({"fetched_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "top10": ",".join(top_ids[:20])})
    UPDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPDATES_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def fetch_gender(gender: str, date: str, period: str = "daily") -> list[dict]:
    """성별 의류 전체 TOP 100 + 종류별 TOP 100 → 상품마다 한 줄 (같은 상품이 두 종류에 있으면 순위 높은 쪽)."""
    overall = fetch_list(GENDER_CATEGORY[gender], period)
    if len(overall) < MIN_OVERALL:
        raise RuntimeError(f"{GENDERS[gender]} 의류 전체 랭킹이 {len(overall)}개뿐 (기준 {MIN_OVERALL}개) — 구조 변경 의심")
    overall_rank = {str(x.get("itemNo")): i for i, x in enumerate(overall, 1)}
    record_update(period, date, gender, [str(x.get("itemNo")) for x in overall])

    best: dict[str, dict] = {}   # 상품번호 → 줄
    counts = []
    mids = [(code, name, big) for code, (g, name, big) in MID_CATEGORIES.items() if g == gender]
    for order, (mid_code, mid_name, big) in enumerate(mids):
        items = fetch_list(mid_code, period)
        if len(items) < MIN_PER_MID:
            raise RuntimeError(f"{GENDERS[gender]} {mid_name} 랭킹이 {len(items)}개뿐 (기준 {MIN_PER_MID}개) — 구조 변경 의심")
        counts.append(f"{mid_name} {len(items)}")
        for rank, item in enumerate(items, 1):
            p = parse_product(item)
            row = {"date": date, "period": PERIODS[period][0], "gender_filter": gender, "rank": rank,
                   "overall_rank": overall_rank.get(p["product_id"], ""), "category_code": big,
                   "category_name": CLOTHING_CATEGORIES[big], "mid_code": mid_code, "mid_name": mid_name,
                   "item_type_raw": item_type_raw(item, mid_code), "_order": order, **p}
            old = best.get(p["product_id"])
            if old is None or rank < old["rank"]:
                best[p["product_id"]] = row

    # 합친 순위: 전체 TOP 100에 든 상품 먼저(그 순위대로), 나머지는 종류 안 순위 순 (같은 순위면 종류 순서)
    rows = sorted(best.values(), key=lambda r: (0, r["overall_rank"], 0) if r["overall_rank"] else (1, r["rank"], r["_order"]))
    for i, r in enumerate(rows, 1):
        r["clothing_rank"] = i
        del r["_order"]
    in_overall = sum(1 for r in rows if r["overall_rank"])
    print(f"  {GENDERS[gender]}: {len(rows)}개 (전체 TOP 100 중 {in_overall}개 · {', '.join(counts)})", flush=True)
    return rows


def collect(date: str, period: str = "daily") -> list[dict]:
    rows = []
    for gender in GENDERS:
        rows += fetch_gender(gender, date, period)
    return rows


def validate(rows: list[dict]) -> None:
    """29CM 데이터 구조가 바뀌어 조용히 빈 결과가 나오는 걸 막는다."""
    for g, name in GENDERS.items():
        mine = [r for r in rows if r["gender_filter"] == g]
        need = MIN_PER_MID * sum(1 for v in MID_CATEGORIES.values() if v[0] == g)
        empty = sum(1 for r in mine if not r["product_id"] or not r["product_name"] or not r["image_url"])
        if len(mine) < need or empty:
            raise RuntimeError(
                f"{name} 수집 결과가 비정상 ({len(mine)}개, 기준 {need}개 · 빈 칸 있는 줄 {empty}개). "
                "29CM 데이터 구조가 바뀌었을 수 있음 — workflows/29cm_daily_report.md의 '구조 변경' 항목 참고."
            )


def save_csv(rows: list[dict], date: str, period: str = "daily"):
    path = history_path(date, period)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def probe_update(date: str, periods: tuple[str, ...] = ("daily",)) -> None:
    """갱신 시각 관찰용: 전체 랭킹 상위만 받아 기록을 남긴다 (기간 × 성별마다 요청 1번, CSV는 건드리지 않음)."""
    for period in periods:
        for gender in GENDERS:
            overall = fetch_list(GENDER_CATEGORY[gender], period)
            record_update(period, date, gender, [str(x.get("itemNo")) for x in overall])


def update_report() -> None:
    """ranking_updates.json을 읽어 상위 20개가 바뀐 시각을 보여 준다 (갱신 시각 추정)."""
    log = json.loads(UPDATES_PATH.read_text(encoding="utf-8")) if UPDATES_PATH.exists() else {}
    for period, days in log.items():
        for gender in GENDERS:
            entries = sorted((e for day in days.values() for e in day.get(gender, [])), key=lambda e: e["fetched_at"])
            print(f"[{PERIODS[period][1]} · {GENDERS[gender]}] 기록 {len(entries)}번")
            for prev, cur in zip(entries, entries[1:]):
                a, b = prev["top10"].split(","), cur["top10"].split(",")
                n = min(len(a), len(b))
                moved = sum(1 for x, y in zip(a[:n], b[:n]) if x != y)
                if moved:
                    print(f"  {prev['fetched_at']} → {cur['fetched_at']}: 상위 {n}개 중 {moved}자리 바뀜")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=today_kst())
    parser.add_argument("--period", choices=list(PERIODS), default="daily")
    parser.add_argument("--probe", action="store_true", help="갱신 시각 관찰: 세 기간 상위만 기록 (요청 6번)")
    parser.add_argument("--updates", action="store_true", help="관찰 기록에서 순위가 바뀐 시각 보기 (요청 없음)")
    args = parser.parse_args()
    if args.updates:
        update_report()
        return 0
    if args.probe:
        probe_update(args.date, tuple(PERIODS))
        print("갱신 시각 관찰 기록 완료")
        return 0

    print(f"29CM 랭킹 수집 시작 ({args.date}, {PERIODS[args.period][2]} · 성별 의류 전체 TOP 100 + 종류별 TOP 100)")
    rows = collect(args.date, args.period)
    validate(rows)
    path = save_csv(rows, args.date, args.period)
    print(f"저장 완료: {path} ({len(rows)}행)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
