// Small dependency-free SVG time charts: one series, one y-axis, crosshair tooltip.
// Colors come from CSS classes (see app.css), so theme switches need no redraw.

const NS = "http://www.w3.org/2000/svg";
const HEIGHT = 200;
const HOUR = 3600e3;
const DAY = 24 * HOUR;

function svgEl(tag, attrs = {}, parent = null) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (parent) parent.appendChild(node);
  return node;
}

// minStep 1 keeps integer counters from getting fractional ticks that all
// round to the same label ("1, 1, 1").
function niceScale(min, max, count = 4, minStep = 0) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) [min, max] = [0, 1];
  if (min === max) [min, max] = [min - 1, max + 1];
  const rough = (max - min) / count;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const norm = rough / magnitude;
  const step = Math.max(minStep, (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * magnitude);
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Number(v.toPrecision(12)));
  return { lo, hi, ticks };
}

// Calendar-aligned time ticks, roughly one per 75px.
function timeTicks(t0, t1, width) {
  const span = t1 - t0;
  const target = Math.max(2, Math.floor(width / 75));
  const steps = [HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY];
  const step = steps.find((s) => span / s <= target);
  const ticks = [];
  if (step) {
    const start = new Date(t0);
    if (step >= DAY) start.setHours(0, 0, 0, 0);
    else start.setMinutes(0, 0, 0);
    for (let t = start.getTime(); t <= t1; t += step) if (t >= t0) ticks.push(t);
    return { ticks, unit: step >= DAY ? "day" : "hour" };
  }
  const months = [1, 2, 3, 6, 12, 24].find((m) => span / (m * 30 * DAY) <= target) || 48;
  const date = new Date(t0);
  date.setDate(1);
  date.setHours(0, 0, 0, 0);
  while (date.getTime() <= t1) {
    if (date.getTime() >= t0) ticks.push(date.getTime());
    date.setMonth(date.getMonth() + months);
  }
  return { ticks, unit: months >= 12 ? "year" : "month" };
}

function nearestIndex(points, t) {
  let lo = 0;
  let hi = points.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (points[mid].t < t) lo = mid;
    else hi = mid;
  }
  return Math.abs(points[lo].t - t) <= Math.abs(points[hi].t - t) ? lo : hi;
}

function roundedTopBar(x, y, width, baseline, radius = 4) {
  const height = baseline - y;
  const r = Math.min(radius, width / 2, height);
  if (height <= 0) return "";
  return `M${x},${baseline}V${y + r}Q${x},${y} ${x + r},${y}H${x + width - r}Q${x + width},${y} ${x + width},${y + r}V${baseline}Z`;
}

/**
 * Render a time chart into `container` and keep it sized to the container.
 *
 * options:
 *   kind: "line" | "step" | "columns"
 *   points: [{ t: ms, v: number, gap?: true }]   gap: break the line before this point
 *   bucket: ms, width of a column (columns only)
 *   format(v), formatTime(t, unit), label (aria)
 *   yMin, yMax: force the axis to include these values
 *   integer: values are whole counts; no fractional axis ticks
 *   refs: [{ v, label, status: "warning" | "critical" }]
 */
