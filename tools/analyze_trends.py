"""랭킹 트렌드 분석 Tool.

입력: 기간별 랭킹 CSV (일간 data/history/, 주간 data/history_weekly/, 월간 data/history_monthly/),
      data/product_details.json (product_details.py)
출력: .tmp/analysis_YYYY-MM-DD.json (일간) / .tmp/analysis_weekly_YYYY-MM-DD.json 등 (build_report.py가 읽음)

분석 기준
- 성별: 29CM 남성의류 / 여성의류 랭킹 그대로. (두 랭킹에 모두 오른 상품은 양쪽에 다 반영)
- 대상: 성별 의류 전체 TOP 100 + 종류(상의·니트웨어·아우터·바지·스커트·원피스)별 TOP 100 을 합친 것.
- 비중(share): 합친 순위(clothing_rank)가 높을수록 큰 가중치(1위=1.0, 꼴찌≈0)로 속성별 비중을 계산.
- 순위 변화(급상승): 같은 종류 안 순위(rank, 1~100)끼리 비교.
- 추세: 일간은 어제 대비·최근 7일 평균 대비, 주간은 7일 전(지난주) 대비, 월간은 한 달 전(지난달) 대비(%p).
  비교할 기록이 없으면 추세 없이 '많이 팔리는 속성'만 보여준다.

사용법: python tools/analyze_trends.py [--date YYYY-MM-DD] [--period daily|weekly|monthly]
"""
import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import product_details
import review_summary
from classify_attributes import GROUP_LABELS, GROUPS, classify
from common import DETAILS_PATH, MID_ORDER, PERIODS, RISE_REASONS_PATH, TMP_DIR, history_path, today_kst

MAX_RANK = 100  # 종류 안 순위의 끝 (twentynine_fetch.PAGE_LIMIT과 같게)
GENDERS = {"M": "남성", "F": "여성"}
HEADLINE_GROUPS = ["item_type", "fit", "silhouette", "fiber", "color"]
MIN_COUNT = 5          # 이보다 적게 등장한 속성은 우연일 수 있어 순위표에서 뺌
TREND_WINDOW_DAYS = 7
MIN_WEEK_DAYS = 3      # 7일 평균 비교는 지난 기록이 3일 이상일 때부터
RECOMMEND_PER_GENDER = 5
# 오늘 요약 (2026-09-27 사장님 요청: 전 카테고리 합계 대신 상품·브랜드·아이템 단위 정보)
IMG_DATE = re.compile(r"/item/(\d{4})(\d{2})/")  # 대표 사진 폴더 = 사진을 올린 연·월 (상세정보의 판매 시작일이 없을 때만)
# 이 기간 안에 판매를 시작한 상품 = 신상 (무신사 리포트와 같은 45일)
FRESH_DAYS = {"daily": 45, "weekly": 45, "monthly": 60}
FRESH_TOP = 100          # 신상은 합친 순위 100위 안(대부분 의류 전체 TOP 100)에 새로 들었거나
FRESH_JUMP = 10          # 종류 안 순위가 이만큼 이상 오른 것만
BRAND_MIN_GAIN = 2       # 랭킹 안 상품 수가 이만큼 이상 늘어난 브랜드 = 뜨는 브랜드
SPEC_MIN_ITEMS = 5       # 아이템 안 스펙 변화는 그 아이템 상품이 양쪽 날 모두 이만큼 이상일 때만 (적으면 우연)
SPEC_MIN_PP = 3.0        # 이 이상 늘어난 스펙만


def load_day(date: str, period: str = "daily") -> list[dict] | None:
    path = history_path(date, period)
    if not path.exists():
        return None
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_details() -> dict:
    return json.loads(DETAILS_PATH.read_text(encoding="utf-8")) if DETAILS_PATH.exists() else {}


