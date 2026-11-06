"use strict";
// Tech3000: loads data/jobs.json once and does all searching and filtering in the browser.

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

let D = null;            // the data file
let hay = [], titles = [], locLower = [];
let companyByName = new Map(), companyBySlug = new Map();
let results = [];

// ---------------------------------------------------------------- state <-> URL

const FIELDS = ["q", "loc", "years", "smin", "smax", "ccy", "size", "company", "dept", "days", "sort"];

function readState() {
  const p = new URLSearchParams(location.search);
  for (const id of FIELDS) if (p.has(id)) $(id).value = p.get(id);
  $("paid").checked = p.get("paid") === "1";
  for (const name of ["work", "level"]) {
    const on = new Set((p.get(name) || "").split(",").filter(Boolean));
    document.querySelectorAll(`input[name=${name}]`).forEach((c) => { c.checked = on.has(c.value); });
  }
  const slug = p.get("company");
  if (slug && companyBySlug.has(slug)) $("company").value = D.companies[companyBySlug.get(slug)][0];
}

function checked(name) {
  return [...document.querySelectorAll(`input[name=${name}]:checked`)].map((c) => c.value);
}

function writeState(f) {
  const p = new URLSearchParams();
  for (const id of FIELDS) {
    const v = id === "company" ? (f.company != null ? D.companies[f.company][1] : "") : $(id).value.trim();
    if (v) p.set(id, v);
  }
  if ($("paid").checked) p.set("paid", "1");
  for (const name of ["work", "level"]) { const v = checked(name); if (v.length) p.set(name, v.join(",")); }
  const qs = p.toString();
  history.replaceState(null, "", qs ? "?" + qs : location.pathname);
}

// ---------------------------------------------------------------- filtering

