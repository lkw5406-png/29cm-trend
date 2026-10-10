"""상품 → 아이템 종류 / 핏 / 실루엣 / 주원료 / 원단·가공 / 컬러 / 디테일 / 두께 분류 Tool.

우선순위
- 아이템 종류: 랭킹·상세정보의 29CM 소분류(product_details.ITEM_TYPES로 이름 통일) → 없으면 상품명 키워드
- 핏: 29CM에는 판매자 입력 핏 칸이 없음 → 상품명 키워드부터
- 주원료: 제품 소재 표기(상세정보) → 없으면 상품명 키워드
- 실루엣·기장: 상품명(+영문명) 키워드 → 없으면 상세 설명 글 키워드
  → 그래도 기장이 비면 공식 소분류(미니/미디/롱스커트 등) → 실측 사이즈표 (size_silhouette)
  → 모양(와이드/스트레이트 등)이 비면 실측 사이즈표 (바지만)
- 원단/가공: 상품명(+영문명) 키워드 → 상세 설명 글 키워드 → 아이템 종류·주원료로 정해지는 원단 (IMPLIED_TEXTURE)
- 핏: 판매자 입력 → 상품명 키워드 → 상세 설명 글 키워드 → 실측 사이즈표 가슴·허벅지 폭 (size_fit)
- 컬러: 상품명(+영문명) 키워드 → 판매 옵션의 색상 목록 (여러 색 상품은 판매 중인 색 모두)
- 디테일: 상품명(+영문명) 키워드
- 두께: 상세정보
- 실루엣·원단·핏·소재·컬러·디테일이 끝까지 비면: 사진 판독 기록(data/photo_labels.json, 사장님 요청 시 Claude가 수동 판독)
  → 그래도 빈 티셔츠 원단은 저지 (DEFAULT_TEXTURE)

키워드 사전: tools/attribute_keywords.json (분류가 빠지면 이 파일에 키워드를 추가)

사용법(점검용): python tools/classify_attributes.py data/history/2026-09-24.csv
"""
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

KEYWORDS_PATH = Path(__file__).resolve().parent / "attribute_keywords.json"
PHOTO_LABELS_PATH = Path(__file__).resolve().parent.parent / "data" / "photo_labels.json"
KEYWORD_GROUPS = ("fit", "silhouette", "fiber", "texture", "color", "detail")
GROUPS = ("item_type", "fit", "silhouette", "fiber", "texture", "color", "detail", "thickness")
LENGTH_LABELS = {"롱/맥시 기장", "미디 기장", "레귤러 기장", "미니/숏 기장", "크롭"}
SHAPE_LABELS = {"와이드", "스트레이트", "부츠컷/플레어", "테이퍼드", "머메이드", "A라인", "H라인"}
GROUP_LABELS = {"item_type": "아이템", "fit": "핏", "silhouette": "실루엣", "fiber": "소재(주원료)",
                "texture": "원단·가공", "color": "컬러", "detail": "디테일", "thickness": "두께"}


def _compile(keyword: str) -> re.Pattern:
    if keyword.startswith("re:"):
        return re.compile(keyword[3:], re.IGNORECASE)
    if re.fullmatch(r"[A-Za-z0-9 ./\-]+", keyword):
        # 영어는 단어 경계로: 'red'가 'tiered' 안에서 잡히지 않게
        return re.compile(r"(?<![a-z])" + re.escape(keyword.lower()) + r"(?![a-z])", re.IGNORECASE)
    return re.compile(re.escape(keyword), re.IGNORECASE)


@lru_cache(maxsize=1)
def load_rules() -> dict:
    raw = json.loads(KEYWORDS_PATH.read_text(encoding="utf-8"))
    rules = {"item_type": {}}
    for cat_code, types in raw["item_type"].items():
        rules["item_type"][cat_code] = [(name, [_compile(k) for k in kws]) for name, kws in types.items()]
    for group in KEYWORD_GROUPS:
        rules[group] = [(name, _compile(k)) for name, kws in raw[group].items() for k in kws]
    return rules


@lru_cache(maxsize=1)
def load_photo_labels() -> dict:
    if not PHOTO_LABELS_PATH.exists():
        return {}
    return {k: v for k, v in json.loads(PHOTO_LABELS_PATH.read_text(encoding="utf-8")).items() if not k.startswith("_")}


OTHER_TYPE = {"001": "기타 상의", "002": "기타 아우터", "003": "기타 하의"}  # 무신사 공식 소분류 이름


