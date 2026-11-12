"use strict";
// Tech3000: loads one data file per job-board platform (listed in data/meta.json), merges them,
// and does all searching and filtering in the browser. The first platform shows as soon as it
// arrives; the others are merged in as they finish loading.

const SOURCES = ["ashby", "greenhouse", "lever"];
const SOURCE_LABELS = ["Ashby", "Greenhouse", "Lever"];
const LEVELS = ["Internship", "Entry level", "", "Senior", "Staff / Lead", "Director+"];
const LEVELS_CSV = ["Internship", "Entry level", "Mid / not stated", "Senior", "Staff / Lead", "Director+"];
const WORK = ["", "Remote", "Hybrid", "In person"];
const EMPLOYMENT = ["Full time", "Part time", "Internship", "Contract", "Temporary"];
const SIZES = { s: [0, 10], m: [11, 50], l: [51, 200], xl: [201, Infinity] };
const DAY = 86400000;

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

// Merged data. companies: [name, slug, openRoles, sourceIndex]
const D = { companies: [], locations: [], departments: [], currencies: [], jobs: [] };
const tableIndex = { locations: new Map(), departments: new Map(), currencies: new Map() };
let hay = [], titles = [], locLower = [];
let companyByName = new Map(), companyByKey = new Map();
let results = [];
let stillLoading = [];        // labels of platforms not merged yet
let pendingCompany = null;    // company from a shared link whose platform hasn't loaded yet

// ---------------------------------------------------------------- data

function companyKey(i) {
  const [, slug, , src] = D.companies[i];
  return src === 0 ? slug : `${SOURCES[src]}:${slug}`;
}

function boardUrl(i) {
  const [, slug, , src] = D.companies[i];
  if (src === 1) return `https://job-boards.greenhouse.io/${encodeURIComponent(slug)}`;
  if (src === 2) return slug.startsWith("eu/") ? `https://jobs.eu.lever.co/${encodeURIComponent(slug.slice(3))}`
    : `https://jobs.lever.co/${encodeURIComponent(slug)}`;
  return `https://jobs.ashbyhq.com/${encodeURIComponent(slug)}`;
}

