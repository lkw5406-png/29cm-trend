"""사진 판독 도우미 Tool — 리포트 '디자인 참고'의 아이템 카드·대분류별 순위에 들어가는데 칸이 빈 상품만 골라 사진을 받아 두고,
Claude가 사진을 보고 쓴 판독 결과를 검사해서 data/photo_labels.json에 합친다.

- 이미 판독한 칸(photo_labels.json에 그 칸이 있는 상품)은 다시 고르지 않음 → 매일 새로 들어온 상품·새로 필요해진 칸만.
- 아이템 카드 칸 = 실루엣·기장(팬츠류만), 원단, 핏, 소재, 컬러, 디테일 (analyze_trends.ITEM_PROFILE_GROUPS와 같음)
- 대분류(아우터·상의·하의) 칸 = 핏, 실루엣·기장(전부), 디테일, 컬러 (analyze_trends.BIG_CATEGORY_GROUPS)
  상의·아우터 기장이 평범하면 '레귤러 기장', 디테일이 없으면 '무지/베이직'.
- 판독이 확실하지 않은 칸은 빈 목록([])으로 적으면 '봤지만 모름'으로 기록되어 다시 고르지 않음.
- 사진은 .tmp/photos/ 에만 (공개 저장소에 올리지 않음).

판독 결과 파일 형식(.tmp/photo_batchN.json): {"상품번호": {"texture": ["니트"], "detail": ["무지/베이직"], ...}, ...}
  빈 칸으로 나온 그룹만 적으면 됨. 이름은 tools/attribute_keywords.json의 이름표만.

사용법:
  python tools/photo_queue.py --next [--date YYYY-MM-DD] [--size 27]   다음 묶음 → .tmp/photo_queue.json (+ 9장씩 모은 판독용 이미지)
  python tools/photo_queue.py --merge .tmp/photo_batchN.json           검사 후 합치기
  python tools/photo_queue.py --check [--date YYYY-MM-DD]              남은 개수
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_trends import (BIG_CATEGORY_GROUPS, ITEM_PROFILE_GROUPS, ITEM_PROFILE_MIN, SILHOUETTE_PROFILE_CODES,
                            big_category, build_products, load_day, load_details)
from classify_attributes import GROUP_LABELS, KEYWORDS_PATH, PHOTO_LABELS_PATH
from common import PERIODS, TMP_DIR, USER_AGENT, today_kst

PHOTO_DIR = TMP_DIR / "photos"
QUEUE_PATH = TMP_DIR / "photo_queue.json"
SHEET_SIZE = 9  # 판독용 이미지 한 장에 사진 9개 (3x3)


def vocab() -> dict[str, set[str]]:
    kw = json.loads(KEYWORDS_PATH.read_text(encoding="utf-8"))
    return {g: set(kw[g].keys()) if isinstance(kw[g], dict) else set(kw[g]) for g in ITEM_PROFILE_GROUPS}


def load_labels() -> dict:
    return json.loads(PHOTO_LABELS_PATH.read_text(encoding="utf-8")) if PHOTO_LABELS_PATH.exists() else {}


def needed_groups(p: dict, in_card: bool) -> set[str]:
    """이 상품이 리포트에서 채워져야 하는 칸.
    - 아이템 카드: ITEM_PROFILE_GROUPS (실루엣은 팬츠류만)
    - 대분류별 핏·실루엣·디테일·컬러: 아우터·상의·하의 전부 (2026-09-27 사장님 지시 "대분류별 파악률 100%")"""
    need = set()
    if in_card:
        need |= {g for g in ITEM_PROFILE_GROUPS
                 if not (g == "silhouette" and p["category_code"] not in SILHOUETTE_PROFILE_CODES)}
    if big_category(p):
        need |= set(BIG_CATEGORY_GROUPS)
    return need


def targets(date: str) -> list[dict]:
    """오늘 리포트(일간·주간·월간 중 기록이 있는 것)의 아이템 카드·대분류 상품 중, 아직 사진으로 안 본 빈 칸이 있는 상품.
    칸 단위로 봄: 예전에 다른 칸만 판독한 상품도 새로 필요해진 칸이 비어 있으면 다시 고름. 순위 높은 순."""
    labels, details = load_labels(), load_details()
    found: dict[str, dict] = {}
    for period in PERIODS:
        rows = load_day(date, period)
        if not rows:
            continue
        for gender, products in build_products(rows, details).items():
            type_count: dict[str, int] = {}
            for p in products:
                type_count[p["item_type"]] = type_count.get(p["item_type"], 0) + 1
            for p in products:
                pid = p["product_id"]
                seen = labels.get(pid, {})
                need = needed_groups(p, type_count[p["item_type"]] >= ITEM_PROFILE_MIN)
                blanks = [g for g in ITEM_PROFILE_GROUPS if g in need and not p[g] and g not in seen]
                if not blanks:
                    continue
                if pid in found:
                    found[pid]["blanks"] = [g for g in ITEM_PROFILE_GROUPS if g in blanks or g in found[pid]["blanks"]]
                    found[pid]["rank"] = min(found[pid]["rank"], p["clothing_rank"])
                    continue
                found[pid] = {"product_id": pid, "name": p["product_name"], "brand": p["brand"],
                              "item_type": p["item_type"], "category": p["category_name"], "gender": gender,
                              "rank": p["clothing_rank"], "blanks": blanks, "image_url": p["image_url"]}
    return sorted(found.values(), key=lambda x: x["rank"])


def download(url: str, path: Path) -> bool:
    if path.exists() and path.stat().st_size > 0:
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r:
            path.write_bytes(r.read())
        time.sleep(0.3)
        return True
    except Exception as e:
        print(f"  사진 못 받음 {path.stem}: {e}")
        return False


def make_sheets(items: list[dict]) -> list[str]:
    """사진 9개씩 번호를 붙여 한 장으로 (Pillow가 있을 때만). 없으면 사진을 하나씩 보면 됨."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("Pillow 없음 → 판독용 묶음 이미지 없이 사진을 하나씩 봄 (.tmp/photos/)")
        return []
    sheets = []
    for s in range(0, len(items), SHEET_SIZE):
        chunk = items[s:s + SHEET_SIZE]
        sheet = Image.new("RGB", (3 * 400, 3 * 480), "white")
        draw = ImageDraw.Draw(sheet)
        for i, it in enumerate(chunk):
            try:
                im = Image.open(it["photo"]).convert("RGB")
                im.thumbnail((400, 460))
                x, y = (i % 3) * 400, (i // 3) * 480
                sheet.paste(im, (x, y + 20))
                draw.rectangle([x, y, x + 60, y + 22], fill="black")
                draw.text((x + 6, y + 4), f"#{it['no']}", fill="white")
            except Exception:
                pass
        out = TMP_DIR / f"photo_sheet_{s // SHEET_SIZE + 1}.jpg"
        sheet.save(out, quality=85)
        sheets.append(str(out.relative_to(TMP_DIR.parent)))
    return sheets


def validate(batch: dict, v: dict) -> list[str]:
    errs = []
    for pid, lab in batch.items():
        if not isinstance(lab, dict):
            errs.append(f"{pid}: 값은 {{그룹: [이름표]}}")
            continue
        for g, vals in lab.items():
            if g not in v:
                errs.append(f"{pid}: 모르는 그룹 '{g}' (쓸 수 있는 것: {', '.join(ITEM_PROFILE_GROUPS)})")
            elif not isinstance(vals, list) or any(x not in v[g] for x in vals):
                errs.append(f"{pid}: {GROUP_LABELS[g]} 이름표 밖 {[x for x in vals if x not in v[g]] if isinstance(vals, list) else vals}")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=today_kst())
    ap.add_argument("--next", action="store_true")
    ap.add_argument("--size", type=int, default=27)
    ap.add_argument("--merge")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if args.merge:
        batch = json.loads(Path(args.merge).read_text(encoding="utf-8"))
        errs = validate(batch, vocab())
        if errs:
            print("판독 묶음에 문제가 있어 합치지 않음:\n  " + "\n  ".join(errs[:40]))
            return 1
        labels = load_labels()
        for pid, lab in batch.items():
            labels[pid] = {**labels.get(pid, {}), **lab, "checked": args.date}  # 예전에 판독한 다른 칸은 그대로 둠
        tmp = PHOTO_LABELS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(labels, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(PHOTO_LABELS_PATH)
        print(f"합침: {len(batch)}개 → 사진 판독 기록 {len([k for k in labels if not k.startswith('_')])}개, "
              f"남은 것 {len(targets(args.date))}개")
        return 0

    todo = targets(args.date)
    if args.next:
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        pick = todo[:args.size]
        for i, it in enumerate(pick, 1):
            it["no"] = i
            path = PHOTO_DIR / f"{it['product_id']}.jpg"
            # 29CM 원본은 1000px → 판독에 충분한 600px로 받음
            it["photo"] = str(path) if download(it["image_url"] + "?width=600", path) else ""
        pick = [it for it in pick if it["photo"]]
        sheets = make_sheets(pick)
        QUEUE_PATH.write_text(json.dumps({"sheets": sheets, "items": pick}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"빈 칸 있는 새 상품 {len(todo)}개 중 {len(pick)}개 → .tmp/photo_queue.json"
              + (f", 판독용 이미지 {len(sheets)}장" if sheets else ""))
        return 0

    from collections import Counter
    print(f"{args.date}: 빈 칸 있는 새 상품 {len(todo)}개 / 빈 칸 {sum(len(t['blanks']) for t in todo)}개 "
          f"({', '.join(f'{GROUP_LABELS[g]} {n}' for g, n in Counter(g for t in todo for g in t['blanks']).most_common())})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
