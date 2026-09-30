#!/usr/bin/env node
/* 取一份快照 → 判断预警 → 需要时推送到手机（ntfy）→ 追加进 data/history.json。
 * 用的是和网页同一份 core.js。由 scripts/loop.sh 每 2 分钟调用一次。
 *   node scripts/snapshot.js            取数、判断、推送、写入
 *   node scripts/snapshot.js --dry-run  只打印，不推送、不写文件
 * 推送：环境变量 NTFY_TOPIC（GitHub secret）设了就推，没设就只写文件，由每小时的巡检兜底。 */
"use strict";
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");
const Core = require("../core.js");

const ROOT = path.join(__dirname, "..");
const CFG = JSON.parse(fs.readFileSync(path.join(ROOT, "config.json"), "utf8"));
const DATA = path.join(ROOT, "data");
const HIST = path.join(DATA, "history.json");
const ARCH = path.join(DATA, "archive");
const URGENT = process.env.URGENT_FLAG || "/tmp/lpm-urgent"; // 有推送时留个标记，loop.sh 看到就立刻提交
const PAGE = "https://attm-agenticcoding.github.io/lst-peg-monitor/";
const dry = process.argv.includes("--dry-run");
const f = Core.fmt;

/* 逐条存档：history.json 只保留 1 天的逐条采样，复盘和回测要更长的原始数据。
 * 每条追加进 data/archive/<UTC 日期>.jsonl；过了当天就压成 .jsonl.gz（约 60 KB/天）。 */
function archive(rec) {
  fs.mkdirSync(ARCH, { recursive: true });
  const day = new Date(rec.ts * 1000).toISOString().slice(0, 10);
  fs.appendFileSync(path.join(ARCH, `${day}.jsonl`), JSON.stringify(rec) + "\n");
  for (const name of fs.readdirSync(ARCH)) {
    const m = name.match(/^(\d{4}-\d{2}-\d{2})\.jsonl$/);
    if (!m || m[1] >= day) continue;
    const src = path.join(ARCH, name);
    fs.writeFileSync(src + ".gz", zlib.gzipSync(fs.readFileSync(src)));
    fs.rmSync(src);
  }
}

function load() {
  try { return JSON.parse(fs.readFileSync(HIST, "utf8")); } catch { return null; }
}

async function push(msgs) {
  const topic = process.env.NTFY_TOPIC;
  if (!topic || dry) return false;
  let ok = true;
  for (const m of msgs) {
    try {
      const r = await fetch("https://ntfy.sh/", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ topic, title: m.title, message: m.body, priority: m.prio, tags: m.tags || [], click: PAGE }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
    } catch (e) { ok = false; console.error("ntfy 推送失败：", e.message); }
  }
  return ok;
}

/* 预警状态机：跌破立即报；连续 clearSamples 次不再满足才算恢复（防止在线附近来回抖） */
function step(prev, G, rec, res) {
  const msgs = [], next = {};
  for (const sym of Core.ASSETS) {
    const p = prev[sym] || {}, n = (next[sym] = { st: rec[sym].st });
    // 1) 绝对档位变化（25 / 75 / 200 bps）
    if (p.st && p.st !== rec[sym].st) {
      const up = Core.LEVEL[rec[sym].st] > Core.LEVEL[p.st];
      msgs.push({
        prio: rec[sym].st === "crisis" || rec[sym].st === "alert" ? 5 : up ? 4 : 3, tags: [up ? "warning" : "white_check_mark"],
        title: `${sym} 状态：${Core.TEXT[p.st]} → ${Core.TEXT[rec[sym].st]}`,
        body: `主报价 ${f(rec[sym].peg)} bps（${res[sym].primaryLabel || "—"}）。${res[sym].why}`,
      });
    }
    // 2) 偏离基线（卖 1 枚 / 卖大额）
    for (const m of ["peg", "big"]) {
      const q = p[m] || { on: false, clear: 0 }, g = G[sym][m], r = g.rep;
      const what = m === "peg" ? "卖 1 枚" : `卖 ${g.size} 枚`;
      const best = g.venues.slice().sort((u, v) => v.x - u.x)[0];
      const bestTxt = best ? `眼下${what}最好的出口：${best.label} ${f(best.x)} bps。` : "";
      if (!q.on && g.hit) {
        n[m] = { on: true, clear: 0, since: rec.ts };
        const hitV = g.venues.filter((v) => v.hit).map((v) => `${v.label} ${f(v.x)}（线 ${f(v.level)}，${v.win} 中位 ${f(v.center)}）`).join("；");
        msgs.push({ prio: 4, tags: ["rotating_light"], title: `${sym} 偏离预警 · ${what}`, body: `跌破预警线：${hitV}。${bestTxt}` });
      } else if (q.on && !g.hit) {
        const clear = (q.clear || 0) + 1;
        if (clear >= CFG.guard.clearSamples) {
          n[m] = { on: false, clear: 0 };
          msgs.push({ prio: 3, tags: ["white_check_mark"], title: `${sym} 偏离已恢复 · ${what}`, body: `${bestTxt}${r ? `预警线 ${f(r.level)}。` : ""}` });
        } else n[m] = { on: true, clear, since: q.since };
      } else n[m] = { on: q.on, clear: 0, since: q.since };
    }
  }
  return { next, msgs };
}

