"""상품 상세정보 수집 Tool.

29CM 상품 페이지(www.29cm.co.kr/products/번호 — robots.txt가 일반 봇에 허용한 주소)에 들어 있는
판매자 입력 정보를 읽어 data/product_details.json에 저장한다. 한 번 가져온 상품은 다시 요청하지 않는다. 상품당 요청 1번.
(화면 뒤 데이터 주소 bff-api.29cm.co.kr 는 2026-10-11 두 번 거부(403)돼 쓰지 않는다. 페이지 안 자료는 그 주소와 같은 모양.)

가져오는 것
- 제품 소재(상품정보제공고시) → 겉감의 주원료
- 29CM 소분류(예: 긴소매 티셔츠) → 리포트의 아이템 종류 이름으로 바꿔 저장 (ITEM_TYPES), 성별
- 상세 설명 글(앞부분) → 상품명에 없는 실루엣·원단 키워드를 찾는 데 사용
- 실측 사이즈표(가운데 사이즈의 총장·단면 등) → 기장, 바지 실루엣, 핏 판단에 사용
- 판매 옵션·상품정보제공고시의 색상 목록 → 상품명에 색이 없을 때 컬러 판단에 사용
- 판매 시작일(registered) → 신상 판정
29CM에는 무신사 같은 판매자 입력 '핏'·'두께' 칸이 없다 (핏은 상품명·사이즈표·사진 판독으로).

사용법: python tools/product_details.py [--date YYYY-MM-DD] [--limit N] [--period daily|weekly|monthly]
  해당 날짜 랭킹 CSV에 있는 상품 중 아직 상세정보가 없는 것만 가져온다.
"""
import argparse
import csv
import json
import re
import sys
import time
from html import unescape

from common import (DETAILS_PATH, MID_CATEGORIES, PERIODS, PRODUCT_URL, TMP_DIR, BlockedError, get_text,
                    history_path, today_kst)

DETAIL_URL = PRODUCT_URL  # 상품 페이지
PAGE_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')  # 페이지가 품고 있는 자료 조각
PAGE_REF = re.compile(r"\$[0-9a-f]+")  # 다른 줄을 가리키는 표시 ("$45" → 45번 줄)
COLOR_OPTION_NAMES = {"c", "color", "colour", "colors", "컬러", "색상", "색깔", "칼라"}
# 2026-10-11: 2초 간격으로 받다가 20개째에서 403 → 사장님 결정으로 훨씬 느리게(10초 간격, 하루 100개) 한 번 더 시도
REQUEST_INTERVAL_SEC = 10
# 2026-10-11 낮 사장님 결정: 상품 페이지 방식이 221개까지 거부 없이 받아져서 하루 한도를 없앰 (간격 10초는 유지)
DAILY_LIMIT = 0         # 하루에 새로 받는 상품 수. 0 = 제한 없음 (새 상품 전부)
SAVE_EVERY = 25
DESC_CHARS = 1500
CLOTHING_CODES = {"001", "002", "003", "100"}  # 상의, 아우터, 바지, 원피스/스커트 (common.CLOTHING_CATEGORIES)
LOCK_PATH = TMP_DIR / "product_details.lock"
LOCK_STALE_SEC = 600  # 25개 저장마다 갱신(약 2분). 10분 넘게 안 바뀌면 멈춘 것으로 보고 새로 시작 허용

