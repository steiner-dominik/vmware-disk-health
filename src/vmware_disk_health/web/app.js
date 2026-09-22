import { timeChart } from "./charts.js";
import { STRINGS } from "./i18n.js";

const HOUR = 3600e3;
const DAY = 24 * HOUR;
const RANK = { critical: 3, warning: 2, unknown: 1, missing: 1, ok: 0 };
const ERROR_COUNTERS = ["reallocated_sectors", "pending_sectors", "offline_uncorrectable", "reported_uncorrectable", "crc_errors", "media_errors"];

const store = {
  lang: "en",
  state: null,
  error: null,
  range: 30,
  detailKey: null,
  detailSeen: null,
  refreshTimer: null,
};

// ------------------------------------------------------------------ storage helpers

function load(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function save(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* private mode: settings just don't persist */
  }
}

// ------------------------------------------------------------------ i18n & formatting

function pickLanguage(preference) {
  const stored = load("vdh-lang");
  if (stored && STRINGS[stored]) return stored;
  if (STRINGS[preference]) return preference;
  return (navigator.language || "en").toLowerCase().startsWith("de") ? "de" : "en";
}

function t(key, params = {}) {
  const text = STRINGS[store.lang][key] ?? STRINGS.en[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (_, name) => (params[name] ?? "").toString());
}

const DASH = "–";
const numberFormats = new Map();
function nf(maxDigits = 0, minDigits = 0) {
  const id = `${store.lang}:${maxDigits}:${minDigits}`;
  if (!numberFormats.has(id)) {
    numberFormats.set(id, new Intl.NumberFormat(store.lang, { maximumFractionDigits: maxDigits, minimumFractionDigits: minDigits }));
  }
  return numberFormats.get(id);
}

function fmtBytes(value) {
  if (value === null || value === undefined) return DASH;
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = value <= 0 ? 0 : Math.min(units.length - 1, Math.floor(Math.log10(value) / 3));
  const scaled = value / 1000 ** i;
  return `${nf(scaled < 10 && i > 0 ? 2 : scaled < 100 && i > 0 ? 1 : 0).format(scaled)} ${units[i]}`;
}

function fmtCapacity(value) {
  if (!value) return DASH;
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log10(value) / 3));
  const scaled = new Intl.NumberFormat(store.lang, { maximumSignificantDigits: 3 }).format(value / 1000 ** i);
  return `${scaled} ${units[i]}`;
}

const fmtTemp = (v) => (v === null || v === undefined ? DASH : `${nf(0).format(v)} °C`);
const fmtPct = (v) => (v === null || v === undefined ? DASH : `${nf(Number.isInteger(v) ? 0 : 1).format(v)} %`);
const fmtInt = (v) => (v === null || v === undefined ? DASH : nf(0).format(v));

function fmtHours(hours) {
  if (hours === null || hours === undefined) return DASH;
  if (hours >= 8766) return t("unit.years", { n: nf(1).format(hours / 8766) });
  if (hours >= 48) return t("unit.days", { n: nf(0).format(hours / 24) });
  return t("unit.hours", { n: nf(0).format(hours) });
}

function fmtRelative(seconds) {
  if (!seconds) return DASH;
  const diff = seconds * 1000 - Date.now();
  const abs = Math.abs(diff);
  if (abs < 45e3) return t("time.justNow");
  const rtf = new Intl.RelativeTimeFormat(store.lang, { numeric: "auto" });
  if (abs < HOUR) return rtf.format(Math.round(diff / 60e3), "minute");
  if (abs < DAY) return rtf.format(Math.round(diff / HOUR), "hour");
  return rtf.format(Math.round(diff / DAY), "day");
}

function fmtDateTime(ms) {
  return new Intl.DateTimeFormat(store.lang, { dateStyle: "medium", timeStyle: "short" }).format(ms);
}

function fmtTick(ms, unit) {
  const options = {
    hour: { hour: "2-digit", minute: "2-digit" },
    day: { day: "numeric", month: "short" },
    month: { month: "short", year: "2-digit" },
    year: { year: "numeric" },
  }[unit];
  if (!options) return fmtDateTime(ms);
  return new Intl.DateTimeFormat(store.lang, options).format(ms);
}

// ------------------------------------------------------------------ DOM helpers

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "hidden") node.hidden = true;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// Constant, trusted markup only.
const ICONS = {
  ok: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="currentColor"/><path d="M4.8 8.3l2.1 2.1 4.3-4.5" fill="none" stroke="#fff" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  warning:
    '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M8 1.3l7.2 13H.8z" fill="currentColor" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/><path d="M8 5.8v3.6" stroke="#1a1a19" stroke-width="1.7" stroke-linecap="round"/><circle cx="8" cy="11.9" r="1" fill="#1a1a19"/></svg>',
  critical:
    '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M5.1 1h5.8L15 5.1v5.8L10.9 15H5.1L1 10.9V5.1z" fill="currentColor"/><path d="M5.6 5.6l4.8 4.8M10.4 5.6l-4.8 4.8" stroke="#fff" stroke-width="1.7" stroke-linecap="round"/></svg>',
  unknown:
    '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="currentColor"/><path d="M6.2 6.2a1.9 1.9 0 1 1 2.6 1.7c-.5.3-.8.6-.8 1.2v.3" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="11.7" r=".95" fill="#fff"/></svg>',
  chevron:
    '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M6 3.5L10.5 8 6 12.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

function icon(name) {
  const template = document.createElement("template");
  template.innerHTML = ICONS[name];
  return template.content.firstChild;
}

function statusEl(status, { pill = false } = {}) {
  const glyph = status === "missing" ? "unknown" : status;
  return h("span", { class: `status status-${status}${pill ? " pill" : ""}` }, icon(glyph), t(`status.${status}`));
}

function toast(message) {
  const node = h("div", { class: "toast", role: "status", text: message });
  document.body.append(node);
  setTimeout(() => node.remove(), 2600);
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch {
      /* not JSON */
    }
    throw new Error(`${response.status} ${detail}`);
  }
  return response.json();
}

