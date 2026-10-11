"""후기 요약 Tool — 종류별 인기 TOP 50 상품의 후기를 모아 좋은 점·아쉬운 점을 정리한다.

29CM 후기 목록(review-api.29cm.co.kr)에서 상품마다
- 베스트순 후기 50개 → 좋은 점(4~5점 후기)
- 별점 낮은 순 후기 중 3점 이하 최대 50개 → 아쉬운 점
을 받아, 자주 나온 표현(REVIEW_PHRASES)을 세고 대표 후기 한 줄씩을 뽑아 data/review_summaries.json에 저장한다.
AI 요약이 아니라 규칙(표현 사전)으로 센 결과라 추가 요금이 없다. 저장소에는 요약만 남긴다(대표 한 줄 포함).
받은 후기 원본은 .tmp/reviews/에 캐시(개인 정보 제외) → 표현 사전을 고치면 --rebuild로 다시 받지 않고 재계산.

한 번 정리한 상품은 REFRESH_DAYS 동안 다시 받지 않는다.
매일 자동 실행은 한 번에 MAX_PER_RUN개까지만(처음 보는 상품 → 오래된 상품, 순위 높은 순). 나머지는 다음 날 이어서.

사용법: python tools/review_summary.py [--date YYYY-MM-DD] [--limit N] [--all]
  --all      MAX_PER_RUN 제한 없이 전부 (처음 한 번 채울 때)
  --rebuild  29CM에 요청하지 않고 .tmp/reviews/ 캐시로 요약만 다시 계산
"""
import argparse
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date as Date

from common import PERIODS, ROOT, TMP_DIR, BlockedError, get_json, history_path, today_kst

# 정렬: BEST(베스트순), POINT_ASC(별점 낮은 순) — 2026-10-11 확인. LOW_POINT·RECENT 는 없는 값(400)
REVIEW_URL = "https://review-api.29cm.co.kr/api/v4/reviews?itemId={id}&page={page}&size=20&sort={sort}"
SUMMARY_PATH = ROOT / "data" / "review_summaries.json"
LOCK_PATH = TMP_DIR / "review_summary.lock"
CACHE_DIR = TMP_DIR / "reviews"
REQUEST_INTERVAL_SEC = 2
TOP_N = 50            # 종류별 인기 TOP 50 (2026-10-11 사장님 결정: 30 → 50, 무신사 리포트와 같게). 종류가 성별 합쳐 10개라 약 500개
MAX_PER_RUN = 120     # 매일 자동 실행 한 번에 정리할 최대 상품 수 (상품당 30~40초 → 약 1시간, 실행 시간 제한 대비)
REVIEWS_EACH = 50     # 도움순 50개 + 낮은 별점 50개 (2026-09-25 사장님 결정: 20개는 적다)
BAD_MAX_GRADE = 3     # 3점 이하를 아쉬운 후기로 봄
REFRESH_DAYS = 7
CLOTHING_CODES = ("001", "002", "003", "100")

