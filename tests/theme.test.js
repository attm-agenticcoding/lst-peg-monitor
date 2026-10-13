/* Dependency-free dark-theme and WCAG contrast regression checks. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const html = fs.readFileSync(require.resolve("../index.html"), "utf8");
const style = html.match(/<style>([\s\S]*?)<\/style>/)[1];
const root = style.match(/:root\{([\s\S]*?)\}/)[1];
const tokens = Object.fromEntries([...root.matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})/g)].map((m) => [m[1], m[2]]));
assert.match(html, /<meta name="color-scheme" content="dark">/);
assert.match(html, /<meta name="theme-color" content="#0b1018">/);
assert.match(root, /color-scheme:\s*dark/);
assert.ok(!/prefers-color-scheme|color-scheme:\s*light/.test(style), "dark default must not depend on the OS preference");
const luminance = (hex) => {
  const rgb = hex.slice(1).match(/../g).map((c) => parseInt(c, 16) / 255)
    .map((c) => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
};
const contrast = (a, b) => {
  const pair = [luminance(tokens[a]), luminance(tokens[b])].sort((a, b) => b - a);
  return (pair[0] + 0.05) / (pair[1] + 0.05);
};
let checked = 0;
const check = (a, b, minimum) => {
  const ratio = contrast(a, b);
  assert.ok(ratio >= minimum, `${a} on ${b}: ${ratio.toFixed(2)} < ${minimum}`);
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
assert.match(style, /\.sp-zero\{stroke:var\(--chart-grid\)/);
assert.match(style, /\.sp-t\{[^}]*fill:var\(--ink-3\)/);
assert.match(style, /:focus-visible\{outline:2px solid var\(--accent\)/);
assert.match(style, /input::placeholder\{color:var\(--ink-3\);opacity:1\}/);
console.log(`ok - default dark theme and ${checked} WCAG AA contrast pairs`);