def build_products(rows: list[dict], details: dict) -> dict[str, list[dict]]:
    """{성별: [상품...]}. 상품 하나 = 성별 랭킹·카테고리별 한 줄."""
    out: dict[str, list[dict]] = {g: [] for g in GENDERS.values()}
    for r in rows:
        gender = GENDERS.get(r["gender_filter"])
        if not gender:
            continue
        p = {k: r[k] for k in ("product_id", "brand", "product_name", "category_code", "category_name",
                               "final_price", "original_price", "discount_rate", "sales_label",
                               "image_url", "product_url")}
        p["rank"] = int(r["rank"])  # 종류(mid_name) 안 순위 1~100
        p["clothing_rank"] = int(r.get("clothing_rank") or r["rank"])  # 합친 순위 (전체 TOP 100 먼저)
        p["overall_rank"] = int(r["overall_rank"]) if r.get("overall_rank") else None  # 의류 전체 TOP 100 순위
        p["mid_name"] = r.get("mid_name") or r["category_name"]
        # 아이템 종류는 랭킹에 적힌 29CM 소분류가 기준 (상세정보가 없어도 알 수 있음)
        detail = dict(details.get(r["product_id"]) or {})
        detail.pop("missing", None)
        if r.get("item_type_raw"):
            detail["category2"] = product_details.item_type_name(p["mid_name"], r["item_type_raw"])
            detail["category2_code"] = r["category_code"]
        p["registered"] = detail.get("registered") or image_date(r["image_url"])
        p.update(classify(r["product_name"], r["category_code"], r["category_name"], detail, r["product_id"]))
        out[gender].append(p)
    for products in out.values():
        products.sort(key=lambda x: x["clothing_rank"])
        for x in products:
            x["weight"] = (len(products) + 1 - x["clothing_rank"]) / len(products)
    return out


def attr_values(p: dict, group: str) -> list[str]:
    v = p[group]
    return [v] if isinstance(v, str) else v


def shares(products: list[dict], group: str) -> dict[str, tuple[float, int]]:
    """{속성: (가중 비중 %, 등장 상품 수)}. 분모는 이 속성이 파악된 상품만 (모르는 상품은 빼고 비교)."""
    known = [p for p in products if attr_values(p, group)]
    total = sum(p["weight"] for p in known) or 1.0
    weight_sum: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    for p in known:
        for v in attr_values(p, group):
            weight_sum[v] += p["weight"]
            count[v] += 1
    return {v: (100 * weight_sum[v] / total, count[v]) for v in weight_sum}


def previous_dates(date: str, days: int) -> list[str]:
    d = datetime.strptime(date, "%Y-%m-%d")
    return [(d - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, days + 1)]


def compare_date(date: str, period: str) -> str:
    """비교할 이전 기록 날짜: 일간 어제, 주간 7일 전, 월간 한 달 전 같은 날 (없는 날이면 그 달 말일)."""
    d = datetime.strptime(date, "%Y-%m-%d")
    if period == "daily":
        return (d - timedelta(days=1)).strftime("%Y-%m-%d")
    if period == "weekly":
        return (d - timedelta(days=7)).strftime("%Y-%m-%d")
    first = d.replace(day=1)
    prev_last = first - timedelta(days=1)
    return prev_last.replace(day=min(d.day, prev_last.day)).strftime("%Y-%m-%d")


def analysis_path(date: str, period: str = "daily"):
    return TMP_DIR / (f"analysis_{date}.json" if period == "daily" else f"analysis_{period}_{date}.json")


def data_scope(rows: list[dict]) -> str:
    """수집 방식 이름. 방식이 바뀌면 이름을 바꿔 서로 다른 방식의 날끼리 비교하지 않게 한다."""
    return "overall" if rows and "overall_rank" in rows[0] else "category"


def image_date(url: str) -> str | None:
    """사진 폴더의 연·월 'YYYY-MM' (대략의 등록 시점. 날짜까지는 알 수 없음)."""
    m = IMG_DATE.search(url or "")
    return f"{m.group(1)}-{m.group(2)}" if m else None


def product_brief(p: dict) -> dict:
    return {k: p[k] for k in ("product_id", "brand", "product_name", "category_name", "mid_name", "item_type", "rank",
                              "clothing_rank", "overall_rank", "final_price", "discount_rate", "sales_label",
                              "image_url", "product_url")} | {
        "attrs": [a for g in ("fit", "silhouette", "fiber") for a in attr_values(p, g)]
                 + attr_values(p, "color")[:3]}  # 여러 색 상품은 카드가 길어지지 않게 3색까지


