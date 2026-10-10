"""분석 결과(JSON) → 리포트 웹페이지(HTML) Tool.

출력 (GitHub Pages가 docs/ 폴더를 그대로 공개 — 주소 하나로 계속 운영):
  docs/index.html               최신 일간 리포트 (사장님이 보는 고정 주소)
  docs/reports/YYYY-MM-DD.html  날짜별 일간 리포트 (페이지 위 날짜 선택으로 이동)
  docs/dates.json               리포트가 있는 날짜 목록
  docs/weekly/…, docs/monthly/…  주간·월간 (같은 구조). 페이지 위 [일간|주간|월간] 전환으로 이동

사용법:
  python tools/build_report.py [--date YYYY-MM-DD] [--period daily|weekly|monthly]
  python tools/build_report.py --date YYYY-MM-DD --standalone 파일.html   사진을 품은 한 장짜리 페이지
"""
import argparse
import base64
import json
import re
import sys
import time
import urllib.request
from html import escape

from analyze_trends import analysis_path
from common import DOCS_DIR, IMAGE_BASE, PERIODS, USER_AGENT, today_kst

CHART_ROWS = 8
SCOPE_TEXT = {
    "overall": "29CM 남성의류·여성의류 베스트({span}) · 의류 전체 TOP 100 + 종류별(상의·니트웨어·아우터·바지·스커트·원피스) TOP 100",
    "category": "29CM 베스트({span})",
}
PERIOD_DIRS = {"daily": "", "weekly": "weekly/", "monthly": "monthly/"}  # docs/ 아래 기간별 폴더
# 지금 만드는 리포트의 비교 말 (render()가 기간에 맞게 바꿈): 일간 어제/내일, 주간 지난주/다음 주, 월간 지난달/다음 달
WORD = {"prev": "어제", "next": "내일"}

CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --bar: #2a78d6; --accent: #2a78d6; --up-text: #006300; --down-text: #b3261e;
  --c1: #2a78d6; --c2: #eb6834; --c3: #1baf7a; --c4: #eda100; --chip: rgba(42,120,214,0.16);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --bar: #3987e5; --accent: #3987e5; --up-text: #0ca30c; --down-text: #e66767;
    --c1: #3987e5; --c2: #d95926; --c3: #199e70; --c4: #c98500; --chip: rgba(57,135,229,0.30);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --bar: #3987e5; --accent: #3987e5; --up-text: #0ca30c; --down-text: #e66767;
  --c1: #3987e5; --c2: #d95926; --c3: #199e70; --c4: #c98500; --chip: rgba(57,135,229,0.30);
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 32px 20px 64px; }
header { display: flex; flex-wrap: wrap; gap: 12px 24px; align-items: flex-end; justify-content: space-between; }
header h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -0.01em; text-wrap: balance; }
header p { margin: 0; color: var(--ink-2); }
.pswitch { display: inline-flex; gap: 2px; padding: 3px; margin-bottom: 10px; border: 1px solid var(--border);
  border-radius: 10px; background: var(--surface); }
.pswitch a { padding: 5px 14px; border-radius: 7px; font-size: 14px; font-weight: 700; text-decoration: none; color: var(--ink-2); }
.pswitch a[aria-current] { background: var(--accent); color: #fff; }
.pswitch a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.datepick { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.datepick select { font: inherit; font-size: 15px; font-weight: 600; color: var(--ink); background: var(--surface);
  border: 1px solid var(--border); border-radius: 10px; padding: 8px 12px; min-width: 170px; }
.note { font-size: 13px; color: var(--muted); margin-top: 8px; }
/* 상단 바: 성별 전환 + 목적별 탭 (스크롤해도 위에 붙어 있음) */
.topbar { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 5; background: var(--page);
  margin: 22px 0 18px; padding: 10px 0; display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: center;
  border-bottom: 1px solid var(--grid); }
.gswitch { display: inline-flex; flex: none; padding: 3px; border: 1px solid var(--border); border-radius: 999px;
  background: var(--surface); }
.gswitch button { font: inherit; font-weight: 700; padding: 6px 16px; border: 0; border-radius: 999px; cursor: pointer;
  background: transparent; color: var(--ink-2); }
.gswitch button[aria-pressed="true"] { background: var(--ink); color: var(--page); }
.tabs { display: flex; gap: 6px; flex: 1 1 0; min-width: 0; overflow-x: auto; scrollbar-width: none; }
.tabs::-webkit-scrollbar { display: none; }
.tabs button { flex: none; white-space: nowrap; font: inherit; font-weight: 600; padding: 7px 14px; border-radius: 999px;
  cursor: pointer; border: 1px solid var(--border); background: var(--surface); color: var(--ink-2); }
.tabs button small { font-size: 11px; color: var(--muted); margin-right: 6px; font-variant-numeric: tabular-nums; }
.tabs button[aria-selected="true"] { background: var(--ink); color: var(--page); border-color: var(--ink); }
.tabs button[aria-selected="true"] small { color: inherit; opacity: 0.7; }
.tabs button:focus-visible, .gswitch button:focus-visible, .row:focus-visible, select:focus-visible,
.chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
section.card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
  padding: 20px; margin-bottom: 18px; }
section.card h2 { font-size: 18px; margin: 0 0 4px; }
section.card .sub { font-size: 13px; color: var(--muted); margin: 0 0 14px; }
.recs { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(160px, 100%), 1fr)); gap: 14px; }
.rec { text-decoration: none; color: inherit; border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
  display: flex; flex-direction: column; background: var(--surface); }
.rec:hover { border-color: var(--accent); }
.rec img { width: 100%; aspect-ratio: 1 / 1.2; object-fit: cover; background: var(--grid); display: block; }
.rec .body { padding: 10px 12px 12px; display: flex; flex-direction: column; gap: 3px; }
.rec .brand { font-size: 12px; color: var(--muted); }
.rec .name { font-size: 13px; font-weight: 600; display: -webkit-box; -webkit-line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden; }
.rec .price { font-size: 14px; font-weight: 700; }
.rec .why { font-size: 12px; color: var(--ink-2); }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr)); gap: 22px 28px; }
.charts > div, .two > div { min-width: 0; }
.chart h3 { font-size: 15px; margin: 0; }
.chart .cov { font-size: 12px; color: var(--muted); margin: 0 0 8px; }
.row { display: grid; grid-template-columns: 104px 1fr 44px 58px; align-items: center; gap: 8px;
  padding: 5px 4px; font-size: 13px; border-radius: 6px; }
.row:hover { background: color-mix(in srgb, var(--grid) 55%, transparent); }
.row .label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row .val, .row .chg { text-align: right; font-variant-numeric: tabular-nums; }
.row .val { font-weight: 600; }
.row .chg { font-size: 12px; }
.track { position: relative; height: 12px; border-left: 1px solid var(--axis); }
.bar { position: absolute; left: 0; top: 1px; height: 10px; background: var(--bar); border-radius: 0 4px 4px 0; }
.chg-head { display: grid; grid-template-columns: 104px 1fr 44px 58px; gap: 8px; font-size: 11px; color: var(--muted);
  padding: 0 4px 4px; }
.chg-head span:nth-child(3), .chg-head span:nth-child(4) { text-align: right; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--grid); vertical-align: middle; }
th { color: var(--muted); font-weight: 600; font-size: 12px; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td img { width: 40px; height: 48px; object-fit: cover; border-radius: 4px; background: var(--grid); display: block; }
td a { color: inherit; text-decoration: none; }
td a:hover { text-decoration: underline; }
.two { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(340px, 100%), 1fr)); gap: 18px; }
.up-t { color: var(--up-text); } .down-t { color: var(--down-text); } .flat-t { color: var(--muted); }
.empty { color: var(--muted); font-size: 13px; }
.sec-h { font-size: 15px; margin: 0 0 8px; }
/* 카테고리 탭 (TOP 10) */
.seg { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 14px; }
.seg button { font: inherit; font-size: 13px; font-weight: 600; padding: 6px 14px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--border); background: transparent; color: var(--ink-2); }
.seg button[aria-selected="true"] { background: var(--chip); color: var(--ink); border-color: var(--accent); }
.seg button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.topgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(170px, 100%), 1fr)); gap: 12px; }
.topcard { position: relative; text-decoration: none; color: inherit; border: 1px solid var(--border); border-radius: 12px;
  overflow: hidden; display: flex; flex-direction: column; background: var(--surface); }
