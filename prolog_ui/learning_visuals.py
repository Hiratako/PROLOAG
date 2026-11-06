"""Browser-owned learning galaxy, luminous trails, and crystal specimens.

Selection, playback, pagination, and resizing stay inside the component. The
component never changes logs or sends a selection back to Python.
"""
from __future__ import annotations

import streamlit as st


_HTML = """
<section class="learning-visuals" aria-label="勉強内容のビジュアライザー">
  <header class="lv-heading"><div><span class="lv-eyebrow">LEARNING ATLAS</span><h3 class="lv-title">学習銀河</h3></div><span class="lv-total"></span></header>
  <nav class="lv-modes" aria-label="見せ方"><button type="button" data-mode="galaxy" aria-pressed="true">学習銀河</button><button type="button" data-mode="trails" aria-pressed="false">学習の航跡</button><button type="button" data-mode="crystals" aria-pressed="false">クリスタル標本</button></nav>
  <div class="lv-filters"><label>科目<select class="lv-subject" aria-label="科目で絞り込む"></select></label><label>教材<select class="lv-material" aria-label="教材で絞り込む"></select></label><label>表示期間<select class="lv-month" aria-label="表示する月"></select></label><label>演出<select class="lv-style" aria-label="発光の強さ"><option value="glow">発光</option><option value="quiet">落ち着いた</option></select></label></div>
  <div class="lv-scene"><svg class="lv-svg" role="group" aria-label="学習の可視化"></svg><p class="lv-empty" hidden></p></div>
  <div class="lv-legend" aria-label="科目の色"></div>
  <div class="lv-playback"><button type="button" class="lv-play">期間を再生</button><label class="lv-slider-label">日付<input type="range" class="lv-slider" min="0" max="0" value="0" aria-label="可視化する日付"></label><output class="lv-date"></output></div>
  <p class="lv-explanation"></p>
  <div class="lv-pagebar"><span class="lv-page-caption" aria-live="polite"></span><button type="button" class="lv-prev" aria-label="前の表示ページ">前へ</button><button type="button" class="lv-next" aria-label="次の表示ページ">次へ</button></div>
  <div class="lv-day-list" hidden><h4>この日に勉強した内容</h4><div class="lv-day-items"></div><div class="lv-day-pages"><span class="lv-day-caption"></span><button type="button" class="lv-day-prev">前へ</button><button type="button" class="lv-day-next">次へ</button></div></div>
  <details class="lv-unit-index"><summary>単元一覧から選ぶ</summary><div class="lv-index-items"></div></details>
  <section class="lv-detail" aria-label="選んだ単元の学習記録"><p class="lv-detail-placeholder">星や結晶を選ぶと、勉強した内容・履歴・メモを確認できます。</p><div class="lv-detail-body" hidden><p class="lv-detail-path"></p><h4 class="lv-detail-title"></h4><p class="lv-detail-stats"></p><p class="lv-detail-period"></p><div class="lv-records"></div><div class="lv-record-pages"><span class="lv-record-caption"></span><button type="button" class="lv-record-prev">前へ</button><button type="button" class="lv-record-next">次へ</button></div><div class="lv-notes"></div></div></section>
</section>
"""