REVIEW_TOP_N = review_summary.TOP_N  # 후기 요약은 종류별 인기 상위만
CATEGORY_ORDER = ["001", "002", "003", "100"]  # 상의, 아우터, 바지, 원피스/스커트
ITEM_PROFILE_GROUPS = ["silhouette", "texture", "fit", "fiber", "color", "detail"]  # 사장님이 정한 순서
SILHOUETTE_PROFILE_CODES = {"003"}  # 실루엣·기장은 팬츠류만 보여줌 (2026-09-25 사장님 결정)
ITEM_PROFILE_MIN = 3        # 이보다 적은 아이템 종류는 따로 정리하지 않음
PRICE_BANDS = [(0, 30000, "3만원 미만"), (30000, 50000, "3~5만원"), (50000, 100000, "5~10만원"),
               (100000, 200000, "10~20만원"), (200000, float("inf"), "20만원 이상")]


def _weight_share(part: list[dict], whole: list[dict]) -> float:
    return 100 * sum(p["weight"] for p in part) / (sum(p["weight"] for p in whole) or 1.0)


def _price(p: dict) -> int | None:
    try:
        return int(float(p["final_price"]))
    except (TypeError, ValueError):
        return None


def _quartiles(values: list[int]) -> tuple[int, int] | None:
    """가운데 50%가 들어가는 가격 범위 (아래 1/4 지점 ~ 위 1/4 지점)."""
    values = sorted(values)
    if len(values) < 4:
        return None
    return values[len(values) // 4], values[(3 * len(values) - 1) // 4]


def _median(values: list[int]) -> int | None:
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) // 2


def top_by_category(products: list[dict], n: int | None = None, reviews: dict | None = None) -> list[dict]:
    """인기 TOP: 맨 앞 '전체'(의류 전체 TOP 100) + 29CM 종류(상의·니트웨어·아우터·바지·스커트·원피스)별 순위.
    후기 요약(review_summary.py)은 종류별 상위 REVIEW_TOP_N 상품에만 붙임
    — 후기를 모으는 대상이 그 상품들이라, 그 밖의 상품에 붙은 예전 요약은 오래된 것일 수 있어서."""
    reviews = reviews or {}
    groups = [(name, sorted((p for p in products if p["mid_name"] == name), key=lambda p: p["rank"])) for name in MID_ORDER]
    groups = [(name, items) for name, items in groups if items]
    targets = {p["product_id"] for _, items in groups for p in items[:REVIEW_TOP_N]}
    overall = sorted((p for p in products if p["overall_rank"]), key=lambda p: p["overall_rank"])
    out = []
    for code, name, items in [("all", "전체", overall)] + [(f"mid{i}", name, items) for i, (name, items) in enumerate(groups)]:
        if not items:
            continue
        briefs = [product_brief(p) for p in items[:n]]
        for b in briefs:
            if b["product_id"] in targets and b["product_id"] in reviews:
                b["review"] = reviews[b["product_id"]]
        out.append({"code": code, "name": name, "count": len(items), "products": briefs})
    return out


def spec_changes(items: list[dict], prev: list[dict]) -> list[dict]:
    """한 아이템 안에서 이전 기록보다 비중이 늘어난 스펙(핏·원단·컬러 등) — 많이 늘어난 순 3개."""
    if len(items) < SPEC_MIN_ITEMS or len(prev) < SPEC_MIN_ITEMS:
        return []
    out = []
    for group in ITEM_PROFILE_GROUPS:
        if group == "silhouette" and items[0]["category_code"] not in SILHOUETTE_PROFILE_CODES:
            continue
        s_prev = shares(prev, group)
        for v, (pct, cnt) in shares(items, group).items():
            gain = pct - s_prev.get(v, (0.0, 0))[0]
            if cnt >= 2 and gain >= SPEC_MIN_PP and not v.startswith("기타"):
                out.append({"group": group, "name": v, "pct": round(pct, 1), "gain": round(gain, 1)})
    return sorted(out, key=lambda x: -x["gain"])[:3]


