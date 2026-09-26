/* clodfarm UI. A living pixel farm: every Claude Code worker is a Claude critter that wanders, tends a crop
 * (its task) at a terminal, or naps when the budget governor says so. Plain JS, no build step, no dependencies. */
"use strict";

// ================================================================== utilities
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const lerp = (a, b, t) => a + (b - a) * t;
function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "text") e.textContent = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
}
function mulberry32(a) {
  return () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
function hashStr(s) { let x = 2166136261; for (const c of String(s)) { x ^= c.charCodeAt(0); x = Math.imul(x, 16777619); } return x >>> 0; }
/** replaceChildren that flattens arrays and drops null/false (plain replaceChildren would print "null"). */
function fill(el, ...kids) { el.replaceChildren(...kids.flat(3).filter(k => k != null && k !== false)); return el; }
const nowS = () => Date.now() / 1000;
function ago(ts) {
  if (!ts) return "-";
  const s = Math.max(0, nowS() - ts);
  return s < 90 ? `${Math.round(s)}s ago` : s < 5400 ? `${Math.round(s / 60)}m ago` : s < 172800 ? `${(s / 3600).toFixed(1)}h ago` : `${(s / 86400).toFixed(1)}d ago`;
}
function until(ts) {
  if (!ts) return "";
  const s = Math.max(0, ts - nowS());
  return s < 5400 ? `${Math.round(s / 60)}m` : s < 172800 ? `${(s / 3600).toFixed(1)}h` : `${(s / 86400).toFixed(1)}d`;
}
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

// ===================================================================== sprites
function canvas(w, hgt) { const c = document.createElement("canvas"); c.width = w; c.height = hgt; return c; }
/** Paint string rows ("." = transparent) with a palette into a new canvas. */
function paint(rows, pal, w = 16) {
  const c = canvas(w, rows.length), g = c.getContext("2d");
  rows.forEach((row, y) => [...row.padEnd(w, ".").slice(0, w)].forEach((ch, x) => {
    if (ch !== "." && pal[ch]) { g.fillStyle = pal[ch]; g.fillRect(x, y, 1, 1); }
  }));
  return c;
}

const CLAY = { o: "#5a2616", h: "#f2a07a", b: "#d97757", s: "#b45a3c", d: "#86381f", e: "#231815", w: "#ffffff", k: "#f6c9b3" };
const HAT_COLORS = ["#3b7dd8", "#7b4bb7", "#2f9e6b", "#d2a21d", "#c0392b", "#2c3e50", "#e06f9f", "#16a0a0"];
const HAT_H = 6; // rows above the body for hats

/** Clawd, the Claude Code critter: a clay block with two eyes, stubby arms and four legs. 16 x 12. */
function clawdGrid({ look = 0, legs = 0, blink = false, sleep = false, arms = 0 }) {
  const W = 16, H = 12, g = Array.from({ length: H }, () => Array(W).fill("."));
  const set = (x, y, c) => { if (x >= 0 && x < W && y >= 0 && y < H) g[y][x] = c; };
  for (let y = 0; y <= 9; y++) for (let x = 2; x <= 13; x++) {
    const edge = x === 2 || x === 13 || y === 0 || y === 9;
    if ((x === 2 || x === 13) && (y === 0 || y === 9)) continue; // rounded corners
    set(x, y, edge ? "o" : (y === 1 || x === 3) ? "h" : (x === 12 || y === 8) ? "s" : "b");
  }
  // arms (arms=1 raises them: typing / cheering)
  const ay = 5 - arms;
  for (const [x0, dir] of [[0, 1], [15, -1]]) {
    for (let y = ay - 1; y <= ay + 2; y++) for (let k = 0; k < 3; k++) {
      const x = x0 + k * dir, rim = y === ay - 1 || y === ay + 2;
      if (k === 2) set(x, y, rim ? "o" : "b");
      else set(x, y, rim || k === 0 ? "o" : (dir === 1 ? "h" : "s"));
    }
  }
  // eyes: tall and dark with a shine; closed = a line
  for (const ex of [5, 9]) {
    const x = ex + look;
    if (blink || sleep) { set(x, 5, "e"); set(x + 1, 5, "e"); }
    else { for (let y = 3; y <= 5; y++) { set(x, y, "e"); set(x + 1, y, "e"); } set(x, 3, "w"); }
  }
  if (!sleep) { set(4 + look, 6, "k"); set(11 + look, 6, "k"); } // blush
  // legs: 4 pairs of 2px; the lifted pair is shorter
  const pairs = [[3, 4], [5, 6], [9, 10], [11, 12]];
  pairs.forEach(([a, b], i) => {
    const lifted = !sleep && legs !== 0 && ((i % 2 === 0) === (legs === 1));
    for (const x of [a, b]) {
      if (sleep) return;
      if (lifted) set(x, 10, "o");
      else { set(x, 10, "d"); set(x, 11, "o"); }
    }
  });
  return g.map(r => r.join(""));
}

const HATS = {
  straw: { pal: { o: "#6b4f1d", a: "#f0cf7a", b: "#d2a94c", r: "#c0392b" }, rows: [
    "................", "......oooo......", ".....oaaaao.....", "....oaaaaaao....", "...orrrrrrrro...", "ooaaaaaaaaaaaaoo", ".oobbbbbbbbbboo."] },
  beanie: { pal: { o: "#1c2233", w: "#f7f3ea", c: "@", d: "@d", l: "@l" }, rows: [
    ".......ww.......", "......owwo......", ".....occcco.....", "...occccccco....", "..occclcccccco..", "..oddddddddddo..", "..oddddddddddo.."] },
  cap: { pal: { o: "#1c2233", w: "#f7f3ea", c: "@", d: "@d" }, rows: [
    "................", "................", ".....occcco.....", "...occcwccccoo..", "..occcccccccccoo", "..ooooooooodddddo", "................"] },
  sprout: { pal: { o: "#1f4d1d", g: "#6fcf5b", l: "#a8ec8c", s: "#3f8f35" }, rows: [
    "................", "...oo.....oo....", "..ollo...oglo...", "..oglgo.ogllo...", "...oogosoggo....", "......os.o......", "......os........"] },
  bow: { pal: { o: "#5b1330", c: "#e0508a", l: "#f59cc0" }, rows: [
    "................", "................", "................", "....oo....oo....", "...olco..oclo...", "...occcoocccoo..", "....oo.oo..oo..."] },
  crown: { pal: { o: "#6b4a07", y: "#f5c542", l: "#fff0a8", r: "#d63a3a", b: "#3b7dd8" }, rows: [
    "................", "................", "...o...o...o....", "..oyo.oyo.oyo...", "..oyyoyyyoyyo...", "..oylyryybyylo..", "..oooooooooooo.."] },
  headphones: { pal: { o: "#15171f", c: "@", l: "@l", g: "#5b6272" }, rows: [
    "................", "................", "....oooooooo....", "...oggggggggo...", "..og........go..", "oo.o........o.oo", "oco..........oco"] },
  flower: { pal: { o: "#6b2f1f", p: "#ffd0dc", y: "#f5c542", g: "#3f8f35" }, rows: [
    "................", "..........opo...", ".........opypo..", "..........opo...", "...........g....", "................", "................"] },
};

function shade(hex, amt) {
  const n = parseInt(hex.slice(1), 16), f = (v) => clamp(Math.round(v + amt * 255), 0, 255);
  return "#" + [f(n >> 16), f((n >> 8) & 255), f(n & 255)].map(v => v.toString(16).padStart(2, "0")).join("");
}

const spriteCache = new Map();
/** A whole critter: hat + body, 16 x 18 (HAT_H rows of hat, then the 12-row body). Cached. */
function critterSprite(hat, color, opts) {
  const key = `${hat}|${color}|${JSON.stringify(opts)}`;
  let c = spriteCache.get(key);
  if (c) return c;
  c = canvas(16, HAT_H + 12);
  const g = c.getContext("2d");
  g.drawImage(paint(clawdGrid(opts), CLAY), 0, HAT_H);
  const def = HATS[hat];
  if (def) {
    const pal = {};
    for (const [k, v] of Object.entries(def.pal)) pal[k] = v === "@" ? color : v === "@d" ? shade(color, -0.18) : v === "@l" ? shade(color, 0.2) : v;
    // hats end on the body's first row; shift down 1 when the eyes are closed so it sits snug
    g.drawImage(paint(def.rows, pal), 0, opts.sleep ? 1 : 0);
  }
  spriteCache.set(key, c);
  return c;
}

const EGG = paint([
  "....oooo....", "...occcco...", "..occsccco..", "..occcccco..", ".occccccsco.", ".ocsccccccco", ".occcccsccco",
  ".occcccccdco", ".odcccccdcco", "..oddcccddo.", "...odddddo..", "....oooo....",
], { o: "#6b5a3a", c: "#f6eed8", s: "#e2875f", d: "#d8c9a3" }, 12);

const LAPTOP = (on) => paint([
  ".ooooooo.", on ? ".ogsggso." : ".osssssso", on ? ".osgssso." : ".osssssso", on ? ".oggsgso." : ".osssssso", ".ooooooo.", "ommmmmmmo", "ooooooooo",
].map(r => r.slice(0, 9)), { o: "#1b1f2a", s: "#22303c", g: "#7cfc9a", m: "#9aa3b2" }, 9);
const LAPTOP_ON = LAPTOP(true), LAPTOP_OFF = LAPTOP(false);

// 10 x 10 icons for bubbles and buttons
const ICONS = {
  terminal: [["oooooooooo", "osssssssso", "osgsssssso", "ossgssssso", "osgssssso", "osssggggso", "osssssssso", "oooooooooo", "...oooo...", "..oooooo.."],
    { o: "#1b1f2a", s: "#22303c", g: "#7cfc9a" }],
  zzz: [["......oooo", ".......oo.", "......oo..", "..oooooooo", "....oo....", "...oo.....", "..oooo....", "oooo......", "..oo......", ".oooo....."],
    { o: "#3c4a6b" }],
  pause: [["..........", ".oo....oo.", ".oo....oo.", ".oo....oo.", ".oo....oo.", ".oo....oo.", ".oo....oo.", ".oo....oo.", ".oo....oo.", ".........."],
    { o: "#3c4a6b" }],
  alert: [["....oo....", "...orro...", "...orro...", "...orro...", "...orro...", "....rr....", "..........", "....rr....", "...orro...", "....oo...."],
    { o: "#6b1a10", r: "#e0513c" }],
  ask: [["..oooooo..", ".oo....oo.", ".......oo.", "......oo..", ".....oo...", "....oo....", "....oo....", "..........", "....oo....", "....oo...."],
    { o: "#3c4a6b" }],
  dots: [["..........", "..........", "..........", "..........", ".oo.oo.oo.", ".oo.oo.oo.", "..........", "..........", "..........", ".........."],
    { o: "#3c4a6b" }],
  scroll: [[".oooooooo.", "oyyyyyyyyo", ".oppppppo.", ".opooopo..", ".oppppppo.", ".opoooopo.", ".oppppppo.", ".opooppo..", "oyyyyyyyyo", ".oooooooo."],
    { o: "#5a3d1e", p: "#f6ecd0", y: "#c9964a" }],
  plan: [[".oooooooo.", "oyyyyyyyyo", ".oppppppo.", ".oprpppro.", ".oppprppo.", ".opprpppo.", ".oppppppo.", ".opppppo..", "oyyyyyyyyo", ".oooooooo."],
    { o: "#5a3d1e", p: "#f6ecd0", y: "#c9964a", r: "#d97757" }],
  swords: [["o........o", ".o......o.", "..o....o..", "...o..o...", "....oo....", "....oo....", "...o..o...", ".bo....ob.", "bb......bb", "b........b"],
    { o: "#9aa3b2", b: "#6b4a2b" }],
  heart: [["..........", ".rr...rr..", "rllr.rrrr.", "rlrrrrrrr.", "rrrrrrrrr.", ".rrrrrrr..", "..rrrrr...", "...rrr....", "....r.....", ".........."],
    { r: "#e0513c", l: "#f7a296" }],
  egg: [["...oooo...", "..occcco..", ".occsccco.", ".occcccco.", "occccccsco", "ocscccccco", "occcccccco", "oddcccccdo", ".oddcccdo.", "..oooooo.."],
    { o: "#6b5a3a", c: "#f6eed8", s: "#e2875f", d: "#d8c9a3" }],
  quill: [["........oo", ".......owo", "......owwo", ".....owwo.", "....owwo..", "...owwo...", "..oowo....", "..ooo.....", ".ooo......", "oo........"],
    { o: "#3a2a1a", w: "#f6ecd0" }],
};
const iconURL = {};
function icon(name, scale = 1) {
  const k = name + scale;
  if (iconURL[k]) return iconURL[k];
  if (name === "party") {
    const s = critterSprite("straw", HAT_COLORS[0], { legs: 0 }), c = canvas(18, 18), g = c.getContext("2d");
    g.drawImage(s, 1, 0);
    return (iconURL[k] = c.toDataURL());
  }
  const [rows, pal] = ICONS[name];
  return (iconURL[k] = paint(rows, pal, 10).toDataURL());
}

// ======================================================================= world
const G = { // Thronglet-ish meadow palette
  g0: "#4a6524", g1: "#577530", g2: "#648436", g3: "#71923c", g4: "#80a246", worn: "#8ea456",
  t0: "#1c3519", t1: "#28501f", t2: "#346a27", t3: "#468a31", t4: "#63a93f", t5: "#8cc65a", trunk: "#4b3421",
  r0: "#34343c", r1: "#55555e", r2: "#6f6f78", r3: "#8d8d96", r4: "#aeaeb5",
  soil: "#6b4a2e", soil2: "#57391f", soil3: "#7d5a3a", soilo: "#3f2915",
  w0: "#2a5a8a", w1: "#3f7fb8", w2: "#5b9ed0", w3: "#9fd0ee",
};
const BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]];