function link(j) {
  const l = j[11];
  if (/^https?:\/\//.test(l)) return l;
  const [, slug, , src] = D.companies[j[1]];
  if (src === 1) return `https://job-boards.greenhouse.io/${slug}/jobs/${l}`;
  if (src === 2) return slug.startsWith("eu/") ? `https://jobs.eu.lever.co/${slug.slice(3)}/${l}` : `https://jobs.lever.co/${slug}/${l}`;
  return `https://jobs.ashbyhq.com/${encodeURIComponent(slug)}/${l}`;
}

function remap(table, values) {
  const map = tableIndex[table], out = new Array(values.length);
  values.forEach((v, k) => {
    if (!map.has(v)) { map.set(v, D[table].length); D[table].push(v); }
    out[k] = map.get(v);
  });
  return out;
}

function merge(part) {
  const src = Math.max(0, SOURCES.indexOf(part.source || "ashby"));
  const offset = D.companies.length;
  for (const c of part.companies) D.companies.push([c[0], c[1], c[2], src]);
  const loc = remap("locations", part.locations), dept = remap("departments", part.departments),
    ccy = remap("currencies", part.currencies);
  for (const j of part.jobs) {
    j[1] += offset;
    j[2] = loc[j[2]];
    j[3] = dept[j[3]];
    if (j[9] != null) j[9] = ccy[j[9]];
    D.jobs.push(j);
  }
  D.jobs.sort((a, b) => (b[10] ?? -1) - (a[10] ?? -1));   // newest first across all platforms
}

// ---------------------------------------------------------------- state <-> URL

const FIELDS = ["q", "loc", "years", "smin", "smax", "ccy", "size", "company", "dept", "days", "sort"];
const CHIPS = ["work", "level", "src"];

function readState() {
  const p = new URLSearchParams(location.search);
  for (const id of FIELDS) if (p.has(id) && id !== "company") $(id).value = p.get(id);
  $("paid").checked = p.get("paid") === "1";
  for (const name of CHIPS) {
    const on = new Set((p.get(name) || "").split(",").filter(Boolean));
    document.querySelectorAll(`input[name=${name}]`).forEach((c) => { c.checked = on.has(c.value); });
  }
  pendingCompany = p.get("company");
  resolvePendingCompany();
}

function resolvePendingCompany() {
  if (!pendingCompany) return;
  const i = companyByKey.get(pendingCompany.toLowerCase());
  if (i != null) { $("company").value = D.companies[i][0]; pendingCompany = null; }
}

function checked(name) {
  return [...document.querySelectorAll(`input[name=${name}]:checked`)].map((c) => c.value);
}

function writeState(f) {
  const p = new URLSearchParams();
  for (const id of FIELDS) {
    let v;
    if (id === "company") v = f.company != null && f.company >= 0 ? companyKey(f.company) : (pendingCompany || "");
    else v = $(id).value.trim();
    if (v) p.set(id, v);
  }
  if ($("paid").checked) p.set("paid", "1");
  for (const name of CHIPS) { const v = checked(name); if (v.length) p.set(name, v.join(",")); }
  const qs = p.toString();
  history.replaceState(null, "", qs ? "?" + qs : location.pathname);
}

// ---------------------------------------------------------------- filtering

function num(v) { const n = parseFloat(v); return Number.isFinite(n) && n >= 0 ? n : null; }
function words(text) { return (text.toLowerCase().match(/[\p{L}\p{N}+#.]+/gu) || []).map((w) => w.replace(/\.+$/, "")).filter(Boolean); }

function readFilters() {
  const raw = $("company").value.trim();
  let company = null;
  if (raw) company = companyByName.get(raw.toLowerCase()) ?? companyByKey.get(raw.toLowerCase()) ?? -1;
  else if (pendingCompany && stillLoading.length) company = -2;   // waiting for its platform to load
  const ccy = $("ccy").value;
  const smin = num($("smin").value), smax = num($("smax").value);
  return {
    words: words($("q").value),
    loc: $("loc").value.trim().toLowerCase(),
    work: new Set(checked("work").map(Number)),
    level: new Set(checked("level").map(Number)),
    src: new Set(checked("src").map(Number)),
    years: num($("years").value),
    smin, smax,
    ccy: ccy === "" ? null : D.currencies.indexOf(ccy),
    pay: smin != null || smax != null || ccy !== "" || $("paid").checked,
    size: SIZES[$("size").value] || null,
    company,
    dept: $("dept").value === "" ? null : Number($("dept").value),
    since: num($("days").value) != null ? Math.floor(Date.now() / DAY) - num($("days").value) : null,
    sort: $("sort").value,
  };
}

function compute(f) {
  const J = D.jobs, C = D.companies, out = [];
  for (let i = 0; i < J.length; i++) {
    const j = J[i];
    if (f.company != null && j[1] !== f.company) continue;
    if (f.src.size && !f.src.has(C[j[1]][3])) continue;
    if (f.work.size && !f.work.has(j[4])) continue;
    if (f.level.size && !f.level.has(j[5])) continue;
    if (f.years != null && j[6] != null && j[6] > f.years) continue;
    if (f.pay) {
      if (j[7] == null) continue;
      if (f.smin != null && j[8] < f.smin) continue;
      if (f.smax != null && j[7] > f.smax) continue;
      if (f.ccy != null && j[9] !== f.ccy) continue;
    }
    if (f.size) { const n = C[j[1]][2]; if (n < f.size[0] || n > f.size[1]) continue; }
    if (f.dept != null && j[3] !== f.dept) continue;
    if (f.since != null && (j[10] == null || j[10] < f.since)) continue;
    if (f.loc && !locLower[j[2]].includes(f.loc)) continue;
    if (f.words.length) {
      const h = hay[i];
      let ok = true;
      for (const w of f.words) if (!h.includes(w)) { ok = false; break; }
      if (!ok) continue;
    }
    out.push(i);
  }
  // Jobs are kept newest first, so "newest" needs no sorting.
  if (f.sort === "pay") {
    out.sort((a, b) => (J[b][8] ?? -1) - (J[a][8] ?? -1));
  } else if (f.sort === "" && f.words.length) {
    const phrase = f.words.join(" ");
    const score = new Map();
    for (const i of out) {
      const t = titles[i];
      let s = t.includes(phrase) ? 10 : 0;
      for (const w of f.words) if (t.includes(w)) s += 2;
      score.set(i, s);
    }
    out.sort((a, b) => score.get(b) - score.get(a));
  }
  return out;
}

// ---------------------------------------------------------------- rendering

const fmtCache = new Map();
function money(v, ccy) {
  if (!fmtCache.has(ccy)) {
    let f;
    try { f = new Intl.NumberFormat("en", { style: "currency", currency: ccy, notation: "compact", maximumFractionDigits: 0 }); }
    catch { f = { format: (n) => `${ccy} ${Math.round(n / 1000)}K` }; }
    fmtCache.set(ccy, f);
  }
  return fmtCache.get(ccy).format(v);
}

function pay(j) {
  if (j[7] == null) return "";
  const c = D.currencies[j[9]];
  return j[7] === j[8] ? money(j[7], c) : `${money(j[7], c)}–${money(j[8], c)}`;
}

function posted(day) {
  if (day == null) return "";
  const ago = Math.floor(Date.now() / DAY) - day;
  if (ago <= 0) return "Posted today";
  if (ago === 1) return "Posted yesterday";
  if (ago < 30) return `Posted ${ago} days ago`;
  return "Posted " + new Date(day * DAY).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
}

const GROUP_PAGE = 20;   // companies per page
const PER_GROUP = 5;     // jobs shown per company before "show more"
let groups = [], shownGroups = 0;

function jobRow(i) {
  const j = D.jobs[i];
  const li = el("li");
  const a = el("a", "row");
  a.href = link(j);
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  const top = el("div", "jtop");
  top.append(el("span", "jtitle", j[0]));
  const p = pay(j);
  if (p) top.append(el("span", "jpay", p + " /yr"));
  const meta = el("div", "jmeta");
  const loc = D.locations[j[2]];
  if (loc) meta.append(el("span", null, loc));
  if (WORK[j[4]]) meta.append(el("span", "pill", WORK[j[4]]));
  if (LEVELS[j[5]]) meta.append(el("span", "pill", LEVELS[j[5]]));
  if (j[6]) meta.append(el("span", null, `${j[6]}+ yrs experience`));
  if (j[12]) meta.append(el("span", null, EMPLOYMENT[j[12]]));
  if (D.departments[j[3]]) meta.append(el("span", null, D.departments[j[3]]));
  const when = posted(j[10]);
  if (when) meta.append(el("span", null, when));
  a.append(top, meta);
  li.append(a);
  return li;
}

function groupCard(g) {
  const [name, , open, src] = D.companies[g.c];
  const li = el("li", "group");
  const head = el("div", "ghead");
  const left = el("div", "gleft");
  const title = el("a", "gname", name);
  title.href = boardUrl(g.c);
  title.target = "_blank";
  title.rel = "noopener noreferrer";
  left.append(title, el("span", "gsrc", SOURCE_LABELS[src]));
  const n = g.items.length;
  let count = `${n.toLocaleString()} matching job${n === 1 ? "" : "s"}`;
  if (open > n) count += ` · ${open.toLocaleString()} open in total`;
  head.append(left, el("span", "gcount", count));
  const ul = el("ul", "gjobs");
  for (const i of g.items.slice(0, PER_GROUP)) ul.append(jobRow(i));
  li.append(head, ul);
  if (n > PER_GROUP) {
    const b = el("button", "gmore", `Show ${(n - PER_GROUP).toLocaleString()} more from ${name}`);
    b.type = "button";
    b.addEventListener("click", () => {
      const frag = document.createDocumentFragment();
      for (const i of g.items.slice(PER_GROUP)) frag.append(jobRow(i));
      ul.append(frag);
      b.remove();
    });
    li.append(b);
  }
  return li;
}

function groupResults(ids) {
  // Companies appear in the order of their best-ranked job, so sorting still applies.
  const byCompany = new Map();
  for (const i of ids) {
    const c = D.jobs[i][1];
    if (!byCompany.has(c)) byCompany.set(c, []);
    byCompany.get(c).push(i);
  }
  return [...byCompany].map(([c, items]) => ({ c, items }));
}

function showMore() {
  const frag = document.createDocumentFragment();
  const end = Math.min(shownGroups + GROUP_PAGE, groups.length);
  for (let k = shownGroups; k < end; k++) frag.append(groupCard(groups[k]));
  $("list").append(frag);
  shownGroups = end;
  $("more").hidden = shownGroups >= groups.length;
  $("more").textContent = `Show more companies (${(groups.length - shownGroups).toLocaleString()} left)`;
}

let timer = null;
function schedule() { clearTimeout(timer); timer = setTimeout(update, 150); }

function loadingNote() {
  return stillLoading.length ? ` · still loading ${stillLoading.join(" and ")} jobs…` : "";
}

function update() {
  const f = readFilters();
  writeState(f);
  updateActiveCount();
  const list = $("list");
  list.replaceChildren();
  shownGroups = 0;
  if (f.company === -1 || f.company === -2) {
    results = [];
    groups = [];
    $("count").textContent = f.company === -2 || stillLoading.length ? "Loading…" : "Pick a company from the list.";
    $("more").hidden = true;
    $("export").disabled = true;
    return;
  }
  results = compute(f);
  groups = groupResults(results);
  const n = results.length, m = groups.length;
  $("count").replaceChildren(
    el("strong", null, n.toLocaleString()), document.createTextNode(n === 1 ? " job at " : " jobs at "),
    el("strong", null, m.toLocaleString()), document.createTextNode((m === 1 ? " company" : " companies") + loadingNote()));
  $("export").disabled = n === 0;
  if (!n) {
    list.append(el("li", "empty", "No jobs match these filters. Try removing one."));
    $("more").hidden = true;
    return;
  }
  showMore();
}

function updateActiveCount() {
  let n = checked("work").length + checked("level").length + checked("src").length + ($("paid").checked ? 1 : 0);
  for (const id of ["years", "smin", "smax", "ccy", "size", "company", "dept", "days"]) if ($(id).value.trim()) n++;
  $("activeCount").textContent = n ? String(n) : "";
}

// ---------------------------------------------------------------- export

function csvCell(v) {
  const s = v == null ? "" : String(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function exportCsv() {
  const rows = [["Title", "Company", "Location", "Work location", "Experience level", "Years asked for",
    "Salary min (yearly)", "Salary max (yearly)", "Currency", "Department", "Employment type", "Posted", "Job board", "Link"]];
  for (const i of results) {
    const j = D.jobs[i];
    const c = D.companies[j[1]];
    rows.push([j[0], c[0], D.locations[j[2]], WORK[j[4]], LEVELS_CSV[j[5]], j[6],
      j[7], j[8], j[9] == null ? "" : D.currencies[j[9]], D.departments[j[3]], EMPLOYMENT[j[12]],
      j[10] == null ? "" : new Date(j[10] * DAY).toISOString().slice(0, 10), SOURCE_LABELS[c[3]], link(j)]);
  }
  const blob = new Blob(["﻿" + rows.map((r) => r.map(csvCell).join(",")).join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = el("a");
  a.href = URL.createObjectURL(blob);
  a.download = `tech3000-jobs-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

// ---------------------------------------------------------------- setup

function buildIndexes() {
  const C = D.companies, deptCount = new Map(), ccyCount = new Map();
  locLower = D.locations.map((l) => l.toLowerCase());
  const coLower = C.map((c) => `${c[0].toLowerCase()} ${c[1].toLowerCase()}`);
  const deptLower = D.departments.map((d) => d.toLowerCase());
  titles = new Array(D.jobs.length);
  hay = new Array(D.jobs.length);
  for (let i = 0; i < D.jobs.length; i++) {
    const j = D.jobs[i];
    titles[i] = j[0].toLowerCase();
    hay[i] = `${titles[i]} ${coLower[j[1]]} ${deptLower[j[3]]}`;
    if (D.departments[j[3]]) deptCount.set(j[3], (deptCount.get(j[3]) || 0) + 1);
    if (j[9] != null) ccyCount.set(j[9], (ccyCount.get(j[9]) || 0) + 1);
  }
  companyByName = new Map();
  companyByKey = new Map();
  const order = C.map((c, i) => i).sort((a, b) => C[a][0].localeCompare(C[b][0]));
  const dl = document.createDocumentFragment();
  for (const i of order) {
    const [name, , open, src] = C[i];
    if (!companyByName.has(name.toLowerCase())) companyByName.set(name.toLowerCase(), i);
    companyByKey.set(companyKey(i).toLowerCase(), i);
    const o = document.createElement("option");
    o.value = name;
    o.label = `${open} open role${open === 1 ? "" : "s"} · ${SOURCE_LABELS[src]}`;
    dl.append(o);
  }
  $("companyList").replaceChildren(dl);
  const keep = { dept: $("dept").value, ccy: $("ccy").value };
  $("dept").replaceChildren(new Option("Any", ""));
  for (const [i, n] of [...deptCount].sort((a, b) => b[1] - a[1]).slice(0, 80))
    $("dept").append(new Option(`${D.departments[i]} (${n.toLocaleString()})`, String(i)));
  $("ccy").replaceChildren(new Option("Any currency", ""));
  for (const [i, n] of [...ccyCount].sort((a, b) => b[1] - a[1]))
    $("ccy").append(new Option(`${D.currencies[i]} (${n.toLocaleString()})`, D.currencies[i]));
  if (keep.dept && !$("dept").querySelector(`option[value="${keep.dept}"]`)) $("dept").append(new Option(D.departments[keep.dept] || keep.dept, keep.dept));
  if (keep.ccy && !$("ccy").querySelector(`option[value="${keep.ccy}"]`)) $("ccy").append(new Option(keep.ccy, keep.ccy));
  $("dept").value = keep.dept;
  $("ccy").value = keep.ccy;
}

function setIssuesLink() {
  const host = location.hostname;
  if (host.endsWith(".github.io")) {
    const user = host.split(".")[0];
    const repo = location.pathname.split("/").filter(Boolean)[0] || host;
    $("issues").href = `https://github.com/${user}/${repo}/issues`;
  } else {
    $("issues").removeAttribute("href");
  }
}

function skeleton() {
  const frag = document.createDocumentFragment();
  for (let k = 0; k < 6; k++) { const li = el("li"); li.append(el("div", "skeleton")); frag.append(li); }
  $("list").replaceChildren(frag);
}

function bindEvents() {
  for (const id of ["q", "loc", "smin", "smax", "company"]) $(id).addEventListener("input", () => {
    if (id === "company") pendingCompany = null;
    schedule();
  });
  for (const id of ["years", "ccy", "size", "dept", "days", "sort", "paid"]) $(id).addEventListener("change", update);
  document.querySelectorAll(CHIPS.map((n) => `input[name=${n}]`).join(",")).forEach((c) => c.addEventListener("change", update));
  $("more").addEventListener("click", showMore);
  $("export").addEventListener("click", exportCsv);
  $("clear").addEventListener("click", () => {
    for (const id of FIELDS) if (id !== "q" && id !== "loc" && id !== "sort") $(id).value = "";
    pendingCompany = null;
    $("paid").checked = false;
    document.querySelectorAll(CHIPS.map((n) => `input[name=${n}]`).join(",")).forEach((c) => { c.checked = false; });
    update();
  });
  const toggle = (open) => {
    $("filters").classList.toggle("open", open);
    $("toggleFilters").setAttribute("aria-expanded", String(open));
  };
  $("toggleFilters").addEventListener("click", () => toggle(!$("filters").classList.contains("open")));
  $("done").addEventListener("click", () => { toggle(false); $("results").focus(); });
}

async function init() {
  setIssuesLink();
  skeleton();
  let meta;
  try {
    meta = await (await fetch("data/meta.json", { cache: "no-cache" })).json();
  } catch (e) {
    $("list").replaceChildren(el("li", "empty", "The job data couldn't be loaded. Please refresh the page."));
    $("count").textContent = "";
    return;
  }
  const files = meta.files || [{ source: "ashby", label: "Ashby", file: "jobs.json" }];
  $("count").textContent = `Loading ${meta.jobs.toLocaleString()} jobs…`;
  $("last").textContent = "Last updated " + new Date(meta.updated).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })
    + ` · ${meta.jobs.toLocaleString()} jobs at ${meta.companies.toLocaleString()} companies`;
  stillLoading = files.map((f) => f.label);
  const requests = files.map((f) => fetch(`data/${f.file}?v=${encodeURIComponent(meta.updated)}`)
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }));
  let first = true, loaded = 0;
  for (let k = 0; k < files.length; k++) {
    try {
      merge(await requests[k]);
      loaded++;
    } catch (e) {
      console.warn(`Couldn't load ${files[k].file}`, e);
    }
    stillLoading = files.slice(k + 1).map((f) => f.label);
    if (!loaded) continue;
    buildIndexes();
    if (first) { readState(); bindEvents(); first = false; } else resolvePendingCompany();
    update();
  }
  if (!loaded) {
    $("list").replaceChildren(el("li", "empty", "The job data couldn't be loaded. Please refresh the page."));
    $("count").textContent = "";
  }
}

init();