def fresh_entries(today: list[dict], yesterday: list[dict] | None, date: str, days: int) -> list[dict]:
    """판매 시작 N일 안의 신상 중 합친 순위 100위 안에 새로 들었거나 종류 안에서 10계단 이상 오른 상품
    (이전 기록이 없으면 100위 안 신상 전부)."""
    since = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    prev = {p["product_id"]: p for p in yesterday or []}
    out, seen = [], set()
    for p in today:
        reg = p.get("registered")
        old = prev.get(p["product_id"])
        same_list = old is not None and old["mid_name"] == p["mid_name"]
        risen = (not yesterday or old is None or old["clothing_rank"] > FRESH_TOP
                 or (same_list and old["rank"] - p["rank"] >= FRESH_JUMP))
        if p["clothing_rank"] > FRESH_TOP or not reg or reg < since or p["product_id"] in seen or not risen:
            continue
        seen.add(p["product_id"])
        out.append(product_brief(p) | {"registered": reg, "prev_rank": old["rank"] if same_list else None})
    return out


def rising_brands(today: list[dict], yesterday: list[dict] | None, movers: list[dict]) -> list[dict]:
    """랭킹 안 상품 수가 이전 기록보다 늘어난 브랜드 (많이 늘어난 순, 같으면 크게 오른 상품 수 순)."""
    if not yesterday:
        return []
    now = defaultdict(set)
    before = defaultdict(set)
    for p in today:
        now[p["brand"]].add(p["product_id"])
    for p in yesterday:
        before[p["brand"]].add(p["product_id"])
    jumped = defaultdict(int)
    for m in movers:
        jumped[m["brand"]] += 1
    rows = [{"brand": b, "count": len(ids), "gain": len(ids) - len(before[b]), "movers": jumped[b],
             "best": min((p for p in today if p["brand"] == b), key=lambda p: p["clothing_rank"])["product_name"]}
            for b, ids in now.items() if len(ids) - len(before[b]) >= BRAND_MIN_GAIN]
    return sorted(rows, key=lambda r: (-r["gain"], -r["movers"], -r["count"]))[:3]


def item_type_profiles(today: list[dict], yesterday: list[dict] | None) -> list[dict]:
    """아이템 종류(긴소매 티셔츠, 데님 팬츠 …)마다 실루엣·원단·핏·소재·컬러·디테일 구성을 정리."""
    by_type: dict[str, list[dict]] = defaultdict(list)
    for p in today:
        by_type[p["item_type"]].append(p)
    prev_share = {}
    prev_by_type: dict[str, list[dict]] = defaultdict(list)
    if yesterday:
        for p in yesterday:
            prev_by_type[p["item_type"]].append(p)
        prev_share = {t: _weight_share(ps, yesterday) for t, ps in prev_by_type.items()}

    profiles = []
    for item_type, items in by_type.items():
        if len(items) < ITEM_PROFILE_MIN:
            continue
        share = _weight_share(items, today)
        groups = {}
        for group in ITEM_PROFILE_GROUPS:
            if group == "silhouette" and items[0]["category_code"] not in SILHOUETTE_PROFILE_CODES:
                continue
            s = shares(items, group)
            known = sum(1 for p in items if attr_values(p, group))
            values = sorted(s.items(), key=lambda kv: -kv[1][0])[:4]
            groups[group] = {"coverage": round(100 * known / len(items)),
                             "values": [{"name": v, "pct": round(pct, 1), "count": c} for v, (pct, c) in values]}
        prices = [x for x in (_price(p) for p in items) if x]
        originals = [x for x in (_price({"final_price": p.get("original_price")}) for p in items) if x]
        discounts = [float(p["discount_rate"]) for p in items if str(p.get("discount_rate") or "").replace(".", "", 1).isdigit()]
        q = _quartiles(prices)
        profiles.append({
            "name": item_type,
            "category_code": items[0]["category_code"],
            "category_name": items[0]["category_name"],
            "count": len(items),
            "share": round(share, 1),
            "dod_pp": round(share - prev_share.get(item_type, 0.0), 1) if yesterday else None,
            "median_price": _median(prices),
            "median_original": _median(originals),  # 정가(할인 전 원상품가)
            "discount_avg": round(sum(discounts) / len(discounts), 1) if discounts else None,
            "price_low": q[0] if q else None,
            "price_high": q[1] if q else None,
            "best_rank": min(p["rank"] for p in items),
            "groups": groups,
            "spec_changes": spec_changes(items, prev_by_type.get(item_type, [])),
            "top_products": [product_brief(p) for p in items[:6]],  # 디자인 참고 탭의 대표 사진
        })
    profiles.sort(key=lambda x: (CATEGORY_ORDER.index(x["category_code"]) if x["category_code"] in CATEGORY_ORDER else 9,
                                 -x["share"]))
    return profiles