_CSS = """
.learning-visuals { --lv-ink:#152238; --lv-muted:#667085; --lv-line:#d9e2ec; --lv-card:#fff; --lv-bg:#f6f9fd; --lv-primary:#003b71; --lv-cosmic:#f2f6ff; --lv-glow:.55; font-family:var(--st-font,sans-serif); color:var(--lv-ink); width:100%; min-width:0; }
.learning-visuals * { box-sizing:border-box; }
.learning-visuals .lv-heading { display:flex; justify-content:space-between; align-items:center; gap:12px; margin:0 0 16px; }
.learning-visuals .lv-eyebrow { color:var(--lv-muted); letter-spacing:.18em; font-size:11px; }
.learning-visuals .lv-title { margin:4px 0 0; font-size:24px; font-weight:650; letter-spacing:.025em; }
.learning-visuals .lv-total { font-size:14px; text-align:right; font-variant-numeric:tabular-nums; max-width:50%; overflow-wrap:anywhere; }
.learning-visuals button,.learning-visuals select { font:inherit; font-size:14px; color:var(--lv-ink); background:var(--lv-card); border:1px solid var(--lv-line); border-radius:8px; min-height:44px; padding:8px 12px; max-width:100%; }
.learning-visuals button { cursor:pointer; }
.learning-visuals button:hover:not(:disabled) { border-color:var(--lv-primary); }
.learning-visuals button:disabled { opacity:.45; cursor:default; }
.learning-visuals button:focus-visible,.learning-visuals select:focus-visible,.learning-visuals summary:focus-visible,.learning-visuals a:focus-visible,.learning-visuals input:focus-visible { outline:2px solid var(--lv-primary); outline-offset:3px; }
.learning-visuals .lv-modes { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }
.learning-visuals .lv-modes button[aria-pressed="true"] { background:var(--lv-primary); color:var(--lv-selected-ink,#fff); border-color:var(--lv-primary); }
.learning-visuals .lv-filters { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-bottom:14px; }
.learning-visuals label { display:flex; flex-direction:column; gap:5px; min-width:0; color:var(--lv-muted); font-size:12px; }
.learning-visuals select { width:100%; text-overflow:ellipsis; }
.learning-visuals .lv-scene { position:relative; overflow:hidden; width:100%; border:1px solid var(--lv-line); border-radius:14px; background:radial-gradient(ellipse at 30% 20%,var(--lv-cosmic),var(--lv-bg) 80%); }
.learning-visuals .lv-svg { display:block; width:100%; height:450px; overflow:hidden; }
.learning-visuals .lv-svg text { font-family:inherit; font-size:12px; fill:var(--lv-ink); }
.learning-visuals .lv-svg .lv-small { font-size:11px; fill:var(--lv-muted); }
.learning-visuals .lv-svg a { cursor:pointer; outline:none; }
.learning-visuals .lv-svg a:focus-visible .lv-focus-ring { stroke:var(--lv-ink); stroke-width:2; opacity:1; }
.learning-visuals .lv-svg .lv-focus-ring { opacity:0; }
.learning-visuals .lv-svg a[aria-current="true"] .lv-focus-ring { opacity:1; stroke:var(--lv-ink); }
.learning-visuals .lv-empty { margin:0; padding:60px 24px; color:var(--lv-muted); text-align:center; }
.learning-visuals .lv-legend { display:flex; flex-wrap:wrap; gap:8px 16px; margin:12px 0; font-size:12px; }
.learning-visuals .lv-legend span { display:inline-flex; align-items:center; gap:6px; max-width:100%; overflow-wrap:anywhere; }
.learning-visuals .lv-legend i { display:block; width:8px; height:8px; flex:none; border-radius:50%; }
.learning-visuals .lv-playback { display:flex; align-items:center; gap:12px; margin-top:16px; }
.learning-visuals .lv-slider-label { flex:1; }
.learning-visuals .lv-slider { width:100%; min-height:30px; accent-color:var(--lv-primary); cursor:pointer; margin:0; }
.learning-visuals .lv-date { font-size:12px; font-variant-numeric:tabular-nums; min-width:80px; text-align:right; }
.learning-visuals .lv-explanation { color:var(--lv-muted); font-size:12px; line-height:1.7; margin:12px 0; }
.learning-visuals .lv-pagebar,.learning-visuals .lv-day-pages,.learning-visuals .lv-record-pages { display:flex; align-items:center; flex-wrap:wrap; gap:8px; font-size:12px; color:var(--lv-muted); }
.learning-visuals .lv-pagebar>span,.learning-visuals .lv-day-pages>span,.learning-visuals .lv-record-pages>span { flex:1; min-width:110px; }
.learning-visuals .lv-pagebar button,.learning-visuals .lv-day-pages button,.learning-visuals .lv-record-pages button { font-size:12px; }
.learning-visuals .lv-unit-index { margin-top:16px; border-top:1px solid var(--lv-line); padding-top:12px; }
.learning-visuals summary { cursor:pointer; min-height:44px; padding:10px 0; font-size:14px; }
.learning-visuals .lv-index-items,.learning-visuals .lv-day-items { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin:8px 0 12px; }
.learning-visuals .lv-index-items button,.learning-visuals .lv-day-items button { text-align:left; display:flex; flex-direction:column; gap:4px; overflow-wrap:anywhere; }
.learning-visuals .lv-index-items small,.learning-visuals .lv-day-items small { color:var(--lv-muted); font-size:11px; }
.learning-visuals .lv-day-list { margin-top:20px; }
.learning-visuals h4 { font-size:16px; line-height:1.5; margin:0 0 12px; overflow-wrap:anywhere; }
.learning-visuals .lv-detail { padding:20px 0 0; margin-top:20px; border-top:1px solid var(--lv-line); overflow-wrap:anywhere; }
.learning-visuals .lv-detail-placeholder,.learning-visuals .lv-detail-path,.learning-visuals .lv-detail-period { color:var(--lv-muted); font-size:12px; line-height:1.7; margin:0 0 10px; }
.learning-visuals .lv-detail-title { font-size:21px; margin-bottom:8px; }
.learning-visuals .lv-detail-stats { margin:0 0 8px; font-size:14px; line-height:1.7; font-variant-numeric:tabular-nums; }
.learning-visuals .lv-record { border-left:2px solid var(--lv-line); padding:0 0 4px 12px; margin:16px 0; }
.learning-visuals .lv-record-heading { color:var(--lv-muted); font-size:12px; line-height:1.7; margin:0; }
.learning-visuals .lv-record p:last-child { font-size:14px; line-height:1.7; white-space:pre-wrap; margin:5px 0; }
.learning-visuals .lv-notes { margin-top:16px; }
.learning-visuals .lv-note { background:var(--lv-bg); border:1px solid var(--lv-line); border-radius:8px; padding:12px; margin:8px 0; }
.learning-visuals .lv-note h5 { margin:0 0 6px; font-size:14px; }
.learning-visuals .lv-note p { margin:0; font-size:13px; white-space:pre-wrap; line-height:1.7; }
.learning-visuals [hidden] { display:none!important; }
@media(max-width:560px) { .learning-visuals .lv-filters { grid-template-columns:repeat(2,minmax(0,1fr)); } .learning-visuals .lv-heading { align-items:flex-start; } .learning-visuals .lv-title { font-size:22px; } .learning-visuals .lv-total { font-size:12px; } .learning-visuals .lv-playback { flex-wrap:wrap; gap:8px; } .learning-visuals .lv-slider-label { min-width:130px; } .learning-visuals .lv-date { min-width:74px; } .learning-visuals .lv-index-items,.learning-visuals .lv-day-items { grid-template-columns:minmax(0,1fr); } .learning-visuals .lv-modes { gap:6px; } .learning-visuals .lv-modes button { font-size:12px; padding:8px 10px; } }
"""