.topcard:hover { border-color: var(--accent); }
.topcard .tlink { display: flex; flex-direction: column; text-decoration: none; color: inherit; }
.rv { border-top: 1px solid var(--grid); padding: 8px 11px 10px; font-size: 12px; display: flex; flex-direction: column;
  gap: 3px; color: var(--ink-2); }
.rv-h { color: var(--muted); font-variant-numeric: tabular-nums; }
.rv-s { color: var(--ink-2); }
.rv b { font-weight: 700; margin-right: 4px; }
.rv-p b { color: var(--up-text); } .rv-c b { color: var(--down-text); }
.rv-none { color: var(--muted); }
.rv details summary { cursor: pointer; color: var(--muted); margin-top: 2px; }
.rv .q { margin: 4px 0 0; padding-left: 8px; border-left: 2px solid var(--grid); line-height: 1.45; }
.rv .q.good { border-left-color: var(--up-text); } .rv .q.bad { border-left-color: var(--down-text); }
.topcard img { width: 100%; aspect-ratio: 1 / 1.15; object-fit: cover; background: var(--grid); display: block; }
.topcard .rk { position: absolute; top: 8px; left: 8px; background: var(--ink); color: var(--page); font-weight: 700;
  font-size: 13px; min-width: 26px; height: 26px; border-radius: 13px; display: grid; place-items: center; padding: 0 7px;
  font-variant-numeric: tabular-nums; }
.topcard .body { padding: 9px 11px 11px; display: flex; flex-direction: column; gap: 2px; font-size: 12px; }
.topcard .brand { color: var(--muted); }
.topcard .name { font-size: 13px; font-weight: 600; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.topcard .meta { color: var(--ink-2); }
.topcard .price { font-weight: 700; font-size: 13px; }
/* 순위가 오른 이유 (시장 동향) */
tr.has-why td { border-bottom: 0; }
tr.why-row td { padding-top: 10px; padding-bottom: 18px; }
.why { font-size: 12.5px; color: var(--ink-2); line-height: 1.6; border-left: 2px solid var(--up-text);
  padding: 4px 0 4px 12px; }
.why.none { color: var(--muted); border-left-color: var(--grid); }
.why .tags { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 6px; }
.why .tag { font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 999px; background: var(--chip); color: var(--ink); }
.why .conf { font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); }
.why .conf.ok { color: var(--up-text); border-color: var(--up-text); }
.why .src { margin-top: 6px; font-size: 11.5px; }
.why .src a { color: var(--accent); text-decoration: none; }
.why .src a:hover { text-decoration: underline; }
@media (max-width: 640px) {  /* 휴대폰: 카테고리 칸을 빼고 이유가 폭 전체를 쓰게 */
  table.movers th:nth-child(4), table.movers tr.has-why td:nth-child(4), table.movers tr.why-row td:first-child { display: none; }
  table.movers th, table.movers td { padding-left: 4px; padding-right: 4px; }
}
/* 아이템 종류별 정리 */
.types { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(440px, 100%), 1fr)); gap: 14px; }
.type-cat { font-size: 15px; margin: 22px 0 10px; color: var(--ink-2); }
.type-cat:first-of-type { margin-top: 4px; }
.tcard { border: 1px solid var(--border); border-radius: 12px; padding: 14px; display: flex; flex-direction: column; gap: 9px; min-width: 0; }
.tcard h4 { margin: 0; font-size: 16px; }
.tcard .ts { font-size: 12px; color: var(--muted); margin-top: 2px; }
.gallery { display: grid; grid-template-columns: repeat(6, 1fr); gap: 4px; }
.gallery img { width: 100%; aspect-ratio: 1 / 1.2; object-fit: cover; border-radius: 6px; background: var(--grid); display: block; }
.share-meter { height: 6px; background: var(--grid); border-radius: 3px; overflow: hidden; }
.share-meter span { display: block; height: 100%; background: var(--bar); border-radius: 0 3px 3px 0; }
.arow { display: grid; grid-template-columns: 78px 1fr; gap: 8px; align-items: start; font-size: 12px; }
.arow .al { color: var(--muted); padding-top: 3px; line-height: 1.3; }
.arow .al small { display: block; font-size: 10.5px; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip { position: relative; overflow: hidden; border: 1px solid var(--border); border-radius: 6px; padding: 2px 8px;
  background: var(--surface); white-space: nowrap; }
.chip .fill { position: absolute; top: 0; bottom: 0; left: 0; background: var(--chip); }
.chip span { position: relative; }
.chip b { font-weight: 600; font-variant-numeric: tabular-nums; }
.sw { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 4px; vertical-align: 0;
  border: 1px solid var(--border); }
.none { color: var(--muted); padding-top: 3px; }
/* 가격대 */
.medians { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 13px; color: var(--ink-2); margin-top: 12px; }
.medians b { color: var(--ink); font-variant-numeric: tabular-nums; }
/* 오늘 요약 */
.brief { margin: 0; padding-left: 1.2em; display: flex; flex-direction: column; gap: 20px; font-size: 15px; }
.brief li::marker { color: var(--muted); }
.brief .hint { font-size: 12px; color: var(--muted); margin-left: 4px; }
/* section.card .sub(카드 설명 문단)보다 우선해야 간격이 먹힘 — '.brief .sub'만 쓰면 덮여서 안 바뀜 */
section.card .brief .sub { margin: 7px 0 6px; padding-left: 1.1em; display: flex; flex-direction: column; gap: 7px;
  font-size: 13px; color: var(--muted); list-style: circle; }
.brief .sub a { color: var(--ink); text-decoration: none; }
.brief .sub a:hover { text-decoration: underline; }
.brief .sub .b { color: var(--muted); font-size: 13px; }
.brief .sub small { color: var(--muted); }
.brief .conf { font-size: 11px; font-weight: 600; padding: 0 6px; border-radius: 999px; border: 1px solid var(--border);
  color: var(--muted); white-space: nowrap; }
.brief .conf.ok { color: var(--up-text); border-color: var(--up-text); }
/* 아이템 순위표 */
.rt td { white-space: nowrap; }
.rt td:nth-child(3) { white-space: normal; min-width: 110px; }
.mini { display: flex; align-items: center; gap: 8px; min-width: 130px; }
.mini-track { flex: 1; height: 8px; background: var(--grid); border-radius: 4px; overflow: hidden; }
.mini-track span { display: block; height: 100%; background: var(--bar); }
.mini b { font-variant-numeric: tabular-nums; min-width: 32px; text-align: right; }
tr.hi td { background: color-mix(in srgb, var(--up-text) 9%, transparent); }
/* 컬러 팔레트 */
.cov { font-size: 12px; color: var(--muted); margin: 0 0 10px; }
.palette { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(128px, 100%), 1fr)); gap: 10px; }
.pcell { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; background: var(--surface); }
.pcell .sq { height: 60px; border-bottom: 1px solid var(--border); }
.pcell .pb { padding: 8px 10px 10px; display: flex; flex-direction: column; font-size: 13px; }
.pcell .pv { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }
.pcell .pn { font-size: 12px; color: var(--muted); display: flex; justify-content: space-between; gap: 6px; }
.more { display: block; margin: 14px auto 0; font: inherit; font-weight: 600; font-size: 14px; padding: 9px 22px;
  border-radius: 999px; border: 1px solid var(--border); background: var(--surface); color: var(--ink); cursor: pointer; }