# 대분류: 디자인 참고·소재컬러 탭에서 아우터/상의/하의로 나눠 보기 (2026-09-25 사장님 요청)
# 하의 = 바지 + 스커트. 원피스는 어디에도 넣지 않음.
BIG_CATEGORIES = [("아우터", "002"), ("상의", "001"), ("하의", "003")]
BIG_CATEGORY_GROUPS = ["fit", "silhouette", "detail", "color"]


def big_category(p: dict) -> str | None:
    if p["category_code"] == "100":
        return "하의" if "스커트" in p["item_type"] else None
    return next((name for name, code in BIG_CATEGORIES if code == p["category_code"]), None)


def attr_table(today: list[dict], yesterday: list[dict] | None, group: str, min_count: int) -> dict:
    """속성 순위표 한 개 (전체 속성 순위와 같은 모양, 추세는 어제 대비만)."""
    s_today = shares(today, group)
    s_yday = shares(yesterday, group) if yesterday else None
    rows = []
    for v, (share, cnt) in s_today.items():
        if cnt < min_count:
            continue
        dod = round(share - s_yday.get(v, (0.0, 0))[0], 1) if s_yday is not None else None
        rows.append({"name": v, "share": round(share, 1), "count": cnt, "dod_pp": dod, "week_pp": None})
    rows.sort(key=lambda r: -r["share"])
    known = sum(1 for p in today if attr_values(p, group))
    return {"rows": rows, "coverage": round(100 * known / max(1, len(today)), 1)}


def by_big_category(today: list[dict], yesterday: list[dict] | None) -> list[dict]:
    out = []
    for name, _ in BIG_CATEGORIES:
        items = [p for p in today if big_category(p) == name]
        if not items:
            continue
        prev = [p for p in yesterday if big_category(p) == name] if yesterday else None
        out.append({"name": name, "count": len(items),
                    "attributes": {g: attr_table(items, prev, g, min_count=2) for g in BIG_CATEGORY_GROUPS}})
    return out


def price_bands(products: list[dict]) -> dict:
    """가격대별 상품 수 (카테고리별로도)."""
    labels = [b[2] for b in PRICE_BANDS]
    rows = []
    for code in CATEGORY_ORDER:
        items = [p for p in products if p["category_code"] == code]
        if not items:
            continue
        counts = [0] * len(PRICE_BANDS)
        for p in items:
            price = _price(p)
            if price is None:
                continue
            for i, (lo, hi, _) in enumerate(PRICE_BANDS):
                if lo <= price < hi:
                    counts[i] += 1
                    break
        prices = [x for x in (_price(p) for p in items) if x]
        rows.append({"name": items[0]["category_name"], "counts": counts, "median": _median(prices)})
    totals = [sum(r["counts"][i] for r in rows) for i in range(len(labels))]
    all_prices = [x for x in (_price(p) for p in products) if x]
    return {"labels": labels, "rows": rows, "totals": totals, "median": _median(all_prices)}


