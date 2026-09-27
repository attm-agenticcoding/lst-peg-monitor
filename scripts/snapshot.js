#!/usr/bin/env node
/* 取一份快照 → 追加进 data/history.json（schema 2）→ 重算常态基线。
 * 用的是和网页同一份 core.js。
 *   node scripts/snapshot.js            取数并写入
 *   node scripts/snapshot.js --dry-run  只打印，不写文件 */
"use strict";
const fs = require("fs");
const path = require("path");
const Core = require("../core.js");

const ROOT = path.join(__dirname, "..");
const CFG = JSON.parse(fs.readFileSync(path.join(ROOT, "config.json"), "utf8"));
const DATA = path.join(ROOT, "data");
const HIST = path.join(DATA, "history.json");
const dry = process.argv.includes("--dry-run");

function load() {
  try { return JSON.parse(fs.readFileSync(HIST, "utf8")); } catch { return null; }
}

(async () => {
  const snap = await Core.collect(CFG, fetch);
  const rec = Core.toRecord(snap, Core.evaluate(snap, CFG));

  let h = load();
  if (h && h.schema !== 2) {
    // 旧版（三个币、多源共识中位数口径）整份归档，不和新口径混在一起算常态
    if (!dry) {
      fs.mkdirSync(path.join(DATA, "legacy"), { recursive: true });
      fs.renameSync(HIST, path.join(DATA, "legacy", "history-v1.json"));
    }
    h = null;
  }
  if (!dry) fs.rmSync(path.join(DATA, "recent.json"), { force: true }); // 旧版前端用的切片，已不用

  const records = Core.thin([...((h && h.records) || []), rec], rec.ts, CFG.history);
  const baseline = {};
  for (const s of Core.ASSETS) baseline[s] = Core.baseline(records, s, rec.ts, CFG.baseline);

  const line = Core.ASSETS.map((s) => `${s} ${Core.fmt(rec[s].peg)}bps/${rec[s].st} via ${rec[s].via}`).join(" | ");
  if (dry) {
    console.log(JSON.stringify(rec, null, 2));
    console.log(line, "\n常态", JSON.stringify(baseline));
    return;
  }
  fs.mkdirSync(DATA, { recursive: true });
  fs.writeFileSync(HIST, JSON.stringify({ schema: 2, updated: rec.ts, baseline, records }));
  console.log(`写入 ${records.length} 条 | ${line}${rec.err ? " | 失败：" + rec.err.join("; ") : ""}`);
  if (Core.ASSETS.every((s) => rec[s].st === "dead")) process.exitCode = 2; // 全灭时让 Actions 里看得见
})().catch((e) => { console.error("快照失败：", e); process.exit(1); });