function valueNoise(seed) {
  const rnd = mulberry32(seed), N = 64, grid = Array.from({ length: N * N }, rnd);
  const at = (x, y) => grid[((y % N + N) % N) * N + ((x % N + N) % N)];
  return (x, y) => {
    const xi = Math.floor(x), yi = Math.floor(y), xf = x - xi, yf = y - yi;
    const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
    return lerp(lerp(at(xi, yi), at(xi + 1, yi), u), lerp(at(xi, yi + 1), at(xi + 1, yi + 1), u), v);
  };
}

function pxCircle(g, cx, cy, r, color) {
  g.fillStyle = color;
  for (let y = -r; y <= r; y++) {
    const w = Math.floor(Math.sqrt(r * r - y * y + r * 0.8));
    g.fillRect(Math.round(cx - w), Math.round(cy + y), w * 2 + 1, 1);
  }
}
function pxEllipse(g, cx, cy, rx, ry, color) {
  g.fillStyle = color;
  for (let y = -ry; y <= ry; y++) {
    const w = Math.floor(rx * Math.sqrt(Math.max(0, 1 - (y * y) / (ry * ry + 0.6))));
    g.fillRect(Math.round(cx - w), Math.round(cy + y), w * 2 + 1, 1);
  }
}

function layoutWorld(W, H) {
  const T = clamp(Math.round(Math.min(W, H) * 0.14), 20, 40);
  const play = { x0: T + 2, y0: T + 8, x1: W - T - 2, y1: H - T - 4 };
  const portrait = H > W * 1.15;
  const barn = { w: 46, h: 44 };
  barn.x = play.x0 + 6; barn.y = play.y0 - 6;
  const board = { x: barn.x + barn.w + 10, y: barn.y + 16, w: 26, h: 20 };
  const cols = portrait ? 2 : (play.x1 - play.x0 > 300 ? 4 : 3), rows = portrait ? 3 : 2;
  const pw = 26, ph = 14, gx = 20, gy = 16;
  const fw = cols * pw + (cols - 1) * gx, fh = rows * ph + (rows - 1) * gy;
  const fx = portrait ? Math.round((W - fw) / 2 + 8) : Math.round(play.x1 - fw - 12);
  const fy = portrait ? Math.round(barn.y + barn.h + 34) : Math.round((play.y0 + play.y1) / 2 - fh / 2 + 12);
  const plots = [];
  for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++)
    plots.push({ x: fx + c * (pw + gx), y: fy + r * (ph + gy), w: pw, h: ph, i: plots.length });
  const field = { x: fx - 12, y: fy - 8, w: fw + 16, h: fh + 14 };
  const pond = { cx: play.x0 + 30, cy: play.y1 - 14, rx: 24, ry: 11 };
  const rest = { x: barn.x + 10, y: barn.y + barn.h + 12 };
  const door = { x: barn.x + barn.w / 2, y: barn.y + barn.h + 2 };
  return { W, H, T, play, portrait, barn, board, plots, field, pond, rest, door, rocks: [] };
}

function blocked(L, x, y) {
  const inR = (r, pad = 0) => x > r.x - pad && x < r.x + r.w + pad && y > r.y - pad && y < r.y + r.h + pad + 4;
  if (inR(L.barn, 8) || inR(L.board, 6) || inR(L.field, 2)) return true;
  if (((x - L.pond.cx) / (L.pond.rx + 8)) ** 2 + ((y - L.pond.cy) / (L.pond.ry + 6)) ** 2 < 1) return true;
  return L.rocks.some(r => Math.abs(x - r.x) < r.w / 2 + 6 && y > r.y - 4 && y < r.y + r.h + 4);
}

function drawTree(g, x, y, r, rnd) {
  pxEllipse(g, x + 2, y + r - 1, r + 1, Math.max(3, r / 3), "rgba(20,32,10,.35)");
  g.fillStyle = G.trunk; g.fillRect(x - 2, y + r - 7, 4, 7);
  g.fillStyle = G.t0; g.fillRect(x - 3, y + r - 7, 1, 7); g.fillRect(x + 2, y + r - 7, 1, 7);
  pxCircle(g, x, y, r + 1, G.t0);
  pxCircle(g, x, y, r, G.t1);
  pxCircle(g, x - 1, y - 1, r - 2, G.t2);
  pxCircle(g, x - r * 0.3, y - r * 0.35, r * 0.55, G.t3);
  pxCircle(g, x - r * 0.4, y - r * 0.45, r * 0.25, G.t4);
  for (let i = 0; i < r * 1.6; i++) { // leafy speckle
    const a = rnd() * Math.PI * 2, d = rnd() * r * 0.9;
    g.fillStyle = rnd() < 0.5 ? G.t4 : G.t1;
    g.fillRect(Math.round(x + Math.cos(a) * d), Math.round(y + Math.sin(a) * d), 1, 1);
  }
  g.fillStyle = G.t5; g.fillRect(Math.round(x - r * 0.45), Math.round(y - r * 0.5), 1, 1);
}

function drawRock(g, r) {
  const { x, y, w, h: hh } = r, x0 = Math.round(x - w / 2);
  pxEllipse(g, x + 1, y + hh, w / 2 + 1, 3, "rgba(20,32,10,.4)");
  g.fillStyle = G.r0; g.fillRect(x0 + 1, y, w - 2, hh + 1); g.fillRect(x0, y + 1, w, hh - 1);
  g.fillStyle = G.r1; g.fillRect(x0 + 1, y + 1, w - 2, hh - 1);
  g.fillStyle = G.r2; g.fillRect(x0 + 1, y + 1, w - 3, Math.round(hh * 0.55));
  g.fillStyle = G.r3; g.fillRect(x0 + 2, y + 1, w - 5, 2);
  g.fillStyle = G.r4; g.fillRect(x0 + 2, y + 1, 2, 1);
  // Claude's spark, carved (the Thronglets' rocks have their sigil too)
  const cx = Math.round(x), cy = Math.round(y + hh * 0.5);
  g.fillStyle = G.r0;
  for (const [dx, dy] of [[0, -2], [0, -1], [0, 1], [0, 2], [-2, 0], [-1, 0], [1, 0], [2, 0], [-1, -1], [1, 1], [1, -1], [-1, 1], [0, 0]]) g.fillRect(cx + dx, cy + dy, 1, 1);
}