function findingText(finding) {
  const key = `finding.${finding.code}`;
  const known = STRINGS.en[key] !== undefined;
  const value = typeof finding.value === "number" ? nf(finding.value % 1 ? 1 : 0).format(finding.value) : finding.value;
  const limit = typeof finding.limit === "number" ? nf(0).format(finding.limit) : finding.limit;
  return known ? t(key, { value, limit, message: finding.message }) : finding.message;
}

function effectiveStatus(disk) {
  return disk.missing ? "missing" : disk.status;
}

const diskHref = (key) => `#/disk/${encodeURIComponent(key)}`;
const diskTitle = (disk) => disk.name || disk.model || disk.key;
// Last few characters of a device id: distinguishes same-model disks that have no
// serial number (drives reporting their own WWN, which esxcli names naa.*/eui.*).
const shortId = (key) => (key.replace(/^[a-z0-9]+\./i, "").slice(-8) || key).toUpperCase();

// ------------------------------------------------------------------ shell

function renderShell() {
  document.documentElement.lang = store.lang;
  document.title = t("app.title");
  const langSelect = h(
    "select",
    { class: "select", "aria-label": t("lang.label"), onChange: (e) => setLanguage(e.target.value) },
    h("option", { value: "en", text: "English" }),
    h("option", { value: "de", text: "Deutsch" }),
  );
  langSelect.value = store.lang;
  const themeSelect = h(
    "select",
    { class: "select", "aria-label": t("theme.label"), onChange: (e) => setTheme(e.target.value) },
    ["auto", "light", "dark"].map((value) => h("option", { value, text: t(`theme.${value}`) })),
  );
  themeSelect.value = load("vdh-theme") || "auto";

  const header = h(
    "header",
    { class: "topbar" },
    h(
      "div",
      { class: "wrap topbar-inner" },
      h("a", { class: "brand", href: "#/" }, h("img", { src: "static/icon.svg", alt: "" }), t("app.title")),
      h(
        "nav",
        { class: "nav" },
        ["overview", "events", "setup"].map((name) => h("a", { href: name === "overview" ? "#/" : `#/${name}`, "data-route": name, text: t(`nav.${name}`) })),
      ),
      h(
        "div",
        { class: "topbar-end" },
        h("span", { class: "poll-status", id: "poll-status" }),
        h("button", { class: "btn btn-primary", id: "poll-button", onClick: pollNow }, t("poll.now")),
        langSelect,
        themeSelect,
      ),
    ),
  );
  const app = document.getElementById("app");
  app.replaceChildren(header, h("main", { class: "wrap", id: "view" }));
  updateHeader();
}