# 표현 사전: 대표 이름 → 정규식. 좋은 점은 4~5점 후기, 아쉬운 점은 3점 이하 후기에서만 센다.
REVIEW_PHRASES = {
    "pros": {
        "핏이 예뻐요": r"핏.{0,4}(예쁘|예뻐|이쁘|이뻐|좋|굿|깔끔|잘\s?빠|미쳤)",
        "디자인·색감이 예뻐요": r"(디자인|색감|색깔|컬러|색상).{0,4}(예쁘|예뻐|이쁘|이뻐|좋|고급|맘에|마음에)",
        "부드러워요": r"부드러|보들",
        "따뜻해요": r"따뜻(?!한\s?(색|톤|베이지|컬러|브라운|느낌|계열|무드))|따듯(?!한\s?(색|톤|컬러))|보온",
        "편해요": r"편하|편안|편해|편함",
        "가벼워요": r"가벼|가볍",
        "가성비 좋아요": r"가성비|가격\s?대비|저렴|착한\s?가격|싸게",
        "퀄리티·재질이 좋아요": r"(퀄리티|품질|재질|소재|원단|마감).{0,4}(좋|훌륭|괜찮|탄탄|짱|최고)",
        "두께가 적당해요": r"두께.{0,5}(적당|딱|좋)",
        "사이즈가 잘 맞아요": r"정사이즈|사이즈.{0,6}(딱|잘\s?맞|적당)",
        "코디하기 좋아요": r"코디|매치|데일리|아무\s?데나|어디에나|활용",
        "재구매·추천해요": r"재구매|또\s?샀|추천|색깔별|깔별|색상별",
    },
    "cons": {
        "보풀": r"보풀",
        "털 빠짐": r"털.{0,3}(빠|날림|날려|묻)|털빠",
        "작게 나와요": r"작아(?!\s?보)|작게(?!\s?보)|작네|작음|작다|작습|타이트|꽉\s?껴|끼어요|껴요|낀다",
        "크게 나와요": r"커요|크게\s?나|크네|큽니다|너무\s?커|많이\s?커|너무\s?크",
        "기장이 아쉬워요": r"기장.{0,5}(길|짧|애매)",
        "얇거나 비쳐요": r"얇(?!아\s?보|게\s?보|어\s?보)|비침|비쳐|비치",
        "두껍거나 더워요": r"두꺼워|두껍|더워|덥",
        "까슬·따가워요": r"까슬|까끌|따가|따갑|간지러|가려",
        "냄새": r"냄새",
        "마감·불량": r"실밥|마감|박음질|구멍|불량|하자|올\s?풀",
        "늘어나요·변형": r"늘어|변형|쳐져|처져|쳐짐|처짐",
        "줄어들어요": r"줄어|수축",
        "물빠짐·이염": r"물\s?빠|이염|색\s?빠",
        "색이 사진과 달라요": r"(색|색감|컬러).{0,6}(다르|달라|차이)",
        "정전기": r"정전기",
        "구김": r"구김|구겨",
        "무거워요": r"무거",
        "배송·응대 불만": r"배송.{0,4}(늦|느리|오래|엉망)|(교환|환불|반품).{0,8}(안\s?해|안\s?된|거절|불가|어렵|느리)|응대|고객센터|문의.{0,6}(답|안)",
        "가격이 아쉬워요": r"(가격|돈).{0,6}(아깝|비싸)|비싼",
    },
}
PATTERNS = {kind: {name: re.compile(rx) for name, rx in d.items()} for kind, d in REVIEW_PHRASES.items()}
PROFANITY = re.compile(r"시발|씨발|ㅅㅂ|ㅆㅂ|존나|졸라|ㅈㄴ|엿같|개같|병신|ㅂㅅ|지랄|ㅈㄹ|좆|썅|미친놈|꺼져")


def _count(reviews: list[dict], kind: str) -> list[tuple[str, int]]:
    c = Counter()
    for r in reviews:
        text = r.get("content") or ""
        for name, rx in PATTERNS[kind].items():
            if rx.search(text):
                c[name] += 1  # 후기 한 개에서 같은 표현은 한 번만
    return c.most_common()


def _quote(reviews: list[dict], kind: str, before: int = 30, after: int = 45) -> str:
    """도움돼요가 많은 후기부터, 좋은 점/아쉬운 점 표현이 나온 부분의 앞뒤를 잘라 한 줄로.
    (문장 단위로 자르면 '~다 잘 어울려요'의 '다'에서 끊기는 일이 있어 표현 위치 기준으로 자름)"""
    for r in sorted(reviews, key=lambda r: -(r.get("likeCount") or 0)):
        text = re.sub(r"\s+", " ", r.get("content") or "").strip()
        hits = [m for rx in PATTERNS[kind].values() for m in [rx.search(text)] if m]
        if not hits:
            continue
        m = min(hits, key=lambda m: m.start())
        start, end = max(0, m.start() - before), min(len(text), m.end() + after)
        if start > 0:  # 단어 중간에서 시작하지 않게: 앞쪽 문장 경계나 띄어쓰기로
            cut = max(text.rfind(ch, start, m.start()) for ch in (". ", "! ", "? ", "~ ", " "))
            start = cut + 1 if cut >= start else start
        if end < len(text):
            cut = max(text.rfind(ch, m.end(), end) for ch in (".", "!", "?", "~", " "))
            end = cut + 1 if cut > m.end() else end
        snippet = text[start:end].strip(" ,")
        if len(snippet) < 8 or PROFANITY.search(snippet):  # 디자이너에게 전달하는 자료라 비속어 섞인 후기는 대표로 안 씀
            continue
        return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")
    return ""