function drawBarn(g, b) {
  const { x, y, w, h: hh } = b;
  pxEllipse(g, x + w / 2 + 3, y + hh, w / 2 + 4, 5, "rgba(20,32,10,.4)");
  const roofH = 16;
  // roof (stepped gable)
  for (let i = 0; i < roofH; i++) {
    const inset = Math.max(0, Math.round((roofH - i) * 0.9) - 2);
    g.fillStyle = i === 0 ? "#2d1511" : "#3b1c16"; g.fillRect(x - 2 + inset, y + i, w + 4 - inset * 2, 1);
    g.fillStyle = "#5e2a20"; g.fillRect(x - 1 + inset, y + i, Math.max(0, w + 2 - inset * 2), 1);
    if (i % 3 === 1) { g.fillStyle = "#70342a"; g.fillRect(x + inset, y + i, Math.max(0, w - inset * 2), 1); }
  }
  // walls
  const wy = y + roofH;
  g.fillStyle = "#3a1410"; g.fillRect(x, wy, w, hh - roofH);
  g.fillStyle = "#a8432f"; g.fillRect(x + 1, wy, w - 2, hh - roofH - 1);
  g.fillStyle = "#933826"; for (let px = x + 4; px < x + w - 2; px += 4) g.fillRect(px, wy, 1, hh - roofH - 1);
  g.fillStyle = "#c2563d"; g.fillRect(x + 1, wy, w - 2, 1);
  // loft window
  g.fillStyle = "#f3ead8"; g.fillRect(x + w / 2 - 5, y + 6, 10, 8);
  g.fillStyle = "#2d1a10"; g.fillRect(x + w / 2 - 4, y + 7, 8, 6);
  g.fillStyle = "#e7b24a"; g.fillRect(x + w / 2 - 3, y + 9, 6, 4);
  // door with the white X
  const dw = 18, dh = hh - roofH - 4, dx = x + w / 2 - dw / 2, dy = wy + 3;
  g.fillStyle = "#f3ead8"; g.fillRect(dx - 1, dy - 1, dw + 2, dh + 1);
  g.fillStyle = "#7a2c1f"; g.fillRect(dx, dy, dw, dh);
  g.fillStyle = "#f3ead8";
  for (let i = 0; i < dh; i++) { const t = Math.round((i / dh) * (dw - 1)); g.fillRect(dx + t, dy + i, 1, 1); g.fillRect(dx + dw - 1 - t, dy + i, 1, 1); }
  g.fillRect(dx + dw / 2, dy, 1, dh);
  // hay bales by the wall
  for (const [hx, hy] of [[x + w + 2, y + hh - 7], [x - 10, y + hh - 7]]) {
    g.fillStyle = "#7a5a1a"; g.fillRect(hx, hy, 9, 7);
    g.fillStyle = "#e0b64e"; g.fillRect(hx + 1, hy + 1, 7, 5);
    g.fillStyle = "#c7973a"; g.fillRect(hx + 1, hy + 3, 7, 1);
  }
}

function drawPlotBase(g, p) {
  g.fillStyle = G.soilo; g.fillRect(p.x - 1, p.y - 1, p.w + 2, p.h + 2);
  g.fillStyle = G.soil; g.fillRect(p.x, p.y, p.w, p.h);
  for (let yy = p.y + 2; yy < p.y + p.h; yy += 4) { g.fillStyle = G.soil2; g.fillRect(p.x + 1, yy, p.w - 2, 1); g.fillStyle = G.soil3; g.fillRect(p.x + 1, yy - 1, p.w - 2, 1); }
}

function buildWorld(W, H, seed = 7) {
  const L = layoutWorld(W, H), rnd = mulberry32(seed);
  const bg = canvas(W, H), g = bg.getContext("2d");
  const fg = canvas(W, H), f = fg.getContext("2d");
  // grass: two octaves of value noise, ordered dithering between five tones, a worn clearing in the middle
  const n1 = valueNoise(seed + 1), n2 = valueNoise(seed + 2), img = g.createImageData(W, H), d = img.data;
  const tones = [G.g0, G.g1, G.g2, G.g3, G.g4, G.worn].map(c => [parseInt(c.slice(1, 3), 16), parseInt(c.slice(3, 5), 16), parseInt(c.slice(5, 7), 16)]);
  const cx = (L.play.x0 + L.play.x1) / 2, cy = (L.play.y0 + L.play.y1) / 2 + 6, rx = (L.play.x1 - L.play.x0) * 0.5, ry = (L.play.y1 - L.play.y0) * 0.52;
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const e = ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2;
    let t = n1(x / 22, y / 22) * 0.62 + n2(x / 7, y / 7) * 0.38 + (1 - Math.min(1.4, e)) * 0.34;
    t += (BAYER[y & 3][x & 3] / 16 - 0.5) * 0.14;
    const k = clamp(Math.floor((t - 0.18) * 5.2), 0, 5), i = (y * W + x) * 4;
    [d[i], d[i + 1], d[i + 2]] = tones[k]; d[i + 3] = 255;
  }
  g.putImageData(img, 0, 0);
  // hexagon-ish worn tiles in the clearing, like the reference's path stones
  g.strokeStyle = "rgba(60,80,25,.25)";
  for (let i = 0; i < 16; i++) {
    const x = cx + (rnd() - 0.5) * rx * 1.2, y = cy + (rnd() - 0.5) * ry * 1.1, s = 8 + rnd() * 6;
    g.fillStyle = "rgba(160,176,96,.18)";
    for (let yy = -s / 2; yy < s / 2; yy++) { const w = s - Math.abs(yy) * 0.8; g.fillRect(Math.round(x - w), Math.round(y + yy), Math.round(w * 2), 1); }
  }
  // tufts and flowers
  for (let i = 0; i < (W * H) / 260; i++) {
    const x = Math.floor(rnd() * W), y = Math.floor(rnd() * H), r = rnd();
    if (r < 0.62) { g.fillStyle = rnd() < 0.5 ? G.g0 : G.g1; g.fillRect(x, y, 1, 2); g.fillRect(x + 2, y + 1, 1, 1); }
    else if (r < 0.8) { g.fillStyle = G.g4; g.fillRect(x, y, 1, 1); }
    else if (!blocked(L, x, y)) { g.fillStyle = ["#f4f1e8", "#f5c542", "#f59cc0", "#b9d9ff"][Math.floor(rnd() * 4)]; g.fillRect(x, y, 1, 1); g.fillStyle = "rgba(255,255,255,.5)"; g.fillRect(x + 1, y, 1, 1); }
  }
  // pond
  const P = L.pond;
  pxEllipse(g, P.cx, P.cy + 1, P.rx + 2, P.ry + 2, "#3d5a22");
  pxEllipse(g, P.cx, P.cy, P.rx + 1, P.ry + 1, G.w0);
  pxEllipse(g, P.cx, P.cy + 1, P.rx, P.ry, G.w1);
  pxEllipse(g, P.cx - 3, P.cy - 2, P.rx - 6, P.ry - 4, G.w2);
  for (let i = 0; i < 8; i++) { g.fillStyle = G.w3; g.fillRect(Math.round(P.cx - P.rx / 2 + rnd() * P.rx), Math.round(P.cy - P.ry / 2 + rnd() * P.ry), 3, 1); }
  for (const s of [-1, 1]) for (let i = 0; i < 3; i++) { g.fillStyle = "#2f5a1f"; g.fillRect(Math.round(P.cx + s * (P.rx - 2 + i * 2)), P.cy - 4 - i, 1, 5); g.fillStyle = "#6b4a2b"; g.fillRect(Math.round(P.cx + s * (P.rx - 2 + i * 2)), P.cy - 5 - i, 1, 2); }
  // rocks (placed where nothing else is)
  for (let tries = 0; L.rocks.length < (L.portrait ? 2 : 4) && tries < 400; tries++) {
    const w = 12 + Math.floor(rnd() * 8), r = { x: L.play.x0 + 10 + rnd() * (L.play.x1 - L.play.x0 - 20), y: L.play.y0 + rnd() * (L.play.y1 - L.play.y0 - 12), w, h: Math.round(w * 0.75) };
    if (!blocked(L, r.x, r.y) && !blocked(L, r.x, r.y + r.h) && L.rocks.every(o => Math.hypot(o.x - r.x, o.y - r.y) > 40)
        && Math.hypot(r.x - L.door.x, r.y - L.door.y) > 40) L.rocks.push(r);
  }
  L.rocks.forEach(r => drawRock(g, r));
  // field plots, barn
  L.plots.forEach(p => drawPlotBase(g, p));
  drawBarn(g, L.barn);
  // trees round the edge: back rows on the background, the bottom row in front of everything
  const trees = [], T = L.T;
  for (let x = -6; x < W + 12; x += 12 + rnd() * 9) { trees.push([x, 2 + rnd() * (T - 16), 10 + rnd() * 5]); if (rnd() < 0.6) trees.push([x + 6, -6 + rnd() * 8, 11 + rnd() * 4]); }
  for (let y = T - 4; y < H - T + 6; y += 12 + rnd() * 8) {
    trees.push([2 + rnd() * (T - 14), y, 9 + rnd() * 5]); trees.push([W - 2 - rnd() * (T - 14), y, 9 + rnd() * 5]);
    if (rnd() < 0.5) trees.push([-4, y + 6, 11]); if (rnd() < 0.5) trees.push([W + 4, y + 6, 11]);
  }
  const front = [];
  for (let x = -6; x < W + 12; x += 12 + rnd() * 9) front.push([x, H - T + 12 + rnd() * 10, 11 + rnd() * 5]);
  trees.sort((a, b) => a[1] - b[1]).forEach(([x, y, r]) => drawTree(g, Math.round(x), Math.round(y), Math.round(r), rnd));
  front.sort((a, b) => a[1] - b[1]).forEach(([x, y, r]) => drawTree(f, Math.round(x), Math.round(y), Math.round(r), rnd));
  return { L, bg, fg };
}