_JS = r"""
const instances = new WeakMap();
let componentSerial = 0;
const names = { galaxy: "学習銀河", trails: "学習の航跡", crystals: "クリスタル標本" };
const explanations = {
  galaxy: "星の面積＝表示期間内の累積時間。選択日までの最近7日間に勉強した星が発光します。線は科目・教材の所属だけを表します。0時間の単元は輪郭で表示し、一覧からも選べます。",
  trails: "リボンの太さ＝科目ごとの日別学習時間。勉強していない日は途切れます。日付を動かすと、その日の教材・単元を下に表示します。",
  crystals: "結晶の高さ＝表示期間内の累積時間。内部の層＝勉強した日数。教材ごとの標本箱から、単元を選んで履歴を確認できます。",
};
const number = value => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
const text = value => String(value ?? "");
const escape = value => text(value).replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
const short = (value, length = 12) => { const chars = Array.from(text(value)); return chars.length > length ? chars.slice(0, length - 1).join("") + "…" : chars.join(""); };
export function formatSeconds(value) {
  const seconds = Math.round(number(value));
  if (seconds < 60) return `${seconds}秒`;
  const minutes = Math.floor(seconds / 60), hours = Math.floor(minutes / 60);
  return hours ? `${hours}時間${minutes % 60}分` : `${minutes}分`;
}
export function stableHash(value) {
  let hash = 2166136261;
  for (const char of text(value)) hash = Math.imul(hash ^ char.codePointAt(0), 16777619);
  return hash >>> 0;
}
function toneColor(state, subjectId) {
  // Only the hierarchy ID determines the hue: changing the sidebar period or
  // dataset membership must not recolor an existing subject after a remount.
  const hash=stableHash(subjectId),hue=hash%360,saturation=62+(hash>>>16)%15;
  return `hsl(${hue} ${saturation}% ${state.dark?72:37}%)`;
}
function query(state, selector) { return state.root.querySelector(selector); }
function make(state, tag, className, content) {
  const element = state.document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = text(content);
  return element;
}
function choice(state, select, options, selected) {
  select.replaceChildren(...options.map(([value, label]) => {
    const option = make(state, "option", "", label); option.value = value; return option;
  }));
  select.value = selected;
}
function stop(state) {
  if (state.timer !== null) state.ownerWindow.clearInterval(state.timer);
  state.timer = null;
  query(state, ".lv-play").textContent = "期間を再生";
}
function theme(state) {
  const p = state.palette || {}, dark = p.mode === "dark";
  state.dark = dark;
  const safe = (value, fallback) => /^#[0-9a-f]{3,8}$/i.test(text(value)) ? value : fallback;
  const colors = {
    ink: safe(p.ink, dark ? "#e4edf8" : "#152238"), muted: safe(p.muted, dark ? "#9caec7" : "#667085"),
    line: safe(p.line, dark ? "#283d57" : "#d9e2ec"), card: safe(p.card, dark ? "#102039" : "#ffffff"),
    bg: safe(p.background, dark ? "#06111f" : "#f6f9fd"), primary: safe(p.primary, dark ? "#59d9ef" : "#003b71"),
    cosmic: dark ? "#132440" : "#e9f0ff",
  };
  for (const [name, value] of Object.entries(colors)) state.root.style.setProperty(`--lv-${name}`, value);
  state.root.style.setProperty("--lv-selected-ink",dark?"#06111f":"#ffffff");
  state.root.style.setProperty("--lv-glow", state.style === "quiet" ? "0" : dark ? "1" : ".55");
}
function selectedDays(state) {
  const days = state.payload.days;
  return state.month ? days.map((day, index) => ({ day, index })).filter(item => item.day.startsWith(state.month)) : days.map((day, index) => ({ day, index }));
}
function sumNode(node, indices) { return indices.reduce((total, index) => total + number(node.daily[index]), 0); }
function periodNodes(state) {
  const days = state.days;
  const indices = days.map(item => item.index), shown = indices.slice(0, state.day + 1);
  return state.payload.nodes.filter(node => (!state.subject || node.group_id === state.subject) && (!state.material || node.material_id === state.material))
    .map(node => ({ ...node, fullSeconds: sumNode(node, indices), shownSeconds: sumNode(node, shown) }))
    .sort((a, b) => text(a.group_id).localeCompare(text(b.group_id)) || text(a.material_id).localeCompare(text(b.material_id)) || text(a.id).localeCompare(text(b.id)));
}
function refreshFilters(state) {
  const subjects = state.payload.subjects, materials = state.payload.materials;
  if (!subjects.some(item => item.id === state.subject)) state.subject = "";
  if (!materials.some(item => item.id === state.material && (!state.subject || item.group_id === state.subject))) state.material = "";
  const duplicateNames = new Set(subjects.filter((item, i) => subjects.some((other, j) => i !== j && other.name === item.name)).map(item => item.name));
  choice(state, query(state, ".lv-subject"), [["", "すべての科目"], ...subjects.map(item => [item.id, duplicateNames.has(item.name) ? `${item.project_name} / ${item.name}` : item.name])], state.subject);
  choice(state, query(state, ".lv-material"), [["", "すべての教材"], ...materials.filter(item => !state.subject || item.group_id === state.subject).map(item => {
    const subject = subjects.find(subject => subject.id === item.group_id);
    return [item.id, state.subject ? item.name : `${subject?.name || "未分類"} / ${item.name}`];
  })], state.material);
  const months = [...new Set(state.payload.days.map(day => day.slice(0, 7)))];
  if (state.month && !months.includes(state.month)) state.month = "";
  choice(state, query(state, ".lv-month"), [["", "全期間"], ...months.map(month => [month, `${month.slice(0, 4)}年${Number(month.slice(5))}月`])], state.month);
}
function setDate(state, value) {
  state.day = Math.max(0, Math.min(state.days.length - 1, Number(value) || 0));
  state.dayPage = 0;
  state.recordPage = 0;
  draw(state);
}
function pick(state, id) {
  state.selected = id;
  state.recordPage = 0;
  draw(state);
}
function svgLink(state, node, content) {
  return `<a href="#" data-unit="${escape(node.id)}" aria-label="${escape(`${node.subject_name} / ${node.material_name} / ${node.label}：${formatSeconds(node.shownSeconds)}`)}" aria-current="${state.selected === node.id}"><title>${escape(`${node.project_name} / ${node.subject_name} / ${node.material_name} / ${node.label}`)}</title>${content}</a>`;
}
function glowFilter(state, subjectId, active) {
  return active && state.style !== "quiet" ? `filter="url(#${state.uid}-glow)"` : "";
}
function nodeGroups(nodes) {
  const groups = [];
  for (const node of nodes) {
    let group = groups.at(-1);
    if (!group || group.key !== node.material_id || group.nodes.length >= 4) {
      group = { key: node.material_id, subject: node.group_id, label: node.material_name, subjectName: node.subject_name, nodes: [] };
      groups.push(group);
    }
    group.nodes.push(node);
  }
  return groups;
}
function recent(state, node) {
  const end=state.days[state.day]?.index;
  return Number.isInteger(end)&&node.daily.slice(Math.max(0,end-6),end+1).some(seconds=>number(seconds)>0);
}
function drawGalaxy(state, nodes, width) {
  const groups = nodeGroups(nodes), columns = width < 500 ? 1 : width < 780 ? 2 : 3;
  const cellWidth = (width - 24) / columns, heights = Array(columns).fill(20), unitMax = Math.max(1, ...state.nodes.map(node => node.fullSeconds));
  let markup = "";
  for (let k = 0; k < 42; k++) {
    const x = stableHash(`${state.uid}-x-${k}`) % Math.max(1, Math.floor(width)), y = stableHash(`${state.uid}-y-${k}`) % 430;
    markup += `<circle cx="${x}" cy="${y}" r="${k % 7 ? .65 : 1.2}" fill="var(--lv-muted)" opacity=".25"/>`;
  }
  for (const group of groups) {
    const column = heights.indexOf(Math.min(...heights)), top = heights[column], x = 12 + column * cellWidth;
    const rows = Math.ceil(group.nodes.length / 2), height = rows * 98 + 66, color = toneColor(state, group.subject);
    const centerX = x + cellWidth / 2, centerY = top + height / 2;
    markup += `<ellipse cx="${centerX}" cy="${centerY}" rx="${cellWidth * .45}" ry="${height * .43}" fill="${color}" opacity=".075"/><ellipse cx="${centerX}" cy="${centerY}" rx="${cellWidth * .40}" ry="${height * .37}" fill="none" stroke="${color}" stroke-width=".7" opacity=".16"/>`;
    markup += `<text x="${centerX}" y="${top + 15}" text-anchor="middle" class="lv-small"><title>${escape(group.subjectName)}</title>${escape(short(group.subjectName, 16))}</text><text x="${centerX}" y="${top + 34}" text-anchor="middle"><title>${escape(group.label)}</title>${escape(short(group.label, Math.max(8, Math.floor(cellWidth / 13))))}</text>`;
    const hubX = centerX, hubY = top + 44;
    markup += `<circle cx="${hubX}" cy="${hubY}" r="2" fill="${color}" opacity=".6"/>`;
    group.nodes.forEach((node, index) => {
      const position = group.nodes.length === 1 ? .5 : index % 2 ? .72 : .28;
      const px = x + cellWidth * position, py = top + 86 + Math.floor(index / 2) * 98;
      const maximumRadius = Math.min(22, cellWidth * .11), radius = Math.sqrt(node.shownSeconds / unitMax) * maximumRadius;
      const active = recent(state, node), glow = glowFilter(state, node.group_id, active);
      markup += `<line x1="${hubX}" y1="${hubY}" x2="${px}" y2="${py}" stroke="${color}" opacity=".20" stroke-width=".7"/>`;
      const star = node.shownSeconds > 0 ? `<circle cx="${px}" cy="${py}" r="${radius}" fill="${color}" opacity=".88" ${glow}/><circle cx="${px - radius * .25}" cy="${py - radius * .25}" r="${radius * .27}" fill="white" opacity=".6"/>` : `<circle cx="${px}" cy="${py}" r="3" fill="none" stroke="${color}" opacity=".4"/>`;
      markup += svgLink(state, node, `<circle cx="${px}" cy="${py}" r="26" fill="transparent"/>${star}<circle class="lv-focus-ring" cx="${px}" cy="${py}" r="${maximumRadius + 4}" fill="none" stroke-width="1"/><text x="${px}" y="${py + 37}" text-anchor="middle">${escape(short(node.label, Math.max(6,Math.min(10,Math.floor(cellWidth*.4/12)))))}</text><text x="${px}" y="${py + 55}" text-anchor="middle" class="lv-small">${escape(formatSeconds(node.shownSeconds))}</text>`);
    });
    heights[column] += height + 16;
  }
  return { markup, height: Math.max(300, ...heights) + 8 };
}
function drawCrystals(state, nodes, width) {
  const groups = nodeGroups(nodes), columns = width < 500 ? 1 : width < 780 ? 2 : 3;
  const cellWidth = (width - 24) / columns, heights = Array(columns).fill(16), maxSeconds = Math.max(1, ...state.nodes.map(node => node.fullSeconds));
  let markup = "";
  for (const group of groups) {
    const column = heights.indexOf(Math.min(...heights)), top = heights[column], x = 12 + column * cellWidth;
    const rows = Math.ceil(group.nodes.length / 2), height = rows * 158 + 66, color = toneColor(state, group.subject);
    markup += `<path d="M ${x + 7} ${top + 42} L ${x + cellWidth - 7} ${top + 42} M ${x + 7} ${top + height - 4} L ${x + cellWidth - 7} ${top + height - 4}" fill="none" stroke="${color}" opacity=".3"/>`;
    markup += `<text x="${x + 12}" y="${top + 15}" class="lv-small"><title>${escape(group.subjectName)}</title>${escape(short(group.subjectName, 18))}</text><text x="${x + 12}" y="${top + 34}"><title>${escape(group.label)}</title>${escape(short(group.label, Math.max(8, Math.floor(cellWidth / 13))))}</text>`;
    group.nodes.forEach((node, index) => {
      const px = x + cellWidth * (group.nodes.length === 1 ? .5 : index % 2 ? .72 : .28), baseline = top + 144 + Math.floor(index / 2) * 158;
      const rx = Math.min(26, cellWidth * .105), ry = rx * .40, h = node.shownSeconds / maxSeconds * 78;
      const active = recent(state, node), glow = glowFilter(state, node.group_id, active);
      const topY = baseline - h;
      let crystal = `<ellipse cx="${px}" cy="${baseline + ry + 3}" rx="${rx * 1.45}" ry="${ry * .5}" fill="${color}" opacity=".09"/>`;
      if (h > 0) {
        crystal += `<g ${glow}><path d="M${px-rx},${topY} L${px},${topY+ry} L${px},${baseline+ry} L${px-rx},${baseline} Z" fill="${color}" opacity=".30"/><path d="M${px},${topY+ry} L${px+rx},${topY} L${px+rx},${baseline} L${px},${baseline+ry} Z" fill="${color}" opacity=".55"/><path d="M${px-rx},${topY} L${px},${topY-ry} L${px+rx},${topY} L${px},${topY+ry} Z" fill="${color}" opacity=".76"/><path d="M${px-rx},${baseline} L${px-rx},${topY} L${px},${topY-ry} L${px+rx},${topY} L${px+rx},${baseline} L${px},${baseline+ry} Z M${px},${topY+ry} L${px},${baseline+ry}" fill="none" stroke="${color}" stroke-width="1"/></g>`;
        const studiedDays = state.days.slice(0, state.day + 1).filter(item => number(node.daily[item.index]) > 0).length;
        // Every study day is represented; dense layers can merge optically, never cap the count.
        for (let layer = 1; layer <= studiedDays; layer++) {
          const y = baseline - h * layer / (studiedDays + 1);
          crystal += `<path d="M${px-rx},${y} L${px},${y+ry} L${px+rx},${y}" fill="none" stroke="${color}" opacity=".30" stroke-width=".55"/>`;
        }
      } else crystal += `<path d="M${px-rx},${baseline} L${px},${baseline-ry} L${px+rx},${baseline} L${px},${baseline+ry} Z" fill="none" stroke="${color}" opacity=".4"/>`;
      markup += svgLink(state, node, `<rect x="${px - 34}" y="${baseline - 96}" width="68" height="145" fill="transparent"/>${crystal}<ellipse class="lv-focus-ring" cx="${px}" cy="${baseline + ry + 3}" rx="${rx * 1.4}" ry="${ry * .7}" fill="none" stroke-width="1"/><text x="${px}" y="${baseline + 34}" text-anchor="middle">${escape(short(node.label,Math.max(6,Math.min(10,Math.floor(cellWidth*.4/12)))))}</text><text x="${px}" y="${baseline + 53}" text-anchor="middle" class="lv-small">${escape(formatSeconds(node.shownSeconds))}</text>`);
    });
    heights[column] += height + 12;
  }
  return { markup, height: Math.max(300, ...heights) + 8 };
}
function drawTrails(state, groups, width) {
  const left = width < 500 ? 18 : 24, right = width - 22, top = 55, lane = 90;
  const height = Math.max(290, top + groups.length * lane + 40), denominator = Math.max(1, state.days.length - 1);
  const xAt = day => left + day / denominator * (right - left);
  let maxDaily = 1;
  for (const group of state.trailGroups) for (const seconds of group.daily) maxDaily = Math.max(maxDaily, seconds);
  let markup = "";
  const ticks = [...new Set([0, Math.floor(denominator / 3), Math.floor(denominator * 2 / 3), state.days.length - 1])].filter(index => index >= 0);
  ticks.forEach((index, i) => {
    const x = xAt(index), day = state.days[index]?.day || "";
    markup += `<line x1="${x}" y1="30" x2="${x}" y2="${height-28}" stroke="var(--lv-line)" stroke-width=".7"/><text x="${x}" y="${height-9}" text-anchor="${i===0?"start":i===ticks.length-1?"end":"middle"}" class="lv-small">${escape(day.slice(5))}</text>`;
  });
  groups.forEach((group, groupIndex) => {
    const center = top + groupIndex * lane + 20, color = toneColor(state, group.id);
    markup += `<text x="${left}" y="${center-32}"><title>${escape(group.name)}</title>${escape(short(group.name,Math.max(6,Math.floor((width-175)/12))))}</text><text x="${right}" y="${center-32}" text-anchor="end" class="lv-small">${escape(formatSeconds(group.daily.slice(0,state.day+1).reduce((a,b)=>a+b,0)))}</text>`;
    const middle = index => center + Math.sin(index * .37 + stableHash(group.id) % 12) * 4;
    const thickness = seconds => seconds / maxDaily * 27;
    let segment = [];
    const flush = () => {
      if (!segment.length) return;
      if (segment.length === 1) {
        const [index, seconds] = segment[0];
        const x = xAt(index), t = thickness(seconds);
        markup += `<path d="M${x},${middle(index)-t} L${x},${middle(index)+t}" stroke="${color}" stroke-width="${Math.max(1,Math.min(6,(right-left)/denominator*.7))}" opacity=".82" ${glowFilter(state,group.id,true)}/>`;
      } else {
        const upper = segment.map(([index,sec], i) => `${i?"L":"M"}${xAt(index)},${middle(index)-thickness(sec)}`).join(" ");
        const lower = [...segment].reverse().map(([index,sec]) => `L${xAt(index)},${middle(index)+thickness(sec)}`).join(" ");
        markup += `<path d="${upper} ${lower} Z" fill="${color}" opacity=".73" ${glowFilter(state,group.id,true)}/>`;
      }
      segment = [];
    };
    for (let index = 0; index <= state.day; index++) {
      const seconds = group.daily[index];
      if (seconds > 0) segment.push([index, seconds]); else flush();
    }
    flush();
    const seconds = group.daily[state.day] || 0;
    if (seconds) markup += `<circle cx="${xAt(state.day)}" cy="${middle(state.day)}" r="3" fill="${color}" ${glowFilter(state,group.id,true)}/>`;
  });
  markup += `<line x1="${xAt(state.day)}" y1="28" x2="${xAt(state.day)}" y2="${height-27}" stroke="var(--lv-ink)" stroke-width="1" stroke-dasharray="3 5" opacity=".4"/>`;
  return { markup, height };
}
function unitButton(state, node, seconds) {
  const button = make(state, "button", "", node.label);
  button.type = "button";
  button.dataset.unit = node.id;
  button.title = `${node.project_name} / ${node.subject_name} / ${node.material_name} / ${node.label}`;
  button.append(make(state, "small", "", `${node.subject_name} / ${node.material_name} · ${formatSeconds(seconds)}`));
  button.onclick = () => pick(state, node.id);
  return button;
}
function drawDay(state) {
  const panel = query(state, ".lv-day-list");
  panel.hidden = state.mode !== "trails";
  if (panel.hidden) return;
  const index = state.days[state.day]?.index;
  const items = state.nodes.filter(node => number(node.daily[index]) > 0).sort((a,b) => number(b.daily[index])-number(a.daily[index]) || a.id.localeCompare(b.id));
  const count = 10, pages = Math.max(1, Math.ceil(items.length/count));
  state.dayPage = Math.min(state.dayPage, pages-1);
  query(state,".lv-day-items").replaceChildren(...items.slice(state.dayPage*count,(state.dayPage+1)*count).map(node => unitButton(state,node,number(node.daily[index]))));
  query(state,".lv-day-caption").textContent = items.length ? `${items.length}単元 · ${state.dayPage+1}/${pages}ページ（最大${count}件ずつ）` : "この日の学習ログはありません。";
  query(state,".lv-day-prev").disabled = state.dayPage===0;
  query(state,".lv-day-next").disabled = state.dayPage>=pages-1;
}
function drawDetail(state) {
  const node = state.nodes.find(node => node.id === state.selected);
  query(state,".lv-detail-placeholder").hidden = !!node;
  query(state,".lv-detail-body").hidden = !node;
  if (!node) return;
  const start = state.days[0]?.day || "", end = state.days[state.day]?.day || "";
  query(state,".lv-detail-path").textContent = `${node.project_name} / ${node.subject_name} / ${node.material_name}`;
  query(state,".lv-detail-title").textContent = node.label;
  query(state,".lv-detail-stats").textContent = `表示期間：${formatSeconds(node.shownSeconds)} · 全集計期間：${number(node.session_count)}件の学習記録 · 最終学習 ${node.last_day || "—"}`;
  query(state,".lv-detail-period").textContent = `${start} 〜 ${end}の履歴。関連ノートは全期間の内容です。`;
  const records = (Array.isArray(node.history)?node.history:[]).filter(record => record.day >= start && record.day <= end);
  const count=5, pages=Math.max(1,Math.ceil(records.length/count));
  state.recordPage=Math.min(state.recordPage,pages-1);
  const children=records.slice(state.recordPage*count,(state.recordPage+1)*count).map(record => {
    const item=make(state,"div","lv-record");
    const clock=value => /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(text(value)) ? text(value).slice(11,16) : "";
    const time = [clock(record.start_at),clock(record.end_at)].filter(Boolean).join("〜");
    item.append(make(state,"p","lv-record-heading",`${record.day} ${time} · ${formatSeconds(record.duration_sec)}`),make(state,"p","",record.memo || "メモなし"));
    return item;
  });
  if (!children.length) children.push(make(state,"p","lv-detail-period",node.shownSeconds>0?`日別の学習実績は${formatSeconds(node.shownSeconds)}ありますが、この範囲の個別履歴は直近の取得分に含まれていません。勉強ログページで確認できます。`:"この日付範囲で取得済みの履歴はありません。日別時間が0の記録も単元一覧から選択できます。"));
  query(state,".lv-records").replaceChildren(...children);
  const available=Array.isArray(node.history)?node.history.length:0, total=number(node.history_total);
  query(state,".lv-record-caption").textContent=`表示範囲 ${records.length}件 · ${state.recordPage+1}/${pages}ページ${total>available?` · 全${total}件中、直近${available}件を取得`:""}`;
  query(state,".lv-record-prev").disabled=state.recordPage===0;
  query(state,".lv-record-next").disabled=state.recordPage>=pages-1;
  const noteArea=query(state,".lv-notes"), notes=Array.isArray(node.notes)?node.notes:[];
  noteArea.replaceChildren(make(state,"h4","",`関連ノート ${number(node.note_count)}件`));
  for (const note of notes) {
    const item=make(state,"article","lv-note"); item.append(make(state,"h5","",note.title||"無題"),make(state,"p","",note.excerpt||"本文なし")); noteArea.append(item);
  }
  if (number(node.note_count)>notes.length) noteArea.append(make(state,"p","lv-detail-period",`${number(node.note_count)}件中${notes.length}件の抜粋を表示しています。全文は「ノート・知識ベース」ページで確認できます。`));
}
function draw(state) {
  if (state.disposed) return;
  const active=state.root.getRootNode().activeElement || state.document.activeElement,focusUnit=active?.dataset?.unit,focusTag=active?.tagName;
  theme(state);
  state.days=selectedDays(state);
  state.day=Math.max(0,Math.min(state.day,state.days.length-1));
  state.nodes=periodNodes(state);
  const total=state.nodes.reduce((sum,node)=>sum+node.shownSeconds,0);
  query(state,".lv-title").textContent=names[state.mode];
  query(state,".lv-eyebrow").textContent=state.mode==="trails"?"MONTHLY LEARNING TRAILS":"CUMULATIVE LEARNING ATLAS";
  query(state,".lv-total").textContent=`${formatSeconds(total)} / ${state.nodes.length}単元`;
  for (const button of state.root.querySelectorAll("[data-mode]")) button.setAttribute("aria-pressed",String(button.dataset.mode===state.mode));
  const slider=query(state,".lv-slider"); slider.max=String(Math.max(0,state.days.length-1)); slider.value=String(state.day); slider.disabled=!state.days.length;
  query(state,".lv-date").textContent=state.days[state.day]?.day || "—";
  query(state,".lv-play").disabled=!state.days.length || !state.nodes.length;
  query(state,".lv-explanation").textContent=explanations[state.mode];
  const width=Math.floor(query(state,".lv-scene").clientWidth||state.root.clientWidth||0);
  // Hidden panels have no measurable width; ResizeObserver redraws on reveal.
  if(width<=0)return;
  const pageSize=state.mode==="trails"?(width<500?4:6):(width<500?4:12);
  const groupMap=new Map();
  for (const node of state.nodes) {
    if (!groupMap.has(node.group_id)) groupMap.set(node.group_id,{id:node.group_id,name:node.subject_name,daily:state.days.map(()=>0)});
    const group=groupMap.get(node.group_id);
    state.days.forEach((item,index)=>group.daily[index]+=number(node.daily[item.index]));
  }
  state.trailGroups=[...groupMap.values()];
  const all=state.mode==="trails"?state.trailGroups:state.nodes, pages=Math.max(1,Math.ceil(all.length/pageSize));
  state.page=Math.min(state.page,pages-1);
  const visible=all.slice(state.page*pageSize,(state.page+1)*pageSize);
  const svg=query(state,".lv-svg"), empty=query(state,".lv-empty");
  svg.hidden=!visible.length;
  empty.hidden=!!visible.length;
  empty.textContent=state.payload.nodes.length?"この科目・教材・表示期間には学習時間の記録がありません。絞り込みを変更してください。":"学習ログを記録すると、ここに自分の銀河・航跡・結晶が育ちます。";
  if (visible.length) {
    const result=state.mode==="galaxy"?drawGalaxy(state,visible,width):state.mode==="crystals"?drawCrystals(state,visible,width):drawTrails(state,visible,width);
    svg.setAttribute("viewBox",`0 0 ${width} ${result.height}`);
    svg.style.height=`${result.height}px`;
    svg.setAttribute("aria-label",`${names[state.mode]}。${state.days[0]?.day}から${state.days[state.day]?.day}まで。単元一覧でも選択できます。`);
    svg.innerHTML=`<defs><filter id="${state.uid}-glow" x="-80%" y="-80%" width="260%" height="260%"><feGaussianBlur stdDeviation="4" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>${result.markup}`;
  } else svg.replaceChildren();
  const unitWord=state.mode==="trails"?"科目":"単元";
  const pageCaption=all.length?`${all.length}${unitWord}中 ${state.page*pageSize+1}〜${Math.min((state.page+1)*pageSize,all.length)} · ${state.page+1}/${pages}ページ`:`0${unitWord}`;
  if(query(state,".lv-page-caption").textContent!==pageCaption)query(state,".lv-page-caption").textContent=pageCaption;
  query(state,".lv-prev").disabled=state.page===0;
  query(state,".lv-next").disabled=state.page>=pages-1;
  const legend=query(state,".lv-legend"); legend.replaceChildren();
  const legendGroups=state.mode==="trails"?visible:[...new Map(visible.map(node=>[node.group_id,{id:node.group_id,name:node.subject_name}])).values()];
  for (const group of legendGroups) { const item=make(state,"span","",group.name),dot=make(state,"i",""); dot.style.backgroundColor=toneColor(state,group.id); item.prepend(dot); legend.append(item); }
  const indexNodes=state.mode==="trails"?state.nodes.filter(node=>visible.some(group=>group.id===node.group_id)):visible;
  // The full index is not silently truncated: its pages follow the visible page.
  const indexCount=12, indexPages=Math.max(1,Math.ceil(indexNodes.length/indexCount));
  state.indexPage=Math.min(state.indexPage,indexPages-1);
  const index=query(state,".lv-index-items");
  index.replaceChildren(...indexNodes.slice(state.indexPage*indexCount,(state.indexPage+1)*indexCount).map(node=>unitButton(state,node,node.shownSeconds)));
  if (indexPages>1) {
    const previous=make(state,"button","","一覧の前へ"),next=make(state,"button","","一覧の次へ");
    previous.type=next.type="button"; previous.disabled=state.indexPage===0; next.disabled=state.indexPage>=indexPages-1;
    previous.onclick=()=>{state.indexPage--;draw(state);};next.onclick=()=>{state.indexPage++;draw(state);};
    index.append(make(state,"p","lv-detail-period",`${indexNodes.length}単元 · 一覧 ${state.indexPage+1}/${indexPages}ページ`),previous,next);
  }
  drawDay(state); drawDetail(state);
  if(focusUnit){const restored=[...state.root.querySelectorAll("[data-unit]")].find(item=>item.dataset.unit===focusUnit&&item.tagName===focusTag);restored?.focus({preventScroll:true});}
}
function bind(state) {
  query(state,".lv-modes").onclick=event=>{
    const button=event.target.closest("button[data-mode]"); if(!button)return;
    stop(state);state.monthByMode[state.mode]=state.month;state.mode=button.dataset.mode;
    const months=[...new Set(state.payload.days.map(day=>day.slice(0,7)))];
    state.month=state.monthByMode[state.mode]??(state.mode==="trails"?months.at(-1)||"":"");
    refreshFilters(state);state.days=selectedDays(state);state.day=Math.max(0,state.days.length-1);
    state.page=state.indexPage=state.dayPage=state.recordPage=0;draw(state);
  };
  query(state,".lv-subject").onchange=event=>{stop(state);state.subject=event.target.value;state.material="";state.page=state.indexPage=state.dayPage=0;refreshFilters(state);draw(state);};
  query(state,".lv-material").onchange=event=>{stop(state);state.material=event.target.value;state.page=state.indexPage=state.dayPage=0;draw(state);};
  query(state,".lv-month").onchange=event=>{stop(state);state.month=event.target.value;state.monthByMode[state.mode]=state.month;state.days=selectedDays(state);state.day=Math.max(0,state.days.length-1);state.page=state.indexPage=state.dayPage=state.recordPage=0;draw(state);};
  query(state,".lv-style").onchange=event=>{state.style=event.target.value;draw(state);};
  query(state,".lv-slider").oninput=event=>{stop(state);setDate(state,event.target.value);};
  query(state,".lv-play").onclick=()=>{
    if (state.timer!==null){stop(state);return;}
    if(state.ownerWindow.matchMedia("(prefers-reduced-motion: reduce)").matches){setDate(state,state.days.length-1);return;}
    setDate(state,0);query(state,".lv-play").textContent="再生を停止";
    // One pass only, at most about 12 seconds for long periods.
    const step=Math.max(1,Math.ceil(state.days.length/72));
    state.timer=state.ownerWindow.setInterval(()=>{
      setDate(state,Math.min(state.day+step,state.days.length-1));
      if(state.day>=state.days.length-1)stop(state);
    },165);
  };
  query(state,".lv-svg").onclick=event=>{const link=event.target.closest("a[data-unit]");if(link){event.preventDefault();pick(state,link.dataset.unit);}};
  query(state,".lv-prev").onclick=()=>{stop(state);state.page=Math.max(0,state.page-1);state.indexPage=0;draw(state);};
  query(state,".lv-next").onclick=()=>{stop(state);state.page++;state.indexPage=0;draw(state);};
  query(state,".lv-day-prev").onclick=()=>{state.dayPage=Math.max(0,state.dayPage-1);drawDay(state);};
  query(state,".lv-day-next").onclick=()=>{state.dayPage++;drawDay(state);};
  query(state,".lv-record-prev").onclick=()=>{state.recordPage=Math.max(0,state.recordPage-1);drawDetail(state);};
  query(state,".lv-record-next").onclick=()=>{state.recordPage++;drawDetail(state);};
}
function dispose(state) {
  if(state.disposed)return;
  stop(state);state.disposed=true;
  state.resizeObserver?.disconnect();
  state.ownerWindow.removeEventListener("pagehide",state.onPageHide);
  if(state.resizeFrame!==null)state.ownerWindow.cancelAnimationFrame(state.resizeFrame);
  for(const element of state.root.querySelectorAll("button,select,input,svg,nav")){element.onclick=null;element.onchange=null;element.oninput=null;}
  instances.delete(state.parentElement);
}
export default function(component) {
  const {parentElement,data}=component;
  let state=instances.get(parentElement);
  if(state&&state.root!==parentElement.querySelector(".learning-visuals")){dispose(state);state=null;}
  if(!state||state.disposed){
    const root=parentElement.querySelector(".learning-visuals");if(!root)return;
    const document=parentElement.ownerDocument,ownerWindow=document.defaultView;
    state={parentElement,root,document,ownerWindow,uid:`learning-visual-${++componentSerial}`,mode:"galaxy",subject:"",material:"",month:"",monthByMode:{galaxy:"",crystals:""},style:"glow",page:0,indexPage:0,dayPage:0,recordPage:0,day:0,selected:"",timer:null,resizeFrame:null,disposed:false};
    instances.set(parentElement,state);bind(state);
    state.onPageHide=()=>dispose(state);ownerWindow.addEventListener("pagehide",state.onPageHide);
    let previousWidth=0;
    if(typeof ownerWindow.ResizeObserver==="function"){
      state.resizeObserver=new ownerWindow.ResizeObserver(entries=>{
        const width=Math.floor(entries[0]?.contentRect.width||0);
        if(width===previousWidth||width<1)return;previousWidth=width;
        if(state.resizeFrame!==null)ownerWindow.cancelAnimationFrame(state.resizeFrame);
        state.resizeFrame=ownerWindow.requestAnimationFrame(()=>{state.resizeFrame=null;draw(state);});
      });
      state.resizeObserver.observe(root);
    }
  }
  stop(state);
  const raw=data?.payload||{};
  const previousDay=state.days?.[state.day]?.day;
  state.payload={...raw,days:Array.isArray(raw.days)?raw.days.map(text):[],subjects:Array.isArray(raw.subjects)?raw.subjects:[],materials:Array.isArray(raw.materials)?raw.materials:[],nodes:Array.isArray(raw.nodes)?raw.nodes.map(node=>({...node,id:text(node.id),group_id:text(node.group_id),material_id:text(node.material_id),daily:Array.isArray(node.daily)?node.daily:[]})):[]};
  state.palette=data?.palette||{};
  refreshFilters(state);state.days=selectedDays(state);
  const restored=previousDay?state.days.findIndex(item=>item.day===previousDay):-1;
  state.day=restored>=0?restored:Math.max(0,state.days.length-1);
  draw(state);
  return ()=>dispose(state);
}
"""


def _get_component():
    return st.components.v2.component(
        "proloag_learning_visuals", html=_HTML, css=_CSS, js=_JS,
    )


def render_learning_visuals(
    payload: dict, *, palette: dict, key: str = "learning_accumulation_visuals"
) -> None:
    """Render all three read-only views from one validated learning snapshot."""
    if not callable(getattr(getattr(st.components, "v2", None), "component", None)):
        st.info("この環境では学習ビジュアライザーを表示できません。学習時間の一覧を表示します。")
        st.dataframe([
            {
                "科目": node.get("subject_name", ""),
                "教材": node.get("material_name", ""),
                "単元": node.get("label", ""),
                "学習時間（分）": round(float(node.get("total_sec", 0)) / 60, 1),
            }
            for node in payload.get("nodes", [])
        ], hide_index=True, width="stretch")
        return
    _get_component()(key=key, data={"payload": payload, "palette": palette}, width="stretch")
