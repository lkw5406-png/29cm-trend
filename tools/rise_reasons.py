"""순위 급상승 원인 도우미 Tool — 리포트 '시장 동향'의 '순위가 크게 오른 상품'마다 Claude가 웹·SNS·유튜브를 조사해
쓴 원인을 검사해서 data/rise_reasons.json에 합친다. 조사 자체는 Claude(웹 검색)가 하고, 이 Tool은
① 조사할 상품 목록과 데이터로 알 수 있는 단서(가격·할인 변화, 등록일, 같은 브랜드 동반 상승 등)를 뽑고
② 조사 결과의 형식(원인 종류·설명·출처 링크)을 검사해 저장한다.

- 원인은 날짜·기간별로 따로 저장 (같은 상품이라도 오른 날마다 이유가 다를 수 있음). 키 = "기간:날짜" → 상품번호.
- 예전에 같은 상품을 조사한 적이 있으면 --next 목록에 그 원인을 같이 보여 줌 (다시 확인해서 쓰면 됨).

조사 결과 파일 형식(.tmp/rise_batch.json):
  {"상품번호": {"causes": ["셀럽·인플루언서 착용"], "summary": "…한 줄 원인…",
               "points": ["…핵심 원인 한 줄…", "…근거(언제·어디서·얼마나)…", "…가격·상품 정보…"],
               "reason": "…무슨 일이 있었고 왜 순위가 올랐는지…",
               "confidence": "확인", "sources": [{"title": "…", "url": "https://…"}]}, ...}
  - causes: CAUSES 안에서 1~3개 (가장 큰 원인 먼저)
  - summary: 오늘 요약에 들어갈 한 줄 원인 (30자 이내, 구체적으로 — 예: '패션플래닛 협업 · 9/25 코디 영상')
  - points: 리포트 '시장 동향'에 보이는 짧은 항목 2~5개 (한 항목 90자 이내, 한 항목에 사실 하나. 긴 문단 말고 '-' 항목으로)
            순서 = 핵심 원인 → 근거(언제·어디서·조회수) → 보조 요인(날씨·동반 상승) → 가격·상품 정보. reason의 내용 안에서만.
  - reason: 조사 내용 전체 (리포트에선 '조사 내용 전체 보기'로 접혀 있음)
  - confidence: "확인"(출처로 원인이 직접 확인됨) / "추정"(정황상 가장 그럴듯함 — 설명에 근거를 적을 것)
  - sources: 실제로 열어 본 페이지만. 확인이면 1개 이상 필수. 데이터 단서만으로 쓴 원인은 [] 가능(추정일 때만)

사용법:
  python tools/rise_reasons.py --next [--date YYYY-MM-DD] [--period daily]   조사할 상품 → .tmp/rise_queue.json
  python tools/rise_reasons.py --merge .tmp/rise_batch.json [--date …] [--period …]   검사 후 합치기
  python tools/rise_reasons.py --check [--date …]                          남은 개수 (오늘 리포트 기간 전부)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_trends import GENDERS, analyze, compare_date, image_date, load_day
from common import PERIODS, RISE_REASONS_PATH as REASONS_PATH, TMP_DIR, today_kst

QUEUE_PATH = TMP_DIR / "rise_queue.json"
CAUSES = [
    "셀럽·인플루언서 착용",   # 아이돌·배우·인플루언서가 입은 사진·영상
    "유튜브·SNS 콘텐츠",      # 리뷰·하울·코디 영상, 인스타·틱톡 게시물, 커뮤니티 화제
    "신상품·리뉴얼 출시",     # 신상 발매, 리뉴얼, 재입고, 새 컬러
    "협업·한정판",            # 콜라보, 한정 발매
    "할인·프로모션",          # 가격 인하, 쿠폰, 29CM 기획전·세일, 타임세일
    "29CM 노출",              # 29CM 메인·기획전·매거진·라이브 등 플랫폼 안 노출
    "브랜드 화제",            # 브랜드 자체 이슈(광고 캠페인, 모델 발탁, 팝업 등)
    "시즌·날씨",              # 기온 변화, 환절기, 명절·행사 수요
]
CONFIDENCE = ("확인", "추정")
SUMMARY_MAX = 30  # 오늘 요약에 들어갈 한 줄 원인
POINT_MAX = 90    # 시장 동향 항목 하나의 글자 수
POINTS_RANGE = (2, 5)


def load_reasons() -> dict:
    return json.loads(REASONS_PATH.read_text(encoding="utf-8")) if REASONS_PATH.exists() else {}


def key(date: str, period: str) -> str:
    return f"{period}:{date}"


def report_periods(date: str) -> list[str]:
    from run_daily import report_periods as rp
    return [p for p in rp(date) if load_day(date, p)]


def movers(date: str, period: str) -> list[dict]:
    """리포트에 나오는 '순위가 크게 오른 상품'(성별마다 최대 10개) + 데이터 단서."""
    result = analyze(date, period)
    prev_rows = load_day(compare_date(date, period), period) or []
    prev = {(r["gender_filter"], r["product_id"]): r for r in prev_rows}
    today = {(r["gender_filter"], r["product_id"]): r for r in load_day(date, period) or []}
    code = {v: k for k, v in GENDERS.items()}
    out = []
    for gender, g in result["genders"].items():
        brands = [m["brand"] for m in g["movers"]]
        for m in g["movers"]:
            t = today.get((code[gender], m["product_id"]), {})
            y = prev.get((code[gender], m["product_id"]), {})
            hints = []
            if y.get("final_price") and t.get("final_price") and y["final_price"] != t["final_price"]:
                hints.append(f"가격 {int(y['final_price']):,}원 → {int(t['final_price']):,}원 "
                             f"(할인율 {y.get('discount_rate') or 0}% → {t.get('discount_rate') or 0}%)")
            else:
                hints.append(f"가격 변화 없음 ({int(t.get('final_price') or 0):,}원, 할인율 {t.get('discount_rate') or 0}%)")
            reg = image_date(m["image_url"])
            if reg:
                hints.append(f"대표 사진을 올린 달 {reg} (상품 등록·사진 교체 시점 추정)")
            if brands.count(m["brand"]) > 1:
                hints.append(f"같은 브랜드 동반 상승 {brands.count(m['brand'])}개")
            for label in ("sales_label", "viewers", "flag"):
                if t.get(label) and t[label].lower() != "none" and t[label] not in hints:
                    hints.append(t[label])
            out.append({"product_id": m["product_id"], "gender": gender, "brand": m["brand"],
                        "name": m["product_name"], "item_type": m["item_type"], "rank": f"{m['prev_rank']}→{m['rank']}",
                        "product_url": m["product_url"], "hints": hints})
    return out


def todo(date: str, period: str) -> list[dict]:
    done = load_reasons().get(key(date, period), {})
    items, seen = [], set()
    for m in movers(date, period):
        if m["product_id"] in done or m["product_id"] in seen:
            continue  # 남성·여성 양쪽에 오른 상품은 한 번만 조사
        seen.add(m["product_id"])
        items.append(m)
    return items


def validate(batch: dict, allowed: set[str]) -> list[str]:
    errs = []
    for pid, r in batch.items():
        if pid not in allowed:
            errs.append(f"{pid}: 이 날짜·기간의 '크게 오른 상품'이 아님")
            continue
        if not isinstance(r, dict):
            errs.append(f"{pid}: 값은 {{causes, reason, confidence, sources}}")
            continue
        causes = r.get("causes")
        if not isinstance(causes, list) or not 1 <= len(causes) <= 3 or any(c not in CAUSES for c in causes):
            errs.append(f"{pid}: causes는 다음 중 1~3개 — {', '.join(CAUSES)}")
        if not isinstance(r.get("summary"), str) or not 5 <= len(r["summary"].strip()) <= SUMMARY_MAX:
            errs.append(f"{pid}: summary(오늘 요약용 한 줄 원인)가 없거나 {SUMMARY_MAX}자를 넘음")
        pts = r.get("points")
        if (not isinstance(pts, list) or not POINTS_RANGE[0] <= len(pts) <= POINTS_RANGE[1]
                or any(not isinstance(x, str) or not 5 <= len(x.strip()) <= POINT_MAX for x in pts)):
            errs.append(f"{pid}: points(시장 동향에 보일 짧은 항목)는 {POINTS_RANGE[0]}~{POINTS_RANGE[1]}개, "
                        f"항목마다 {POINT_MAX}자 이내 — 핵심 원인 → 근거 → 보조 요인 → 가격·상품 정보 순")
        if not isinstance(r.get("reason"), str) or len(r["reason"].strip()) < 30:
            errs.append(f"{pid}: reason(설명)이 없거나 너무 짧음 — 무슨 일이 있었고 왜 올랐는지 30자 이상")
        if r.get("confidence") not in CONFIDENCE:
            errs.append(f"{pid}: confidence는 '확인' 또는 '추정'")
        srcs = r.get("sources", [])
        if not isinstance(srcs, list) or any(not isinstance(s, dict) or not str(s.get("url", "")).startswith("http")
                                             or not s.get("title") for s in srcs):
            errs.append(f"{pid}: sources는 [{{'title': …, 'url': 'https://…'}}] 형식")
        elif r.get("confidence") == "확인" and not srcs:
            errs.append(f"{pid}: '확인'이면 출처 링크가 1개 이상 필요 (없으면 '추정')")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=today_kst())
    ap.add_argument("--period", choices=list(PERIODS), default="daily")
    ap.add_argument("--next", action="store_true")
    ap.add_argument("--merge")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if args.merge:
        batch = json.loads(Path(args.merge).read_text(encoding="utf-8"))
        allowed = {m["product_id"] for m in movers(args.date, args.period)}
        errs = validate(batch, allowed)
        if errs:
            print("조사 결과에 문제가 있어 합치지 않음:\n  " + "\n  ".join(errs[:40]))
            return 1
        reasons = load_reasons()
        day = reasons.setdefault(key(args.date, args.period), {})
        for pid, r in batch.items():
            day[pid] = {"causes": r["causes"], "summary": r["summary"].strip(),
                        "points": [x.strip() for x in r["points"]], "reason": r["reason"].strip(),
                        "confidence": r["confidence"],
                        "sources": [{"title": s["title"].strip(), "url": s["url"].strip()} for s in r.get("sources", [])],
                        "checked": today_kst()}
        REASONS_PATH.parent.mkdir(exist_ok=True)
        tmp = REASONS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(reasons, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(REASONS_PATH)
        print(f"합침: {len(batch)}개 → {PERIODS[args.period][1]} {args.date} 남은 것 {len(todo(args.date, args.period))}개")
        return 0

    if args.next:
        items = todo(args.date, args.period)
        past = {}
        for day in load_reasons().values():
            past.update(day)  # 예전에 같은 상품을 조사한 기록 (나중 것이 남음)
        for it in items:
            if it["product_id"] in past:
                it["earlier"] = past[it["product_id"]]
        TMP_DIR.mkdir(exist_ok=True)
        QUEUE_PATH.write_text(json.dumps({"date": args.date, "period": args.period, "causes": CAUSES, "items": items},
                                         ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{PERIODS[args.period][1]} {args.date}: 원인 조사할 상품 {len(items)}개 → .tmp/rise_queue.json")
        for it in items:
            print(f"  {it['product_id']} [{it['gender']}] {it['brand']} | {it['name']} | {it['rank']} | "
                  + " · ".join(it["hints"]))
        return 0

    for period in report_periods(args.date):
        print(f"{PERIODS[period][1]} {args.date}: 원인 조사할 상품 {len(todo(args.date, period))}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