.more:hover { border-color: var(--accent); }
.more:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.pal-h { margin: 18px 0 8px; } .pal-h:first-of-type { margin-top: 0; }
.pal-h small { font-weight: 400; color: var(--muted); font-size: 12px; }
/* 아이템별 가격 */
.pt tr.grp td { font-weight: 700; color: var(--ink-2); padding-top: 14px; border-bottom: 1px solid var(--axis); }
.pt td.num { white-space: nowrap; }
.pr-cell { min-width: 140px; width: 34%; }
.prange { position: relative; height: 10px; background: var(--grid); border-radius: 5px; }
.prange span { position: absolute; top: 0; bottom: 0; background: var(--chip); border: 1px solid var(--accent); border-radius: 5px; }
.prange i { position: absolute; top: -3px; bottom: -3px; width: 2px; margin-left: -1px; background: var(--ink); border-radius: 1px; }
.prange i.o { width: 0; background: none; border-left: 2px dotted var(--down-text); }
footer { margin-top: 32px; font-size: 12px; color: var(--muted); max-width: 70ch; }
#tip { position: fixed; pointer-events: none; z-index: 20; background: var(--ink); color: var(--page);
  font-size: 12px; line-height: 1.5; padding: 8px 10px; border-radius: 8px; max-width: 260px; }