def _item_type(name: str, category_code: str, category_name: str) -> str:
    """상세정보가 없을 때만 쓰는 대체 분류. 이름은 무신사 공식 소분류와 같게 맞춰 둠."""
    for type_name, patterns in load_rules()["item_type"].get(category_code, []):
        if any(p.search(name) for p in patterns):
            return type_name
    return OTHER_TYPE.get(category_code, f"기타 {category_name}")


def keywords(text: str, group: str) -> list[str]:
    """겹치는 매칭은 긴 쪽이 이김 ('크림화이트'는 화이트가 아니라 아이보리/크림)."""
    hits = []
    for canonical, pattern in load_rules()[group]:
        for m in pattern.finditer(text or ""):
            hits.append((m.end() - m.start(), m.start(), m.end(), canonical))
    hits.sort(key=lambda h: (-h[0], h[1]))
    taken: list[tuple[int, int]] = []
    found: list[str] = []
    for _, start, end, canonical in hits:
        if any(start < t_end and end > t_start for t_start, t_end in taken):
            continue
        taken.append((start, end))
        if canonical not in found:
            found.append(canonical)
    return found


# 아이템 종류 자체가 원단을 말해 주는 경우 (상품명·설명에 원단 단서가 없을 때만)
IMPLIED_TEXTURE = {
    "데님 팬츠": "데님", "트러커 재킷": "데님",
    "니트/스웨터": "니트", "카디건": "니트",
    "맨투맨/스웨트": "테리/스웨트", "후드 티셔츠": "테리/스웨트", "후드 집업": "테리/스웨트",
    "플리스/뽀글이": "플리스/보아", "무스탕/퍼": "퍼/시어링", "레더/라이더스 재킷": "레더",
    "숏패딩/헤비 아우터": "다운/패딩", "롱패딩/헤비 아우터": "다운/패딩", "경량 패딩/패딩 베스트": "다운/패딩",
    "피케/카라 티셔츠": "피케",
    # 29CM 니트웨어·데님 종류
    "크루넥 니트": "니트", "브이넥 니트": "니트", "터틀넥 니트": "니트", "니트 베스트": "니트", "니트 폴로": "니트",
    "캐시미어 니트": "니트", "니트 집업": "니트", "니트 후드": "니트", "기타 니트": "니트",
    "데님 스커트": "데님", "데님 원피스": "데님",
}
FIBER_TEXTURE = {"가죽": "레더", "합성피혁": "레더"}
# 티셔츠는 원단 표기가 거의 없고(대부분 '면 100%'만) 기본 원단이 저지. 상품명·설명·사진 판독으로도 못 찾으면 저지로 봄
# (2026-09-25 티셔츠 58개 사진 판독: 50개 민무늬 저지, 골지 3 · 슬럽 2 · 플리스·스웨트·레이스 각 1)
DEFAULT_TEXTURE = {"긴소매 티셔츠": "저지/인터록", "반소매 티셔츠": "저지/인터록", "민소매 티셔츠": "저지/인터록"}
# 소분류(아이템 종류)가 기장을 정해 주는 경우
CATEGORY_LENGTH = {"미니스커트": "미니/숏 기장", "미니원피스": "미니/숏 기장", "미디스커트": "미디 기장",
                   "미디원피스": "미디 기장", "롱스커트": "롱/맥시 기장", "맥시원피스": "롱/맥시 기장",
                   "롱 코트": "롱/맥시 기장", "하프 코트": "미디 기장", "숏 코트": "레귤러 기장"}
# 29CM 바지 소분류는 모양으로 나뉨 → 상품명에 모양이 없으면 소분류를 따름
CATEGORY_SHAPE = {"와이드 팬츠": "와이드", "스트레이트 팬츠": "스트레이트", "부츠컷 팬츠": "부츠컷/플레어"}
CATEGORY_FIT = {"슬림 팬츠": "슬림"}