# 29CM 소분류 이름 → 리포트의 아이템 종류 이름. (29CM 중분류, 소분류) 순으로 찾고, 없으면 29CM 이름 그대로.
# 무신사 리포트와 뜻이 같은 것은 같은 이름으로 맞춤 (키워드 사전·원단 추정 규칙을 같이 쓰려고).
ITEM_TYPES = {
    "상의": {"슬리브리스": "민소매 티셔츠", "블라우스": "셔츠/블라우스", "셔츠": "셔츠/블라우스",
           "긴소매 셔츠": "셔츠/블라우스", "반소매 셔츠": "셔츠/블라우스", "스웨트셔츠": "맨투맨/스웨트",
           "후디": "후드 티셔츠", "집업": "집업 상의"},
    "니트웨어": {"크루넥": "크루넥 니트", "브이넥": "브이넥 니트", "터틀넥": "터틀넥 니트", "베스트": "니트 베스트",
             "폴로셔츠": "니트 폴로", "캐시미어": "캐시미어 니트", "집업": "니트 집업", "후드": "니트 후드"},
    "아우터": {"바람막이": "나일론/코치 재킷", "나일론 재킷": "나일론/코치 재킷", "블레이저": "슈트/블레이저 재킷",
            "레더 재킷": "레더/라이더스 재킷", "무스탕": "무스탕/퍼", "퍼 재킷": "무스탕/퍼", "블루종": "블루종/MA-1",
            "아노락": "아노락 재킷", "데님 재킷": "트러커 재킷", "트렌치/맥코트": "트렌치 코트", "야상": "사파리/헌팅 재킷",
            "플리스": "플리스/뽀글이", "바시티": "스타디움 재킷", "경량패딩": "경량 패딩/패딩 베스트",
            "숏패딩": "숏패딩/헤비 아우터", "롱패딩": "롱패딩/헤비 아우터", "하프코트": "하프 코트", "숏코트": "숏 코트",
            "롱코트": "롱 코트"},
    "바지": {"쇼트": "숏 팬츠", "데님": "데님 팬츠", "트레이닝": "트레이닝/조거 팬츠", "트레이닝 팬츠": "트레이닝/조거 팬츠",
           "와이드": "와이드 팬츠", "스트레이트": "스트레이트 팬츠", "슬랙스": "슈트 팬츠/슬랙스", "슬림": "슬림 팬츠",
           "부츠컷": "부츠컷 팬츠", "기타 팬츠": "기타 하의"},
    "스커트": {"롱": "롱스커트", "미디": "미디스커트", "미니": "미니스커트", "데님": "데님 스커트"},
    "원피스": {"롱 원피스": "맥시원피스", "미디 원피스": "미디원피스", "미니 원피스": "미니원피스"},
}
ITEM_TYPES["하의"] = ITEM_TYPES["바지"]  # 남성의류는 중분류 이름이 '하의'
# 사이즈표 열 → 저장 이름 (무신사 리포트의 사이즈표 이름과 같게: classify_attributes가 그대로 읽음)
SIZE_FIELDS = {"shirtLength": "총장", "chestWidth": "가슴단면", "shoulderWidth": "어깨너비", "sleeveLength": "소매길이",
               "waistWidth": "허리단면", "hipsWidth": "엉덩이단면", "thighWidth": "허벅지단면", "crotchLength": "밑위",
               "hemWidth": "밑단단면"}
# 판매자가 칸을 채우지 않고 '상세 페이지 참고'처럼 적은 경우
NO_INFO = re.compile(r"상세\s*(페이지|설명|이미지|정보)|참조|참고|별도\s*표기|해당\s*(없음|사항)")

# 제품 소재 표기 → 대표 이름. 위에서부터 먼저 맞는 것.
FIBERS = [
    ("스판(폴리우레탄)", ["spandex", "polyurethane", "elastane", "스판", "폴리우레탄", "pu "]),
    ("합성피혁", ["합성피혁", "인조가죽", "synthetic leather", "faux leather", "vegan leather"]),
    ("가죽", ["leather", "가죽", "소가죽", "양가죽", "램스킨", "lambskin", "cowhide"]),
    ("캐시미어", ["cashmere", "캐시미어"]),
    ("울", ["wool", "울", "양모", "merino", "메리노", "lambswool"]),
    ("모헤어·알파카", ["mohair", "모헤어", "alpaca", "알파카", "angora", "앙고라"]),
    ("코튼", ["cotton", "코튼", "면"]),
    ("린넨", ["linen", "린넨", "리넨", "아마", "ramie", "라미"]),
    ("나일론", ["nylon", "나일론", "polyamide", "폴리아미드"]),
    ("레이온·모달·큐프라", ["rayon", "레이온", "viscose", "비스코스", "modal", "모달", "tencel", "텐셀",
                         "lyocell", "라이오셀", "cupro", "큐프라", "acetate", "아세테이트"]),
    ("아크릴", ["acrylic", "아크릴"]),
    ("실크", ["silk", "실크", "견"]),
    ("폴리에스터", ["polyester", "폴리에스터", "폴리에스테르", "polyeseter", "poly", "폴리"]),
]
# 겉감 이외 부분이 시작되는 표시 (안감·충전재·배색 등은 주원료 판단에서 뺀다)
NON_SHELL = re.compile(r"(lining|안감|filling|충전|padding|배색|trim|rib|립|포켓|pocket|주머니|심지)", re.I)