// crops: the field shows the work. Running tasks grow; finished ones bloom into Claude's spark; failed ones wilt.
function drawCrop(g, x, y, stage, t, glow) {
  const sway = Math.round(Math.sin(t * 1.6 + x) * 0.6);
  if (stage === "seed") { g.fillStyle = "#8fd06a"; g.fillRect(x, y - 1, 1, 1); g.fillRect(x + 1, y - 2, 1, 1); return; }
  if (stage === "wilt") { g.fillStyle = "#7a6a3a"; g.fillRect(x, y - 4, 1, 4); g.fillRect(x + 1, y - 4, 2, 1); g.fillStyle = "#5c4b27"; g.fillRect(x + 3, y - 3, 1, 1); return; }
  const tall = stage === "sprout" ? 4 : 7;
  g.fillStyle = "#3f8f35"; g.fillRect(x + sway, y - tall, 1, tall);
  g.fillStyle = "#6fcf5b"; g.fillRect(x - 2 + sway, y - tall + 2, 2, 1); g.fillRect(x + 1 + sway, y - tall + 3, 2, 1);
  if (stage === "grow") { g.fillStyle = "#d97757"; g.fillRect(x + sway, y - tall - 1, 1, 1); }
  if (stage === "ripe") { // the spark
    const cx = x + sway, cy = y - tall - 2;
    if (glow) { g.fillStyle = "rgba(255,200,140,.35)"; g.fillRect(cx - 3, cy - 3, 7, 7); }
    g.fillStyle = "#d97757";
    for (const [dx, dy] of [[0, -2], [0, -1], [0, 1], [0, 2], [-2, 0], [-1, 0], [1, 0], [2, 0], [-1, -1], [1, 1], [1, -1], [-1, 1]]) g.fillRect(cx + dx, cy + dy, 1, 1);
    g.fillStyle = "#ffd59e"; g.fillRect(cx, cy, 1, 1);
  }
}

// ==================================================================== critters
class Critter {
  constructor(key, x, y) {
    Object.assign(this, { key, x, y, tx: x, ty: y, wait: Math.random() * 2, dir: 0, moving: false, phase: 0,
      phaseT: 0, blinkT: 2 + Math.random() * 3, blink: 0, born: performance.now(), hop: 0, speed: 20 + Math.random() * 6 });
    this.mode = "wander"; this.kind = "claude"; this.hat = "straw"; this.color = HAT_COLORS[0];
    this.label = ""; this.bubble = null; this.gone = 0;
  }
  setTarget(x, y) { this.tx = x; this.ty = y; }
  update(dt, world) {
    const L = world.L;
    if (this.kind === "egg") { this.hop = (this.hop + dt) % 3; return; }
    this.blinkT -= dt;
    if (this.blinkT < 0) { this.blink = 0.14; this.blinkT = 2 + Math.random() * 4; }
    this.blink = Math.max(0, this.blink - dt);
    let goal = null;
    if (this.mode === "work" && this.plot) goal = { x: this.plot.x - 7, y: this.plot.y + this.plot.h };
    else if (this.mode === "sleep") goal = world.restSpot(this);
    else if (this.mode === "starting") goal = { x: L.door.x + ((hashStr(this.key) % 5) - 2) * 7, y: L.door.y + 8 };
    if (goal) { this.tx = goal.x; this.ty = goal.y; }
    else if (this.mode === "wander") {
      if (Math.hypot(this.tx - this.x, this.ty - this.y) < 1.5) {
        this.wait -= dt;
        if (this.wait <= 0) { const p = world.randomSpot(); this.tx = p.x; this.ty = p.y; this.wait = 1 + Math.random() * 4; }
      }
    } else { this.tx = this.x; this.ty = this.y; }
    const dx = this.tx - this.x, dy = this.ty - this.y, dist = Math.hypot(dx, dy);
    this.moving = dist > 1.2;
    if (this.moving) {
      const sp = (this.mode === "work" ? 34 : this.speed) * dt * (REDUCED ? 0.7 : 1);
      this.x += (dx / dist) * Math.min(sp, dist); this.y += (dy / dist) * Math.min(sp, dist);
      this.dir = Math.abs(dx) > 0.5 ? Math.sign(dx) : this.dir;
      this.phaseT += dt; if (this.phaseT > 0.16) { this.phaseT = 0; this.phase = this.phase === 1 ? 2 : 1; }
    } else {
      this.phase = 0;
      if (this.mode === "work") this.dir = 1;
    }
    if (this.mode === "error") this.hop = (this.hop + dt * 6) % (Math.PI * 2);
  }
  sprite(t) {
    if (this.kind === "egg") return EGG;
    const sleep = this.mode === "sleep" && !this.moving;
    const typing = this.mode === "work" && !this.moving;
    const cheer = performance.now() - this.born < 1800;
    return critterSprite(this.hat, this.color, {
      look: this.dir, legs: this.phase, blink: this.blink > 0 || this.mode === "offline", sleep,
      arms: (typing && Math.floor(t * 6) % 2) || (cheer && Math.floor(t * 4) % 2) ? 1 : 0,
    });
  }
  bob(t) {
    if (this.kind === "egg") return 0;
    if (this.mode === "error") return -Math.abs(Math.sin(this.hop)) * 3;
    if (this.moving) return this.phase === 1 ? -1 : 0;
    if (this.mode === "sleep") return 1;
    return Math.sin(t * 2 + this.x) > 0.7 ? -1 : 0; // idle breathing
  }
}