def analyze_gender(today: list[dict], yesterday: list[dict] | None, history: list[list[dict]], date: str,
                   reviews: dict | None = None, prev_word: str = "어제", fresh_days: int = 14) -> dict:
    """yesterday = 비교할 이전 기록(일간 어제, 주간 지난주, 월간 지난달), prev_word = 그 이름."""
    has_week = len(history) >= MIN_WEEK_DAYS
    attributes = {}
    trend: dict[tuple[str, str], float] = {}     # 속성별 추세 (7일 대비 우선, 없으면 어제 대비)
    share_today: dict[tuple[str, str], float] = {}
    for group in GROUPS:
        s_today = shares(today, group)
        s_yday = shares(yesterday, group) if yesterday else None
        week = [shares(h, group) for h in history] if has_week else []
        table = []
        for v, (share, cnt) in s_today.items():
            if cnt < MIN_COUNT:
                continue
            entry = {"name": v, "share": round(share, 1), "count": cnt, "dod_pp": None, "week_pp": None}
            if s_yday is not None:
                entry["dod_pp"] = round(share - s_yday.get(v, (0.0, 0))[0], 1)
            if week:
                entry["week_pp"] = round(share - sum(w.get(v, (0.0, 0))[0] for w in week) / len(week), 1)
            t = entry["week_pp"] if entry["week_pp"] is not None else entry["dod_pp"]
            if t is not None:
                trend[(group, v)] = t
            share_today[(group, v)] = share
            table.append(entry)
        table.sort(key=lambda e: -e["share"])
        known = sum(1 for p in today if attr_values(p, group))
        attributes[group] = {"rows": table, "coverage": round(100 * known / max(1, len(today)), 1)}

    has_trend = bool(trend)
    trend_label = "7일 평균 대비" if has_week else f"{prev_word} 대비"

    # 순위 급등 / 신규 진입 (어제 대비, 같은 29CM 종류 안 순위끼리)
    movers, new_entries = [], []
    prev_rank = {}
    if yesterday:
        prev_rank = {(p["mid_name"], p["product_id"]): p["rank"] for p in yesterday}
        seen_before = {p["product_id"] for p in yesterday}
        for p in today:
            prev = prev_rank.get((p["mid_name"], p["product_id"]))
            if prev is None:
                if p["clothing_rank"] <= 50 and p["product_id"] not in seen_before:
                    new_entries.append(product_brief(p))
            elif prev - p["rank"] >= 10:
                movers.append(product_brief(p) | {"prev_rank": prev, "change": prev - p["rank"]})
        movers.sort(key=lambda m: -m["change"])

    # 추천: 순위가 높고, (기록이 있으면) 뜨는 속성·순위 상승, (첫날엔) 인기 속성을 가진 상품. 아이템 종류는 겹치지 않게.
    def score(p: dict) -> tuple[float, list[str]]:
        bonus, reasons = 0.0, []
        for group in ("item_type", "fit", "silhouette", "fiber", "color"):
            for v in attr_values(p, group):
                if v.startswith("기타"):
                    continue
                if has_trend:
                    t = trend.get((group, v), 0.0)
                    if t > 0:
                        bonus += 0.05 * t
                        reasons.append((t, f"{v} +{t:.1f}%p"))
                elif group != "item_type":
                    s = share_today.get((group, v), 0.0)
                    bonus += 0.004 * s
                    reasons.append((s, f"{v} {s:.0f}%"))
        prev = prev_rank.get((p["mid_name"], p["product_id"]))
        if prev:
            bonus += max(0, prev - p["rank"]) / MAX_RANK
        reasons.sort(reverse=True)
        return p["weight"] + bonus, [r for _, r in reasons[:3]]

    recommendations, used_types = [], set()
    for p, (s, reasons) in sorted(((p, score(p)) for p in today), key=lambda x: -x[1][0]):
        if p["item_type"] in used_types:
            continue
        used_types.add(p["item_type"])
        why = f"{p['mid_name']} {p['rank']}위" + (f" · 의류 전체 {p['overall_rank']}위" if p["overall_rank"] else "")
        prev = prev_rank.get((p["mid_name"], p["product_id"]))
        if prev and prev > p["rank"]:
            why += f" ({prev_word} {prev}위)"
        if reasons:
            why += (f" · 뜨는 속성({trend_label}): " if has_trend else " · 인기 속성 비중: ") + ", ".join(reasons)
        recommendations.append(product_brief(p) | {"reason": why, "score": round(s, 3)})
        if len(recommendations) >= RECOMMEND_PER_GENDER:
            break

    headlines = []
    for group in HEADLINE_GROUPS:
        rows = [r for r in attributes[group]["rows"] if not r["name"].startswith("기타")]  # '기타 하의'는 정보가 없음
        if has_trend:
            best = max(rows, key=lambda e: trend.get((group, e["name"]), -99), default=None)
            t = trend.get((group, best["name"])) if best else None
            if best and t is not None and t > 0:
                headlines.append({"kind": "뜨는", "group": GROUP_LABELS[group], "name": best["name"],
                                  "value": f"+{t:.1f}%p", "note": f"비중 {best['share']:.0f}% · {trend_label}"})
        elif rows:
            headlines.append({"kind": "가장 많은", "group": GROUP_LABELS[group], "name": rows[0]["name"],
                              "value": f"{rows[0]['share']:.0f}%", "note": "인기 비중"})

    return {
        "count": len(today),
        "has_trend": has_trend,
        "trend_label": trend_label,
        "headlines": headlines,
        "attributes": attributes,
        "top_by_category": top_by_category(today, reviews=reviews),  # 전부. 화면엔 20개, 더보기로 50위씩
        "big_categories": by_big_category(today, yesterday),
        "item_types": item_type_profiles(today, yesterday),
        "price_bands": price_bands(today),
        "movers": movers[:10],
        "new_entries": new_entries[:10],
        "fresh_entries": fresh_entries(today, yesterday, date, fresh_days),
        "fresh_days": fresh_days,
        "rising_brands": rising_brands(today, yesterday, movers),
        "recommendations": recommendations,
    }