def clean_text(html: str) -> str:
    html = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)  # 꾸밈 코드(CSS) 제거
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    return re.sub(r"\s+", " ", text.replace("﻿", "")).strip()


def fiber_name(word: str, exact: bool = False) -> str | None:
    """영어·긴 단어는 앞부분 일치(POLYESTER→poly), '면'·'견' 같은 한 글자는 정확히 일치할 때만."""
    w = word.lower().strip()
    for name, keys in FIBERS:
        for k in keys:
            k = k.strip()
            if w == k or (not exact and len(k) >= 2 and w.startswith(k)):
                return name
    return None


def _best_pair(pairs: list[tuple[str, str]]) -> tuple[str, int] | None:
    best = None
    for word, pct in pairs:
        name = fiber_name(word)
        if name and (best is None or float(pct) > best[1]):
            best = (name, float(pct))
    return (best[0], int(best[1])) if best else None


def main_fiber(material_text: str) -> tuple[str | None, int | None]:
    """'(OUTSHELL) NYLON 100% (LINING) ...' → ('나일론', 100). 겉감에서 비율이 가장 큰 섬유."""
    text = clean_text(material_text).replace("％", "%")
    cut = NON_SHELL.search(text)
    shell = text[:cut.start()] if cut and cut.start() > 0 else text
    for segment in (shell, text):
        # '면 80%' 형식을 먼저, 없을 때만 '80% 면' 형식
        found = _best_pair(re.findall(r"([A-Za-z가-힣]+)\s*[:\-]?\s*(\d{1,3}(?:\.\d+)?)\s*%", segment))
        if not found:
            found = _best_pair([(w, n) for n, w in re.findall(r"(\d{1,3}(?:\.\d+)?)\s*%\s*([A-Za-z가-힣]+)", segment)])
        if found:
            return found
    # 비율 없이 '면' 같은 이름만 적힌 경우
    for token in re.findall(r"[A-Za-z가-힣]+", shell):
        name = fiber_name(token, exact=True)
        if name:
            return name, None
    return None, None


def item_type_name(mid_name: str, raw: str) -> str:
    return ITEM_TYPES.get(mid_name, {}).get(raw, raw)


def category1(detail: dict | None) -> str:
    """대분류 코드 (001 상의, 002 아우터, 003 바지, 100 원피스/스커트). 수집 대상 종류가 아니면 ''."""
    if not detail or detail.get("missing"):
        return ""
    return detail.get("category1_code") or ""


def is_clothing(detail: dict | None) -> bool:
    return category1(detail) in CLOTHING_CODES