// ======================================================================= scene
const Scene = {
  cv: $("#world"), ctx: null, S: 3, world: null, critters: new Map(), sparkles: [], fireflies: [],
  labels: $("#labels"), selected: null, hover: null, plotTasks: [], boardCount: 0, demo: false, t: 0,
  init() {
    this.ctx = this.cv.getContext("2d");
    addEventListener("resize", () => this.resize());
    this.cv.addEventListener("pointermove", (e) => this.onMove(e));
    this.cv.addEventListener("click", (e) => this.onClick(e));
    this.resize();
    let last = performance.now();
    const loop = (now) => { const dt = Math.min(0.05, (now - last) / 1000); last = now; this.t += dt; this.frame(dt); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  },
  resize() {
    const S = clamp(Math.round(Math.min(innerWidth / 330, innerHeight / 205)), 2, 6);
    const W = Math.ceil(innerWidth / S), H = Math.ceil(innerHeight / S);
    this.S = S;
    this.cv.width = W; this.cv.height = H;
    this.cv.style.width = W * S + "px"; this.cv.style.height = H * S + "px";
    this.ctx.imageSmoothingEnabled = false;
    this.world = buildWorld(W, H, 7);
    this.world.randomSpot = () => this.randomSpot();
    this.world.restSpot = (c) => this.restSpot(c);
    for (const c of this.critters.values()) {
      const p = this.randomSpot(); if (c.x > W || c.y > H || blocked(this.world.L, c.x, c.y)) { c.x = p.x; c.y = p.y; } c.tx = c.x; c.ty = c.y;
    }
  },
  randomSpot() {
    const L = this.world.L;
    for (let i = 0; i < 60; i++) {
      const x = L.play.x0 + 8 + Math.random() * (L.play.x1 - L.play.x0 - 16), y = L.play.y0 + 16 + Math.random() * (L.play.y1 - L.play.y0 - 16);
      if (!blocked(L, x, y)) return { x, y };
    }
    return { x: L.door.x, y: L.door.y + 12 };
  },
  restSpot(c) {
    const L = this.world.L, sleepers = [...this.critters.values()].filter(o => o.mode === "sleep").sort((a, b) => a.key < b.key ? -1 : 1);
    const i = Math.max(0, sleepers.indexOf(c));
    return { x: L.barn.x - 2 + (i % 5) * 19, y: L.barn.y + L.barn.h + 16 + Math.floor(i / 5) * 16 };
  },
  toLogical(e) { return { x: e.clientX / this.S, y: e.clientY / this.S }; },
  hit(p) {
    let best = null;
    for (const c of this.critters.values()) {
      if (c.gone) continue;
      const w = c.kind === "egg" ? 7 : 9;
      if (p.x > c.x - w && p.x < c.x + w && p.y > c.y - 19 && p.y < c.y + 2 && (!best || c.y > best.y)) best = c;
    }
    if (best) return { critter: best };
    const L = this.world.L;
    for (const [i, p2] of L.plots.entries()) if (p.x >= p2.x - 2 && p.x <= p2.x + p2.w + 2 && p.y >= p2.y - 10 && p.y <= p2.y + p2.h + 2 && this.plotTasks[i]) return { task: this.plotTasks[i] };
    const b = L.board; if (p.x >= b.x - 2 && p.x <= b.x + b.w + 2 && p.y >= b.y - 4 && p.y <= b.y + b.h + 8) return { board: true };
    const br = L.barn; if (p.x >= br.x && p.x <= br.x + br.w && p.y >= br.y && p.y <= br.y + br.h) return { barn: true };
    return null;
  },
  onMove(e) { if (this.demo) return; const r = this.hit(this.toLogical(e)); this.hover = r?.critter || null; this.cv.style.cursor = r ? "pointer" : "default"; },
  onClick(e) {
    if (this.demo) return;
    const r = this.hit(this.toLogical(e));
    if (!r) { this.selected = null; return; }
    if (r.critter) { this.selected = r.critter; UI.openCritter(r.critter); }
    else if (r.task) UI.openQuest(r.task);
    else if (r.board) UI.openQuests("queued");
    else if (r.barn) UI.openParty();
  },
  sparkle(x, y, n = 14, color) {
    for (let i = 0; i < n; i++) { const a = Math.random() * Math.PI * 2, s = 10 + Math.random() * 26;
      this.sparkles.push({ x, y, vx: Math.cos(a) * s, vy: Math.sin(a) * s - 12, life: 0.7 + Math.random() * 0.5, c: color || ["#fff4c2", "#ffd59e", "#f2a07a", "#ffffff"][i % 4] }); }
  },
  darkness() {
    const d = new Date(), hr = d.getHours() + d.getMinutes() / 60;
    const N = 0.36;
    if (hr >= 20.5 || hr < 5.5) return N;
    if (hr >= 18.5) return (hr - 18.5) / 2 * N;
    if (hr < 7) return (7 - hr) / 1.5 * N;
    return 0;
  },
  frame(dt) {
    const g = this.ctx, W = this.cv.width, H = this.cv.height, w = this.world, L = w.L, t = this.t;
    g.drawImage(w.bg, 0, 0);
    // crops
    this.plotTasks.forEach((task, i) => {
      const p = L.plots[i]; if (!p || !task) return;
      const stage = task.status === "done" ? "ripe" : task.status === "failed" ? "wilt"
        : (nowS() - (task.started || task.updated || nowS())) < 120 ? "seed" : (nowS() - (task.started || 0)) < 900 ? "sprout" : "grow";
      for (let k = 0; k < 3; k++) drawCrop(g, p.x + 5 + k * 8, p.y + p.h - 3, stage, t + k, task.status === "done");
    });
    // quest board
    const b = L.board;
    g.fillStyle = "rgba(20,32,10,.35)"; g.fillRect(b.x + 1, b.y + b.h + 5, b.w, 3);
    g.fillStyle = "#4a3120"; g.fillRect(b.x + 2, b.y + 4, 2, b.h + 4); g.fillRect(b.x + b.w - 4, b.y + 4, 2, b.h + 4);
    g.fillStyle = "#3a2616"; g.fillRect(b.x, b.y, b.w, b.h - 2);
    g.fillStyle = "#8a6038"; g.fillRect(b.x + 1, b.y + 1, b.w - 2, b.h - 4);
    g.fillStyle = "#a37446"; g.fillRect(b.x + 1, b.y + 1, b.w - 2, 1);
    for (let i = 0; i < Math.min(6, this.boardCount); i++) {
      const nx = b.x + 3 + (i % 3) * 7, ny = b.y + 3 + Math.floor(i / 3) * 7;
      g.fillStyle = i % 2 ? "#fff4c2" : "#f6f1e4"; g.fillRect(nx, ny, 5, 5);
      g.fillStyle = "#b9ad90"; g.fillRect(nx + 1, ny + 2, 3, 1);
      g.fillStyle = "#c0392b"; g.fillRect(nx + 2, ny, 1, 1);
    }
    // critters, depth-sorted
    const list = [...this.critters.values()];
    for (const c of list) c.update(dt, w);
    list.sort((a, c) => a.y - c.y);
    for (const c of list) {
      const s = c.sprite(t), bob = Math.round(c.bob(t));
      const fade = c.gone ? clamp(1 - (performance.now() - c.gone) / 500, 0, 1) : clamp((performance.now() - c.born) / 350, 0, 1);
      g.globalAlpha = fade;
      pxEllipse(g, Math.round(c.x), Math.round(c.y), c.kind === "egg" ? 5 : 7, 2, "rgba(20,28,10,.35)");
      if (c.kind === "egg") {
        const wob = Math.floor(c.hop * 4) % 6 === 0 ? (Math.floor(c.hop * 8) % 2 ? 1 : -1) : 0;
        g.drawImage(s, Math.round(c.x - 6 + wob), Math.round(c.y - 12));
      } else {
        g.drawImage(s, Math.round(c.x - 8), Math.round(c.y - (HAT_H + 12) + 1 + bob));
        if (c.mode === "work" && !c.moving) g.drawImage(Math.floor(t * 3) % 2 ? LAPTOP_ON : LAPTOP_OFF, Math.round(c.x + 4), Math.round(c.y - 6));
      }
      g.globalAlpha = 1;
      if (c === this.selected && !c.gone) { const ay = Math.round(c.y - 26 + Math.sin(t * 5) * 1.5); g.fillStyle = "#fff"; g.fillRect(c.x - 2, ay, 5, 1); g.fillRect(c.x - 1, ay + 1, 3, 1); g.fillRect(c.x, ay + 2, 1, 1); }
    }
    for (const c of list) if (c.gone && performance.now() - c.gone > 520) { this.critters.delete(c.key); c.el?.remove(); c.tagEl?.remove(); }
    g.drawImage(w.fg, 0, 0);
    // night: a blue wash, glowing terminals and fireflies
    const dk = this.darkness();
    if (dk > 0) {
      g.fillStyle = `rgba(16,22,58,${dk})`; g.fillRect(0, 0, W, H);
      for (const c of list) if (c.mode === "work" && !c.moving) { g.fillStyle = "rgba(124,252,154,.25)"; g.fillRect(Math.round(c.x + 2), Math.round(c.y - 9), 12, 9); g.drawImage(LAPTOP_ON, Math.round(c.x + 4), Math.round(c.y - 6)); }
      if (this.fireflies.length < 18) this.fireflies.push({ x: Math.random() * W, y: Math.random() * H, p: Math.random() * 9 });
      for (const f of this.fireflies) { f.p += dt; f.x += Math.sin(f.p * 0.7) * 0.2; f.y += Math.cos(f.p * 0.5) * 0.15;
        if (Math.sin(f.p * 2) > 0.2) { g.fillStyle = "#fff6a8"; g.fillRect(Math.round(f.x), Math.round(f.y), 1, 1); g.fillStyle = "rgba(255,246,168,.25)"; g.fillRect(Math.round(f.x) - 1, Math.round(f.y) - 1, 3, 3); } }
    }
    // sparkles
    this.sparkles = this.sparkles.filter(p => (p.life -= dt) > 0);
    for (const p of this.sparkles) { p.x += p.vx * dt; p.y += p.vy * dt; p.vy += 40 * dt; g.fillStyle = p.c; g.fillRect(Math.round(p.x), Math.round(p.y), 1, 1); }
    this.placeLabels(list, t);
  },
  placeLabels(list, t) {
    const S = this.S;
    for (const c of list) {
      if (this.demo) break;
      let want = c.gone ? null : c.bubble;
      if (want?.title) want = { ...want, text: (c === this.selected || c === this.hover) ? want.title.slice(0, 30) : "" };
      if (want) {
        if (!c.el) { c.el = h("div", { class: "bubble" }); this.labels.append(c.el); }
        const sig = want.icon + "|" + (want.text || "") + "|" + (want.alert ? 1 : 0);
        if (c.el.dataset.sig !== sig) {
          c.el.dataset.sig = sig; c.el.className = "bubble" + (want.alert ? " alert" : ""); fill(c.el, h("img", { src: icon(want.icon), alt: "" }), want.text ? h("span", { text: want.text }) : null);
        }
        const lift = c.kind === "egg" ? 16 : 25;
        c.el.style.transform = `translate(${Math.round(c.x * S)}px, ${Math.round((c.y - lift) * S)}px) translate(-50%, -100%)`;
      } else if (c.el) { c.el.remove(); c.el = null; }
      const showTag = c.mode !== "sleep" || c === this.selected || c === this.hover; // nappers huddle: tags on hover
      if (c.label && !c.gone && showTag) {
        if (!c.tagEl) { c.tagEl = h("div", { class: "tag" }); this.labels.append(c.tagEl); }
        if (c.tagEl.textContent !== c.label) c.tagEl.textContent = c.label;
        c.tagEl.classList.toggle("sel", c === this.selected || c === this.hover);
        c.tagEl.style.transform = `translate(${Math.round(c.x * S)}px, ${Math.round((c.y + 3) * S)}px) translate(-50%, 0)`;
      } else if (c.tagEl) { c.tagEl.remove(); c.tagEl = null; }
    }
  },
};

// ============================================================== farm state sync
const App = { state: null, seenAgents: null, seenEvents: 0, lastTitles: {}, polling: null, user: null };

function modeOf(w, paused) {
  const s = (w?.state || "").toLowerCase();
  if (s === "running") return "work";
  if (s.startsWith("throttled")) return "sleep";
  if (s.startsWith("paused") || paused) return "rest";
  if (s.startsWith("error")) return "error";
  return "wander";
}

function colorFor(id) { return HAT_COLORS[hashStr(id) % HAT_COLORS.length]; }

function reconcile(st) {
  const first = App.seenAgents === null;
  App.seenAgents = App.seenAgents || new Set();
  const want = new Map();
  for (const a of st.agents) {
    const base = { agent: a, hat: a.hat || "straw", color: colorFor(a.id) };
    if (!a.loggedIn && !a.remote) want.set("egg:" + a.id, { ...base, kind: "egg", mode: "egg" });
    else if (!a.workers.length) want.set("agent:" + a.id, { ...base, kind: "claude", mode: a.alive ? "starting" : "offline", worker: null });
    else for (const w of a.workers) want.set(w.id, { ...base, kind: "claude", worker: w, mode: modeOf(w, st.paused) });
  }
  const L = Scene.world.L;
  const eggs = new Map([...Scene.critters.values()].filter(c => c.kind === "egg" && !c.gone).map(c => [c.agent.id, c]));
  for (const [key, c] of Scene.critters) if (!want.has(key) && !c.gone) { c.gone = performance.now(); if (c.kind !== "egg") Scene.sparkle(c.x, c.y - 8, 8, "#d8e6c4"); }
  const running = [...st.tasks.running].sort((a, b) => a.id < b.id ? -1 : 1);
  const plotOf = new Map(running.map((t, i) => [t.id, L.plots[i]]));
  for (const [key, d] of want) {
    let c = Scene.critters.get(key);
    if (!c) {
      const egg = eggs.get(d.agent.id);
      let p = first ? Scene.randomSpot() : { x: L.door.x + (Math.random() - 0.5) * 6, y: L.door.y + 4 };
      if (egg && d.kind === "claude") { p = { x: egg.x, y: egg.y }; Scene.sparkle(egg.x, egg.y - 6, 24); }
      if (d.kind === "egg" && !first) p = Scene.randomSpot();
      c = new Critter(key, p.x, p.y);
      if (!first && d.kind === "claude") { c.born = performance.now(); if (!egg) Scene.sparkle(p.x, p.y - 8, 16); }
      else c.born = performance.now() - 5000;
      Scene.critters.set(key, c);
      if (!first && !App.seenAgents.has(d.agent.id)) UI.say(d.kind === "egg" ? `An egg appeared! Tap it to hatch ${d.agent.name.toUpperCase()}.` : `A wild ${d.agent.name.toUpperCase()} appeared!`);
      else if (!first && egg && d.kind === "claude") UI.say(`${d.agent.name.toUpperCase()} hatched! Welcome to the farm.`);
    }
    Object.assign(c, { kind: d.kind, agent: d.agent, worker: d.worker, hat: d.hat, color: d.color, mode: d.mode });
    c.plot = d.worker?.task ? plotOf.get(d.worker.task) : null;
    if (c.mode === "work" && !c.plot) c.mode = "wander";
    const title = d.worker?.task_title || "";
    c.label = d.kind === "egg" ? d.agent.name : (d.agent.workers.length > 1 && d.worker ? `${d.agent.name}·${d.worker.name}` : d.agent.name);
    c.bubble = c.kind === "egg" ? { icon: d.agent.login?.state === "waiting_code" ? "dots" : "ask" }
      : c.mode === "work" ? { icon: d.worker?.task && st.tasks.running.find(t => t.id === d.worker.task)?.kind === "plan" ? "plan" : "terminal", title }
      : c.mode === "sleep" ? { icon: "zzz" } : c.mode === "rest" ? { icon: "pause" } : c.mode === "error" ? { icon: "alert", alert: true }
      : c.mode === "starting" ? { icon: "dots" } : c.mode === "offline" ? { icon: "alert", alert: true } : null;
  }
  for (const a of st.agents) App.seenAgents.add(a.id);
  // the field: running first, then the latest harvest, then what wilted
  const plots = [...running, ...st.tasks.done, ...st.tasks.failed.slice(0, 2)].slice(0, L.plots.length);
  Scene.plotTasks = plots;
  Scene.boardCount = st.counts.queued;
}

// =========================================================================== UI
const api = async (path, body) => {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Clodfarm": "1" }, body: JSON.stringify(body) };
  const r = await fetch(path, { credentials: "same-origin", ...opt });
  let data = {};
  try { data = await r.json(); } catch { /* empty */ }
  if (r.status === 401 && path !== "/api/login" && path !== "/api/me") { UI.showTitle(); throw new Error("log in first"); }
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
};

const EVENT_TEXT = {
  "task.added": (e, T) => `New quest on the board: “${T(e)}”.`,
  "task.claimed": (e, T) => { const w = e.msg.split(" by ")[1] || ""; return `${w ? w.split("@")[0] + "·" + w.split("/").pop() : "A Claude"} took on “${T(e)}”.`; },
  "task.done": (e, T) => `★ Quest complete: “${T(e)}”!`,
  "task.failed": (e, T) => `Quest failed: “${T(e)}”. Check the quest log.`,
  "task.waiting": (e, T) => `“${T(e)}” split into sub-quests and waits for them.`,
  "task.resume": (e, T) => `Back to “${T(e)}”: its sub-quests are done.`,
  "task.cancelled": (e, T) => `Quest cancelled: “${T(e)}”.`,
  "verify.passed": (e, T) => `Tests passed for “${T(e)}”. Harvesting it into main!`,
  "verify.failed": (e, T) => `Tests failed for “${T(e)}”. Sent back to fix them.`,
  "farm.paused": (e) => `The farm is paused: ${e.msg || "by hand"}.`,
  "farm.resumed": () => "The farm is back at work!",
  "budget.rejected": () => "Usage limit reached. These Claudes nap until the window resets.",
  "rc.connected": () => "Remote Control is live: steer the farm from the Claude app.",
  "mission.set": (e) => `New main quest: ${e.msg}`,
  "planner.no_mission": () => "No main quest yet. Tap GOAL to set one.",
  "agent.added": (e) => `A new egg for ${e.msg.split(" ")[0].toUpperCase()}. Finish its login to hatch it.`,
  "agent.removed": (e) => `${e.msg.split(" ")[0].toUpperCase()} left the farm.`,
};

const UI = {
  queue: [], typing: null, tab: "active", hatchFor: null, hatchPoll: null, questBack: null,

  // ------------------------------------------------------------ title / login
  async boot() {
    for (const img of $$("img[data-icon]")) img.src = icon(img.dataset.icon);
    Scene.init();
    this.bind();
    try { const me = await api("/api/me"); App.user = me.user; this.showFarm(); }
    catch { this.showTitle(); }
  },
  showTitle() {
    clearInterval(App.polling); App.polling = null;
    for (const d of $$("dialog[open]")) d.close();
    $("#hud").hidden = true; $("#title").hidden = false;
    Scene.demo = true; fill(Scene.labels, null); Scene.critters.clear(); Scene.plotTasks = []; Scene.boardCount = 3;
    App.seenAgents = null;
    const hats = ["straw", "beanie", "cap", "sprout", "bow", "headphones"];
    hats.forEach((hat, i) => { const p = Scene.randomSpot(), c = new Critter("demo" + i, p.x, p.y); c.hat = hat; c.color = HAT_COLORS[i + 1]; c.born -= 5000; Scene.critters.set(c.key, c); });
    setTimeout(() => $("#login-form [name=password]").focus(), 50);
  },
  showFarm() {
    $("#title").hidden = true; $("#hud").hidden = false;
    Scene.demo = false; Scene.critters.clear(); fill(Scene.labels, null);
    App.seenAgents = null; App.seenEvents = nowS() - 1;
    this.refresh(true);
    clearInterval(App.polling);
    App.polling = setInterval(() => this.refresh(), 2500);
  },
  async refresh(first = false) {
    let st;
    try { st = await api("/api/state"); } catch { return; }
    App.state = st;
    for (const k of ["running", "waiting", "queued", "done", "failed"]) for (const t of st.tasks[k]) App.lastTitles[t.id] = t.title;
    reconcile(st);
    this.renderHud(st);
    if (first) this.welcome(st);
    else this.pushEvents(st);
    if ($("#dlg-party").open) this.renderParty();
    if ($("#dlg-quests").open) this.renderQuests();
    if ($("#dlg-summary").open && this.summaryKey) { const c = Scene.critters.get(this.summaryKey); if (c) this.renderSummary(c); }
  },
  welcome(st) {
    const live = st.agents.filter(a => a.loggedIn);
    if (!live.length) { this.say(`Welcome to ${st.farm.toUpperCase()}! No Claude lives here yet. Tap HATCH to log in your first one.`); return; }
    const n = st.agents.reduce((s, a) => s + a.workers.length, 0);
    this.say(`Welcome back to ${st.farm.toUpperCase()}! ${n} Claude${n === 1 ? "" : "s"} on the farm, ${st.counts.running} quest${st.counts.running === 1 ? "" : "s"} in progress, ${st.counts.queued} on the board.`);
    if (!st.goal) this.say("There's no main quest yet. Tap GOAL to give the farm a mission.");
  },
  pushEvents(st) {
    const T = (e) => App.lastTitles[e.task] || (e.msg || "").replace(/^\S+\s*/, "").slice(0, 60) || e.task;
    for (const e of st.events) {
      if (e.at <= App.seenEvents) continue;
      App.seenEvents = Math.max(App.seenEvents, e.at);
      const f = EVENT_TEXT[e.type];
      if (f) this.say(f(e, T));
      if (e.type === "task.done") { const p = Scene.plotTasks.findIndex(t => t.id === e.task), pl = Scene.world.L.plots[p]; if (pl) Scene.sparkle(pl.x + pl.w / 2, pl.y, 20); }
    }
  },
  renderHud(st) {
    $("#goal-text").textContent = (st.goal || "SET A MISSION").toUpperCase();
    const claudes = st.agents.reduce((s, a) => s + a.workers.length, 0), eggs = st.agents.filter(a => !a.loggedIn && !a.remote).length;
    const chips = [
      h("span", { class: "chip" }, h("i", { class: "dot" + (claudes ? "" : " off") }), `${st.farm.toUpperCase()}`),
      h("span", { class: "chip" }, "CLAUDES ", h("b", { text: String(claudes) }), eggs ? ` · EGGS ${eggs}` : ""),
      h("span", { class: "chip" }, "QUESTS ", h("b", { text: String(st.counts.running) }), " ACTIVE · ", h("b", { text: String(st.counts.queued) }), " QUEUED"),
    ];
    if (st.paused) chips.push(h("span", { class: "chip warn" }, "⏸ PAUSED: " + (st.pause_reason || "").slice(0, 40).toUpperCase()));
    fill($("#chips"), ...chips);
    $("#menu-pause").textContent = st.paused ? "RESUME FARM" : "PAUSE FARM";
  },

  // ---------------------------------------------------------------- textbox
  say(text) { this.queue.push(text); if (!this.typing) this.next(); },
  next() {
    const box = $("#textbox"), p = $("#textbox-text");
    clearTimeout(this.hideT);
    const text = this.queue.shift();
    if (text == null) { this.typing = null; this.hideT = setTimeout(() => box.classList.add("idle"), 7000); return; }
    box.classList.remove("idle");
    let i = 0;
    this.typing = { text, done: false };
    clearInterval(this.typeT);
    this.typeT = setInterval(() => {
      i = Math.min(text.length, i + (REDUCED ? text.length : 2));
      p.textContent = text.slice(0, i);
      if (i >= text.length) { clearInterval(this.typeT); this.typing.done = true; this.hideT = setTimeout(() => this.next(), this.queue.length ? 1800 : 5000); }
    }, 28);
  },
  skip() {
    if (!this.typing) return $("#textbox").classList.add("idle");
    if (!this.typing.done) { clearInterval(this.typeT); $("#textbox-text").textContent = this.typing.text; this.typing.done = true; clearTimeout(this.hideT); this.hideT = setTimeout(() => this.next(), 4000); }
    else { clearTimeout(this.hideT); this.next(); }
  },

  // ------------------------------------------------------------------- binds
  bind() {
    $("#login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(e.target), err = $("#login-error"), btn = e.target.querySelector("button");
      err.textContent = ""; btn.disabled = true;
      try { const r = await api("/api/login", { password: f.get("password") }); App.user = r.user; e.target.reset(); this.showFarm(); }
      catch (x) { err.textContent = x.message.toUpperCase(); }
      finally { btn.disabled = false; }
    });
    document.addEventListener("click", (e) => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act) { this.act(act, e); return; }
      if (e.target.closest("[data-close]")) e.target.closest("dialog").close();
      if (!e.target.closest("#menu, #menu-btn")) $("#menu").hidden = true;
    });
    $("#menu-btn").addEventListener("click", (e) => { e.stopPropagation(); const m = $("#menu"); m.hidden = !m.hidden; if (!m.hidden) m.querySelector("button").focus(); });
    $("#goal").addEventListener("click", () => this.openMission());
    $("#textbox").addEventListener("click", () => this.skip());
    for (const d of $$("dialog")) {
      d.addEventListener("click", (e) => { // a click on the backdrop (outside the box) closes it
        const r = d.getBoundingClientRect();
        if (e.target === d && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)) d.close();
      });
      d.addEventListener("close", () => { if (d.id === "dlg-hatch") this.stopHatchPoll(); if (d.id === "dlg-summary") { this.summaryKey = null; Scene.selected = null; } });
    }
    $("#new-form [name=priority]").addEventListener("input", (e) => ($("#prio-out").textContent = e.target.value));
    $("#new-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(e.target), err = e.target.querySelector(".form-error");
      try { await api("/api/tasks", { title: f.get("title"), prompt: f.get("prompt") || f.get("title"), priority: +f.get("priority") });
        $("#dlg-new").close(); e.target.reset(); $("#prio-out").textContent = "5"; this.refresh(); }
      catch (x) { err.textContent = x.message; }
    });
    $("#mission-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = e.target.querySelector(".form-error");
      try { await api("/api/mission", { text: new FormData(e.target).get("text") }); $("#dlg-mission").close(); this.say("Main quest saved. The planner reads it when the board is empty."); this.refresh(); }
      catch (x) { err.textContent = x.message; }
    });
    $("#quest-tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) { this.tab = b.dataset.tab; this.renderQuests(); } });
    addEventListener("keydown", (e) => {
      if (e.key === "Escape") $("#menu").hidden = true;
      if ($("#hud").hidden || $$("dialog[open]").length || /INPUT|TEXTAREA/.test(document.activeElement?.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
      const k = { n: "new", q: "quests", p: "party", h: "hatch", m: "mission", j: "journal" }[e.key.toLowerCase()];
      if (k) { e.preventDefault(); this.act(k); }
    });
  },
  async act(a) {
    $("#menu").hidden = true;
    if (a === "quests") return this.openQuests();
    if (a === "party") return this.openParty();
    if (a === "hatch") return this.openHatch();
    if (a === "new") { for (const d of $$("dialog[open]")) d.close(); $("#dlg-new").showModal(); $("#new-form [name=title]").focus(); return; }
    if (a === "mission") return this.openMission();
    if (a === "journal") return this.openJournal();
    if (a === "pause") { const p = App.state?.paused; await api(p ? "/api/resume" : "/api/pause", {}); return this.refresh(); }
    if (a === "logout") { await api("/api/logout", {}); return this.showTitle(); }
  },

  // ----------------------------------------------------------------- dialogs
  hp(label, util, resets) {
    if (util == null) { const i = h("i"); i.style.width = "0"; return h("div", { class: "hp" }, h("span", { text: label }), h("div", { class: "bar" }, i), h("span", { class: "lbl", text: "NOT MEASURED YET" })); }
    const left = clamp(1 - util, 0, 1), cls = left > 0.5 ? "" : left > 0.2 ? "mid" : "low";
    const bar = h("i", { class: cls }); bar.style.width = `${Math.round(left * 100)}%`;
    return h("div", { class: "hp", title: `${Math.round(util * 100)}% used` }, h("span", { text: label }), h("div", { class: "bar" }, bar),
      h("span", { class: "lbl", text: `${Math.round(left * 100)}% LEFT${resets ? " · " + until(resets) : ""}` }));
  },
  spriteCanvas(c, size) {
    const cv = h("canvas", { width: 20, height: 20 }), g = cv.getContext("2d");
    g.imageSmoothingEnabled = false;
    if (c.kind === "egg") g.drawImage(EGG, 4, 6); else g.drawImage(critterSprite(c.hat, c.color, { legs: 0 }), 2, 1);
    if (size) cv.style.width = cv.style.height = size + "px";
    return cv;
  },
  agentState(a) {
    if (!a.loggedIn && !a.remote) return a.login?.state === "waiting_code" ? "WAITING FOR ITS LOGIN CODE" : "AN EGG: NEEDS A CLAUDE LOGIN";
    if (!a.workers.length) return a.alive ? "WAKING UP…" : "NOT RUNNING";
    const busy = a.workers.filter(w => w.state === "running").length, sleep = a.workers.filter(w => (w.state || "").startsWith("throttled")).length;
    return `${busy}/${a.workers.length} WORKING` + (sleep ? ` · ${sleep} NAPPING (BUDGET)` : "");
  },
  openParty() { for (const d of $$("dialog[open]")) d.close(); this.renderParty(); $("#dlg-party").showModal(); },
  renderParty() {
    const st = App.state; if (!st) return;
    fill($("#party-list"), ...st.agents.map(a => {
      const fake = { kind: a.loggedIn || a.remote ? "claude" : "egg", hat: a.hat, color: colorFor(a.id) };
      const b = a.budget || {};
      return h("li", { class: fake.kind === "egg" ? "egg" : "" }, h("button", { type: "button", onclick: () => this.openAgent(a.id) },
        this.spriteCanvas(fake),
        h("div", {},
          h("div", { class: "pname" }, h("span", { text: a.name.toUpperCase() }), h("em", { text: a.primary ? "PRIMARY" : a.remote ? "OTHER BOX" : (a.plan || "").toUpperCase() })),
          a.loggedIn ? [this.hp("5H", b.five_hour, b.five_hour_resets), this.hp("7D", b.seven_day, b.seven_day_resets)] : null,
          h("div", { class: "pstate", text: this.agentState(a) }))));
    }));
  },
  openAgent(id) {
    const c = [...Scene.critters.values()].find(c => c.agent?.id === id && !c.gone);
    if (c) this.openCritter(c);
  },
  openCritter(c) {
    if (c.kind === "egg") return this.openHatch(c.agent.id);
    for (const d of $$("dialog[open]")) d.close();
    this.summaryKey = c.key; Scene.selected = c;
    this.renderSummary(c);
    $("#dlg-summary").showModal();
  },
  renderSummary(c) {
    const a = c.agent, w = c.worker, st = App.state, b = a.budget || {};
    const g = $("#sum-sprite").getContext("2d"); g.imageSmoothingEnabled = false; g.clearRect(0, 0, 32, 32);
    g.drawImage(critterSprite(c.hat, c.color, { legs: 0 }), 8, 8);
    $("#sum-name").textContent = (w ? `${a.name} · ${w.name}` : a.name).toUpperCase();
    $("#sum-sub").textContent = [a.plan && `Claude ${a.plan}`, a.email, a.primary ? "the farm's own login" : a.remote ? "on another box" : "hatched here"].filter(Boolean).join(" · ");
    const task = w?.task && [...st.tasks.running, ...st.tasks.waiting].find(t => t.id === w.task);
    const rows = [
      ["STATE", (w?.state || this.agentState(a)).toUpperCase()],
      ["QUEST", task ? h("a", { href: "#", onclick: (e) => { e.preventDefault(); this.openQuest(task); } }, task.title) : "—"],
      ["SEAT", a.seat || "—"],
      ["HEARTBEAT", w ? ago(w.at) : "—"],
      ["GOVERNOR", b.reason ? `${b.allowed}/${b.max} allowed · ${b.reason}` : "—"],
    ];
    fill($("#sum-body"), 
      h("dl", { class: "stat-row" }, rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", {}, v)])),
      h("h3", { text: "STAMINA (USAGE LEFT)" }), this.hp("5H", b.five_hour, b.five_hour_resets), this.hp("7D", b.seven_day, b.seven_day_resets));
    const acts = [];
    if (task) acts.push(h("button", { class: "btn", type: "button", onclick: () => this.openQuest(task) }, "VIEW QUEST"));
    if (!a.primary && !a.remote) {
      const rel = h("button", { class: "btn danger", type: "button" }, "RELEASE");
      rel.addEventListener("click", async () => {
        if (rel.dataset.sure !== "1") { rel.dataset.sure = "1"; rel.textContent = "SURE? LOGS IT OUT"; return; }
        rel.disabled = true; rel.textContent = "RELEASING…";
        try { await api(`/api/agents/${a.id}/remove`, {}); $("#dlg-summary").close(); this.say(`${a.name.toUpperCase()} was released. Bye bye!`); this.refresh(); }
        catch (x) { rel.textContent = x.message.slice(0, 40); }
      });
      acts.push(rel);
    }
    fill($("#sum-actions"), ...acts);
  },

  openQuests(tab) { for (const d of $$("dialog[open]")) d.close(); if (tab) this.tab = tab; this.renderQuests(); $("#dlg-quests").showModal(); },
  renderQuests() {
    const st = App.state; if (!st) return;
    const lists = { active: [...st.tasks.running, ...st.tasks.waiting], queued: st.tasks.queued, done: st.tasks.done, failed: st.tasks.failed };
    for (const b of $$("#quest-tabs [data-tab]")) {
      b.setAttribute("aria-selected", String(b.dataset.tab === this.tab));
      fill(b, b.dataset.tab.toUpperCase(), h("span", { class: "n", text: String(lists[b.dataset.tab].length) }));
    }
    const items = lists[this.tab];
    fill($("#quest-list"), ...(items.length ? items.map(t => h("li", {}, h("button", { type: "button", onclick: () => this.openQuest(t, "quests") },
      h("span", {}, t.kind === "plan" ? "📜 " : "", t.title),
      h("span", { class: "qmeta" }, h("span", { class: `badge ${t.status}`, text: t.status.toUpperCase() }), ` P${t.priority ?? 5} · ${ago(t.updated)}`),
      t.summary ? h("span", { class: "qsub", text: t.summary.replace(/\s+/g, " ") }) : t.worker ? h("span", { class: "qsub", text: "tended by " + t.worker.split("/").pop() + " of " + t.worker.split("@")[0] }) : null)))
      : [h("li", { class: "empty", text: { active: "Nobody is on a quest right now.", queued: "The quest board is empty. The planner fills it from the main quest.", done: "No harvest yet.", failed: "Nothing failed. Nice." }[this.tab] })]));
  },
  async openQuest(t, back) {
    for (const d of $$("dialog[open]")) d.close();
    this.questBack = back || null;
    $("#quest-kicker").textContent = `QUEST ${t.id} · ${t.status.toUpperCase()}`;
    $("#quest-title").textContent = t.title;
    fill($("#quest-body"), h("p", { class: "muted", text: "Loading…" }));
    fill($("#quest-actions"), null);
    $("#dlg-quest").showModal();
    let d;
    try { d = await api(`/api/tasks/${t.id}`); } catch (x) { fill($("#quest-body"), h("p", { class: "form-error", text: x.message })); return; }
    $("#quest-kicker").textContent = `QUEST ${d.id} · ${d.status.toUpperCase()}${d.kind && d.kind !== "task" ? " · " + d.kind.toUpperCase() : ""}`;
    const rows = [["PRIORITY", `P${d.priority ?? 5}`], ["ATTEMPTS", `${d.attempts || 0} / ${d.max_attempts || 3}`], ["POSTED", `${ago(d.created)} by ${d.created_by || "human"}`]];
    if (d.worker) rows.push(["TENDED BY", d.worker]);
    if (d.branch) rows.push(["BRANCH", d.branch]);
    if (d.parent) rows.push(["PARENT", d.parent]);
    if (d.children?.length) rows.push(["SUB-QUESTS", d.children.join(", ")]);
    const runs = (d.runs || []).slice(-8);
    fill($("#quest-body"), 
      h("dl", { class: "stat-row" }, rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])),
      h("h3", { text: "INSTRUCTIONS" }), h("pre", { text: d.prompt || "" }),
      d.result ? [h("h3", { text: "RESULT" }), h("pre", { text: d.result })] : null,
      runs.length ? [h("h3", { text: "RUNS" }), h("table", { class: "runs" }, h("tr", {}, ["WHEN", "OK", "TIME", "TURNS", "LIST $"].map(x => h("th", { text: x }))),
        runs.map(r => h("tr", {}, h("td", { text: ago(r.started) }), h("td", { text: r.ok ? "✓" : "✗" }), h("td", { text: r.duration_s != null ? `${Math.round(r.duration_s / 60)}m` : "-" }),
          h("td", { text: r.turns ?? "-" }), h("td", { text: r.cost_usd_list_price != null ? r.cost_usd_list_price.toFixed(2) : "-" }))))] : null);
    const acts = [];
    if (this.questBack) acts.push(h("button", { class: "btn", type: "button", onclick: () => this.openQuests() }, "◀ BACK"));
    if (["queued", "waiting", "running"].includes(d.status)) acts.push(h("button", { class: "btn danger", type: "button", onclick: async () => { await api(`/api/tasks/${d.id}/cancel`, {}); this.refresh(); this.openQuest(d, this.questBack); } }, "CANCEL QUEST"));
    if (["failed", "cancelled", "done"].includes(d.status)) acts.push(h("button", { class: "btn primary", type: "button", onclick: async () => { await api(`/api/tasks/${d.id}/retry`, {}); this.refresh(); this.openQuest(d, this.questBack); } }, "↻ RETRY"));
    fill($("#quest-actions"), ...acts);
  },
  openMission() {
    for (const d of $$("dialog[open]")) d.close();
    const ta = $("#mission-form [name=text]"); ta.value = App.state?.mission || "";
    $("#mission-form .form-error").textContent = "";
    $("#dlg-mission").showModal(); ta.focus();
  },
  openJournal() {
    for (const d of $$("dialog[open]")) d.close();
    const evs = [...(App.state?.events || [])].reverse();
    fill($("#journal-list"), ...evs.map(e => h("li", {}, h("time", { text: new Date(e.at * 1000).toTimeString().slice(0, 5) }), h("span", { class: "etype", text: e.type.toUpperCase() }), h("span", { text: e.msg }))));
    $("#dlg-journal").showModal();
  },

  // ----------------------------------------------------------------- hatching
  openHatch(agentId) {
    for (const d of $$("dialog[open]")) d.close();
    const st = App.state;
    const primary = st?.agents.find(a => a.primary);
    if (!agentId && primary && !primary.loggedIn) agentId = primary.id; // the farm's own login comes first
    this.hatchFor = agentId || null;
    $("#dlg-hatch").showModal();
    if (agentId) { this.renderHatch({ state: "starting" }); this.beginLogin(agentId); }
    else this.renderHatchName();
  },
  renderHatchName() {
    const form = h("form", {},
      h("canvas", { class: "egg-anim", width: 12, height: 12, id: "egg-cv" }),
      h("label", {}, "NAME", h("input", { name: "name", maxlength: 24, required: true, placeholder: "e.g. gil or night-shift", autocomplete: "off" })),
      h("p", { class: "muted", text: "A new Claude Code login with its own agents. Log in with another Claude account to add capacity: each account is paced on its own budget. The same account again just shares its budget." }),
      h("p", { class: "form-error", role: "alert" }),
      h("div", { class: "dlg-actions" }, h("button", { class: "btn primary", type: "submit" }, "▶ HATCH")));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = form.querySelector("button"); btn.disabled = true;
      try { const a = await api("/api/agents", { name: new FormData(form).get("name") }); this.hatchFor = a.id; this.renderHatch({ state: "starting" }); this.pollHatch(); this.refresh(); }
      catch (x) { form.querySelector(".form-error").textContent = x.message; btn.disabled = false; }
    });
    fill($("#hatch-body"), form);
    $("#egg-cv").getContext("2d").drawImage(EGG, 0, 0);
    form.querySelector("input").focus();
  },
  async beginLogin(id) {
    try { const s = await api(`/api/agents/${id}/login`, {}); this.renderHatch(s); this.pollHatch(); }
    catch (x) { this.renderHatch({ state: "failed", error: x.message }); }
  },
  pollHatch() {
    this.stopHatchPoll();
    this.hatchPoll = setInterval(async () => {
      if (!this.hatchFor) return;
      try { const s = await api(`/api/agents/${this.hatchFor}/login`); this.renderHatch(s); if (s.state === "done" || s.state === "failed") this.stopHatchPoll(); }
      catch { /* keep polling */ }
    }, 1000);
  },
  stopHatchPoll() { clearInterval(this.hatchPoll); this.hatchPoll = null; },
  renderHatch(s) {
    const body = $("#hatch-body"), key = s.state + "|" + (s.url || "") + "|" + (s.error || "");
    if (body.dataset.key === key) return;
    body.dataset.key = key;
    const agent = App.state?.agents.find(a => a.id === this.hatchFor);
    const name = (agent?.name || this.hatchFor || "claude").toUpperCase();
    const egg = h("canvas", { class: "egg-anim", width: 12, height: 12 });
    egg.getContext("2d").drawImage(EGG, 0, 0);
    if (s.state === "done") {
      const cv = h("canvas", { class: "egg-anim", width: 18, height: 18 });
      cv.getContext("2d").drawImage(critterSprite(agent?.hat || "straw", colorFor(this.hatchFor || ""), { legs: 0, arms: 1 }), 1, 0);
      fill(body, cv, h("p", { class: "center", text: `${name} hatched! It's logged in and joins the farm in a few seconds.` }),
        h("div", { class: "dlg-actions" }, h("button", { class: "btn primary", type: "button", onclick: () => $("#dlg-hatch").close() }, "▶ YAY")));
      this.say(`${name} hatched!`); this.refresh();
      return;
    }
    if (s.state === "failed") {
      fill(body, egg, h("p", { class: "form-error", text: s.error || "The login didn't finish." }),
        h("div", { class: "dlg-actions" }, h("button", { class: "btn primary", type: "button", onclick: () => { body.dataset.key = ""; this.renderHatch({ state: "starting" }); this.beginLogin(this.hatchFor); } }, "↻ TRY AGAIN")));
      return;
    }
    const codeForm = h("form", {},
      h("label", {}, "LOGIN CODE", h("input", { name: "code", autocomplete: "off", spellcheck: "false", required: true, placeholder: "paste the code from the Claude page", disabled: s.state !== "waiting_code" })),
      h("p", { class: "form-error", role: "alert" }),
      h("div", { class: "dlg-actions" }, h("button", { class: "btn primary", type: "submit", disabled: s.state !== "waiting_code" }, s.state === "checking" ? "HATCHING…" : "▶ HATCH")));
    codeForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      try { const r = await api(`/api/agents/${this.hatchFor}/code`, { code: new FormData(codeForm).get("code") }); this.renderHatch(r); this.pollHatch(); }
      catch (x) { codeForm.querySelector(".form-error").textContent = x.message; }
    });
    const link = s.url ? h("a", { class: "btn primary login-link", href: s.url, target: "_blank", rel: "noopener noreferrer" }, "OPEN THE CLAUDE LOGIN ↗")
      : h("p", { class: "muted", text: "Warming the egg… (starting Claude Code's login)" });
    fill(body, egg,
      h("p", { class: "center", text: `Log ${name} in to a Claude account.` }),
      h("ol", { class: "hatch-steps" },
        h("li", { class: s.url ? "done" : "" }, "Open the Claude login page and approve.", link),
        h("li", {}, "Copy the code it shows you and paste it here.", codeForm),
        h("li", {}, "The egg hatches, and the new Claude starts on the quests.")));
    if (s.state === "waiting_code") setTimeout(() => codeForm.querySelector("input")?.focus(), 30);
  },
};

UI.boot();