function num(v) { const n = parseFloat(v); return Number.isFinite(n) && n >= 0 ? n : null; }
function words(text) { return (text.toLowerCase().match(/[\p{L}\p{N}+#.]+/gu) || []).map((w) => w.replace(/\.+$/, "")).filter(Boolean); }

function readFilters() {
  const name = $("company").value.trim().toLowerCase();
  const company = name ? (companyByName.get(name) ?? companyBySlug.get($("company").value.trim()) ?? -1) : null;
  const ccy = $("ccy").value;
  const smin = num($("smin").value), smax = num($("smax").value);
  return {
    words: words($("q").value),
    loc: $("loc").value.trim().toLowerCase(),
    work: new Set(checked("work").map(Number)),
    level: new Set(checked("level").map(Number)),
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
  // The data file is newest first, so "newest" needs no sorting.
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

function link(j) {
  const l = j[11];
  return /^https?:\/\//.test(l) ? l : `https://jobs.ashbyhq.com/${encodeURIComponent(D.companies[j[1]][1])}/${l}`;
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
  const [name, slug, open] = D.companies[g.c];
  const li = el("li", "group");
  const head = el("div", "ghead");
  const title = el("a", "gname", name);
  title.href = `https://jobs.ashbyhq.com/${encodeURIComponent(slug)}`;
  title.target = "_blank";
  title.rel = "noopener noreferrer";
  const n = g.items.length;
  let count = `${n.toLocaleString()} matching job${n === 1 ? "" : "s"}`;
  if (open > n) count += ` · ${open.toLocaleString()} open in total`;
  head.append(title, el("span", "gcount", count));
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

function update() {
  const f = readFilters();
  writeState(f);
  updateActiveCount();
  const list = $("list");
  list.replaceChildren();
  shownGroups = 0;
  if (f.company === -1) {
    results = [];
    groups = [];
    $("count").textContent = "Pick a company from the list.";
    $("more").hidden = true;
    $("export").disabled = true;
    return;
  }
  results = compute(f);
  groups = groupResults(results);
  const n = results.length, m = groups.length;
  $("count").replaceChildren(
    el("strong", null, n.toLocaleString()), document.createTextNode(n === 1 ? " job at " : " jobs at "),
    el("strong", null, m.toLocaleString()), document.createTextNode(m === 1 ? " company" : " companies"));
  $("export").disabled = n === 0;
  if (!n) {
    const li = el("li", "empty", "No jobs match these filters. Try removing one.");
    list.append(li);
    $("more").hidden = true;
    return;
  }
  showMore();
}

function updateActiveCount() {
  let n = checked("work").length + checked("level").length + ($("paid").checked ? 1 : 0);
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
    "Salary min (yearly)", "Salary max (yearly)", "Currency", "Department", "Employment type", "Posted", "Link"]];
  for (const i of results) {
    const j = D.jobs[i];
    rows.push([j[0], D.companies[j[1]][0], D.locations[j[2]], WORK[j[4]], LEVELS_CSV[j[5]], j[6],
      j[7], j[8], j[9] == null ? "" : D.currencies[j[9]], D.departments[j[3]], EMPLOYMENT[j[12]],
      j[10] == null ? "" : new Date(j[10] * DAY).toISOString().slice(0, 10), link(j)]);
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
  for (let i = 0; i < D.jobs.length; i++) {
    const j = D.jobs[i];
    titles[i] = j[0].toLowerCase();
    hay[i] = `${titles[i]} ${C[j[1]][0].toLowerCase()} ${C[j[1]][1].toLowerCase()} ${D.departments[j[3]].toLowerCase()}`;
    if (D.departments[j[3]]) deptCount.set(j[3], (deptCount.get(j[3]) || 0) + 1);
    if (j[9] != null) ccyCount.set(j[9], (ccyCount.get(j[9]) || 0) + 1);
  }
  const order = C.map((c, i) => i).sort((a, b) => C[a][0].localeCompare(C[b][0]));
  const dl = document.createDocumentFragment();
  for (const i of order) {
    const [name, slug, open] = C[i];
    if (!companyByName.has(name.toLowerCase())) companyByName.set(name.toLowerCase(), i);
    companyBySlug.set(slug, i);
    const o = document.createElement("option");
    o.value = name;
    o.label = `${open} open role${open === 1 ? "" : "s"}`;
    dl.append(o);
  }
  $("companyList").append(dl);
  for (const [i, n] of [...deptCount].sort((a, b) => b[1] - a[1]).slice(0, 60))
    $("dept").append(new Option(`${D.departments[i]} (${n.toLocaleString()})`, String(i)));
  for (const [i, n] of [...ccyCount].sort((a, b) => b[1] - a[1]))
    $("ccy").append(new Option(`${D.currencies[i]} (${n.toLocaleString()})`, D.currencies[i]));
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

async function init() {
  setIssuesLink();
  skeleton();
  try {
    const meta = await (await fetch("data/meta.json", { cache: "no-cache" })).json();
    $("count").textContent = `Loading ${meta.jobs.toLocaleString()} jobs…`;
    const res = await fetch(`data/jobs.json?v=${encodeURIComponent(meta.updated)}`);
    if (!res.ok) throw new Error(res.status);
    D = await res.json();
  } catch (e) {
    $("list").replaceChildren(el("li", "empty", "The job data couldn't be loaded. Please refresh the page."));
    $("count").textContent = "";
    return;
  }
  const when = new Date(D.updated);
  $("last").textContent = "Last updated " + when.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })
    + ` · ${D.jobs.length.toLocaleString()} jobs at ${D.companies.length.toLocaleString()} companies`;
  buildIndexes();
  readState();

  for (const id of ["q", "loc", "smin", "smax", "company"]) $(id).addEventListener("input", schedule);
  for (const id of ["years", "ccy", "size", "dept", "days", "sort", "paid"]) $(id).addEventListener("change", update);
  document.querySelectorAll("input[name=work], input[name=level]").forEach((c) => c.addEventListener("change", update));
  $("more").addEventListener("click", showMore);
  $("export").addEventListener("click", exportCsv);
  $("clear").addEventListener("click", () => {
    for (const id of FIELDS) if (id !== "q" && id !== "loc" && id !== "sort") $(id).value = "";
    $("paid").checked = false;
    document.querySelectorAll("input[name=work], input[name=level]").forEach((c) => { c.checked = false; });
    update();
  });
  const toggle = (open) => {
    $("filters").classList.toggle("open", open);
    $("toggleFilters").setAttribute("aria-expanded", String(open));
  };
  $("toggleFilters").addEventListener("click", () => toggle(!$("filters").classList.contains("open")));
  $("done").addEventListener("click", () => { toggle(false); $("results").focus(); });
  update();
}

init();
