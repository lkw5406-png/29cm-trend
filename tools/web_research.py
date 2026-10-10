"""원인 조사용 웹 조회 Tool — 순위 급상승 원인(rise_reasons.py)을 조사할 때 유튜브를 빠르게 훑는다.
일반 웹 검색 결과는 한국 유튜브·SNS를 잘 못 잡아서(2026-09-27 확인), 유튜브 검색 결과·영상 설명란을 직접 읽는다.
영상 설명란에 소개 상품 목록(브랜드·상품명)이 적혀 있어 '어떤 영상이 이 상품을 소개했는지'를 확인할 수 있다.

유튜브 공식 API(YouTube Data API v3, 무료 사용량 하루 10,000)를 쓴다 (2026-09-27 사장님 결정: 막힌 걸 위장해서 뚫지 않음).
- PC: 비밀 파일(~/.secrets/29cm-trend.env — 무신사 리포트와 같은 키를 써도 됨)의 YOUTUBE_API_KEY
- 클라우드 루틴: 환경(Default)의 'API 자격 증명'이 www.googleapis.com 요청에 키 헤더를 붙여 줌 (세션은 키를 못 봄)
- 사용량: 검색 1번 = 100, 영상·채널 조회 = 1~3. 하루 검색 약 100번까지. 다 쓰거나 키가 없으면 웹페이지 읽기로 대신(요청 사이 3초)

사용법:
  python tools/web_research.py search "검색어" ["검색어2" ...] [--recent]   유튜브 검색 → 게시일·조회수·채널·제목·링크 + 설명란(검색어가 든 줄)
  python tools/web_research.py video 영상ID [키워드 ...]                      게시일·조회수 + 설명란·채널 주인 댓글(고정 댓글 상품 목록)
                                                                             (키워드가 든 줄만, 키워드 없으면 전부)
  python tools/web_research.py channel 채널ID|@핸들 [키워드 ...]              채널 최근 영상 15개 + 설명란(키워드 줄)
    --recent: 최근 14일 안에 올라온 영상만
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from common import TMP_DIR, USER_AGENT, load_env

API = "https://www.googleapis.com/youtube/v3/"
KST = timezone(timedelta(hours=9))
RECENT_DAYS = 14
MAX_RESULTS = 15
DESC_LINES = 5  # 영상마다 보여줄 설명란 줄 수

# 수집 원칙과 같게: 정직한 이름표, 브라우저 위장 금지 (2026-09-27 사장님 확인)
UA = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"}
RECENT = re.compile(r"(분|시간|[1-9]일) 전|1주 전|스트리밍")
# 웹페이지 읽기(대체 경로) 요청 사이 쉬는 시간. 쉬지 않고 연달아 부르면 429(요청 과다)로 막힘 (2026-09-27).
# 반복문으로 이 Tool을 여러 번 실행해도 지켜지게 마지막 요청 시각을 파일에 남김
REQUEST_INTERVAL_SEC = 3
LAST_REQUEST_PATH = TMP_DIR / "web_research.last"


class ApiUnavailable(Exception):
    """키가 없거나 하루 사용량을 다 씀 → 웹페이지 읽기로 대신."""


# ---------- 공식 API ----------

def api(path: str, **params) -> dict:
    headers = {"User-Agent": USER_AGENT}
    key = os.environ.get("YOUTUBE_API_KEY")
    if key:
        headers["X-Goog-Api-Key"] = key
    url = API + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        try:
            err = json.load(exc).get("error", {})
            reason = (err.get("errors") or [{}])[0].get("reason") or err.get("message", "")
        except ValueError:
            reason = ""
        if exc.code in (400, 401, 403, 429):
            raise ApiUnavailable(f"HTTP {exc.code} {reason}".strip()) from exc
        if exc.code == 404:  # 영상 목록이 없는 채널 등 — 빈 결과로
            return {"items": []}
        raise


def video_details(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    items = api("videos", part="snippet,statistics", id=",".join(ids)).get("items", [])
    order = {vid: i for i, vid in enumerate(ids)}
    return sorted(items, key=lambda v: order.get(v["id"], 0))


def when(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KST).strftime("%Y-%m-%d %H:%M")


def desc_lines(desc: str, kws: list[str], limit: int | None = DESC_LINES) -> list[str]:
    lines = [ln.strip() for ln in desc.splitlines() if ln.strip()]
    if kws:
        lines = [ln for ln in lines if any(k.lower() in ln.lower() for k in kws)]
    return ["      └ " + ln[:220] for ln in lines[:limit]]


def video_line(v: dict) -> str:
    s, views = v["snippet"], v.get("statistics", {}).get("viewCount")
    views = f"조회수 {int(views):,}회" if views else "조회수 ?"
    return f"  {when(s['publishedAt'])} | {views:16} | {s['channelTitle']} | {s['title']} | https://youtu.be/{v['id']}"


def search_api(q: str, recent: bool) -> list[str]:
    params = {"part": "id", "type": "video", "q": q, "maxResults": MAX_RESULTS,
              "regionCode": "KR", "relevanceLanguage": "ko"}
    if recent:
        since = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)
        params["publishedAfter"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    ids = [it["id"]["videoId"] for it in api("search", **params).get("items", []) if it["id"].get("videoId")]
    words = [w for w in re.split(r"\s+", q) if len(w) >= 2]
    out: list[str] = []
    for v in video_details(ids):
        out.append(video_line(v))
        # 설명란에서 검색어가 든 줄 (없으면 첫 줄) — 상품 목록이 있는지 바로 보이게
        out += desc_lines(v["snippet"].get("description", ""), words) or \
            desc_lines(v["snippet"].get("description", ""), [], 1)
    return out or ["  (없음)"]


def owner_comments(v: dict, kws: list[str]) -> list[str]:
    """채널 주인이 단 댓글 — 소개 상품 목록을 설명란 대신 고정 댓글에 올리는 채널이 많음 (2026-09-27 옷튜브 확인)."""
    try:
        threads = api("commentThreads", part="snippet", videoId=v["id"], order="relevance", maxResults=20).get("items", [])
    except ApiUnavailable as exc:  # 댓글을 막아 둔 영상 등 — 설명란 결과는 그대로 보여줌
        return [f"  (댓글 못 읽음: {exc})"]
    owner = v["snippet"]["channelId"]
    out: list[str] = []
    for t in threads:
        c = t["snippet"]["topLevelComment"]["snippet"]
        if c.get("authorChannelId", {}).get("value") == owner:
            # 키워드 줄이 없으면 앞 3줄 — 상품 목록 대신 큐레이터 링크만 올린 경우도 있다는 걸 알 수 있게
            out += desc_lines(c.get("textOriginal", ""), kws, None) or desc_lines(c.get("textOriginal", ""), [], 3)
    return (["  [채널 주인 댓글]"] + out) if out else []


def video_api(vid: str, kws: list[str]) -> list[str]:
    items = video_details([vid])
    if not items:
        return [f"{vid}: 영상을 못 찾음 (비공개·삭제)"]
    v = items[0]
    # 채널 번호도 보여 줌 — 핸들(@이름)이 동명 다른 채널을 잡는 일이 있어 이 번호로 `channel` 조회 (2026-10-09)
    return [video_line(v).strip() + f" | 채널 {v['snippet']['channelId']}"] + desc_lines(v["snippet"].get("description", ""), kws, None) + owner_comments(v, kws)


def channel_api(ch: str, kws: list[str]) -> list[str]:
    params = {"forHandle": ch} if ch.startswith("@") else {"id": ch}
    items = api("channels", part="snippet,contentDetails", **params).get("items", [])
    if not items:
        return [f"{ch}: 채널을 못 찾음"]
    c = items[0]
    uploads = c["contentDetails"]["relatedPlaylists"]["uploads"]
    ids = [it["contentDetails"]["videoId"] for it in
           api("playlistItems", part="contentDetails", playlistId=uploads, maxResults=MAX_RESULTS).get("items", [])]
    out = [f"{c['snippet']['title']} (채널 {c['id']})"]
    if not ids:
        # 핸들(@이름)이 영상 없는 빈 채널을 가리키는 경우가 있음 (2026-10-08 @쩡혁) — 그 사람 영상 하나를 video로 열어 진짜 채널 번호를 찾을 것
        out.append("  (이 채널엔 공개 영상이 0개 — 동명 빈 채널일 수 있음. 그 사람 영상을 video로 열어 채널 번호를 확인해 다시)")
    for v in video_details(ids):
        out.append("\n" + video_line(v))
        if kws:
            out += desc_lines(v["snippet"].get("description", ""), kws)
    return out


# ---------- 대체 경로: 웹페이지 읽기 (API를 못 쓸 때만) ----------

def fetch(url: str) -> str:
    try:
        last = float(LAST_REQUEST_PATH.read_text())
    except (OSError, ValueError):
        last = 0.0
    time.sleep(max(0.0, min(REQUEST_INTERVAL_SEC, last + REQUEST_INTERVAL_SEC - time.time())))
    TMP_DIR.mkdir(exist_ok=True)
    LAST_REQUEST_PATH.write_text(str(time.time()))
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read().decode("utf-8", "ignore")


def text(o: dict) -> str:
    return o.get("simpleText") or "".join(r.get("text", "") for r in o.get("runs", []))


def search_html(q: str, recent: bool) -> list[str]:
    raw = fetch("https://www.youtube.com/results?hl=ko&gl=KR&search_query=" + urllib.parse.quote(q))
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", raw)
    if not m:
        return ["  (검색 결과를 못 읽음 — 유튜브가 막았거나 형식이 바뀜. 웹 검색으로 대신)"]
    out: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            v = o.get("videoRenderer")
            if v:
                pub = text(v.get("publishedTimeText", {}))
                if not recent or RECENT.search(pub):
                    out.append(f"  {pub:10} | {text(v.get('viewCountText', {})):14} | {text(v.get('ownerText', {}))} | "
                               f"{text(v.get('title', {}))} | https://youtu.be/{v.get('videoId')}")
                    # 설명란 발췌(검색어가 걸린 부분) — 영상 페이지가 429로 막혀도 설명란에 상품이 있는지 볼 수 있음
                    snip = " ".join(text(s.get("snippetText", {})) for s in v.get("detailedMetadataSnippets", []))
                    if snip.strip():
                        out.append(f"      └ 설명란 발췌: {snip.strip()[:200]}")
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(json.loads(m.group(1)))
    return out[:MAX_RESULTS] or ["  (없음)"]


def video_html(vid: str, kws: list[str]) -> list[str]:
    raw = fetch(f"https://www.youtube.com/watch?v={vid}&hl=ko")
    m = re.search(r"var ytInitialPlayerResponse = (\{.*?\});(?:var|</script>)", raw)
    if not m:
        return [f"{vid}: 영상 정보를 못 읽음"]
    p = json.loads(m.group(1))
    vd, mf = p.get("videoDetails", {}), p.get("microformat", {}).get("playerMicroformatRenderer", {})
    lines = [f"{mf.get('publishDate', '')[:16]} | {vd.get('viewCount')}회 | {vd.get('author')} | {vd.get('title')} | https://youtu.be/{vid}"]
    lines += ["    " + ln.strip()[:220] for ln in vd.get("shortDescription", "").splitlines()
              if ln.strip() and (not kws or any(k.lower() in ln.lower() for k in kws))]
    return lines


def channel_html(ch: str, kws: list[str]) -> list[str]:
    ns = {"a": "http://www.w3.org/2005/Atom", "m": "http://search.yahoo.com/mrss/", "yt": "http://www.youtube.com/xml/schemas/2015"}
    root = ET.fromstring(fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={ch}"))
    lines = [root.findtext("a:title", namespaces=ns) or ch]
    for e in root.findall("a:entry", ns):
        g = e.find("m:group", ns)
        stats = g.find("m:community/m:statistics", ns)
        lines.append(f"\n{e.findtext('a:published', namespaces=ns)[:16]} | {stats.get('views') if stats is not None else '?'}회 | "
                     f"{e.findtext('a:title', namespaces=ns)} | https://youtu.be/{e.findtext('yt:videoId', namespaces=ns)}")
        lines += ["    " + ln.strip()[:220] for ln in g.findtext("m:description", default="", namespaces=ns).splitlines()
                  if ln.strip() and kws and any(k.lower() in ln.lower() for k in kws)]
    return lines


MODES = {"search": (search_api, search_html), "video": (video_api, video_html), "channel": (channel_api, channel_html)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=list(MODES))
    ap.add_argument("args", nargs="+")
    ap.add_argument("--recent", action="store_true")
    # '-'로 시작하는 영상 ID(예: -m01BbhrCoY)를 옵션으로 오해하지 않게 '--recent' 밖의 인자는 '--' 뒤로 보냄
    argv = sys.argv[1:]
    recent = [x for x in argv if x == "--recent"]
    rest = [x for x in argv if x not in ("--recent", "--")]
    a = ap.parse_args(recent + ["--"] + rest if rest and rest[0] not in ("-h", "--help") else argv)
    sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    by_api, by_html = MODES[a.mode]
    use_api = True

    def run(*args) -> list[str]:
        nonlocal use_api
        if use_api:
            try:
                return by_api(*args)
            except ApiUnavailable as exc:
                use_api = False
                print(f"(유튜브 공식 API 사용 불가: {exc} → 웹페이지 읽기로 대신. 설명란이 덜 보일 수 있음)", flush=True)
        return by_html(*args)

    try:
        if a.mode == "search":
            for q in a.args:
                print(f"\n=== {q}\n" + "\n".join(run(q, a.recent)), flush=True)
        else:
            print("\n".join(run(a.args[0], a.args[1:])))
    except Exception as exc:  # 막히면 멈추고 알림 — 억지로 재시도하지 않음
        print(f"조회 실패: {exc} — 웹 검색(WebSearch)으로 대신 조사")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