function updateHeader() {
  const status = document.getElementById("poll-status");
  const button = document.getElementById("poll-button");
  if (!status || !store.state) return;
  const app = store.state.app;
  if (app.polling) {
    status.textContent = t("poll.running");
  } else if (app.last_poll_finished) {
    const parts = [t("poll.last", { when: fmtRelative(app.last_poll_finished) })];
    if (app.next_poll_at) parts.push(t("poll.next", { when: fmtRelative(app.next_poll_at) }));
    status.textContent = parts.join(" · ");
  } else {
    status.textContent = t("poll.never");
  }
  button.disabled = app.polling;
  button.replaceChildren(...(app.polling ? [h("span", { class: "spinner" }), t("poll.running")] : [t("poll.now")]));
  const route = currentRoute().name;
  const section = route === "disk" ? "overview" : route;
  for (const link of document.querySelectorAll(".nav a")) {
    if (link.dataset.route === section) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function setLanguage(lang) {
  store.lang = lang;
  save("vdh-lang", lang);
  numberFormats.clear();
  renderShell();
  route();
}

function setTheme(theme) {
  save("vdh-theme", theme === "auto" ? null : theme);
  if (theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
}

async function pollNow() {
  try {
    const result = await api("api/poll", { method: "POST" });
    toast(result.started ? t("poll.started") : t("poll.busy"));
  } catch (error) {
    toast(t("error.load", { error: error.message }));
  }
  setTimeout(refreshState, 800);
}

// ------------------------------------------------------------------ state refresh

async function refreshState() {
  clearTimeout(store.refreshTimer);
  try {
    store.state = await api("api/state");
    store.error = null;
  } catch (error) {
    store.error = error;
  }
  updateHeader();
  const current = currentRoute();
  if (current.name === "overview") renderOverview();
  if (current.name === "disk" && store.state) {
    const disk = store.state.disks.find((d) => d.key === current.key);
    if (disk && disk.last_seen !== store.detailSeen) renderDisk(current.key);
  }
  store.refreshTimer = setTimeout(refreshState, store.state?.app.polling ? 3000 : 30000);
}

// ------------------------------------------------------------------ routing

function currentRoute() {
  const hash = location.hash.slice(1) || "/";
  if (hash.startsWith("/disk/")) return { name: "disk", key: decodeURIComponent(hash.slice(6)) };
  if (hash === "/events") return { name: "events" };
  if (hash === "/setup") return { name: "setup" };
  return { name: "overview" };
}

function route() {
  const current = currentRoute();
  updateHeader();
  window.scrollTo(0, 0);
  if (current.name === "disk") renderDisk(current.key);
  else if (current.name === "events") renderEvents();
  else if (current.name === "setup") renderSetup();
  else renderOverview();
}

function view() {
  return document.getElementById("view");
}

function errorBanner(error) {
  return h("div", { class: "banner", role: "alert" }, icon("critical"), h("div", {}, t("error.load", { error: error.message })));
}

// ------------------------------------------------------------------ overview

function renderOverview() {
  const target = view();
  const s = store.state;
  if (!s) {
    target.replaceChildren(store.error ? errorBanner(store.error) : h("p", { class: "empty", text: t("empty.waiting") }));
    return;
  }
  const blocks = [];
  if (store.error) blocks.push(errorBanner(store.error));
  if (!s.hosts.length) {
    target.replaceChildren(...blocks, h("div", { class: "card empty", text: t("empty.noHosts") }));
    return;
  }

  const reachable = s.hosts.filter((host) => host.last_success && host.last_success === host.last_attempt).length;
  blocks.push(
    h(
      "section",
      {},
      h(
        "div",
        { class: "summary" },
        ["critical", "warning", "unknown", "ok"].map((status) =>
          h("div", { class: `card${s.counts[status] ? "" : " is-zero"}` }, statusEl(status), h("div", { class: "count", text: fmtInt(s.counts[status]) })),
        ),
      ),
      h("div", {
        class: "summary-note",
        text: `${t("summary.hosts", { up: reachable, total: s.hosts.length })} · ${t("summary.disks", { count: s.disks.length })}`,
      }),
    ),
  );

  const attention = s.disks.filter((d) => effectiveStatus(d) !== "ok").sort((a, b) => RANK[effectiveStatus(b)] - RANK[effectiveStatus(a)]);
  if (attention.length) {
    blocks.push(
      h(
        "section",
        { class: "section" },
        h("div", { class: "section-head" }, h("h2", { text: t("attention.title") })),
        h(
          "ul",
          { class: "card attention" },
          attention.map((disk) =>
            h(
              "li",
              {},
              h(
                "a",
                { href: diskHref(disk.key) },
                statusEl(effectiveStatus(disk)),
                h("span", {}, h("span", { class: "disk-name", text: diskTitle(disk) }), h("span", { class: "muted", text: ` · ${disk.host}` })),
                icon("chevron"),
                h("span", {
                  class: "what",
                  style: "grid-column: 2 / -1",
                  text: disk.missing ? t("disk.missing", { when: fmtRelative(disk.last_seen) }) : disk.findings.map(findingText).join(" · "),
                }),
              ),
            ),
          ),
        ),
      ),
    );
  }

  for (const host of s.hosts) blocks.push(hostSection(host, s.disks.filter((d) => d.host === host.name)));
  target.replaceChildren(...blocks);
}

function hostSection(host, disks) {
  const failed = host.last_attempt && host.last_attempt !== host.last_success;
  const head = h(
    "div",
    { class: "section-head" },
    h("h2", { text: host.name }),
    h("span", { class: "meta mono", text: host.address }),
    host.esxi_version && h("span", { class: "badge", text: host.esxi_version.replace(/^VMware /, "") }),
    host.last_success &&
      h("span", {
        class: "badge",
        title: t(host.smartctl_path ? "host.smartctl.hint" : "host.builtin.hint"),
        text: t(host.smartctl_path ? "host.smartctl" : "host.builtin"),
      }),
    host.last_success && h("span", { class: "meta", text: t("host.lastSuccess", { when: fmtRelative(host.last_success) }) }),
  );
  const parts = [head];
  if (failed) {
    parts.push(
      h(
        "div",
        { class: "banner", role: "alert" },
        icon("critical"),
        h(
          "div",
          {},
          h("div", { text: t("host.unreachable", { when: fmtRelative(host.last_attempt) }) }),
          host.last_error && h("div", { class: "mono secondary", text: host.last_error }),
          h("a", { href: "#/setup", text: t("host.setupLink") }),
        ),
      ),
    );
  }
  if (!host.last_attempt) {
    parts.push(h("div", { class: "card empty", text: t(store.state.app.polling ? "empty.waiting" : "host.never") }));
  } else if (!disks.length && host.last_success) {
    parts.push(h("div", { class: "card empty", text: t("host.noDisks") }));
  } else if (!disks.length) {
    // Never collected successfully: the error banner above says it all.
  } else {
    parts.push(h("div", { class: "card table-wrap" }, diskTable(disks)));
  }
  return h("section", { class: "section" }, parts);
}

const isNum = (v) => v !== null && v !== undefined;

function lifeMeter(disk) {
  const estimated = !isNum(disk.life_remaining_pct) && isNum(disk.life_remaining_estimated_pct);
  const remaining = estimated ? disk.life_remaining_estimated_pct : disk.life_remaining_pct;
  if (!isNum(remaining)) return h("span", { class: "muted", text: DASH });
  const finding = disk.findings.find((f) => f.code === (estimated ? "life_low_estimated" : "life_low"));
  const severity = finding ? (finding.severity === 3 ? " is-critical" : " is-warning") : "";
  return h(
    "div",
    { class: `meter${estimated ? " is-estimated" : ""}`, title: estimated ? t("life.estimated.title") : null },
    h("span", {}, estimated && h("small", { class: "est", text: `${t("life.estimated.short")} ` }), fmtPct(Math.round(remaining))),
    h("div", { class: "meter-track", "aria-hidden": "true" }, h("div", { class: `meter-fill${severity}`, style: `width:${Math.max(2, remaining)}%` })),
  );
}

function vsanBadge(tier, group) {
  if (!tier) return null;
  return h("span", { class: `badge badge-vsan is-${tier}`, title: group ? t("badge.vsan.hint", { group }) : null, text: t(`badge.vsan.${tier}`) });
}

function diskTable(disks) {
  const sorted = [...disks].sort((a, b) => RANK[effectiveStatus(b)] - RANK[effectiveStatus(a)]);
  return h(
    "table",
    { class: "disks" },
    h(
      "thead",
      {},
      h(
        "tr",
        {},
        h("th", { text: t("col.status") }),
        h("th", { text: t("col.disk") }),
        h("th", { class: "num", text: t("col.temperature") }),
        h("th", { class: "num", text: t("col.life") }),
        h("th", { class: "num", text: t("col.written") }),
        h("th", { class: "num", text: t("col.powerOn") }),
        h("th", {}),
      ),
    ),
    h(
      "tbody",
      {},
      sorted.map((disk) =>
        h(
          "tr",
          { class: disk.missing ? "is-missing" : "", onClick: () => (location.hash = diskHref(disk.key)) },
          h("td", {}, statusEl(effectiveStatus(disk))),
          h(
            "td",
            { class: "cell-disk" },
            h("a", { class: "disk-name", href: diskHref(disk.key), text: diskTitle(disk), onClick: (e) => e.stopPropagation() }),
            h(
              "div",
              { class: "disk-sub" },
              disk.name && disk.model && h("span", { text: disk.model }),
              disk.serial
                ? h("span", { class: "mono", text: disk.serial })
                : h("span", { class: "mono muted", text: shortId(disk.key) }),
              h("span", { class: "badge", text: t(`kind.${disk.kind}`) }),
              h("span", { text: fmtCapacity(disk.size_bytes) }),
              vsanBadge(disk.vsan_tier),
              disk.is_boot && h("span", { class: "badge", text: t("badge.boot") }),
            ),
          ),
          h("td", { class: "num", "data-label": t("col.temperature"), text: fmtTemp(disk.temperature_c) }),
          h("td", { class: "num", "data-label": t("col.life") }, lifeMeter(disk)),
          h("td", { class: "num", "data-label": t("col.written"), text: fmtBytes(disk.written_bytes) }),
          h("td", { class: "num", "data-label": t("col.powerOn"), text: fmtHours(disk.power_on_hours) }),
          h("td", { class: "cell-chevron muted" }, icon("chevron")),
        ),
      ),
    ),
  );
}

// ------------------------------------------------------------------ disk detail

async function renderDisk(key) {
  const target = view();
  if (store.detailKey !== key) {
    store.detailKey = key;
    target.replaceChildren(h("p", { class: "empty", text: t("empty.waiting") }));
  }
  let detail;
  try {
    detail = await api(`api/disks/${encodeURIComponent(key)}`);
  } catch (error) {
    if (currentRoute().key !== key) return;
    target.replaceChildren(error.message.startsWith("404") ? h("div", { class: "card empty", text: t("disk.notFound") }) : errorBanner(error));
    return;
  }
  if (currentRoute().key !== key) return;
  store.detailSeen = detail.summary.last_seen;

  const { disk, summary, insights: info } = detail;
  const r = disk.reading;
  const status = effectiveStatus(summary);

  const header = h(
    "div",
    {},
    h("div", { class: "crumbs" }, h("a", { href: "#/", text: t("disk.back") }), ` / ${disk.host}`),
    h("div", { class: "detail-head" }, h("h1", { text: diskTitle(summary) }), statusEl(status, { pill: true })),
    h(
      "div",
      { class: "detail-sub" },
      summary.name && h("span", { text: disk.info.model }),
      disk.info.serial
        ? h("span", { class: "mono", text: disk.info.serial })
        : h("span", { class: "mono muted", text: shortId(disk.info.device_id) }),
      h("span", { class: "badge", text: t(`kind.${disk.info.kind}`) }),
      h("span", { text: fmtCapacity(disk.info.size_bytes) }),
      disk.info.format_type && h("span", { class: "badge", text: disk.info.format_type }),
      vsanBadge(disk.info.vsan_tier, disk.info.vsan_disk_group),
      disk.info.is_boot && h("span", { class: "badge", text: t("badge.boot") }),
    ),
  );

  const blocks = [header];
  if (summary.missing) {
    blocks.push(h("div", { class: "banner is-warning section" }, icon("warning"), h("div", { text: t("disk.missing", { when: fmtRelative(summary.last_seen) }) })));
  }

  const severityIcon = (label) => h("span", { class: `status status-${label}` }, icon(label));
  const findings = disk.findings.length
    ? disk.findings.map((f) => h("li", {}, severityIcon(["ok", "unknown", "warning", "critical"][f.severity]), h("span", { text: findingText(f) })))
    : [h("li", {}, severityIcon("ok"), h("span", { text: t("disk.noFindings") }))];
  blocks.push(
    h(
      "section",
      { class: "section" },
      h("div", { class: "section-head" }, h("h2", { text: t("disk.findings") })),
      h(
        "div",
        { class: "card" },
        h("ul", { class: "findings" }, findings),
        disk.errors.length > 0 && h("div", { class: "notes" }, t("disk.notes"), h("ul", {}, disk.errors.map((e) => h("li", { text: e })))),
      ),
    ),
  );

  blocks.push(h("section", { class: "section tiles" }, tiles(detail, r, info)));

  const charts = h("div", { class: "charts" });
  const rangeBar = h(
    "div",
    { class: "range", role: "group", "aria-label": t("range.label") },
    [7, 30, 90, 365, 0].map((days) =>
      h("button", {
        type: "button",
        "aria-pressed": String(store.range === days),
        text: t(`range.${days}`),
        onClick: (event) => {
          store.range = days;
          for (const button of rangeBar.children) button.setAttribute("aria-pressed", String(button === event.currentTarget));
          loadCharts(detail, charts);
        },
      }),
    ),
  );
  blocks.push(h("section", { class: "section" }, h("div", { class: "section-head" }, rangeBar), charts));
  blocks.push(h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", { text: t("details.title") })), detailsCard(detail)));

  target.replaceChildren(...blocks);
  loadCharts(detail, charts);
}

function tile(label, value, ...extra) {
  return h("div", { class: "card tile" }, h("div", { class: "tile-label", text: label }), h("div", { class: "tile-value", text: value }), extra);
}

function tiles(detail, r, info) {
  const kind = detail.disk.info.kind;
  const out = [];

  if (kind === "hdd") {
    out.push(tile(t("tile.life"), DASH, h("div", { class: "tile-sub", text: t("tile.life.hdd") })));
  } else if (r.life_used_pct === null && isNum(r.life_used_estimated_pct) && detail.disk.endurance) {
    const remaining = Math.max(0, 100 - r.life_used_estimated_pct);
    const endurance = detail.disk.endurance;
    const finding = detail.disk.findings.find((f) => f.code === "life_low_estimated");
    let end = null;
    if (info.life_end_ts) end = `${t("tile.life.end", { date: new Intl.DateTimeFormat(store.lang, { month: "short", year: "numeric" }).format(info.life_end_ts * 1000) })} (${t(`basis.${info.life_basis}`)})`;
    else if (info.life_end_beyond_cap) end = t("tile.life.beyond");
    out.push(
      h(
        "div",
        { class: "card tile is-estimated", title: t("tile.life.estimatedNote") },
        h("div", { class: "tile-label", text: t("tile.life.estimated") }),
        h("div", { class: "tile-value" }, h("small", { class: "est", text: "≈ " }), fmtPct(Math.round(remaining))),
        h("div", { class: "meter is-estimated" }, h("div", { class: "meter-track" }, h("div", { class: `meter-fill${finding ? " is-warning" : ""}`, style: `width:${Math.max(2, remaining)}%` }))),
        h("div", { class: "tile-sub", text: t("tile.life.estimatedFrom", { written: fmtBytes(r.written_bytes), tbw: fmtBytes(endurance.tbw_bytes), family: endurance.family }) }),
        end && h("div", { class: "tile-sub", text: end }),
        h("div", { class: "tile-sub muted", text: t("tile.life.estimatedNote") }),
        endurance.ambiguous && h("div", { class: "tile-sub muted", text: t("tile.life.ambiguous") }),
      ),
    );
  } else if (r.life_used_pct === null) {
    out.push(tile(t("tile.life"), DASH, h("div", { class: "tile-sub", text: t("tile.life.none") })));
  } else {
    const remaining = Math.max(0, 100 - r.life_used_pct);
    let sub = t("tile.life.unmeasured");
    if (info.life_end_ts) sub = `${t("tile.life.end", { date: new Intl.DateTimeFormat(store.lang, { month: "short", year: "numeric" }).format(info.life_end_ts * 1000) })} (${t(`basis.${info.life_basis}`)})`;
    else if (info.life_end_beyond_cap) sub = t("tile.life.beyond");
    const finding = detail.disk.findings.find((f) => f.code === "life_low");
    const severity = finding ? (finding.severity === 3 ? " is-critical" : " is-warning") : "";
    out.push(
      tile(
        t("tile.life"),
        fmtPct(Math.round(remaining)),
        h("div", { class: "meter" }, h("div", { class: "meter-track" }, h("div", { class: `meter-fill${severity}`, style: `width:${Math.max(2, remaining)}%` }))),
        h("div", { class: "tile-sub", text: sub }),
      ),
    );
  }

  const rate = info.written_per_day_bytes;
  out.push(
    tile(
      t("tile.written"),
      fmtBytes(r.written_bytes),
      rate !== null && h("div", { class: "tile-sub", text: `${t("tile.written.rate", { rate: fmtBytes(rate) })} (${t(`basis.${info.written_basis}`)})` }),
    ),
  );

  out.push(
    tile(
      t("tile.temperature"),
      fmtTemp(r.temperature_c),
      h("div", { class: "tile-sub", text: t("tile.temperature.limits", { warn: fmtTemp(detail.temperature_warn_c), crit: fmtTemp(detail.temperature_crit_c) }) }),
    ),
  );

  out.push(
    tile(
      t("tile.powerOn"),
      fmtHours(r.power_on_hours),
      h("div", { class: "tile-sub", text: t("tile.powerOn.detail", { hours: fmtInt(r.power_on_hours), cycles: fmtInt(r.power_cycles) }) }),
    ),
  );

  const counters = [...ERROR_COUNTERS, "unsafe_shutdowns", "available_spare_pct"].filter((name) => r[name] !== null && r[name] !== undefined);
  const bad = ERROR_COUNTERS.filter((name) => r[name]);
  out.push(
    tile(
      t("tile.errors"),
      bad.length ? fmtInt(bad.reduce((sum, name) => sum + r[name], 0)) : t("tile.errors.none"),
      h(
        "ul",
        { class: "counter-list" },
        counters.map((name) =>
          h(
            "li",
            { class: ERROR_COUNTERS.includes(name) && r[name] ? "is-bad" : "" },
            t(`counter.${name}`),
            h("b", { text: name === "available_spare_pct" ? fmtPct(r[name]) : fmtInt(r[name]) }),
          ),
        ),
      ),
    ),
  );
  return out;
}

async function loadCharts(detail, container) {
  container.classList.add("is-loading");
  let history;
  try {
    history = await api(`api/disks/${encodeURIComponent(detail.key)}/history?days=${store.range}`);
  } catch (error) {
    container.replaceChildren(errorBanner(error));
    container.classList.remove("is-loading");
    return;
  }
  const interval = (store.state?.app.poll_interval_minutes || 60) * 60e3;
  const points = history.points.map((p) => ({ ...p, t: p.ts * 1000 }));
  const series = (field) => {
    const out = [];
    let previous = null;
    for (const p of points) {
      if (p[field] === null || p[field] === undefined) continue;
      const limit = p.daily || previous?.daily ? 3 * DAY : Math.max(3 * interval, 3 * HOUR);
      out.push({ t: p.t, v: p[field], gap: previous !== null && p.t - previous.t > limit });
      previous = p;
    }
    return out;
  };

  const cards = [];
  const temps = series("temperature_c");
  cards.push(
    chartCard(t("chart.temperature"), "°C", temps, {
      kind: "line",
      format: fmtTemp,
      refs: [
        { v: detail.temperature_warn_c, status: "warning", label: `${t("chart.ref.warning")} ${fmtTemp(detail.temperature_warn_c)}` },
        { v: detail.temperature_crit_c, status: "critical", label: `${t("chart.ref.critical")} ${fmtTemp(detail.temperature_crit_c)}` },
      ],
    }),
  );

  const written = bucketWritten(history.written_per_day);
  cards.push(chartCard(t("chart.written"), t(`chart.written.${written.unit}`), written.points, { kind: "columns", bucket: written.bucket, format: fmtBytes, yMin: 0, tooltipTime: written.label }));

  if (detail.disk.info.kind !== "hdd") {
    const life = series("life_used_pct");
    if (life.length) cards.push(chartCard(t("chart.life"), "%", life, { kind: "step", format: fmtPct, yMin: 0 }));
    else if (detail.disk.reading.life_used_estimated_pct !== null) {
      const estimated = series("life_used_estimated_pct");
      if (estimated.length) cards.push(chartCard(t("chart.life.estimated"), "%", estimated, { kind: "line", format: fmtPct, yMin: 0 }));
    }
  }

  const active = ERROR_COUNTERS.filter((name) => points.some((p) => p[name]));
  for (const name of active) {
    cards.push(chartCard(t(`counter.${name}`), t("chart.errors"), series(name), { kind: "step", format: fmtInt, yMin: 0, integer: true }));
  }
  if (!active.length && ERROR_COUNTERS.some((name) => points.some((p) => p[name] !== null && p[name] !== undefined))) {
    cards.push(h("div", { class: "card chart-card" }, h("div", { class: "chart-title" }, h("h3", { text: t("chart.errors") })), h("div", { class: "chart-empty", text: t("chart.errors.allZero") })));
  }

  container.replaceChildren(...cards);
  container.classList.remove("is-loading");
  for (const card of cards) card.mount?.();
}

function bucketWritten(perDay) {
  const days = perDay.map((d) => ({ t: d.ts * 1000, v: d.bytes }));
  if (!days.length) return { points: [], unit: "day", bucket: DAY };
  const span = days[days.length - 1].t - days[0].t;
  if (span <= 120 * DAY) {
    return { points: days, unit: "day", bucket: DAY, label: (ms) => new Intl.DateTimeFormat(store.lang, { dateStyle: "medium" }).format(ms) };
  }
  const weekly = span <= 730 * DAY;
  const groups = new Map();
  for (const day of days) {
    const date = new Date(day.t);
    if (weekly) date.setUTCDate(date.getUTCDate() - ((date.getUTCDay() + 6) % 7));
    else date.setUTCDate(1);
    const key = date.getTime();
    groups.set(key, (groups.get(key) || 0) + day.v);
  }
  const points = [...groups.entries()].sort((a, b) => a[0] - b[0]).map(([tms, v]) => ({ t: tms, v }));
  const dateFormat = new Intl.DateTimeFormat(store.lang, weekly ? { dateStyle: "medium" } : { month: "long", year: "numeric" });
  return { points, unit: weekly ? "week" : "month", bucket: weekly ? 7 * DAY : 30 * DAY, label: (ms) => dateFormat.format(ms) };
}

function chartCard(title, subtitle, points, options) {
  const body = h("div", { class: "chart-body" });
  const tableHost = h("div", { class: "chart-table", hidden: true });
  const toggle = h("button", {
    type: "button",
    class: "link-btn",
    text: t("chart.table.show"),
    onClick: () => {
      tableHost.hidden = !tableHost.hidden;
      toggle.textContent = t(tableHost.hidden ? "chart.table.show" : "chart.table.hide");
      if (!tableHost.hidden && !tableHost.firstChild) {
        const timeText = options.tooltipTime || fmtDateTime;
        tableHost.append(
          h(
            "table",
            {},
            h("thead", {}, h("tr", {}, h("th", { text: t("chart.col.time") }), h("th", { class: "num", text: t("chart.col.value") }))),
            h("tbody", {}, [...points].reverse().slice(0, 1000).map((p) => h("tr", {}, h("td", { text: timeText(p.t) }), h("td", { class: "num", text: options.format(p.v) })))),
          ),
        );
      }
    },
  });
  const card = h(
    "div",
    { class: "card chart-card" },
    h("div", { class: "chart-title" }, h("h3", { text: title }), h("span", { class: "sub", text: subtitle })),
    body,
    points.length > 0 && h("div", { class: "chart-foot" }, toggle),
    tableHost,
  );
  card.mount = () => {
    if (!points.length) {
      body.append(h("div", { class: "chart-empty", text: t("chart.empty") }));
      return;
    }
    timeChart(body, {
      ...options,
      points,
      label: `${title} (${subtitle})`,
      formatTime: fmtTick,
      formatTooltipTime: options.tooltipTime || fmtDateTime,
    });
  };
  return card;
}

function detailsCard(detail) {
  const { disk, summary } = detail;
  const info = disk.info;
  const rows = [
    [t("details.model"), info.model],
    [t("details.serial"), info.serial, true],
    [t("details.firmware"), info.firmware],
    [t("details.capacity"), info.size_bytes ? `${fmtCapacity(info.size_bytes)} (${fmtInt(info.size_bytes)} B)` : DASH],
    [t("details.format"), [info.format_type, info.logical_block_size && `${info.logical_block_size} B`].filter(Boolean).join(" · ") || DASH],
    [t("details.host"), disk.host],
    info.nvme_adapter && [t("details.adapter"), info.nvme_adapter, true],
    info.vsan_tier && [t("details.vsan"), t(`tier.${info.vsan_tier}`)],
    info.vsan_disk_group && [t("details.vsanGroup"), info.vsan_disk_group, true],
    disk.endurance && [t("details.endurance"), t("details.endurance.value", { tbw: fmtBytes(disk.endurance.tbw_bytes), family: disk.endurance.family })],
    [t("details.deviceId"), info.device_id, true],
    [t("details.sources"), disk.sources.map((s) => t(`source.${s}`)).join(", ") || DASH],
    [t("details.lastSeen"), summary.last_seen ? fmtDateTime(summary.last_seen * 1000) : DASH],
  ].filter(Boolean);

  const card = h(
    "div",
    { class: "card" },
    h(
      "dl",
      { class: "kv" },
      rows.map(([label, value, mono]) => [h("dt", { text: label }), h("dd", { class: mono ? "mono" : "", text: value || DASH })]),
    ),
  );

  const device = disk.raw.esxcli_device;
  if (device) {
    const labels = device._labels || {};
    const entries = Object.entries(device).filter(([name]) => name !== "_labels");
    card.append(rawTable(t("raw.device"), [t("raw.col.name"), t("raw.col.value")], entries.map(([name, value]) => ({ cells: [labels[name] || name, String(value)] })), [1]));
  }
  const smartctl = disk.raw.smartctl?.attributes;
  if (smartctl?.length) {
    card.append(
      rawTable(
        t("raw.smartctl"),
        [t("raw.col.id"), t("raw.col.name"), t("raw.col.value"), t("raw.col.worst"), t("raw.col.threshold"), t("raw.col.raw"), t("raw.col.failed")],
        smartctl.map((a) => ({ cells: [a.id, a.name, a.value, a.worst, a.thresh, a.raw, a.when_failed || ""], failed: a.when_failed === "now" })),
        [0, 2, 3, 4],
      ),
    );
  }
  const esxcli = disk.raw.esxcli_smart;
  if (esxcli?.length) {
    card.append(
      rawTable(
        t("raw.esxcli"),
        [t("raw.col.name"), t("raw.col.value"), t("raw.col.threshold"), t("raw.col.worst"), t("raw.col.raw")],
        esxcli.map((row) => ({ cells: [row.parameter, row.value, row.threshold, row.worst, row.raw ?? ""], failed: (disk.reading.failing_attributes || []).includes(row.parameter) })),
        [1, 2, 3, 4],
      ),
    );
  }
  const nvme = disk.raw.esxcli_nvme;
  if (nvme) {
    const labels = nvme._labels || {};
    const entries = Object.entries(nvme).filter(([name]) => name !== "_labels");
    card.append(rawTable(t("raw.nvme"), [t("raw.col.name"), t("raw.col.value")], entries.map(([name, value]) => ({ cells: [labels[name] || name, String(value)] })), [1]));
  }
  return card;
}

function rawTable(title, headers, rows, numeric = []) {
  return h(
    "details",
    { class: "raw" },
    h("summary", { text: title }),
    h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        {},
        h("thead", {}, h("tr", {}, headers.map((label, i) => h("th", { class: numeric.includes(i) ? "num" : "", text: label })))),
        h(
          "tbody",
          {},
          rows.map((row) => h("tr", { class: row.failed ? "is-failed" : "" }, row.cells.map((cell, i) => h("td", { class: numeric.includes(i) ? "num" : "", text: cell ?? "" })))),
        ),
      ),
    ),
  );
}

// ------------------------------------------------------------------ events

async function renderEvents() {
  const target = view();
  store.detailKey = null;
  let data;
  try {
    data = await api("api/events");
  } catch (error) {
    target.replaceChildren(errorBanner(error));
    return;
  }
  if (currentRoute().name !== "events") return;
  const names = new Map((store.state?.disks || []).map((d) => [d.key, diskTitle(d)]));
  const labels = ["ok", "unknown", "warning", "critical"];
  const content = data.events.length
    ? h(
        "div",
        { class: "card table-wrap" },
        h(
          "table",
          {},
          h("thead", {}, h("tr", {}, ["time", "change", "disk", "details"].map((c) => h("th", { text: t(`events.col.${c}`) })))),
          h(
            "tbody",
            {},
            data.events.map((event) =>
              h(
                "tr",
                {},
                h("td", { class: "num", style: "text-align:left", text: fmtDateTime(event.ts * 1000) }),
                h(
                  "td",
                  { style: "white-space:nowrap" },
                  event.old_status === null ? h("span", { class: "badge", text: t("events.new") }) : statusEl(labels[event.old_status]),
                  h("span", { class: "muted", text: "  →  " }),
                  statusEl(labels[event.new_status]),
                ),
                h(
                  "td",
                  {},
                  event.disk_key ? h("a", { href: diskHref(event.disk_key), text: names.get(event.disk_key) || event.disk_key }) : DASH,
                  h("div", { class: "muted", text: event.host }),
                ),
                h(
                  "td",
                  { class: "secondary" },
                  event.findings?.length ? event.findings.map(findingText).join(" · ") : t(event.new_status === 0 ? "disk.noFindings" : `status.${labels[event.new_status]}`),
                ),
              ),
            ),
          ),
        ),
      )
    : h("div", { class: "card empty", text: t("events.empty") });
  target.replaceChildren(h("div", { class: "section-head" }, h("h1", { text: t("events.title") })), content);
}

// ------------------------------------------------------------------ setup

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Clipboard API is often blocked inside iframes (Home Assistant ingress): select instead.
    const range = document.createRange();
    range.selectNodeContents(button.previousElementSibling);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    document.execCommand?.("copy");
  }
  toast(t("setup.copied"));
}