def _survey(reviews: list[dict]) -> list[dict]:
    """구매자 설문(사이즈·색감·두께 등)에서 항목마다 가장 많은 답과 비율."""
    answers: dict[str, Counter] = defaultdict(Counter)
    for r in reviews:
        for q in r.get("survey") or []:
            for a in q.get("answers") or []:
                answers[q.get("attribute", "")][a.get("answerShortText", "")] += 1
    out = []
    for attr, c in answers.items():
        total = sum(c.values())
        if not attr or total < 5:
            continue
        top, n = c.most_common(1)[0]
        out.append({"attribute": attr, "answer": top, "pct": round(100 * n / total), "count": total})
    return out


def _slim(r: dict) -> dict:
    """캐시에 남길 것만 (닉네임·키·몸무게 등 개인 정보는 버림)."""
    return {"no": r.get("itemReviewNo"), "grade": int(r.get("point") or 0), "likeCount": r.get("helpfulCount") or 0,
            "content": r.get("contents") or "",
            "survey": []}  # 29CM 설문(surveyList)은 번호만 와서 뜻을 알 수 없어 쓰지 않음


def fetch_reviews(product_id: str, sort: str, want: int, stop_above_grade: int | None = None) -> tuple[list[dict], int, bool]:
    """(후기들, 전체 후기 수, 끝까지 다 받았는지). 낮은 별점순은 3점 넘는 후기가 나오면 멈춤."""
    got, total, complete = [], 0, False
    for page in range((want + 19) // 20):
        res = get_json(REVIEW_URL.format(id=product_id, sort=sort, page=page), allow_404=True)
        time.sleep(REQUEST_INTERVAL_SEC)
        data = (res or {}).get("data") or {}
        page_size = len(data.get("results") or [])
        batch = [_slim(r) for r in data.get("results") or [] if not r.get("isBlind")]
        total = data.get("count") or total
        if stop_above_grade is not None:
            keep = [r for r in batch if 0 < r["grade"] <= stop_above_grade]
            got += keep
            if len(keep) < len(batch):
                complete = True
                break
        else:
            got += batch
        if page_size < 20:
            complete = True
            break
    return got[:want], total, complete and len(got) <= want


def load_raw(product_id: str, date: str) -> dict:
    """후기 원본(.tmp 캐시). 요약 방식을 고쳐도 다시 받지 않게 REFRESH_DAYS 동안 재사용. 없거나 오래됐으면 받음."""
    path = CACHE_DIR / f"{product_id}.json"
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not _stale(raw, date):
            return raw
    helpful, total, _ = fetch_reviews(product_id, "BEST", REVIEWS_EACH)
    low, _, low_complete = fetch_reviews(product_id, "POINT_ASC", REVIEWS_EACH, stop_above_grade=BAD_MAX_GRADE)
    raw = {"fetched": date, "total": total, "helpful": helpful, "low": low, "low_complete": low_complete}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return raw


def summarize(raw: dict) -> dict:
    helpful, low, total = raw["helpful"], raw["low"], raw["total"]
    good = [r for r in helpful if r["grade"] >= 4]
    seen: set = set()
    bad = [r for r in low + helpful if 0 < r["grade"] <= BAD_MAX_GRADE and not (r["no"] in seen or seen.add(r["no"]))]
    # 3점 이하 후기 수: 낮은 별점순으로 끝까지 받았으면 정확한 수, 아니면 '이 수 이상'
    bad_total = len(low)
    return {
        "total": total,
        "sampled": len(helpful),
        "bad_total": bad_total,
        "bad_exact": bool(raw.get("low_complete")),
        "bad_pct": round(100 * bad_total / total, 1) if total else None,
        "survey": _survey(helpful),
        "pros": [{"name": n, "count": c} for n, c in _count(good, "pros")[:4]],
        "cons": [{"name": n, "count": c} for n, c in _count(bad, "cons")[:4]],
        "good_quote": _quote(good, "pros"),
        "bad_quote": _quote(bad, "cons"),
        "fetched": raw["fetched"],
    }


def top_products(date: str, n: int = TOP_N, period: str = "daily") -> list[str]:
    """성별 × 29CM 종류별 인기 순위 상위 n개 상품번호 (리포트의 종류별 인기 TOP과 같은 순서)."""
    with open(history_path(date, period), encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if r["category_code"] in CLOTHING_CODES:
            groups[(r["gender_filter"], r.get("mid_name") or r["category_code"])].append(r)
    ranked = [sorted(items, key=lambda r: int(r["rank"]))[:n] for items in groups.values()]
    out: list[str] = []
    for pos in range(n):  # 모든 카테고리의 1위 → 2위 → … 순서 (도중에 멈춰도 윗순위부터 채워지게)
        for items in ranked:
            if pos < len(items) and items[pos]["product_id"] not in out:
                out.append(items[pos]["product_id"])
    return out


def load_summaries() -> dict:
    return json.loads(SUMMARY_PATH.read_text(encoding="utf-8")) if SUMMARY_PATH.exists() else {}


def save_summaries(s: dict) -> None:
    tmp = SUMMARY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    tmp.replace(SUMMARY_PATH)


def _stale(entry: dict | None, date: str) -> bool:
    if not entry or not entry.get("fetched"):
        return True
    return (Date.fromisoformat(date) - Date.fromisoformat(entry["fetched"])).days >= REFRESH_DAYS


def update_summaries(date: str, limit: int | None = MAX_PER_RUN, periods: tuple[str, ...] = ("daily",)) -> int:
    """periods의 랭킹 TOP 50을 합쳐서 정리 (같은 상품은 한 번만). 후기 요약은 상품 단위라 기간끼리 같이 씀."""
    if LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime < 600:
        print("다른 후기 수집이 이미 돌고 있어서 이번엔 건너뜀 (잠금: .tmp/review_summary.lock)")
        return 0
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(str(time.time()), encoding="utf-8")
    summaries = load_summaries()
    targets: list[str] = []
    for period in periods:
        if history_path(date, period).exists():
            targets += [pid for pid in top_products(date, period=period) if pid not in targets]
    todo = [pid for pid in targets if pid not in summaries] + \
           [pid for pid in targets if pid in summaries and _stale(summaries[pid], date)]
    if limit:
        todo = todo[:limit]
    print(f"후기 요약: 새로 정리할 상품 {len(todo)}개 (이미 있음 {len(summaries)}개)", flush=True)
    done = 0
    try:
        for pid in todo:
            summaries[pid] = summarize(load_raw(pid, date))
            done += 1
            if done % 10 == 0:
                save_summaries(summaries)
                LOCK_PATH.write_text(str(time.time()), encoding="utf-8")
                print(f"  {done}/{len(todo)}", flush=True)
    finally:
        save_summaries(summaries)  # 멈춰도 받은 만큼은 저장
        LOCK_PATH.unlink(missing_ok=True)
    print(f"후기 요약 저장: {done}개 → 총 {len(summaries)}개")
    return done


def rebuild_from_cache() -> int:
    summaries = load_summaries()
    n = 0
    for path in sorted(CACHE_DIR.glob("*.json")):
        summaries[path.stem] = summarize(json.loads(path.read_text(encoding="utf-8")))
        n += 1
    save_summaries(summaries)
    print(f"캐시로 다시 계산: {n}개")
    return n


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=today_kst())
    parser.add_argument("--limit", type=int, default=MAX_PER_RUN)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--period", choices=list(PERIODS), action="append",
                        help="여러 번 쓸 수 있음. 없으면 daily")
    args = parser.parse_args()
    if args.rebuild:
        rebuild_from_cache()
        return 0
    try:
        update_summaries(args.date, None if args.all else args.limit, tuple(args.period or ["daily"]))
    except BlockedError as e:
        print(f"중단: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