@media (max-width: 480px) {
  .row, .chg-head { grid-template-columns: 84px 1fr 38px 52px; }
  .wrap { padding: 20px 16px 48px; }
  .gallery { grid-template-columns: repeat(3, 1fr); }
  .gswitch button { padding: 6px 12px; }
  .tabs { flex-basis: 100%; }
}
"""

JS = """
// 성별 × 목적 탭. 주소 끝(#여성-소재컬러)으로 화면 공유, 마지막 화면은 이 브라우저에 기억
const G = [...document.querySelectorAll('.gswitch button')], T = [...document.querySelectorAll('.tabs button')];
let curG = G.length ? G[0].dataset.g : '0', curT = T.length ? T[0].dataset.t : '';
function viewHash() { const b = G.find(x => x.dataset.g === curG); return '#' + encodeURIComponent((b ? b.dataset.name : '') + '-' + curT); }
function apply(save) {
  G.forEach(b => b.setAttribute('aria-pressed', b.dataset.g === curG));
  T.forEach(b => b.setAttribute('aria-selected', b.dataset.t === curT));
  document.querySelectorAll('.panel').forEach(p => p.hidden = !(p.dataset.g === curG && p.dataset.t === curT));
  if (save) {
    try { history.replaceState(null, '', viewHash()); } catch (e) {}
    try { localStorage.setItem('mss-view', viewHash()); } catch (e) {}
  }
}
function fromHash(h) {
  if (!h) return false;
  let s; try { s = decodeURIComponent(h.replace(/^#/, '')); } catch (e) { return false; }
  const i = s.lastIndexOf('-'); if (i < 0) return false;
  const gb = G.find(b => b.dataset.name === s.slice(0, i)), tb = T.find(b => b.dataset.t === s.slice(i + 1));
  if (!gb || !tb) return false;
  curG = gb.dataset.g; curT = tb.dataset.t; return true;
}
function toTop() { const h = document.querySelector('header'); const y = h.offsetTop + h.offsetHeight;
  if (window.scrollY > y) window.scrollTo(0, y); }
G.forEach(b => b.addEventListener('click', () => { curG = b.dataset.g; apply(true); }));
T.forEach(b => b.addEventListener('click', () => { curT = b.dataset.t; apply(true); toTop();
  b.scrollIntoView({block: 'nearest', inline: 'nearest'}); }));
let saved = null; try { saved = localStorage.getItem('mss-view'); } catch (e) {}
fromHash(location.hash) || fromHash(saved);
apply(false);
window.addEventListener('hashchange', () => { if (fromHash(location.hash)) apply(false); });

const tip = document.getElementById('tip');
function show(e) { const t = e.currentTarget.dataset.tip; if (!t) return; tip.innerHTML = t; tip.hidden = false; move(e); }
function move(e) { const r = e.currentTarget.getBoundingClientRect();
  const x = e.clientX ?? (r.left + r.width / 2), y = e.clientY ?? r.top;
  tip.style.left = Math.max(8, Math.min(x + 12, window.innerWidth - tip.offsetWidth - 8)) + 'px';
  tip.style.top = Math.max(8, y - tip.offsetHeight - 10) + 'px'; }
function hide() { tip.hidden = true; }
document.querySelectorAll('[data-tip]').forEach(el => {
  el.addEventListener('mouseenter', show); el.addEventListener('mousemove', move);
  el.addEventListener('mouseleave', hide); el.addEventListener('focus', show); el.addEventListener('blur', hide);
});

// 카테고리 탭: 같은 섹션 안의 패널만 전환
document.querySelectorAll('.seg').forEach(seg => {
  const btns = seg.querySelectorAll('button');
  btns.forEach(b => b.addEventListener('click', () => {
    btns.forEach(x => x.setAttribute('aria-selected', x === b));
    seg.parentElement.querySelectorAll('.seg-panel').forEach(p => p.hidden = p.id !== b.dataset.show);
  }));
});

// 기간 전환: 보던 성별·탭 그대로
document.querySelectorAll('.pswitch a').forEach(a => a.addEventListener('click', ev => {
  ev.preventDefault(); location.href = a.getAttribute('href') + viewHash();
}));

// 인기 TOP: 더보기(50위 단위로 하나씩 더 펼침) → 다 펼치면 접기
document.querySelectorAll('.more').forEach(b => {
  const cards = [...b.parentElement.querySelectorAll('.topcard')], first = +b.dataset.visible, step = +b.dataset.step;
  let shown = first;
  b.addEventListener('click', () => {
    const closing = shown >= cards.length;
    shown = closing ? first : Math.min(cards.length, (Math.floor(shown / step) + 1) * step);
    cards.forEach((c, i) => c.hidden = i >= shown);
    const next = Math.min(cards.length, (Math.floor(shown / step) + 1) * step);
    b.textContent = shown >= cards.length ? '접기' : `더보기 · ${shown + 1}~${next}위`;
    if (closing) b.parentElement.scrollIntoView({block: 'start'});
  });
});

// 날짜 선택: 최신 목록(dates.json)을 불러와 채움. 못 불러오면 페이지에 들어 있는 목록을 씀.
const pick = document.getElementById('date');
if (pick) {
  const base = pick.dataset.base, current = pick.dataset.current;
  const fill = dates => {
    pick.innerHTML = '';
    dates.forEach((d, i) => { const o = document.createElement('option'); o.value = d;
      o.textContent = d + (i === 0 ? ' (최신)' : ''); o.selected = d === current; pick.appendChild(o); });
  };
  fetch(base + 'dates.json', {cache: 'no-store'}).then(r => r.ok ? r.json() : Promise.reject())
    .then(fill).catch(() => {});
  pick.addEventListener('change', () => {
    const d = pick.value, latest = pick.options[0] && pick.options[0].value;
    location.href = (d === latest ? base + 'index.html' : base + 'reports/' + d + '.html') + viewHash();
  });
}
"""


def e(v) -> str:
    return escape(str(v if v is not None else ""))


def won(v) -> str:
    try:
        return f"{int(float(v)):,}원"
    except (TypeError, ValueError):
        return ""


def pp(v) -> str:
    return "–" if v is None else f"{'+' if v > 0 else ''}{v:.1f}%p"


def change_html(v) -> str:
    if v is None:
        return "<span class='flat-t'>–</span>"
    if abs(v) < 0.05:
        return "<span class='flat-t'>0.0</span>"
    cls, arrow = ("up-t", "▲") if v > 0 else ("down-t", "▼")
    return f"<span class='{cls}'>{arrow}{abs(v):.1f}</span>"


def rec_cards(recs: list[dict]) -> str:
    if not recs:
        return '<p class="empty">추천할 상품이 없어요.</p>'
    out = []
    for r in recs:
        discount = str(r.get("discount_rate") or "0")
        discount_html = f" <small>{e(discount)}%↓</small>" if discount != "0" else ""
        out.append(
            f'<a class="rec" href="{e(r["product_url"])}" target="_blank" rel="noopener">'
            f'<img src="{e(r["image_url"])}" alt="" loading="lazy" referrerpolicy="no-referrer">'
            f'<div class="body"><span class="brand">{e(r["brand"])} · {e(r["item_type"])}</span>'
            f'<span class="name">{e(r["product_name"])}</span>'
            f'<span class="price">{won(r["final_price"])}{discount_html}</span>'
            f'<span class="why">{e(r["reason"])}</span></div></a>')
    return f'<div class="recs">{"".join(out)}</div>'


def share_chart(title: str, group: dict, trend_label: str, has_trend: bool) -> str:
    rows = group["rows"][:CHART_ROWS]
    cov = f"상품 {group['coverage']:.0f}%에서 파악 · 파악된 상품 중 인기 비중"
    if not rows:
        return f'<div class="chart"><h3>{e(title)}</h3><p class="cov">{cov}</p><p class="empty">데이터가 부족해요.</p></div>'
    scale = max(10.0, max(r["share"] for r in rows))
    head = (f"<div class='chg-head'><span></span><span></span><span>비중</span>"
            f"<span>{'7일比' if trend_label.startswith('7일') else WORD['prev'] + '比'}</span></div>")
    out = []
    for r in rows:
        change = r["week_pp"] if r["week_pp"] is not None else r["dod_pp"]
        tip = f'<b>{e(r["name"])}</b><br>비중 {r["share"]:.1f}% ({r["count"]}개 상품)'
        if r["dod_pp"] is not None:
            tip += f'<br>{WORD["prev"]} 대비 {pp(r["dod_pp"])}'
        if r["week_pp"] is not None:
            tip += f'<br>7일 평균 대비 {pp(r["week_pp"])}'
        out.append(
            f'<div class="row" tabindex="0" data-tip="{e(tip)}"><span class="label">{swatch(r["name"])}{e(r["name"])}</span>'
            f'<span class="track"><span class="bar" style="width:{r["share"] / scale * 100:.1f}%"></span></span>'
            f'<span class="val">{r["share"]:.0f}%</span><span class="chg">{change_html(change) if has_trend else ""}</span></div>')
    return f'<div class="chart"><h3>{e(title)}</h3><p class="cov">{cov}</p>{head if has_trend else ""}{"".join(out)}</div>'


def why_html(why: dict | None) -> str:
    """순위가 오른 이유 한 칸: 원인 종류 칩 + 확인/추정 + 설명 + 출처 링크."""
    if not why:
        return "<div class='why none'>원인 조사 전 — 매일 오전 조사 후 채워져요.</div>"
    tags = "".join(f"<span class='tag'>{e(c)}</span>" for c in why["causes"])
    ok = why["confidence"] == "확인"
    conf = (f"<span class='conf{' ok' if ok else ''}' title='"
            f"{'출처로 원인을 직접 확인함' if ok else '직접 증거는 못 찾음 — 정황상 가장 그럴듯한 원인'}'>{e(why['confidence'])}</span>")
    src = " · ".join(f"<a href='{e(s['url'])}' target='_blank' rel='noopener'>{e(s['title'])}</a>" for s in why["sources"])
    return (f"<div class='why'><div class='tags'>{tags}{conf}</div>{e(why['reason'])}"
            + (f"<div class='src'>출처: {src}</div>" if src else "") + "</div>")


PRODUCT_HEAD = "<th class='num'>종류 순위</th><th></th><th>상품</th><th>카테고리</th><th class='num'>가격</th>"


def rank_text(p: dict) -> str:
    """'니트웨어 5위 · 전체 9위' — 종류 안 순위는 항상, 의류 전체 TOP 100 안이면 그 순위도."""
    text = f"{p.get('mid_name') or p['category_name']} {p['rank']}위"
    return text + (f" · 전체 {p['overall_rank']}위" if p.get("overall_rank") else "")


def product_cells(p: dict) -> str:
    attrs = " · ".join(p.get("attrs") or [])
    attrs_html = f"<br><small style='color:var(--muted)'>{e(attrs)}</small>" if attrs else ""
    overall = f"<br><small>전체 {p['overall_rank']}</small>" if p.get("overall_rank") else ""
    return (f"<td class='num'>{p['rank']}{overall}</td>"
            f"<td><img src='{e(p['image_url'])}' alt='' loading='lazy' referrerpolicy='no-referrer'></td>"
            f"<td><a href='{e(p['product_url'])}' target='_blank' rel='noopener'><small>{e(p['brand'])}</small><br>"
            f"{e(p['product_name'])}</a>{attrs_html}</td>"
            f"<td>{e(p.get('mid_name') or p['category_name'])}<br><small>{e(p['item_type'])}</small></td>"
            f"<td class='num'>{won(p['final_price'])}</td>")


def mover_table(title: str, products: list[dict], empty: str = "") -> str:
    """순위가 크게 오른 상품 + 오른 이유(상품 아래 줄)."""
    if not products:
        return f'<div><h3>{e(title)}</h3><p class="empty">{e(empty or "해당 상품이 없어요.")}</p></div>'
    body = "".join(
        f"<tr class='has-why'>{product_cells(p)}"
        f"<td class='num up-t'>▲{p['change']}<br><small>({p['prev_rank']}→{p['rank']})</small></td></tr>"
        f"<tr class='why-row'><td></td><td colspan='5'>{why_html(p.get('why'))}</td></tr>" for p in products)
    return (f'<div><h3>{e(title)}</h3><div class="table-wrap"><table class="movers"><thead><tr>{PRODUCT_HEAD}'
            f"<th class='num'>변화</th></tr></thead><tbody>{body}</tbody></table></div></div>")


def product_table(title: str, products: list[dict], empty: str = "") -> str:
    if not products:
        return f'<div><h3>{e(title)}</h3><p class="empty">{e(empty or "해당 상품이 없어요.")}</p></div>'
    body = "".join(f"<tr>{product_cells(p)}</tr>" for p in products)
    return (f'<div><h3>{e(title)}</h3><div class="table-wrap"><table><thead><tr>{PRODUCT_HEAD}</tr></thead>'
            f'<tbody>{body}</tbody></table></div></div>')


COLOR_HEX = {  # 컬러 칩에 보여줄 실제 색 (대표색)
    "블랙": "#111111", "화이트": "#ffffff", "아이보리/크림": "#f1e8d4", "그레이/차콜": "#7d7d7d",
    "네이비": "#1f2a48", "블루/인디고": "#3e64a8", "브라운/카멜": "#8a5a2e", "베이지/샌드": "#d6c2a1",
    "카키/올리브": "#6b6a3a", "그린/민트": "#4e9a6c", "레드/버건디": "#9c2233", "핑크": "#f0a6b9",
    "퍼플/라벤더": "#8d6bb7", "옐로우/머스타드": "#dfb236", "오렌지": "#e77a2f",
}
PROFILE_ROWS = [("silhouette", "실루엣·기장"), ("texture", "원단"), ("fit", "핏"),
                ("fiber", "소재"), ("color", "컬러"), ("detail", "디테일")]


def swatch(name: str) -> str:
    hexv = COLOR_HEX.get(name)
    return f"<i class='sw' style='background:{hexv}'></i>" if hexv else ""


def top10_section(idx: int, cats: list[dict]) -> str:
    if not cats:
        return '<p class="empty">데이터가 없어요.</p>'
    btns, panels = [], []
    for i, c in enumerate(cats):
        pid = f"top-{idx}-{c['code']}"
        btns.append(f"<button type='button' data-show='{pid}' aria-selected='{str(i == 0).lower()}'>"
                    f"{e(c['name'])} <small>({c['count']})</small></button>")
        cards = []
        total = len(c["products"])
        for n, p in enumerate(c["products"], 1):
            attrs = " · ".join(p.get("attrs") or [])
            attrs_html = f"<span class='meta'>{e(attrs)}</span>" if attrs else ""
            hide = " hidden" if n > TOP_VISIBLE else ""
            cards.append(
                f"<div class='topcard'{hide}>"
                f"<a class='tlink' href='{e(p['product_url'])}' target='_blank' rel='noopener'>"
                f"<span class='rk'>{n}</span>"
                f"<img src='{e(p['image_url'])}' alt='' loading='lazy' referrerpolicy='no-referrer'>"
                f"<div class='body'><span class='brand'>{e(p['brand'])}</span>"
                f"<span class='name'>{e(p['product_name'])}</span>"
                f"<span class='price'>{won(p['final_price'])}</span>"
                f"<span class='meta'>{e(p['item_type'])} · {e(rank_text(p))}</span>{attrs_html}</div></a>"
                f"{review_html(p.get('review'))}</div>")
        hidden = "hidden" if i else ""
        more = (f"<button type='button' class='more' data-visible='{TOP_VISIBLE}' data-step='{MORE_STEP}'>"
                f"더보기 · {TOP_VISIBLE + 1}~{min(total, MORE_STEP)}위</button>") if total > TOP_VISIBLE else ""
        panels.append(f"<div class='seg-panel' id='{pid}' {hidden}><div class='topgrid'>{''.join(cards)}</div>{more}</div>")
    return f"<div><div class='seg' role='tablist'>{''.join(btns)}</div>{''.join(panels)}</div>"


def review_html(r: dict | None) -> str:
    """TOP 카드 아래 후기 요약: 좋은 점 · 아쉬운 점 · 대표 후기.
    (설문, 후기 수·3점 이하 비율은 2026-09-25 사장님 결정으로 화면에서 뺌)"""
    if not r:
        return ""
    if not r.get("sampled"):
        return "<div class='rv'><span class='rv-none'>아직 후기가 없어요</span></div>"
    pros = " · ".join(e(x["name"]) for x in r.get("pros") or [])
    cons = " · ".join(e(x["name"]) for x in r.get("cons") or [])
    rows = []
    rows.append(f"<div class='rv-p'><b>좋아요</b> {pros or '<span class=rv-none>특별히 많이 나온 말 없음</span>'}</div>")
    rows.append(f"<div class='rv-c'><b>아쉬워요</b> {cons or '<span class=rv-none>눈에 띄는 불만 없음</span>'}</div>")
    quotes = ""
    if r.get("good_quote"):
        quotes += f"<p class='q good'>“{e(r['good_quote'])}”</p>"
    if r.get("bad_quote"):
        quotes += f"<p class='q bad'>“{e(r['bad_quote'])}”</p>"
    if quotes:
        rows.append(f"<details><summary>대표 후기</summary>{quotes}</details>")
    return f"<div class='rv'>{''.join(rows)}</div>"


def chips_html(key: str, group: dict) -> str:
    vals = group.get("values") or []
    if not vals:
        return "<span class='none'>정보 없음</span>"
    out = []
    for v in vals:
        pct = min(100.0, v["pct"])
        sw = swatch(v["name"]) if key == "color" else ""
        tip = f"<b>{v['name']}</b><br>이 아이템 중 인기 비중 {v['pct']:.0f}% ({v['count']}개)"
        out.append(f"<span class='chip' tabindex='0' data-tip='{e(tip)}'><i class='fill' style='width:{pct:.0f}%'></i>"
                   f"<span>{sw}{e(v['name'])} <b>{v['pct']:.0f}%</b></span></span>")
    return f"<div class='chips'>{''.join(out)}</div>"


def price_section(pb: dict | None) -> str:
    if not pb or not pb.get("rows"):
        return '<p class="empty">데이터가 없어요.</p>'
    scale = max(pb["totals"]) or 1
    total_all = sum(pb["totals"]) or 1
    rows = []
    for i, label in enumerate(pb["labels"]):
        n = pb["totals"][i]
        detail = "<br>".join(f"{r['name']} {r['counts'][i]}개" for r in pb["rows"] if r["counts"][i])
        tip = f"<b>{label}</b><br>{n}개 ({100 * n / total_all:.0f}%)" + (f"<br>{detail}" if detail else "")
        rows.append(f"<div class='row' tabindex='0' data-tip='{e(tip)}'><span class='label'>{e(label)}</span>"
                    f"<span class='track'><span class='bar' style='width:{100 * n / scale:.1f}%'></span></span>"
                    f"<span class='val'>{n}개</span><span class='chg'>{100 * n / total_all:.0f}%</span></div>")
    medians = "".join(f"<span>{e(r['name'])} <b>{won(r['median'])}</b></span>" for r in pb["rows"] if r.get("median"))
    return f"<div>{''.join(rows)}<div class='medians'>카테고리별 중간 가격: {medians}</div></div>"


PURPOSES = [  # 목적별 탭 (주소 끝 #여성-소재컬러 처럼 공유 가능). 2026-09-25 디자이너 실무용으로 개편
    ("요약", "오늘 요약"),
    ("인기", "인기 TOP"),
    ("기획", "카테고리 순위"),
    ("디자인", "디자인 참고"),
    ("소재컬러", "컬러"),  # 주소(#여성-소재컬러)는 공유된 링크가 안 깨지게 그대로. 원단·소재 카드는 2026-09-27 사장님 결정으로 뺌
    ("가격", "가격"),
    ("동향", "시장 동향"),
]
DESIGN_CHARTS = [("fit", "핏"), ("silhouette", "실루엣·기장"), ("detail", "디테일")]
TOP_VISIBLE = 20  # 인기 TOP: 처음 보이는 개수, 나머지는 '더보기'로
MORE_STEP = 50    # 더보기 한 번에 50위 단위까지 (21~50 → 51~100 → …, 2026-09-27 사장님)
BRIEF_PRODUCTS = 3  # 오늘 요약에 넣을 상품·브랜드 수


def _product_link(p: dict) -> str:
    name = p["product_name"] if len(p["product_name"]) <= 30 else p["product_name"][:29] + "…"
    return (f"<a href='{e(p['product_url'])}' target='_blank' rel='noopener'>"
            f"<span class='b'>{e(p['brand'])}</span> {e(name)}</a>")


def _sub(items: list[str]) -> str:
    return "<ul class='sub'>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>"


def brief_lines(g: dict) -> str:
    """오늘 요약: 회의에 그대로 옮겨 쓸 수 있는 문장 몇 줄.
    전 카테고리를 합친 컬러·원단·가격은 실무에 안 맞아 뺌 (2026-09-27 사장님) — 상품·브랜드·아이템 단위로."""
    types = sorted(g.get("item_types", []), key=lambda t: -t["share"])
    labels = dict(PROFILE_ROWS)
    lines = []
    if types:
        rest = ", ".join(e(t["name"]) for t in types[1:3])
        lines.append(f"가장 인기 있는 아이템은 <b>{e(types[0]['name'])}</b>(인기 비중 {types[0]['share']:.0f}%)"
                     + (f", 이어서 {rest}." if rest else "."))
    rising = sorted((t for t in types if (t.get("dod_pp") or 0) >= 0.5), key=lambda t: -t["dod_pp"])[:3]
    if rising:
        specs = [f"<b>{e(t['name'])}</b>에서 늘어난 스펙: " + " · ".join(
                     f"{e(labels[s['group']])} {swatch(s['name']) if s['group'] == 'color' else ''}<b>{e(s['name'])}</b> "
                     f"<span class='up-t'>+{s['gain']:.0f}%p</span>" for s in t["spec_changes"])
                 for t in rising if t.get("spec_changes")]
        lines.append(f"{WORD['prev']}보다 오른 아이템: " + ", ".join(
            f"<b>{e(t['name'])}</b> <span class='up-t'>▲{t['dod_pp']:.1f}%p</span>" for t in rising)
            + (_sub(specs) if specs else ""))
    movers = g.get("movers", [])[:BRIEF_PRODUCTS]
    if movers:
        rows = []
        for m in movers:
            w = m.get("why")
            if w:
                ok = w["confidence"] == "확인"
                why = (f"{e(w.get('summary') or ' · '.join(w['causes']))} "
                       f"<span class='conf{' ok' if ok else ''}'>{e(w['confidence'])}</span>")
            else:
                why = "<span class='hint'>원인 조사 전</span>"
            rows.append(f"{_product_link(m)} <span class='up-t'>▲{m['change']}</span> "
                        f"<small>({e(m.get('mid_name') or '')} {m['prev_rank']}→{m['rank']}위)</small> — {why}")
        lines.append(f"순위가 크게 오른 상품 <span class='hint'>자세한 원인·출처는 7 시장 동향</span>" + _sub(rows))
    fresh = g.get("fresh_entries", [])
    if fresh:
        def registered(r: str) -> str:  # 'YYYY-MM-DD'(상세정보) 또는 'YYYY-MM'(사진 폴더)
            return f"{int(r[5:7])}/{int(r[8:])} 판매 시작" if len(r) > 7 else f"{int(r[5:7])}월 등록"

        def before(p: dict) -> str:
            if not g.get("has_trend"):
                return ""
            return f" <small>({WORD['prev']} {str(p['prev_rank']) + '위' if p.get('prev_rank') else '순위 밖'})</small>"
        rows = [f"{_product_link(p)} {e(rank_text(p))}{before(p)}"
                f" · {won(p['final_price'])} · <small>{registered(p['registered'])}</small>"
                for p in fresh[:BRIEF_PRODUCTS]]
        more = f" 외 {len(fresh) - BRIEF_PRODUCTS}개" if len(fresh) > BRIEF_PRODUCTS else ""
        lines.append(f"뜨는 신상 <span class='hint'>판매 시작 {g.get('fresh_days', 45)}일 안의 상품 중 의류 전체 100위권에 새로 들었거나 "
                     f"종류 안에서 10계단 이상 오름{more}</span>"
                     + _sub(rows))
    brands = g.get("rising_brands", [])
    if brands:
        rows = [f"<b>{e(b['brand'])}</b> {b['count']}개 <span class='up-t'>(+{b['gain']})</span>"
                + (f" · 크게 오른 상품 {b['movers']}개" if b["movers"] else "")
                + f" <small>· 대표: {e(b['best'][:30])}</small>" for b in brands]
        lines.append(f"뜨는 브랜드 <span class='hint'>랭킹 안 상품 수가 {WORD['prev']}보다 늘어난 브랜드</span>" + _sub(rows))
    if not lines:
        return '<p class="empty">데이터가 없어요.</p>'
    return "<ul class='brief'>" + "".join(f"<li>{x}</li>" for x in lines) + "</ul>"


def item_rank_table(types: list[dict], has_yesterday: bool) -> str:
    """카테고리 순위: 아이템 종류를 인기 비중 순으로. 이전 기록보다 0.5%p 이상 오른 아이템은 강조."""
    if not types:
        return '<p class="empty">데이터가 없어요.</p>'
    types = sorted(types, key=lambda t: -t["share"])
    scale = max(t["share"] for t in types) or 1
    body = []
    for n, t in enumerate(types, 1):
        first = t["top_products"][0] if t["top_products"] else None
        img = (f"<a href='{e(first['product_url'])}' target='_blank' rel='noopener'>"
               f"<img src='{e(first['image_url'])}' alt='' loading='lazy' referrerpolicy='no-referrer'></a>") if first else ""
        hi = " class='hi'" if (t.get("dod_pp") or 0) >= 0.5 else ""
        body.append(
            f"<tr{hi}><td class='num'>{n}</td><td>{img}</td>"
            f"<td><b>{e(t['name'])}</b><br><small style='color:var(--muted)'>{e(t['category_name'])}</small></td>"
            f"<td><div class='mini'><span class='mini-track'><span style='width:{100 * t['share'] / scale:.0f}%'></span></span>"
            f"<b>{t['share']:.0f}%</b></div></td>"
            f"<td class='num'>{change_html(t.get('dod_pp')) if has_yesterday else '–'}</td>"
            f"<td class='num'>{t['count']}개</td><td class='num'>{won(t.get('median_price'))}</td>"
            f"<td class='num'>{t['best_rank']}위</td></tr>")
    head = ("<th class='num'>#</th><th></th><th>아이템</th><th>인기 비중</th><th class='num'>{WORD['prev']} 대비(%p)</th>"
            "<th class='num'>상품 수</th><th class='num'>중간 가격</th><th class='num'>종류 안 최고 순위</th>")
    return (f"<div class='table-wrap'><table class='rt'><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>")


def item_card(p: dict, max_share: float) -> str:
    dod = f" · {WORD['prev']} 대비 {change_html(p['dod_pp'])}%p" if p.get("dod_pp") is not None else ""
    median = f" · 중간 가격 {won(p['median_price'])}" if p.get("median_price") else ""
    gallery = "".join(
        f"<a href='{e(t['product_url'])}' target='_blank' rel='noopener' title='{e(t['brand'])} {e(t['product_name'])}'>"
        f"<img src='{e(t['image_url'])}' alt='' loading='lazy' referrerpolicy='no-referrer'></a>"
        for t in p["top_products"])
    rows = "".join(
        f"<div class='arow'><span class='al'>{e(label)}<small>파악 {p['groups'][key]['coverage']}%</small></span>"
        f"{chips_html(key, p['groups'][key])}</div>" for key, label in PROFILE_ROWS if key in p["groups"])
    return (f"<div class='tcard'><div class='tt'><h4>{e(p['name'])}</h4>"
            f"<div class='ts'>{p['count']}개 · 인기 비중 {p['share']:.0f}%{median} · 종류 안 최고 {p['best_rank']}위{dod}</div></div>"
            f"<div class='share-meter' title='인기 비중'><span style='width:{100 * p['share'] / max_share:.0f}%'></span></div>"
            f"<div class='gallery'>{gallery}</div>{rows}</div>")


def design_section(gi: int, profiles: list[dict]) -> str:
    """디자인 참고: 카테고리 버튼으로 걸러 보는 아이템별 스펙 카드."""
    if not profiles:
        return '<p class="empty">데이터가 없어요.</p>'
    max_share = max(p["share"] for p in profiles) or 1
    cats: dict[str, list[dict]] = {}
    for p in profiles:
        cats.setdefault(p["category_name"], []).append(p)
    btns, panels = [], []
    for i, (name, items) in enumerate(cats.items()):
        pid = f"dz-{gi}-{i}"
        btns.append(f"<button type='button' data-show='{pid}' aria-selected='{str(i == 0).lower()}'>"
                    f"{e(name)} <small>({len(items)})</small></button>")
        cards = "".join(item_card(p, max_share) for p in items)
        panels.append(f"<div class='seg-panel' id='{pid}' {'hidden' if i else ''}><div class='types'>{cards}</div></div>")
    return f"<div><div class='seg' role='tablist'>{''.join(btns)}</div>{''.join(panels)}</div>"


def palette(group: dict, has_trend: bool) -> str:
    rows = [r for r in group["rows"] if r["name"] in COLOR_HEX]
    if not rows:
        return '<p class="empty">데이터가 부족해요.</p>'
    cells = []
    for r in rows:
        change = r["week_pp"] if r["week_pp"] is not None else r["dod_pp"]
        chg = f"<span class='pc'>{change_html(change)}%p</span>" if has_trend else ""
        cells.append(f"<div class='pcell'><div class='sq' style='background:{COLOR_HEX[r['name']]}'></div>"
                     f"<div class='pb'><span>{e(r['name'])}</span><span class='pv'>{r['share']:.0f}%</span>"
                     f"<span class='pn'>{r['count']}개{chg}</span></div></div>")
    return (f"<p class='cov'>상품 {group['coverage']:.0f}%에서 파악 · 파악된 상품 중 인기 비중</p>"
            f"<div class='palette'>{''.join(cells)}</div>")


def item_price_table(types: list[dict]) -> str:
    """아이템별 가격: 중간 가격과 주로 팔리는 가격 범위(가운데 50%)를 막대로."""
    rows = [t for t in types if t.get("median_price")]
    if not rows:
        return '<p class="empty">데이터가 없어요.</p>'
    top = max(max(t.get("price_high") or 0, t.get("median_original") or 0, t["median_price"]) for t in rows) or 1
    body, current = [], None
    for t in rows:
        if t["category_name"] != current:
            current = t["category_name"]
            body.append(f"<tr class='grp'><td colspan='6'>{e(current)}</td></tr>")
        lo, hi = t.get("price_low"), t.get("price_high")
        rng = f"{won(lo)} ~ {won(hi)}" if lo and hi else "<span class='flat-t'>상품이 적어 생략</span>"
        bar = ""
        if lo and hi:
            bar = (f"<span style='left:{100 * lo / top:.1f}%;width:{max(1.0, 100 * (hi - lo) / top):.1f}%'></span>")
        bar += f"<i style='left:{100 * t['median_price'] / top:.1f}%'></i>"
        orig = t.get("median_original")
        if orig:
            bar += f"<i class='o' style='left:{min(100.0, 100 * orig / top):.1f}%'></i>"
        disc = f"{t['discount_avg']:.0f}%" if t.get("discount_avg") is not None else "–"
        body.append(f"<tr><td><b>{e(t['name'])}</b> <small style='color:var(--muted)'>{t['count']}개</small></td>"
                    f"<td class='num'><b>{won(t['median_price'])}</b></td><td class='num'>{won(orig) or '–'}</td>"
                    f"<td class='num'>{disc}</td><td class='num'>{rng}</td>"
                    f"<td class='pr-cell'><div class='prange'>{bar}</div></td></tr>")
    head = ("<th>아이템</th><th class='num'>실판매가(중간)</th><th class='num'>정가(중간)</th><th class='num'>평균 할인율</th>"
            "<th class='num'>주로 팔리는 실판매가</th><th>분포</th>")
    return (f"<div class='table-wrap'><table class='pt'><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>")


def big_category_charts(gi: int, g: dict) -> str:
    """디자인 참고: 아우터/상의/하의 버튼으로 바꿔 보는 핏·실루엣·디테일 순위."""
    cats = g.get("big_categories") or []
    if not cats:
        return '<p class="empty">데이터가 없어요.</p>'
    btns, panels = [], []
    for i, c in enumerate(cats):
        pid = f"bc-{gi}-{i}"
        btns.append(f"<button type='button' data-show='{pid}' aria-selected='{str(i == 0).lower()}'>"
                    f"{e(c['name'])} <small>({c['count']})</small></button>")
        charts = "".join(share_chart(title, c["attributes"][key], f"{WORD['prev']} 대비", g["has_trend"]) for key, title in DESIGN_CHARTS)
        panels.append(f"<div class='seg-panel' id='{pid}' {'hidden' if i else ''}><div class='charts'>{charts}</div></div>")
    return f"<div><div class='seg' role='tablist'>{''.join(btns)}</div>{''.join(panels)}</div>"


def big_category_palettes(g: dict) -> str:
    """컬러: 아우터/상의/하의 컬러 팔레트를 차례로."""
    cats = g.get("big_categories") or []
    if not cats:
        return '<p class="empty">데이터가 없어요.</p>'
    return "".join(f"<h3 class='sec-h pal-h'>{e(c['name'])} <small>{c['count']}개</small></h3>"
                   f"{palette(c['attributes']['color'], g['has_trend'])}" for c in cats)


def gender_panels(gi: int, gender: str, g: dict, has_yesterday: bool) -> str:
    if g["has_trend"]:
        chart_sub = (f"막대 = 인기 비중(순위가 높을수록 크게 반영). 오른쪽 숫자 = {e(g['trend_label'])} 변화(%p). "
                     "막대에 마우스를 올리면 자세한 수치.")
    else:
        chart_sub = f"막대 = 인기 비중(순위가 높을수록 크게 반영). {WORD['prev']} 대비 변화는 기록이 쌓이면 표시돼요."
    no_yday = f"{WORD['prev']} 기록이 없어 {WORD['next']}부터 표시돼요."
    trend_word = "뜨는" if g["has_trend"] else "인기"
    types = g.get("item_types", [])
    body = {
        "요약": f"""
  <section class="card">
    <h2>{e(gender)} · 오늘 요약</h2>
    <p class="sub">29CM 베스트 의류 {g['count']}개 기준 · 회의 자료에 그대로 옮겨 쓸 수 있게 정리했어요.</p>
    {brief_lines(g)}
  </section>""",
        "인기": f"""
  <section class="card">
    <h2>인기 TOP</h2>
    <p class="sub">종류를 눌러 바꿔 보세요. 맨 아래 '더보기'로 50위씩 더 볼 수 있어요. '전체' = 29CM 의류 전체 TOP 100(수집 대상이 아닌 홈웨어·셋업 등은 빠져 100개보다 적어요), 나머지 = 종류별 TOP 100.
    카드 아래 후기 요약(종류별 30위까지) = 베스트순 후기 50개·별점 낮은 후기 최대 50개에서 자주 나온 표현(좋아요 = 4~5점 후기, 아쉬워요 = 3점 이하 후기)과 대표 후기.</p>
    {top10_section(gi, g.get('top_by_category', []))}
  </section>""",
        "기획": f"""
  <section class="card">
    <h2>아이템 순위</h2>
    <p class="sub">어떤 아이템이 잘 팔리는지 인기 비중 순으로. 초록 줄 = {WORD['prev']}보다 0.5%p 이상 오른 아이템. 3개 이상 오른 아이템만.</p>
    {item_rank_table(types, has_yesterday)}
  </section>
  <section class="card">
    <h2>추천 아이템</h2>
    <p class="sub">순위가 높고 {trend_word} 속성을 많이 가진 상품 (아이템 종류별 1개)</p>
    {rec_cards(g['recommendations'])}
  </section>""",
        "디자인": f"""
  <section class="card">
    <h2>아이템별 스펙</h2>
    <p class="sub">카테고리를 눌러 바꿔 보세요. 사진 = 그 아이템에서 잘 팔리는 순서. 칩의 % = 그 아이템 중 해당 속성의 인기 비중(파악된 상품 기준). 실루엣·기장은 팬츠류만.</p>
    {design_section(gi, types)}
  </section>
  <section class="card">
    <h2>대분류별 핏 · 실루엣 · 디테일</h2>
    <p class="sub">아우터 / 상의 / 하의(바지·스커트)를 눌러 바꿔 보세요. {chart_sub}</p>
    {big_category_charts(gi, g)}
  </section>""",
        "소재컬러": f"""
  <section class="card">
    <h2>컬러 팔레트</h2>
    <p class="sub">대분류별로 잘 팔리는 컬러를 인기 비중 순으로. 여러 색으로 파는 상품은 판매 중인 색을 모두 셌어요.{' 작은 숫자 = ' + WORD['prev'] + ' 대비 변화.' if g['has_trend'] else ''}</p>
    {big_category_palettes(g)}
  </section>""",
        "가격": f"""
  <section class="card">
    <h2>아이템별 가격</h2>
    <p class="sub">실판매가 = 할인이 적용된 지금 가격, 정가 = 할인 전 원래 가격. 주로 팔리는 실판매가 = 가운데 절반의 상품이 들어가는 범위. 막대: 파란 칸 = 주로 팔리는 실판매가, 검은 선 = 실판매가 중간, 빨간 점선 = 정가 중간.</p>
    {item_price_table(types)}
  </section>
  <section class="card">
    <h2>가격대 분포</h2>
    <p class="sub">막대 = 상품 수. 마우스를 올리면 카테고리별 개수가 나와요.</p>
    {price_section(g.get('price_bands'))}
  </section>""",
        "동향": f"""
  <section class="card">
    {mover_table(f"{WORD['prev']}보다 순위가 크게 오른 상품", g['movers'], empty=no_yday if not has_yesterday else '')}
    <p class="note">오른 이유 = Claude가 웹·유튜브·SNS·커뮤니티를 조사해 찾은 유입 경로. <b>확인</b> = 출처에서 이 상품(또는 같은 모델)이 직접 소개된 것을 확인, <b>추정</b> = 직접 증거는 못 찾아 정황(할인·브랜드 노출·시즌)으로 판단. 일간 순위는 전날 판매가 반영돼요.</p>
  </section>
  <section class="card">
    {product_table('오늘 새로 TOP 50에 진입', g['new_entries'], empty=no_yday if not has_yesterday else '')}
  </section>""",
    }
    return "".join(
        f'<div class="panel" data-g="{gi}" data-t="{key}" role="tabpanel" {"" if gi == 0 and n == 0 else "hidden"}>{body[key]}</div>'
        for n, (key, _) in enumerate(PURPOSES))


def render(a: dict, dates: list[str] | None, base: str, root: str = "") -> str:
    """base = 이 페이지에서 같은 기간 폴더까지 경로, root = docs/ 까지 경로 (기간 전환 링크용)."""
    period = a.get("period", "daily")
    _, period_name, span, WORD["prev"], WORD["next"] = PERIODS[period]
    genders = list(a["genders"].items())
    pswitch = "".join(
        f'<a href="{root}{PERIOD_DIRS[k]}index.html" data-period="{k}"'
        f'{" aria-current=page" if k == period else ""}>{v[1]}</a>' for k, v in PERIODS.items())
    gswitch = "".join(
        f'<button type="button" data-g="{i}" data-name="{e(name)}" aria-pressed="{str(i == 0).lower()}">{e(name)}</button>'
        for i, (name, _) in enumerate(genders))
    tabs = "".join(
        f'<button type="button" role="tab" data-t="{key}" aria-selected="{str(n == 0).lower()}">'
        f'<small>{n + 1}</small>{e(label)}</button>' for n, (key, label) in enumerate(PURPOSES))
    panels = "".join(gender_panels(i, name, g, a["has_yesterday"]) for i, (name, g) in enumerate(genders))
    if period == "daily":
        history_note = (f"최근 {a['history_days']}일 기록과 비교했어요." if a["history_days"]
                        else "첫 기록이라 어제·7일 비교는 기록이 쌓이면 표시돼요.")
    else:
        history_note = (f"{WORD['prev']}({a['compare_date']}) 기록과 비교했어요." if a["has_yesterday"]
                        else f"{WORD['prev']} 기록이 아직 없어 비교는 {WORD['next']}부터 표시돼요.")
    title = f"{period_name} · {a['date']}" + ("" if period == "daily" else f" 기준 {span}")
    date_picker = ""
    if dates:
        options = "".join(f'<option value="{e(d)}"{" selected" if d == a["date"] else ""}>{e(d)}{" (최신)" if i == 0 else ""}</option>'
                          for i, d in enumerate(dates))
        date_picker = (f'<label class="datepick" for="date">날짜 선택'
                       f'<select id="date" data-base="{e(base)}" data-current="{e(a["date"])}">{options}</select></label>')
    return f"""<meta charset="utf-8">
<title>29CM 의류 트렌드 · {period_name}</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>{CSS}</style>
<div class="wrap">
  <header>
    <div>
      <nav class="pswitch" aria-label="기간">{pswitch}</nav>
      <h1>29CM 의류 랭킹 트렌드 · {e(title)}</h1>
      <p>{e(SCOPE_TEXT.get(a.get('scope', 'category'), '').format(span=span))}</p>
      <p class="note">{e(history_note)} 상품 상세정보(소재·사이즈표·색상) 반영 {a['detail_coverage']:.0f}%</p>
    </div>
    {date_picker}
  </header>
  <div class="topbar">
    <div class="gswitch" role="group" aria-label="성별">{gswitch}</div>
    <nav class="tabs" role="tablist" aria-label="목적">{tabs}</nav>
  </div>
  {panels}
  <footer>
    <p>매일 오전 6시에 29CM 베스트의 일간·주간·월간 랭킹을 모두 받아 쌓고,
    리포트는 일간은 매일, 주간은 월요일, 월간은 1일에 만들어요. 주간·월간은 29CM의 '최근 1주일'·'최근 1개월' 랭킹 그대로예요
    (정확한 집계 구간·갱신 시각은 29CM이 공개하지 않음). 남성·여성은 29CM 남성의류·여성의류 랭킹 그대로예요.
    29CM은 랭킹을 100위까지만 공개해서, 의류 전체 TOP 100에 종류별 TOP 100을 더해 봐요.
    '인기 비중'은 순위가 높을수록 크게 반영한 비중(전체 TOP 100 먼저, 그다음 종류 안 순위 순)이고, 속성을 파악한 상품끼리 비교해요.
    순위 변화는 같은 종류 안 순위끼리 비교해요.
    아이템 종류는 29CM 소분류, 소재는 상품정보제공고시의 겉감 주원료, 핏·실루엣·원단은 상품명·상세 설명·실측 사이즈표·사진으로 분류해요.
    보고 있는 화면의 주소를 복사하면 같은 성별·탭이 열려요.</p>
  </footer>
</div>
<div id="tip" hidden></div>
<script>{JS}</script>
"""


def page(body: str) -> str:
    return f"<!doctype html>\n<html lang=\"ko\">\n<head>\n{body}"  # 브라우저가 head/body를 알아서 닫음


def period_dir(period: str = "daily"):
    return DOCS_DIR / PERIOD_DIRS[period] if PERIOD_DIRS[period] else DOCS_DIR


def report_dates(period: str = "daily") -> list[str]:
    return sorted((p.stem for p in (period_dir(period) / "reports").glob("*.html")), reverse=True)


IMAGE_WIDTH = 400  # 카드·표 사진 폭. 원본(1000px)을 그대로 쓰면 한 화면에 수백 장이라 너무 무거움


def shrink_images(node) -> None:
    """분석 결과 안의 모든 image_url 에 ?width= 를 붙인다."""
    if isinstance(node, dict):
        url = node.get("image_url")
        if isinstance(url, str) and url.startswith(IMAGE_BASE) and "?" not in url:
            node["image_url"] = f"{url}?width={IMAGE_WIDTH}"
        for v in node.values():
            shrink_images(v)
    elif isinstance(node, list):
        for v in node:
            shrink_images(v)


def load_analysis(date: str, period: str = "daily") -> dict:
    a = json.loads(analysis_path(date, period).read_text(encoding="utf-8"))
    shrink_images(a)
    return a


def build(date: str, period: str = "daily") -> None:
    a = load_analysis(date, period)
    out = period_dir(period)
    (out / "reports").mkdir(parents=True, exist_ok=True)
    dates = sorted(set(report_dates(period)) | {date}, reverse=True)
    up = "" if period == "daily" else "../"  # 기간 폴더 → docs/

    (out / "reports" / f"{date}.html").write_text(page(render(a, dates, "../", "../" + up)), encoding="utf-8")
    if date == dates[0]:
        (out / "index.html").write_text(page(render(a, dates, "", up)), encoding="utf-8")
    (out / "dates.json").write_text(json.dumps(dates), encoding="utf-8")
    print(f"리포트 생성: {out / 'index.html'} ({PERIODS[period][1]}, 날짜 {len(dates)}개)")


def embed_images(html: str) -> str:
    """외부 이미지를 막는 곳(Claude 링크 페이지 등)용: 상품 사진을 페이지 안에 넣는다.
    (모든 사진은 이미 ?width= 로 줄인 주소)"""
    cache: dict[str, str] = {}

    def fetch(url: str) -> str:
        if url not in cache:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    cache[url] = "data:image/jpeg;base64," + base64.b64encode(resp.read()).decode()
                time.sleep(0.2)
            except Exception:
                cache[url] = ""
        return cache[url]

    host = re.escape(IMAGE_BASE)
    html = re.sub(rf"src='({host}[^']+)'", lambda m: f"src='{fetch(m.group(1))}'", html)
    return re.sub(rf'src="({host}[^"]+)"', lambda m: f'src="{fetch(m.group(1))}"', html)


def build_standalone(date: str, out_path: str) -> None:
    """사진을 품은 한 장짜리 페이지 (날짜 선택 없음)."""
    global IMAGE_WIDTH
    IMAGE_WIDTH = 160  # 사진 약 1,000장을 페이지 안에 넣으므로 작게 (400px이면 16MB 제한을 넘음)
    html = embed_images(render(load_analysis(date), None, ""))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"단독 페이지 생성: {out_path} ({len(html) / 1_000_000:.1f}MB)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=today_kst())
    parser.add_argument("--period", choices=list(PERIODS), default="daily")
    parser.add_argument("--standalone", metavar="OUT", help="사진을 품은 단독 페이지로 저장")
    args = parser.parse_args()
    if args.standalone:
        build_standalone(args.date, args.standalone)
    else:
        build(args.date, args.period)
    return 0


if __name__ == "__main__":
    sys.exit(main())