def pick_size(charts: list[dict]) -> dict:
    """실측 사이즈표에서 가운데 사이즈 하나의 치수만 저장. 사이즈표가 없으면 {}."""
    sizes = [c for c in charts or [] if any(c.get(k) for k in SIZE_FIELDS)]
    if not sizes:
        return {}
    mid = sizes[(len(sizes) - 1) // 2]  # S/M/L이면 M, 사이즈가 짝수 개면 작은 쪽
    out = {"name": mid.get("sizeTypeText") or "", "count": len(sizes)}
    for key, name in SIZE_FIELDS.items():
        try:
            value = float(mid.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[name] = value
    return out


def pick_colors(d: dict) -> list[str]:
    """판매 옵션 중 색상 옵션의 값 이름들. 옵션에 없으면 상품정보제공고시 '색상' 칸. 없으면 [].
    옵션은 '색상:사이즈' 한 줄(값 '네이비:M')이거나 ['COLOR', 'SIZE'] 단계로 온다."""
    options = d.get("optionItems") or {}
    layout = [part.strip().lower() for name in options.get("layout") or [] for part in str(name).split(":")]
    colors: list[str] = []

    def walk(items: list, depth: int) -> None:
        for it in items or []:
            parts = str(it.get("title") or "").split(":")
            for i, part in enumerate(parts):
                idx = depth + i
                if idx < len(layout) and layout[idx] in COLOR_OPTION_NAMES and part.strip() and part.strip() not in colors:
                    colors.append(part.strip())
            walk(it.get("list") or [], depth + len(parts))
    walk(options.get("list") or [], 0)
    if not colors:
        for row in d.get("itemDetailsList") or []:
            value = clean_text(row.get("itemDetailsValue") or "")
            if row.get("itemDetailsTitles") == "색상" and value and not NO_INFO.search(value):
                colors = [c.strip() for c in re.split(r"[,/·]", value) if c.strip()]
    return colors[:20]


def page_product(html: str) -> dict | None:
    """상품 페이지 안의 상품 자료(데이터 주소가 주던 것과 같은 모양)를 꺼낸다. 못 찾으면 None.
    페이지는 자료를 '줄번호:내용' 줄들로 품고 있고, 값이 "$45"면 45번 줄을 가리킨다. 'T길이,' 로 시작하는 줄은 긴 글."""
    try:
        data = "".join(json.loads(c) for c in PAGE_CHUNK.findall(html)).encode("utf-8")
    except ValueError:
        return None
    rows: dict[str, object] = {}
    pos = 0
    while pos < len(data):
        colon = data.find(b":", pos)
        if colon < 0:
            break
        rid = data[pos:colon].decode("ascii", "replace")
        if data[colon + 1:colon + 2] == b"T":  # 긴 글: T<16진 바이트 수>,<글>
            comma = data.find(b",", colon)
            try:
                size = int(data[colon + 2:comma], 16)
            except ValueError:
                break
            rows[rid] = ("text", data[comma + 1:comma + 1 + size].decode("utf-8", "replace"))
            pos = comma + 1 + size
        else:
            end = data.find(b"\n", colon)
            end = len(data) if end < 0 else end
            rows[rid] = ("json", data[colon + 1:end].decode("utf-8", "replace"))
            pos = end + 1

    def resolve(value, depth: int = 0):
        if depth > 6:
            return value
        if isinstance(value, str) and PAGE_REF.fullmatch(value) and value[1:] in rows:
            kind, body = rows[value[1:]]
            if kind == "text":
                return body
            try:
                return resolve(json.loads(body), depth + 1)
            except ValueError:
                return None
        if isinstance(value, list):
            return [resolve(v, depth + 1) for v in value]
        if isinstance(value, dict):
            return {k: resolve(v, depth + 1) for k, v in value.items()}
        return value

    wanted = ("itemDetailsList", "itemSizeCharts", "optionItems", "itemDescriptions", "frontCategoryInfo",
              "visibleBeginTimestamp")
    for kind, body in rows.values():
        if kind == "json" and body.startswith("{") and '"itemDetailsList"' in body and '"itemNo"' in body:
            try:
                product = json.loads(body)
            except ValueError:
                continue
            return {k: resolve(product.get(k)) for k in wanted}
    return None


def fetch_detail(product_id: str) -> dict:
    html = get_text(DETAIL_URL.format(id=product_id), allow_404=True)
    time.sleep(REQUEST_INTERVAL_SEC)
    if html is None:
        return {"missing": True}
    d = page_product(html)
    if d is None:
        # 페이지는 왔는데 자료를 못 찾음 = 페이지 구조가 바뀜. 빈 기록을 쌓지 않게 멈춘다
        raise RuntimeError(f"상품 페이지에서 자료를 못 찾음 (구조 변경 의심): {DETAIL_URL.format(id=product_id)}")

    cats = d.get("frontCategoryInfo") or []
    mine = next((c for c in cats if c.get("category2Code") in MID_CATEGORIES), None)
    sex = [name[:2] for name in dict.fromkeys(c.get("category1Name") or "" for c in cats) if name in ("여성의류", "남성의류")]
    out: dict = {"sex": sex, "registered": str(d.get("visibleBeginTimestamp") or "")[:10]}
    if not mine:
        return out | {"category1_code": ""}  # 홈웨어·셋업 등 수집 대상 밖
    _, mid_name, big = MID_CATEGORIES[mine["category2Code"]]
    out |= {
        "category1_code": big,
        "mid_name": mid_name,
        "category2": item_type_name(mid_name, mine.get("category3Name") or ""),
        "category2_code": f"{big}-{mine.get('category3Code', '')}",  # classify가 대분류 일치를 앞자리로 확인
        "desc": clean_text(d.get("itemDescriptions") or "")[:DESC_CHARS],
    }
    material_raw = ""
    for row in d.get("itemDetailsList") or []:
        if row.get("itemDetailsTitles") == "제품 소재":
            material_raw = clean_text(row.get("itemDetailsValue") or "")
    if NO_INFO.search(material_raw):
        material_raw = ""  # '상세 페이지 참고'
    out["material_raw"] = material_raw[:200]
    out["main_fiber"], out["main_fiber_pct"] = main_fiber(material_raw)
    out["size"] = pick_size(d.get("itemSizeCharts") or [])
    out["colors"] = pick_colors(d)
    return out


def load_details() -> dict:
    if DETAILS_PATH.exists():
        return json.loads(DETAILS_PATH.read_text(encoding="utf-8"))
    return {}


def save_details(details: dict) -> None:
    DETAILS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DETAILS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(details, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    tmp.replace(DETAILS_PATH)


def _another_run_active() -> bool:
    """다른 수집이 최근 LOCK_STALE_SEC 안에 잠금 파일을 갱신했으면 돌고 있는 것으로 본다.
    (2026-09-24: 창을 다시 켤 때마다 새로 시작해 3개가 동시에 돌며 같은 파일을 덮어쓴 일이 있었음)"""
    return LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime < LOCK_STALE_SEC


def _touch_lock() -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(str(time.time()), encoding="utf-8")


def update_details(date: str, limit: int | None = None, period: str = "daily") -> int:
    """해당 날짜 랭킹에서 상세정보가 없는 상품만 가져온다. 새로 가져온 개수를 돌려준다."""
    if _another_run_active():
        print("다른 상세정보 수집이 이미 돌고 있어서 이번엔 건너뜀 (잠금: .tmp/product_details.lock)")
        return 0
    _touch_lock()
    try:
        return _update_details(date, limit, period)
    finally:
        LOCK_PATH.unlink(missing_ok=True)


def _update_details(date: str, limit: int | None, period: str = "daily") -> int:
    with open(history_path(date, period), encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    details = load_details()
    # 높은 순위부터: 도중에 멈춰도 중요한 상품이 먼저 채워지게
    todo = []
    for r in sorted(rows, key=lambda r: int(r.get("clothing_rank") or r["rank"])):
        pid = r["product_id"]
        if pid not in details and pid not in todo:
            todo.append(pid)
    if limit:
        todo = todo[:limit]
    print(f"상세정보: 새 상품 {len(todo)}개 (이미 있음 {len(details)}개)", flush=True)

    done = 0
    try:
        for pid in todo:
            info = fetch_detail(pid)
            info["fetched"] = date
            details[pid] = info
            done += 1
            if done % SAVE_EVERY == 0:
                save_details(details)
                _touch_lock()
                print(f"  {done}/{len(todo)}", flush=True)
    finally:
        save_details(details)  # 차단·오류로 멈춰도 받은 만큼은 저장
    print(f"상세정보 저장 완료: {done}개 추가 → 총 {len(details)}개")
    return done


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=today_kst())
    parser.add_argument("--limit", type=int, default=DAILY_LIMIT)
    parser.add_argument("--period", choices=list(PERIODS), default="daily")
    args = parser.parse_args()
    try:
        update_details(args.date, args.limit, args.period)
    except BlockedError as e:
        print(f"중단: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