async function renderSetup() {
  const target = view();
  store.detailKey = null;
  let data;
  try {
    data = await api("api/setup");
  } catch (error) {
    target.replaceChildren(errorBanner(error));
    return;
  }
  if (currentRoute().name !== "setup") return;

  const ssh = h(
    "section",
    { class: "section" },
    h("div", { class: "section-head" }, h("h2", { text: t("setup.ssh.title") })),
    h(
      "div",
      { class: "card" },
      h("p", { class: "card-pad secondary", style: "margin:0;padding-bottom:0", text: t("setup.ssh.intro") }),
      h(
        "ol",
        { class: "steps" },
        h("li", { text: t("setup.ssh.step1") }),
        h(
          "li",
          {},
          t("setup.ssh.step2"),
          h(
            "div",
            { class: "codebox" },
            h("code", { text: data.authorize_command }),
            h("button", { class: "btn", type: "button", text: t("setup.copy"), onClick: (e) => copyText(data.authorize_command, e.currentTarget) }),
          ),
        ),
        h("li", { text: t("setup.ssh.step3") }),
      ),
    ),
  );

  const hosts = h(
    "section",
    { class: "section" },
    h("div", { class: "section-head" }, h("h2", { text: t("setup.hosts.title") })),
    h(
      "div",
      { class: "card table-wrap" },
      h(
        "table",
        {},
        h("thead", {}, h("tr", {}, h("th", { text: t("setup.col.host") }), h("th", { text: t("setup.col.auth") }), h("th", { text: t("setup.col.hostKey") }), h("th", {}))),
        h("tbody", {}, data.hosts.map((host) => setupHostRow(host, data.ha_mode))),
      ),
    ),
    !data.ha_mode && data.hosts.some((host) => host.host_key) && h("p", { class: "muted mono", text: t("setup.forget.cli", { host: "<name>" }) }),
  );

  const integ = data.integrations || {};
  const mqttText = !integ.mqtt_enabled
    ? t("setup.mqtt.off")
    : integ.mqtt_error
      ? t("setup.mqtt.error", { error: integ.mqtt_error })
      : t(integ.mqtt_connected ? "setup.mqtt.connected" : "setup.mqtt.connecting", { broker: integ.mqtt_broker || DASH });
  const integrations = h(
    "section",
    { class: "section" },
    h("div", { class: "section-head" }, h("h2", { text: t("setup.integrations.title") })),
    h(
      "div",
      { class: "card" },
      h(
        "dl",
        { class: "kv" },
        h("dt", { text: t("setup.mqtt") }),
        h("dd", {}, h("span", { class: `status status-${integ.mqtt_connected ? "ok" : integ.mqtt_enabled ? "warning" : "unknown"}` }, icon(integ.mqtt_connected ? "ok" : integ.mqtt_enabled ? "warning" : "unknown")), " ", mqttText),
        h("dt", { text: t("setup.alerts") }),
        h("dd", { text: t(integ.alerts_enabled ? "setup.alerts.on" : "setup.alerts.off") }),
      ),
    ),
  );

  const th = data.thresholds;
  const settingsRows = [
    [t("setup.settings.interval"), t("setup.settings.minutes", { n: data.poll_interval_minutes })],
    [t("setup.settings.retention"), t("setup.settings.days", { n: data.retention_raw_days })],
    [t("setup.settings.lifeWarn"), fmtPct(th.life_remaining_warn_pct)],
    [t("setup.settings.lifeCrit"), fmtPct(th.life_remaining_crit_pct)],
    ...["hdd", "ssd", "nvme"].map((kind) => [t("setup.settings.temp", { kind: t(`kind.${kind}`) }), `${fmtTemp(th[`temp_${kind}_warn_c`])} / ${fmtTemp(th[`temp_${kind}_crit_c`])}`]),
    [t("setup.settings.driveLimit"), t(th.use_drive_temp_limit ? "setup.settings.yes" : "setup.settings.no")],
    [t("setup.settings.ssdRealloc"), fmtInt(th.ssd_reallocated_warn_count)],
  ];
  const metricsLink = h("a", { href: "metrics", text: "/metrics" });
  const settings = h(
    "section",
    { class: "section" },
    h("div", { class: "section-head" }, h("h2", { text: t("setup.settings.title") }), h("span", { class: "meta", text: t(data.ha_mode ? "setup.settings.intro.ha" : "setup.settings.intro.standalone") })),
    h("div", { class: "card" }, h("dl", { class: "kv" }, settingsRows.map(([label, value]) => [h("dt", { text: label }), h("dd", { text: value })]))),
    h("p", { class: "muted" }, ...interpolate(t("setup.metrics", { link: LINK_MARK }), metricsLink)),
  );

  target.replaceChildren(h("h1", { text: t("nav.setup") }), ssh, hosts, integrations, settings);
}