# 실측 사이즈표 기준 (가운데 사이즈, cm). 이름에 실루엣이 적힌 상품들의 실제 치수로 정함 (2026-09-25, 의류 556개)
# - 바지: 와이드/스트레이트는 밑단 비율이 아니라 허벅지 폭에서 갈림 (이름에 와이드 중앙값 36cm, 스트레이트 31cm).
#   부츠컷은 허벅지가 좁고(중앙값 29.5cm) 밑단이 허벅지와 비슷하게 넓음, 테이퍼드는 밑단이 허벅지의 0.6배 미만.
# - 상의 '크롭'은 판매자 표현일 뿐 실제 길이 차이가 작음(총장÷가슴 1.14 vs 일반 1.20) → 확실히 짧은 것만 크롭으로.
TOP_CROP_RATIO = 1.0        # 상의·아우터: 총장 ÷ 가슴단면이 이보다 작고
TOP_CROP_MAX_LENGTH = 58    # … 총장이 이 이하일 때만 크롭
OUTER_LONG_LENGTH = 95      # 아우터: 총장이 이 이상이면 롱 기장
OUTER_MIDI_LENGTH = 80      # 아우터: 총장이 이 이상(롱 미만)이면 미디 기장(하프 코트 등)
# 그 밖(크롭도 롱도 아닌 상의·아우터)은 레귤러 기장. 이름에 기장이 없는 상의·아우터 총장은 대부분 55~75cm
# (2026-09-27 상의 368개 중앙값 66.5, 아우터 552개 중앙값 65) — 사장님 지시 "대분류별 실루엣 파악률 100%"
TOP_REGULAR_MAX_LENGTH = 80 # 상의: 총장이 이 미만이면 레귤러 기장 (그 이상은 사진 판독)
PANTS_TAPERED = 0.6         # 바지: 밑단단면 ÷ 허벅지단면이 이보다 작으면 테이퍼드
PANTS_FLARE = 0.95          # … 이 이상이면서 허벅지가 PANTS_WIDE_THIGH 미만이면 부츠컷/플레어
PANTS_WIDE_THIGH = 33       # … 허벅지단면이 이 이상이면 와이드
PANTS_STRAIGHT_THIGH = 31   # … 이 미만이면 스트레이트 (31~33cm는 판단 보류)


def size_silhouette(size: dict | None, category_code: str) -> list[str]:
    """실측 사이즈표로 기장(크롭/롱)과 바지 모양을 추정. 판단할 치수가 없으면 []."""
    if not size:
        return []
    out = []
    length, chest = size.get("총장"), size.get("가슴단면")
    if category_code in ("001", "002") and length and chest:
        if length / chest < TOP_CROP_RATIO and length <= TOP_CROP_MAX_LENGTH:
            out.append("크롭")
        elif category_code == "002" and length >= OUTER_LONG_LENGTH:
            out.append("롱/맥시 기장")
        elif category_code == "002" and length >= OUTER_MIDI_LENGTH:
            out.append("미디 기장")
        elif category_code == "002" or length < TOP_REGULAR_MAX_LENGTH:
            out.append("레귤러 기장")
    thigh, hem = size.get("허벅지단면"), size.get("밑단단면")
    if category_code == "003" and thigh and hem and (length or 0) >= 80:  # 반바지는 모양 판단 안 함
        ratio = hem / thigh
        if ratio < PANTS_TAPERED:
            out.append("테이퍼드")
        elif thigh >= PANTS_WIDE_THIGH:
            out.append("와이드")
        elif ratio >= PANTS_FLARE:
            out.append("부츠컷/플레어")
        elif thigh < PANTS_STRAIGHT_THIGH:
            out.append("스트레이트")
    return out


# 상의·아우터에 붙은 '스트레이트 지퍼', '커브드 소매', '피쉬테일 파카' 등은 몸판 실루엣이 아님
TOP_SILHOUETTES = LENGTH_LABELS | {"와이드", "A라인"}
PANTS_SILHOUETTES = LENGTH_LABELS | SHAPE_LABELS - {"A라인", "H라인", "머메이드"} | {"하이웨이스트", "로우라이즈"}


def _allowed(labels: list[str], category_code: str) -> list[str]:
    allow = {"001": TOP_SILHOUETTES, "002": TOP_SILHOUETTES, "003": PANTS_SILHOUETTES}.get(category_code)
    return [x for x in labels if allow is None or x in allow]


# 핏 추정 기준: 판매자가 핏을 입력한 상품들의 가운데 사이즈 가슴단면(상의·아우터)·허벅지단면(바지), cm (2026-09-25)
# 예) 남성·공용 상의 레귤러 중앙값 56, 루즈 62 / 여성 상의 슬림 42, 레귤러 45~54, 루즈 60
#     바지는 핏끼리 허벅지 폭이 거의 같아(레귤러 34 vs 루즈 36) 아주 넓거나 좁은 것만 판단
FIT_BY_SIZE = {  # (카테고리, 여성?) → (이 이하면 슬림, 이 이상이면 루즈), 그 사이는 레귤러
    ("001", False): (50, 61), ("001", True): (44, 57),
    ("002", False): (52, 64), ("002", True): (46, 58),
    ("003", False): (28, 39), ("003", True): (27, 38),
}
FIT_REGULAR_FROM_SIZE = {"001", "002"}  # 바지는 레귤러 판단은 안 함 (구분이 안 돼서)


