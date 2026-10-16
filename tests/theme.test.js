/* Dependency-free theme preference and WCAG contrast regression checks. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const html = fs.readFileSync(require.resolve("../index.html"), "utf8");
const style = html.match(/<style>([\s\S]*?)<\/style>/)[1];
const root = style.match(/:root\{([\s\S]*?)\}/)[1];
const light = style.match(/:root\[data-theme="light"\]\{([\s\S]*?)\}/)[1];
const getTokens = (css) => Object.fromEntries([...css.matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})/g)].map((m) => [m[1], m[2]]));
const source = html.match(/<script id="theme-bootstrap">([\s\S]*?)<\/script>/)[1];
assert.ok(html.indexOf('id="theme-bootstrap"') < html.indexOf('<link rel="stylesheet"'), "theme resolves before render-blocking CSS");
assert.match(html, /<meta name="color-scheme" content="light dark">/);
assert.match(root, /color-scheme:\s*dark/);
assert.match(light, /color-scheme:\s*light/);
assert.deepEqual(getTokens(style.match(/:root:not\(\[data-theme\]\)\{([\s\S]*?)\}/)[1]), getTokens(light), "script-free system fallback uses the same light palette");
assert.match(html, /<label class="theme-control" for="theme-select">/);
assert.match(html, /<select id="theme-select" aria-label="Theme \/ 显示主题">/);
for (const option of ["System", "Light", "Dark"]) assert.ok(html.includes(`<option value="${option.toLowerCase()}">${option}</option>`));

function browser({ saved = null, dark = false, brokenStorage = false, legacyMedia = false } = {}) {
  const events = {}, media = { matches: dark }, storage = new Map(), metadata = {};
  let onMedia, onReady, onSelect;
  if (saved !== null) storage.set("dashboard-theme", saved);
  const select = { value: "system", addEventListener(type, listener) { assert.equal(type, "change"); onSelect = listener; } };
  media[legacyMedia ? "addListener" : "addEventListener"] = (...args) => { onMedia = args.at(-1); };
  const root = { dataset: {}, style: {} };
  const document = { documentElement: root,
    querySelector(selector) { return metadata[selector] ||= {}; },
    addEventListener(type, listener) { assert.equal(type, "DOMContentLoaded"); onReady = listener; },
    getElementById(id) { assert.equal(id, "theme-select"); return select; } };
  vm.runInNewContext(source, { document,
    window: { matchMedia: () => media, addEventListener(type, listener) { events[type] = listener; } },
    localStorage: {
      getItem(key) { if (brokenStorage) throw Error("denied"); return storage.get(key); },
      setItem(key, value) { if (brokenStorage) throw Error("denied"); storage.set(key, value); },
    },
  });
  const firstTheme = root.dataset.theme; // Before DOMContentLoaded / the select exists.
  onReady();
  return { root, select, storage, metadata, firstTheme,
    choose(value) { select.value = value; onSelect(); },
    system(dark) { media.matches = dark; onMedia(); },
    storageEvent(key, newValue) { events.storage({ key, newValue }); },
  };
}
for (const dark of [false, true]) {
  const b = browser({ dark });
  assert.equal(b.firstTheme, dark ? "dark" : "light", "first paint follows system with no saved preference");
  assert.equal(b.select.value, "system");
  assert.equal(b.storage.size, 0, "system fallback does not overwrite storage on page load");
  b.system(!dark);
  assert.equal(b.root.dataset.theme, dark ? "light" : "dark");
}
for (const saved of ["light", "dark"]) {
  const b = browser({ saved, dark: saved !== "dark" });
  assert.equal(b.firstTheme, saved, "saved choice wins before first paint even with the opposite OS theme");
  assert.equal(b.select.value, saved);
  b.system(saved === "dark");
  b.system(saved !== "dark");
  assert.equal(b.root.dataset.theme, saved, "OS changes must not override an explicit choice");
  assert.equal(b.root.style.colorScheme, saved);
  assert.equal(b.metadata['meta[name="theme-color"]'].content, saved === "dark" ? "#0b1018" : "#f5f7fb");
}
const choice = browser({ dark: true });
choice.choose("light");
assert.equal(choice.root.dataset.theme, "light");
assert.equal(choice.storage.get("dashboard-theme"), "light");
assert.equal(browser({ saved: choice.storage.get("dashboard-theme"), dark: true }).firstTheme, "light", "reload preserves choice");
choice.choose("system");
assert.equal(choice.storage.get("dashboard-theme"), "system");
assert.equal(choice.root.dataset.theme, "dark");
choice.system(false);
assert.equal(choice.root.dataset.theme, "light");
choice.storageEvent("lpm-view-v1", "dark");
assert.equal(choice.select.value, "system", "data-cache updates do not affect theme");
choice.storageEvent("dashboard-theme", "dark");
assert.equal(choice.root.dataset.theme, "dark");
assert.equal(choice.select.value, "dark", "another tab's theme choice updates the control");
choice.storageEvent(null, null);
assert.equal(choice.select.value, "system", "clearing storage returns to system");
assert.equal(browser({ saved: "invalid", dark: true }).firstTheme, "dark");
const denied = browser({ dark: true, brokenStorage: true, legacyMedia: true });
assert.equal(denied.firstTheme, "dark");
denied.choose("light");
assert.equal(denied.root.dataset.theme, "light", "unavailable storage does not break theme selection");
const luminance = (hex) => {
  const rgb = hex.slice(1).match(/../g).map((c) => parseInt(c, 16) / 255)
    .map((c) => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
};
const contrast = (tokens, a, b) => {
  const pair = [luminance(tokens[a]), luminance(tokens[b])].sort((a, b) => b - a);
  return (pair[0] + 0.05) / (pair[1] + 0.05);
};
let checked = 0;
for (const [theme, css] of [["dark", root], ["light", light]]) {
  const tokens = getTokens(css);
  const check = (a, b, minimum) => {
    const ratio = contrast(tokens, a, b);
    assert.ok(ratio >= minimum, `${theme}: ${a} on ${b}: ${ratio.toFixed(2)} < ${minimum}`);
    checked++;
  };
  for (const bg of ["ground", "panel", "panel-2"]) {
    for (const fg of ["ink", "ink-2", "ink-3", "accent", "cat-1", "cat-2", "cat-3", "ok", "watch", "alert", "crisis", "dead"]) check(fg, bg, 4.5);
  }
  for (const status of ["ok", "watch", "alert", "crisis", "dead"]) check(status, `${status}-bg`, 4.5);
  check("ink-2", "watch-bg", 4.5);
  check("chart-grid", "panel", 3);
  check("chart-grid", "ground", 3);
  check("ink", "panel", 4.5); // Inverted primary-venue badge.
  check("accent", "ground", 4.5); // Selected text uses the inverse of this pair.
}
assert.match(style, /\.sp-zero\{stroke:var\(--chart-grid\)/);
assert.match(style, /\.sp-t\{[^}]*fill:var\(--ink-3\)/);
assert.match(style, /:focus-visible\{outline:2px solid var\(--accent\)/);
assert.match(style, /input::placeholder\{color:var\(--ink-3\);opacity:1\}/);
console.log(`ok - theme preferences, first paint, storage recovery, and ${checked} WCAG AA contrast pairs across light/dark`);