def analyze(date: str, period: str = "daily") -> dict:
    rows = load_day(date, period)
    if rows is None:
        raise FileNotFoundError(f"{history_path(date, period)} 없음 — 먼저 twentynine_fetch.py 실행")
    details = load_details()
    today = build_products(rows, details)
    scope = data_scope(rows)
    yesterday_date = compare_date(date, period)
    yesterday, history = None, []
    # 일간만 최근 7일 평균과도 비교. 주간·월간은 지난주·지난달 한 번과만 비교
    for d in previous_dates(date, TREND_WINDOW_DAYS) if period == "daily" else [yesterday_date]:
        prev = load_day(d, period)
        if prev is None or data_scope(prev) != scope:
            continue  # 수집 방식이 다른 날(예: 9/24 카테고리별 랭킹)은 비교하지 않음
        products = build_products(prev, details)
        if period == "daily":
            history.append(products)
        if d == yesterday_date:
            yesterday = products  # 비교는 정확히 그 날 기록이 있을 때만

    ids = {r["product_id"] for r in rows}
    reviews = review_summary.load_summaries()
    genders = {g: analyze_gender(today[g], yesterday[g] if yesterday else None, [h[g] for h in history], date,
                                 reviews, PERIODS[period][3], FRESH_DAYS[period])
               for g in GENDERS.values()}
    # 순위가 크게 오른 상품의 원인 (Claude가 조사해 rise_reasons.py로 저장한 것. 아직 없으면 비워 둠)
    reasons = json.loads(RISE_REASONS_PATH.read_text(encoding="utf-8")) if RISE_REASONS_PATH.exists() else {}
    day_reasons = reasons.get(f"{period}:{date}", {})
    for g in genders.values():
        for m in g["movers"]:
            m["why"] = day_reasons.get(m["product_id"])
    return {
        "date": date,
        "period": period,
        "compare_date": yesterday_date,
        "scope": scope,
        "history_days": len(history),
        "has_yesterday": yesterday is not None,
        "detail_coverage": round(100 * sum(1 for i in ids if i in details) / max(1, len(ids)), 1),
        "genders": genders,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=today_kst())
    parser.add_argument("--period", choices=list(PERIODS), default="daily")
    args = parser.parse_args()
    result = analyze(args.date, args.period)
    TMP_DIR.mkdir(exist_ok=True)
    out = analysis_path(args.date, args.period)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"분석 완료: {out} (상세정보 반영 {result['detail_coverage']}%)")
    for g, data in result["genders"].items():
        print(f"  {g}: {data['count']}개")
        for h in data["headlines"]:
            print(f"    - {h['kind']} {h['group']}: {h['name']} {h['value']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
