"use strict";
// Optional browser regression: PLAYWRIGHT_MODULE=/path/to/playwright node tests/mobile-scroll.browser.js
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fs = require("node:fs"), assert = require("node:assert/strict");
const path = require("node:path"), root = path.resolve(__dirname, "..");
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    const errors = []; page.on("pageerror", e => errors.push(e.message));
    const snapshot = require("../data/latest.json").snapshot;
    await page.addInitScript(s => { window.__snapshot = s; }, snapshot);
    await page.route("**/*", async route => {
      const u = new URL(route.request().url());
      if (u.origin !== "http://lst.test") return route.abort();
      const file = path.join(root, u.pathname === "/" ? "index.html" : u.pathname.slice(1));
      if (!fs.existsSync(file)) return route.fulfill({ status: 404, body: "" });
      let body = fs.readFileSync(file);
      if (u.pathname === "/core.js") body = body.toString() + `\nwindow.PegCore.collect = async () => {
        const s = structuredClone(window.__snapshot), now = Math.floor(Date.now()/1000);
        s.ts = now; s.sectionAt = {}; s.rt.quoteAt = {};
        if (window.__partial) { delete s.rt.kyber; delete s.rt.ws.kyber; }
        return s;
      };`;
      await route.fulfill({ body, contentType: file.endsWith(".js") ? "application/javascript" : file.endsWith(".json") ? "application/json" : "text/html" });
    });
    await page.clock.install();
    await page.goto("http://lst.test/");
    await page.locator("#cb-redemption > summary").click();
    await page.locator("#cb-redemption .tw").first().evaluate(el => {
      window.__scroller = el; el.scrollLeft = 200;
      window.__left = el.scrollLeft;
    });
    const before = await page.evaluate(() => window.__left);
    assert.ok(before > 0, "390px viewport has horizontal scroll");
    await page.clock.runFor(11000);
    assert.deepEqual(await page.evaluate(() => ({ same: window.__scroller === document.querySelector("#cb-redemption .tw"), left: window.__scroller.scrollLeft })), { same: true, left: before });
    await page.locator("#refresh").click();
    assert.equal(await page.locator("#cb-redemption .tw").first().evaluate(el => el.scrollLeft), before);
    await page.evaluate(() => window.__partial = true);
    await page.locator("#refresh").click();
    assert.ok((await page.locator("#cb-redemption").innerText()).includes("仅单池"));
    assert.ok(!(await page.locator("#cb-redemption").innerText()).includes("7975"));
    assert.equal(await page.locator("#cb-redemption .tw").first().evaluate(el => el === window.__scroller), true);
    await page.evaluate(() => window.__partial = false);
    await page.locator("#refresh").click();
    await page.locator("#cb-redemption").scrollIntoViewIfNeeded();
    await page.screenshot({ path: "/tmp/lst-mobile-verified.png" });
    assert.deepEqual(errors, []);
    console.log("Mobile browser passed: retained DOM identity and horizontal offset across 10s expiry, refresh and missing/recovered aggregator; no fabricated single-pool return.");
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
