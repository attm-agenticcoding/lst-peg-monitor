"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const Core = require("../core.js"), config = require("../config.json");
const script = fs.readFileSync(require.resolve("../scripts/snapshot.js"), "utf8");
const snap = { ts: 1792065600, sectionAt: { stETH: 1792065590 }, nav: { stETH: 1, cbETH: 1, wstETH: 1.2 }, px: { stETH: {}, cbETH: {} }, rt: {}, st: null, err: [],
  wstethConversion: { observedAt: 1792065580, fetchedAt: 1792065585, raw: "1200000000000000000" },
  cbethConversion: { observedAt: 1792065580, fetchedAt: 1792065585, raw: "1141640441769055706" } };
async function run(dry, failRename, failCollect = false) {
  const files = new Map([["latest.json", "previous cache"]]), conversion = [], writes = [];
  const fakeFs = {
    readFileSync(file) { return path.basename(file) === "config.json" ? JSON.stringify(config) : JSON.stringify({ schema: 2, records: [] }); },
    mkdirSync() {}, readdirSync() { return []; },
    writeFileSync(file, contents) { writes.push(file); files.set(path.basename(file), contents); },
    appendFileSync(file, contents) { writes.push(file); files.set(path.basename(file), contents); },
    rmSync(file) { files.delete(path.basename(file)); },
    renameSync(from, to) { if (failRename) throw Error("rename failed"); files.set(path.basename(to), files.get(path.basename(from))); files.delete(path.basename(from)); },
  };
  const ctx = { __dirname: "/repo/scripts", process: { argv: dry ? ["node", "snapshot.js", "--dry-run"] : [], env: {}, exit(code) { this.exitCode = code; } },
    fetch() { throw Error("no network expected"); }, console: { log() {}, error() {} },
    require(name) {
      if (name === "fs") return fakeFs;
      if (name === "../core.js") return { ...Core, collect: async () => { if (failCollect) throw Error("collection failed"); return snap; } };
      if (name === "./conversion-rate.js") return { buildConversionRate: () => ({}), writeConversionRate: (...args) => conversion.push(args),
        buildCbethConversionRate: () => ({}), writeCbethConversionRate: (...args) => conversion.push(args) };
      return require(name);
    },
  };
  await vm.runInNewContext(script, ctx);
  return { files, writes, conversion, exitCode: ctx.process.exitCode };
}
(async () => {
  const result = await run(false, false), cache = JSON.parse(result.files.get("latest.json"));
  assert.deepEqual(cache.snapshot, snap, "cache retains every original source time and exact conversion string");
  assert.equal(cache.config, JSON.stringify(config));
  assert.equal(cache.schema, 1);
  assert.ok(!result.files.has("latest.json.tmp"));
  assert.equal(result.exitCode, 2, "empty market fixture keeps the existing dead-source exit code");
  assert.equal(result.conversion.length, 2);
  assert.deepEqual(result.conversion.map((args) => path.basename(args[0])), ["wsteth-conversion-rate.json", "cbeth-conversion-rate.json"]);
  assert.ok(result.conversion.every((args) => args[1] === snap), "both exports use the same collection");
  const failed = await run(false, true);
  assert.equal(failed.files.get("latest.json"), "previous cache");
  assert.ok(!failed.files.has("latest.json.tmp"));
  assert.ok([...failed.files.keys()].some((name) => name.endsWith(".jsonl")), "optional cache failure does not prevent archive");
  assert.equal(failed.conversion.length, 2, "cache failure cannot invalidate successful public conversion exports");
  const collectionFailure = await run(false, false, true);
  assert.equal(collectionFailure.exitCode, 1);
  assert.equal(collectionFailure.conversion.length, 2);
  assert.ok(collectionFailure.conversion.every((args) => args[1] === null), "failed collection invalidates both public rates");
  const dry = await run(true, false);
  assert.equal(dry.writes.length, 0); assert.equal(dry.conversion.length, 0);
  const dryFailure = await run(true, false, true);
  assert.equal(dryFailure.writes.length, 0); assert.equal(dryFailure.conversion.length, 0);
  console.log("Latest cache publication passed: atomic update, original timestamps, failed replacement, independent archival/export and dry-run");
})().catch((e) => { console.error(e); process.exitCode = 1; });