(async () => {
  const snap = await Core.collect(CFG, fetch);
  const res = Core.evaluate(snap, CFG);
  const rec = Core.toRecord(snap, res, CFG);
  rec.rv = CFG.guard.version; // 当时生效的预警规则版本，复盘时按版本分开算

  let h = load();
  if (h && h.schema !== 2) {
    // 旧版（三个币、多源共识中位数口径）整份归档，不和新口径混在一起算
    if (!dry) {
      fs.mkdirSync(path.join(DATA, "legacy"), { recursive: true });
      fs.renameSync(HIST, path.join(DATA, "legacy", "history-v1.json"));
    }
    h = null;
  }
  const prevRecords = (h && h.records) || [];
  const G = Core.guard(prevRecords, rec, CFG);

  // 状态机：上一轮的状态存在 history.json 顶层 alerts 里；第一次跑时用最后一条记录的档位做起点，避免开机就误报一次
  const prevState = (h && h.alerts) || Object.fromEntries(Core.ASSETS.map((s) => [s, { st: prevRecords.length ? prevRecords[prevRecords.length - 1][s].st : null }]));
  const { next, msgs } = step(prevState, G, rec, res);
  const active = Object.fromEntries(Core.ASSETS.map((s) => [s, ["peg", "big"].filter((m) => next[s][m] && next[s][m].on)]).filter(([, a]) => a.length));
  if (Object.keys(active).length) rec.g = active; // 巡检在没有 ntfy 时靠这个字段看预警的开关

  // 第一次接上 ntfy 时发一条确认，证明通道是通的
  const hasTopic = !!process.env.NTFY_TOPIC;
  if (hasTopic && !(h && h.push === "ntfy")) msgs.unshift({ prio: 3, tags: ["bell"], title: "LST 预警通道已接通", body: "之后偏离预警、档位变化、恢复都会从这里推送。" });
  const pushed = msgs.length ? await push(msgs) : false;

  const records = Core.thin([...prevRecords, rec], rec.ts, CFG.history.tiers);
  const baseline = {};
  for (const s of Core.ASSETS) baseline[s] = Core.baseline(records, s, rec.ts, CFG.baseline);
  const guardView = {};
  for (const s of Core.ASSETS) {
    guardView[s] = {};
    for (const m of ["peg", "big"]) {
      const g = G[s][m];
      guardView[s][m] = { size: g.size, hit: g.hit, on: !!(next[s][m] && next[s][m].on), hits: g.hits,
        rep: g.rep ? { id: g.rep.id, x: g.rep.x, level: g.rep.level, center: g.rep.center, scale: g.rep.scale, win: g.rep.win } : null };
    }
  }
  const out = { schema: 2, updated: rec.ts, push: hasTopic ? "ntfy" : (h && h.push) || null, alerts: next, guard: guardView, baseline, records };

  const line = Core.ASSETS.map((s) => {
    const r = G[s].peg.rep, b = G[s].big.rep;
    return `${s} ${f(rec[s].peg)}bps/${rec[s].st} via ${rec[s].via} 线 ${r ? f(r.level) : "—"} | 大额 ${b ? `${f(b.x)}/线 ${f(b.level)}` : "—"}`;
  }).join(" || ");
  if (dry) {
    console.log(JSON.stringify(rec, null, 2));
    console.log(line);
    for (const m of msgs) console.log("[推送]", m.title, "—", m.body);
    return;
  }
  fs.mkdirSync(DATA, { recursive: true });
  fs.rmSync(path.join(DATA, "recent.json"), { force: true }); // 旧版前端用的切片，已不用
  fs.writeFileSync(HIST, JSON.stringify(out));
  archive(rec);
  if (msgs.length) fs.writeFileSync(URGENT, String(rec.ts));
  console.log(`写入 ${records.length} 条 | ${line}${msgs.length ? ` | 推送 ${msgs.length} 条${pushed ? "" : "（未发出）"}` : ""}${rec.err ? " | 失败：" + rec.err.join("; ") : ""}`);
  if (Core.ASSETS.every((s) => rec[s].st === "dead")) process.exitCode = 2; // 全灭时让 Actions 里看得见
})().catch((e) => { console.error("快照失败：", e); process.exit(1); });