// Split a translated sentence at its placeholder so a node (a link) can sit inside it.
const LINK_MARK = "@@link@@";
function interpolate(text, node) {
  const [before, after = ""] = text.split(LINK_MARK);
  return [before, node, after];
}

function setupHostRow(host, haMode) {
  const result = h("div", { class: "test-result", "aria-live": "polite" });
  const keyCell = h("td", { class: host.host_key ? "mono" : "muted", text: host.host_key || t("setup.hostKey.none") });
  const testButton = h("button", {
    class: "btn",
    type: "button",
    text: t("setup.test"),
    onClick: async () => {
      testButton.disabled = true;
      testButton.replaceChildren(h("span", { class: "spinner" }), t("setup.testing"));
      result.replaceChildren();
      try {
        const outcome = await api(`api/hosts/${encodeURIComponent(host.name)}/test`, { method: "POST" });
        if (outcome.host_key) {
          keyCell.textContent = outcome.host_key;
          keyCell.className = "mono";
        }
        if (outcome.ok) {
          result.append(
            h("div", { class: "ok-text", text: t("setup.test.ok", { version: outcome.esxi_version || DASH }) }),
            h(
              "ul",
              {},
              h("li", { text: outcome.smartctl_path ? t("setup.test.smartctl", { path: outcome.smartctl_path }) : t("setup.test.noSmartctl") }),
              outcome.esxcli_json && h("li", { text: t("setup.test.json") }),
            ),
          );
        } else {
          result.append(h("div", { class: "bad-text", text: t("setup.test.failed", { error: outcome.error }) }));
        }
      } catch (error) {
        result.append(h("div", { class: "bad-text", text: t("setup.test.failed", { error: error.message }) }));
      }
      testButton.disabled = false;
      testButton.textContent = t("setup.test");
    },
  });
  const forget =
    haMode &&
    host.host_key &&
    h("button", {
      class: "btn",
      type: "button",
      text: t("setup.forget"),
      onClick: async () => {
        if (!confirm(t("setup.forget.confirm", { host: host.name }))) return;
        await api(`api/hosts/${encodeURIComponent(host.name)}/forget-host-key`, { method: "POST" });
        renderSetup();
      },
    });
  return h(
    "tr",
    {},
    h("td", {}, h("div", { class: "disk-name", text: host.name }), h("div", { class: "muted mono", text: `${host.address}:${host.port}` })),
    h("td", { text: t(`setup.auth.${host.auth}`, { user: host.username }) }),
    keyCell,
    h("td", {}, h("div", { style: "display:flex;gap:8px;flex-wrap:wrap" }, testButton, forget), result),
  );
}

// ------------------------------------------------------------------ start

async function start() {
  store.lang = pickLanguage("auto");
  renderShell();
  window.addEventListener("hashchange", route);
  await refreshState();
  const preferred = store.state ? pickLanguage(store.state.app.language) : store.lang;
  if (preferred !== store.lang) {
    // The configured default; not stored, so the browser's own choice still wins later.
    store.lang = preferred;
    renderShell();
    route();
  } else if (currentRoute().name !== "overview") {
    route();
  }
}

start();