export function timeChart(container, options) {
  container.classList.add("chart");
  let active = null;
  const tooltip = document.createElement("div");
  tooltip.className = "chart-tooltip";
  tooltip.hidden = true;
  const tipValue = document.createElement("strong");
  const tipTime = document.createElement("span");
  tooltip.append(tipValue, tipTime);

  function draw() {
    const width = Math.max(260, container.clientWidth);
    container.replaceChildren();
    const { points, kind, refs = [], format, formatTime } = options;
    const values = points.map((p) => p.v).concat(refs.map((r) => r.v));
    if (options.yMin !== undefined) values.push(options.yMin);
    if (options.yMax !== undefined) values.push(options.yMax);
    const y = niceScale(Math.min(...values), Math.max(...values), 4, options.integer ? 1 : 0);

    const tickLabels = y.ticks.map((v) => format(v, true));
    const left = Math.max(...tickLabels.map((s) => s.length)) * 7 + 14;
    const right = kind === "columns" ? 8 : 12;
    const top = 12;
    const bottom = 26;
    const plotW = width - left - right;
    const plotH = HEIGHT - top - bottom;

    const bucket = kind === "columns" ? options.bucket : 0;
    let t0 = points[0].t;
    let t1 = points[points.length - 1].t + bucket;
    if (t0 === t1) [t0, t1] = [t0 - HOUR, t1 + HOUR];
    const sx = (t) => left + ((t - t0) / (t1 - t0)) * plotW;
    const sy = (v) => top + plotH - ((v - y.lo) / (y.hi - y.lo)) * plotH;

    const svg = svgEl("svg", {
      width,
      height: HEIGHT,
      viewBox: `0 0 ${width} ${HEIGHT}`,
      tabindex: "0",
      role: "img",
      "aria-label": options.label,
    });

    for (const [i, v] of y.ticks.entries()) {
      svgEl("line", { x1: left, x2: width - right, y1: sy(v), y2: sy(v), class: v === 0 ? "chart-baseline" : "chart-grid" }, svg);
      const text = svgEl("text", { x: left - 8, y: sy(v), class: "chart-tick", "text-anchor": "end", "dominant-baseline": "middle" }, svg);
      text.textContent = tickLabels[i];
    }
    const xt = timeTicks(t0, t1, plotW);
    for (const t of xt.ticks) {
      const text = svgEl("text", { x: sx(t), y: HEIGHT - 8, class: "chart-tick", "text-anchor": "middle" }, svg);
      text.textContent = formatTime(t, xt.unit);
    }

    for (const ref of refs) {
      if (ref.v < y.lo || ref.v > y.hi) continue;
      svgEl("line", { x1: left, x2: width - right, y1: sy(ref.v), y2: sy(ref.v), class: `chart-ref chart-ref-${ref.status}` }, svg);
      const text = svgEl("text", { x: left + 6, y: sy(ref.v) - 5, class: "chart-ref-label" }, svg);
      text.textContent = ref.label;
    }

    const marks = [];
    if (kind === "columns") {
      const baseline = sy(Math.max(0, y.lo));
      for (const p of points) {
        const band = sx(p.t + bucket) - sx(p.t);
        const barW = Math.max(1, Math.min(24, band - 2));
        const x = sx(p.t) + (band - barW) / 2;
        const path = svgEl("path", { d: roundedTopBar(x, sy(p.v), barW, baseline), class: "chart-bar" }, svg);
        marks.push({ node: path, cx: x + barW / 2, band: [sx(p.t), sx(p.t + bucket)] });
      }
    } else {
      let d = "";
      points.forEach((p, i) => {
        const [x, py] = [sx(p.t), sy(p.v)];
        if (i === 0 || p.gap) d += `M${x},${py}`;
        else if (kind === "step") d += `H${x}V${py}`;
        else d += `L${x},${py}`;
      });
      svgEl("path", { d, class: "chart-line" }, svg);
      const last = points[points.length - 1];
      svgEl("circle", { cx: sx(last.t), cy: sy(last.v), r: 4, class: "chart-dot" }, svg);
    }

    const crosshair = svgEl("line", { y1: top, y2: top + plotH, class: "chart-crosshair", visibility: "hidden" }, svg);
    const focusDot = svgEl("circle", { r: 4, class: "chart-dot", visibility: "hidden" }, svg);
    const overlay = svgEl("rect", { x: left, y: top, width: plotW, height: plotH, class: "chart-overlay" }, svg);

    function show(index) {
      active = index;
      const p = points[index];
      tipValue.textContent = format(p.v);
      tipTime.textContent = options.formatTooltipTime ? options.formatTooltipTime(p.t) : formatTime(p.t, "full");
      tooltip.hidden = false;
      let anchorX;
      if (kind === "columns") {
        marks.forEach((m, i) => m.node.classList.toggle("is-active", i === index));
        anchorX = marks[index].cx;
      } else {
        anchorX = sx(p.t);
        crosshair.setAttribute("x1", anchorX);
        crosshair.setAttribute("x2", anchorX);
        crosshair.setAttribute("visibility", "visible");
        focusDot.setAttribute("cx", anchorX);
        focusDot.setAttribute("cy", sy(p.v));
        focusDot.setAttribute("visibility", "visible");
      }
      const tipWidth = tooltip.offsetWidth || 140;
      const flip = anchorX + 12 + tipWidth > width;
      tooltip.style.left = `${flip ? anchorX - 12 - tipWidth : anchorX + 12}px`;
      tooltip.style.top = `${Math.max(0, sy(p.v) - 48)}px`;
    }

    function hide() {
      active = null;
      tooltip.hidden = true;
      crosshair.setAttribute("visibility", "hidden");
      focusDot.setAttribute("visibility", "hidden");
      marks.forEach((m) => m.node.classList.remove("is-active"));
    }

    overlay.addEventListener("pointermove", (event) => {
      const box = svg.getBoundingClientRect();
      const x = event.clientX - box.left;
      if (kind === "columns") {
        const index = marks.findIndex((m) => x >= m.band[0] && x < m.band[1]);
        if (index >= 0) show(index);
        else hide();
      } else {
        const t = t0 + ((x - left) / plotW) * (t1 - t0);
        show(nearestIndex(points, t));
      }
    });
    overlay.addEventListener("pointerleave", hide);
    svg.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        const step = event.key === "ArrowLeft" ? -1 : 1;
        show(Math.min(points.length - 1, Math.max(0, (active ?? points.length) + step)));
        event.preventDefault();
      } else if (event.key === "Escape") hide();
    });
    svg.addEventListener("focus", () => show(active ?? points.length - 1));
    svg.addEventListener("blur", hide);

    container.append(svg, tooltip);
  }

  draw();
  let lastWidth = container.clientWidth;
  const observer = new ResizeObserver(() => {
    if (!container.isConnected) return observer.disconnect();
    if (container.clientWidth !== lastWidth) {
      lastWidth = container.clientWidth;
      requestAnimationFrame(draw);
    }
  });
  observer.observe(container);
}