def size_fit(size: dict | None, category_code: str, sex: list[str]) -> list[str]:
    if not size:
        return []
    women = sex == ["여성"]
    width = size.get("허벅지단면") if category_code == "003" else size.get("가슴단면")
    rule = FIT_BY_SIZE.get((category_code, women))
    if not width or not rule:
        return []
    slim, loose = rule
    if width <= slim:
        return ["슬림"]
    if width >= loose:
        return ["루즈"]
    return ["레귤러"] if category_code in FIT_REGULAR_FROM_SIZE else []


def silhouette(name: str, desc: str, item_type: str, category_code: str, size: dict | None) -> list[str]:
    found = (_allowed(keywords(name, "silhouette"), category_code)
             or _allowed(keywords(desc, "silhouette"), category_code))
    if not any(f in LENGTH_LABELS for f in found) and item_type in CATEGORY_LENGTH:
        found.append(CATEGORY_LENGTH[item_type])
    if not any(f in SHAPE_LABELS for f in found) and item_type in CATEGORY_SHAPE:
        found.append(CATEGORY_SHAPE[item_type])
    for label in size_silhouette(size, category_code):
        axis = LENGTH_LABELS if label in LENGTH_LABELS else SHAPE_LABELS
        if not any(f in axis for f in found):
            found.append(label)
    return found


def texture(name: str, desc: str, item_type: str, fiber: list[str]) -> list[str]:
    """설명 글은 배송·세탁 안내까지 섞여 있어 덜 정확하므로 아이템 종류로 정해지는 원단을 먼저 씀."""
    found = keywords(name, "texture")
    if found:
        return found
    implied = IMPLIED_TEXTURE.get(item_type) or next((FIBER_TEXTURE[f] for f in fiber if f in FIBER_TEXTURE), None)
    return [implied] if implied else keywords(desc, "texture")


def clean_desc(desc: str) -> str:
    """2026-09-25 이전 수집분엔 CSS 코드('padding: 0 …')가 섞여 있어 '패딩'으로 잘못 잡힘 → 제거."""
    return re.sub(r"[^{}]*\{[^}]*\}", " ", desc or "")


def classify(product_name: str, category_code: str, category_name: str, detail: dict | None = None,
             product_id: str = "") -> dict:
    d = detail if detail and not detail.get("missing") else {}
    name = " / ".join(x for x in (product_name, d.get("name_eng")) if x)  # 영문명은 2026-09-25 이후 수집분만
    desc = clean_desc(d.get("desc", ""))

    cat2 = d.get("category2", "")
    item_type = cat2 if cat2 and str(d.get("category2_code", "")).startswith(category_code) else \
        _item_type(name, category_code, category_name)

    photo = load_photo_labels().get(product_id, {})
    fiber = [d["main_fiber"]] if d.get("main_fiber") else (keywords(name, "fiber") or photo.get("fiber", []))
    return {
        "item_type": item_type,
        "fit": (d.get("fit") or keywords(name, "fit") or keywords(desc, "fit")
                or ([CATEGORY_FIT[item_type]] if item_type in CATEGORY_FIT else [])
                or size_fit(d.get("size"), category_code, d.get("sex") or []) or photo.get("fit", [])),
        "silhouette": silhouette(name, desc, item_type, category_code, d.get("size")) or photo.get("silhouette", []),
        "fiber": fiber,
        "texture": (texture(name, desc, item_type, fiber) or photo.get("texture", [])
                    or ([DEFAULT_TEXTURE[item_type]] if item_type in DEFAULT_TEXTURE else [])),
        "color": (keywords(name, "color") or keywords(" / ".join(d.get("colors") or []), "color")
                  or photo.get("color", [])),
        "detail": keywords(name, "detail") or photo.get("detail", []),
        "thickness": d.get("thickness") or [],
    }


def main() -> int:
    import csv

    from common import DETAILS_PATH

    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    details = json.loads(DETAILS_PATH.read_text(encoding="utf-8")) if DETAILS_PATH.exists() else {}
    with open(sys.argv[1], encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    seen, samples = set(), []
    missing = {g: 0 for g in GROUPS}
    for r in rows:
        if r["product_id"] in seen:
            continue
        seen.add(r["product_id"])
        c = classify(r["product_name"], r["category_code"], r["category_name"], details.get(r["product_id"]),
                     r["product_id"])
        for g in GROUPS:
            missing[g] += not c[g]
        if len(samples) < 30:
            samples.append((r["product_name"], c))
    for name, c in samples:
        print(f"- {name}\n    {c}")
    n = len(seen)
    have = sum(1 for pid in seen if pid in details)
    print(f"\n고유 상품 {n}개 (상세정보 있음 {have}개)")
    for g in GROUPS:
        print(f"  {GROUP_LABELS[g]} 미분류: {missing[g] / n:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
