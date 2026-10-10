"""여러 Tool이 같이 쓰는 경로, 날짜, .env 읽기, 29CM 데이터 요청·카테고리."""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = ROOT / "data" / "history"  # 일간 랭킹 (주간·월간은 history_path로)
UPDATES_PATH = ROOT / "data" / "ranking_updates.json"
DETAILS_PATH = ROOT / "data" / "product_details.json"
RISE_REASONS_PATH = ROOT / "data" / "rise_reasons.json"  # 순위 급상승 원인 (Claude 조사, rise_reasons.py)
TMP_DIR = ROOT / ".tmp"
DOCS_DIR = ROOT / "docs"

KST = timezone(timedelta(hours=9))

# 랭킹 기간 (일간·주간·월간 모두 매일 수집, 리포트는 일간 매일 · 주간 월요일 · 월간 1일 — 무신사 리포트와 같게)
# 이름 → (29CM periodSort 값, 한글 이름, 29CM 표시, 이전 비교 말, 다음 말)
PERIODS = {
    "daily": ("ONE_DAY", "일간", "최근 1일", "어제", "내일"),
    "weekly": ("ONE_WEEK", "주간", "최근 1주일", "지난주", "다음 주"),
    "monthly": ("ONE_MONTH", "월간", "최근 1개월", "지난달", "다음 달"),
}

# 29CM 카테고리 (2026-10-11 recommend-api …/best/categories 로 확인)
GENDER_CATEGORY = {"M": 272100100, "F": 268100100}  # 남성의류, 여성의류
# 대분류 코드·이름은 무신사 리포트와 같게 (분석·리포트 도구를 같이 쓰려고)
CLOTHING_CATEGORIES = {"001": "상의", "002": "아우터", "003": "바지", "100": "원피스/스커트"}
# 수집하는 29CM 중분류: 코드 → (성별, 29CM 이름, 대분류 코드). 순서 = 리포트 버튼 순서
# 뺀 것: 홈웨어·언더웨어/이너웨어·셋업·점프수트·파티복(의류 스펙 비교에 안 맞음), 단독·해외브랜드(종류가 아니라 묶음 표시)
MID_CATEGORIES = {
    268103100: ("F", "상의", "001"), 268105100: ("F", "니트웨어", "001"), 268102100: ("F", "아우터", "002"),
    268106100: ("F", "바지", "003"), 268107100: ("F", "스커트", "100"), 268104100: ("F", "원피스", "100"),
    272103100: ("M", "상의", "001"), 272110100: ("M", "니트웨어", "001"), 272102100: ("M", "아우터", "002"),
    272104100: ("M", "하의", "003"),
}
MID_ORDER = ["상의", "니트웨어", "아우터", "바지", "하의", "스커트", "원피스"]

# 상품 단위 추가 수집 스위치. False면 run_daily가 그 단계를 건너뜀 (랭킹·분석·리포트는 그대로 돎)
# 2026-10-11: 상세정보(bff-api)를 2초 간격으로 받다가 20개째에서 403(거부) → 재시도 없이 멈춤.
#   같은 날 사장님 결정: 상세정보만 10초 간격·하루 100개로 다시 시도(product_details.DAILY_LIMIT). 또 거부되면 False로 되돌리고 보고.
#   → 05:3x 재시도: 첫 요청은 성공, 10초 뒤 두 번째 요청에서 다시 403. 속도 문제가 아님 → bff-api는 다시 쓰지 말 것.
#   → 같은 날 사장님 결정: 위장하지 않고, robots.txt가 허용한 상품 페이지(www.29cm.co.kr/products/번호)에서 같은 정보를 읽음
#     (10초 간격·하루 100개). 한 번이라도 거부되면 False로 바꾸고 보고.
#   후기(review-api)는 거부된 적 없지만 같은 회사라 보류.
DETAILS_ENABLED = True
REVIEWS_ENABLED = False

IMAGE_BASE = "https://img.29cm.co.kr"          # 랭킹·상세의 imageUrl('/item/2026xx/….jpg') 앞에 붙임
PRODUCT_URL = "https://www.29cm.co.kr/products/{id}"


def history_dir(period: str = "daily") -> Path:
    return HISTORY_DIR if period == "daily" else ROOT / "data" / f"history_{period}"


def history_path(date: str, period: str = "daily") -> Path:
    return history_dir(period) / f"{date}.csv"

# 봇 위장 없음: 누가 왜 요청하는지 그대로 밝힌다
USER_AGENT = "TwentyNineCmTrendReport/0.1 (personal research; daily, low-volume)"


class BlockedError(Exception):
    """29CM이 요청을 거부함 (401/403/429). 억지로 재시도하지 않는다."""


def get_json(url: str, allow_404: bool = False) -> dict | None:
    """JSON 요청. 거부되면 BlockedError, 일시적 네트워크 오류면 30초 뒤 한 번만 재시도."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):
                raise BlockedError(f"HTTP {e.code} — 29CM이 요청을 거부함: {url}") from e
            if e.code == 404 and allow_404:
                return None
            if attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(30)
    raise RuntimeError("unreachable")


def get_text(url: str, allow_404: bool = False) -> str | None:
    """웹페이지(HTML) 요청. 거부되면 BlockedError, 일시적 네트워크 오류면 30초 뒤 한 번만 재시도."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):
                raise BlockedError(f"HTTP {e.code} — 29CM이 요청을 거부함: {url}") from e
            if e.code == 404 and allow_404:
                return None
            if attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(30)
    raise RuntimeError("unreachable")


def today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def load_env() -> None:
    """.env의 '이름=값' 줄을 환경변수로 읽음. 이미 설정된 값(클라우드 Secrets)은 덮어쓰지 않음.
    PC에서는 OneDrive 동기화를 피해 ~/.secrets/29cm-trend.env에 둠 (없으면 폴더 안 .env)."""
    env_path = Path.home() / ".secrets" / "29cm-trend.env"
    if not env_path.exists():
        env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
