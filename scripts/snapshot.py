#!/usr/bin/env python3
"""
LST Peg Monitor — 快照脚本
每次运行取一份多源报价，算出共识脱锚，追加到 data/history.json，
并把最近 N 天切出来写成 data/recent.json 供前端画历史曲线。

只用标准库；在 GitHub Actions runner 上跑（那里有完整外网）。
    python3 scripts/snapshot.py            # 取数并写入
    python3 scripts/snapshot.py --dry-run  # 只打印，不写文件
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
A, S, U = CFG["addresses"], CFG["selectors"], CFG["uniswap"]
SIZES = CFG["sizes"]
TH = CFG["thresholds"]
E18 = 10 ** 18
RECENT_DAYS = 14
MAX_RECORDS = 40000
UA = {"User-Agent": "lst-peg-monitor/1.0 (+github actions)", "Content-Type": "application/json"}


# ----------------------------- HTTP -----------------------------
def _req(url: str, data: bytes | None = None, timeout: int = 20):
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_json(url: str, timeout: int = 20):
    return _req(url, None, timeout)


# ----------------------------- ABI -----------------------------
def pad(h: str) -> str:
    return h.replace("0x", "").lower().rjust(64, "0")


def eU(n: int) -> str:
    return pad(format(int(n), "x"))


def eA(a: str) -> str:
    return pad(a)


def wordn(h: str, i: int) -> str:
    return "0x" + h.replace("0x", "")[i * 64:i * 64 + 64]


def to_int(h: str) -> int:
    v = int(h, 16)
    return v - (1 << 256) if v >> 255 else v


# ----------------------------- RPC -----------------------------
class Rpc:
    def __init__(self, urls):
        self.urls = list(urls)
        self.url = None

    def _pick(self):
        if self.url:
            return self.url
        last = None
        for u in self.urls:
            try:
                r = _req(u, json.dumps({"jsonrpc": "2.0", "id": 1,
                                        "method": "eth_blockNumber", "params": []}).encode(), 12)
                if r.get("result"):
                    self.url = u
                    return u
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(f"没有可用的 RPC 节点：{last}")

    def calls(self, items):
        """items: [(key, to, data)] -> {key: hex|None}，小批发送。"""
        url = self._pick()
        out = {}
        n = CFG.get("rpcBatchSize", 4)
        for i in range(0, len(items), n):
            chunk = items[i:i + n]
            payload = [{"jsonrpc": "2.0", "id": j + 1, "method": "eth_call",
                        "params": [{"to": to, "data": data}, "latest"]}
                       for j, (_k, to, data) in enumerate(chunk)]
            try:
                res = _req(url, json.dumps(payload).encode(), 25)
                if isinstance(res, list):
                    for x in res:
                        k = chunk[x["id"] - 1][0]
                        out[k] = x.get("result")
                    continue
            except Exception:  # noqa: BLE001
                pass
            for k, to, data in chunk:  # 退化为单发
                try:
                    r = _req(url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                              "params": [{"to": to, "data": data}, "latest"]}).encode(), 20)
                    out[k] = r.get("result")
                except Exception:  # noqa: BLE001
                    out[k] = None
        return out


# ----------------------------- 计算 -----------------------------
def peg_bps(price, nav):
    if price is None or not nav:
        return None
    return (price / nav - 1) * 1e4


def aggregate(sources, nav):
    for s in sources:
        s["bps"] = peg_bps(s.get("price"), nav)
    usable = [s for s in sources if s["bps"] is not None and not s.get("stale") and not s.get("failed")]
    vals = [s["bps"] for s in usable]
    if not vals:
        return {"consensus": None, "confirms": 0, "dispersion": None, "medPrice": None}
    med0 = statistics.median(vals)
    if len(usable) >= 3:
        mad = statistics.median([abs(v - med0) for v in vals])
        cut = max(3 * 1.4826 * mad, TH["outlierAbsFloor"])
        for s in usable:
            s["outlier"] = abs(s["bps"] - med0) > cut
    kept = [s for s in usable if not s.get("outlier")]
    pool = kept if len(kept) >= 2 else usable
    b = [s["bps"] for s in pool]
    return {
        "consensus": round(statistics.median(b), 3),
        "confirms": len(pool),
        "dispersion": round(max(b) - min(b), 3) if len(b) >= 2 else None,
        "medPrice": statistics.median([s["price"] for s in pool]),
    }


def classify(agg, best_by_size):
    if agg["consensus"] is None or agg["confirms"] < TH["minConfirms"]:
        return "dead"
    a = abs(agg["consensus"])
    thin = best_by_size.get(TH["thinSize"]) is not None and best_by_size[TH["thinSize"]] < -TH["thinBps"]
    if a > TH["alert"]:
        return "crisis"
    if a > TH["watch"] or thin:
        return "alert"
    if a > TH["ok"] or (agg["dispersion"] is not None and agg["dispersion"] > TH["disp"]):
        return "watch"
    return "ok"


# ----------------------------- 取数 -----------------------------
def collect():
    now = int(time.time())
    rpc = Rpc(CFG["rpcs"])

    r1 = rpc.calls([
        ("nav_wst", A["wstETH"], S["stEthPerToken"]),
        ("nav_cb", A["cbETH"], S["exchangeRate"]),
        ("coinsA", A["curveStEth"], S["coins"] + eU(0)),
        ("coinsB", A["curveStEthNg"], S["coins"] + eU(0)),
    ])
    nav_wst = int(r1["nav_wst"], 16) / 1e18 if r1.get("nav_wst") else None
    nav_cb = int(r1["nav_cb"], 16) / 1e18 if r1.get("nav_cb") else None

    def order(hexv):
        if not hexv:
            return (0, 1, False)
        addr = "0x" + hexv.replace("0x", "")[24:]
        is_eth = addr.lower() == A["ethPlaceholder"].lower()
        return (0, 1, True) if is_eth else (1, 0, True)

    ethA, stA, okA = order(r1.get("coinsA"))
    ethB, stB, okB = order(r1.get("coinsB"))

    items = []
    for s in SIZES:
        dx = eU(s * E18)
        items += [
            (f"cA{s}", A["curveStEth"], S["get_dy"] + eU(stA) + eU(ethA) + dx),
            (f"cB{s}", A["curveStEthNg"], S["get_dy"] + eU(stB) + eU(ethB) + dx),
            (f"uW{s}", A["uniQuoterV2"], S["quoteExactInputSingle"] + eA(A["wstETH"]) + eA(A["WETH"])
             + eU(s * E18) + eU(U["wstETH"]["fee"]) + eU(0)),
            (f"uC{s}", A["uniQuoterV2"], S["quoteExactInputSingle"] + eA(A["cbETH"]) + eA(A["WETH"])
             + eU(s * E18) + eU(U["cbETH"]["fee"]) + eU(0)),
        ]
    items += [("clS", A["chainlinkStEthEth"], S["latestRoundData"]),
              ("clC", A["chainlinkCbEthEth"], S["latestRoundData"])]
    r2 = rpc.calls(items)

    def unit(prefix, size):
        h = r2.get(f"{prefix}{size}")
        if not h or len(h) < 66:
            return None
        return int(wordn(h, 0), 16) / (size * E18)

    def feed(key):
        h = r2.get(key)
        if not h or len(h.replace("0x", "")) < 320:
            return None
        ans, upd = to_int(wordn(h, 1)), int(wordn(h, 3), 16)
        if ans <= 0:
            return None
        return {"price": ans / 1e18, "age": now - upd,
                "stale": (now - upd) > TH["feedHeartbeatSeconds"]}

    # --- HTTP 源 ---
    def safe(fn):
        try:
            return fn(), None
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    def llama():
        ids = ",".join("ethereum:" + A[k] for k in ("stETH", "wstETH", "cbETH", "WETH"))
        d = get_json(CFG["http"]["llama"] + ids)
        coins = {k.lower(): v for k, v in d.get("coins", {}).items()}
        weth = coins.get(("ethereum:" + A["WETH"]).lower())
        if not weth or not weth.get("price"):
            raise RuntimeError("缺 WETH 报价")
        out = {}
        for sym in ("stETH", "wstETH", "cbETH"):
            c = coins.get(("ethereum:" + A[sym]).lower())
            if c and c.get("price"):
                out[sym] = c["price"] / weth["price"]
        return out

    def gecko():
        out = {}
        for sym in ("stETH", "wstETH", "cbETH"):
            try:
                d = get_json(f"{CFG['http']['gecko']}?contract_addresses={A[sym]}&vs_currencies=eth", 15)
                k = next((k for k in d if k.lower() == A[sym].lower()), None)
                if k and d[k].get("eth"):
                    out[sym] = d[k]["eth"]
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5)  # keyless 档限流很紧
        if not out:
            raise RuntimeError("全部失败或被限流")
        return out

    def okx():
        d = get_json(CFG["http"]["okxStEth"])
        t = (d.get("data") or [None])[0]
        if not t:
            raise RuntimeError("无数据")
        bid, ask = float(t["bidPx"]), float(t["askPx"])
        return (bid + ask) / 2 if bid > 0 and ask > 0 else float(t["last"])

    def coinbase():
        d = get_json(CFG["http"]["coinbaseCbEth"])
        bid, ask = float(d["bid"]), float(d["ask"])
        return (bid + ask) / 2 if bid > 0 and ask > 0 else float(d["price"])

    L, Le = safe(llama)
    G, Ge = safe(gecko)
    O, Oe = safe(okx)
    C, Ce = safe(coinbase)

    fS, fC = feed("clS"), feed("clC")
    cA1, cB1 = unit("cA", 1), unit("cB", 1)

    def src(v, p, **kw):
        d = {"venue": v, "price": p}
        d.update(kw)
        return d

    tokens = {}

    st = []
    if cA1: st.append(src("curve_steth", cA1))
    if cB1: st.append(src("curve_steth_ng", cB1))
    if fS: st.append(src("chainlink_steth_eth", fS["price"], stale=fS["stale"], age=fS["age"]))
    st.append(src("okx_steth_eth", O, failed=O is None, error=Oe))
    if L: st.append(src("defillama", L.get("stETH")))
    if G: st.append(src("coingecko", G.get("stETH")))
    tokens["stETH"] = {"nav": 1.0, "sources": st,
                       "depth": {"curve_steth": {s: unit("cA", s) for s in SIZES},
                                 "curve_steth_ng": {s: unit("cB", s) for s in SIZES}}}

    ws = []
    uW1 = unit("uW", 1)
    if uW1: ws.append(src("uniswap_v3_wsteth", uW1))
    if cA1 and nav_wst: ws.append(src("curve_derived", cA1 * nav_wst))
    if fS and nav_wst: ws.append(src("chainlink_derived", fS["price"] * nav_wst, stale=fS["stale"]))
    if O and nav_wst: ws.append(src("okx_derived", O * nav_wst))
    if L: ws.append(src("defillama", L.get("wstETH")))
    if G: ws.append(src("coingecko", G.get("wstETH")))
    tokens["wstETH"] = {"nav": nav_wst, "sources": ws,
                        "depth": {"uniswap_v3_wsteth": {s: unit("uW", s) for s in SIZES}}}

    cb = []
    uC1 = unit("uC", 1)
    if uC1: cb.append(src("uniswap_v3_cbeth", uC1))
    if fC: cb.append(src("chainlink_cbeth_eth", fC["price"], stale=fC["stale"], age=fC["age"]))
    cb.append(src("coinbase_cbeth_eth", C, failed=C is None, error=Ce))
    if L: cb.append(src("defillama", L.get("cbETH")))
    if G: cb.append(src("coingecko", G.get("cbETH")))
    tokens["cbETH"] = {"nav": nav_cb, "sources": cb,
                       "depth": {"uniswap_v3_cbeth": {s: unit("uC", s) for s in SIZES}}}

    rec = {"ts": now, "rpc": rpc.url,
           "curveOrderVerified": {"steth": okA, "steth_ng": okB},
           "errors": {k: v for k, v in
                      {"defillama": Le, "coingecko": Ge, "okx": Oe, "coinbase": Ce}.items() if v}}

    for sym, t in tokens.items():
        srcs = [s for s in t["sources"] if s.get("price") is not None or s.get("failed")]
        agg = aggregate(srcs, t["nav"])
        best = {}
        for size in SIZES:
            vals = [peg_bps(d.get(size), t["nav"]) for d in t["depth"].values()]
            vals = [v for v in vals if v is not None]
            best[size] = round(max(vals), 3) if vals else None
        clean = None
        for size in sorted(SIZES):
            if best.get(size) is not None and best[size] >= -TH["cleanExitBps"]:
                clean = size
        rec[sym] = {
            "nav": t["nav"],
            "consensus": agg["consensus"],
            "confirms": agg["confirms"],
            "dispersion": agg["dispersion"],
            "medPrice": agg["medPrice"],
            "status": classify(agg, best),
            "exitBps": best,
            "cleanExitSize": clean,
            "sources": {s["venue"]: (round(s["bps"], 3) if s.get("bps") is not None else None)
                        for s in srcs},
        }
    return rec


# ----------------------------- 写入 -----------------------------
def write(rec, dry=False):
    ddir = os.path.join(ROOT, "data")
    os.makedirs(ddir, exist_ok=True)
    hpath, rpath = os.path.join(ddir, "history.json"), os.path.join(ddir, "recent.json")

    hist = {"records": []}
    if os.path.exists(hpath):
        try:
            hist = json.load(open(hpath, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            hist = {"records": []}
    recs = hist.get("records", [])
    if recs and recs[-1].get("ts") == rec["ts"]:
        recs[-1] = rec
    else:
        recs.append(rec)
    recs = recs[-MAX_RECORDS:]
    hist = {"updated": rec["ts"], "records": recs}

    cutoff = rec["ts"] - RECENT_DAYS * 86400
    recent = {"updated": rec["ts"],
              "records": [r for r in recs if r.get("ts", 0) >= cutoff]}

    if dry:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        print(f"[dry-run] history 将有 {len(recs)} 条，recent {len(recent['records'])} 条")
        return
    json.dump(hist, open(hpath, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump(recent, open(rpath, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"写入 {len(recs)} 条历史；本轮："
          + " ".join(f"{s}={rec[s]['consensus']}bps/{rec[s]['status']}"
                     for s in ("stETH", "wstETH", "cbETH")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        rec = collect()
    except Exception as e:  # noqa: BLE001
        print(f"取数失败：{e}", file=sys.stderr)
        return 1
    write(rec, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
