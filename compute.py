# -*- coding: utf-8 -*-
"""
計算引擎（active-etf-radar）
輸入：docs/data/latest.json（持股）、docs/data/market.json（市值權重/收盤/加權指數）
輸出：
  docs/data/analysis.json    主動權重、兩兩重疊矩陣（原始/主動）、共識廣度排行、分層清單
  docs/data/portfolio.json   影子投組狀態（現金、持倉、上次檢查月份）
  docs/data/nav_history.json 影子淨值與加權指數每日序列
  docs/data/trade_log.json   換股日誌

規則（2026-07-08 凍結規格）：
  起始資金 1,000,000 NTD；買費 0.0855%、賣費 0.0855%＋稅 0.3%
  廣度排行前5買進；跌出前8才賣出；每月第一個交易日檢查
  廣度排序鍵：被幾檔ETF持有（多者前）→ 主動權重加總（大者前）
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "docs" / "data"
TPE = timezone(timedelta(hours=8))
MIN_FRESH_FUNDS = 6   # 至少幾檔資料新鮮才允許交易動作


def jload(name, default):
    p = DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def jsave(name, obj):
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1),
                             encoding="utf-8")


def is_stock_code(code):
    code = str(code).strip()
    return len(code) == 4 and code.isdigit()


def build_analysis(latest, market, cfg):
    weights_mkt = market.get("weights", {})
    fund_holdings = {}   # etf -> {code: {"name","w","aw"}}
    fund_meta = {}
    for f in latest.get("funds", []):
        if f.get("status") not in ("ok", "stale"):
            continue
        etf = f["etf"]
        fund_meta[etf] = {"name": f.get("name"), "issuer": f.get("issuer"),
                          "status": f.get("status"), "data_date": f.get("data_date")}
        hmap = {}
        for h in f.get("holdings", []):
            code = str(h.get("code", "")).strip()
            if h.get("cash_like") or not is_stock_code(code):
                continue
            w = float(h.get("weight") or 0)
            if w <= 0:
                continue
            aw = w - float(weights_mkt.get(code, 0.0))
            hmap[code] = {"name": h.get("name", ""), "w": w, "aw": round(aw, 4)}
        if hmap:
            fund_holdings[etf] = hmap

    etfs = sorted(fund_holdings.keys())
    fresh = [e for e in etfs if fund_meta[e]["status"] == "ok"]

    # --- 共識廣度排行 ---
    stocks = {}
    for etf, hmap in fund_holdings.items():
        for code, h in hmap.items():
            s = stocks.setdefault(code, {"code": code, "name": h["name"],
                                         "n_etfs": 0, "sum_active": 0.0,
                                         "sum_weight": 0.0, "funds": []})
            if h["name"] and not s["name"]:
                s["name"] = h["name"]
            s["n_etfs"] += 1
            s["sum_active"] += max(h["aw"], 0.0)
            s["sum_weight"] += h["w"]
            s["funds"].append({"etf": etf, "w": h["w"], "aw": h["aw"]})
    breadth = sorted(stocks.values(),
                     key=lambda s: (-s["n_etfs"], -s["sum_active"]))
    for i, s in enumerate(breadth, 1):
        s["rank"] = i
        s["sum_active"] = round(s["sum_active"], 4)
        s["sum_weight"] = round(s["sum_weight"], 4)
        s["mkt_weight"] = round(float(market.get("weights", {}).get(s["code"], 0.0)), 4)

    # --- 分層清單：被 ≥k 檔共同持有 ---
    max_k = max((s["n_etfs"] for s in breadth), default=0)
    tiers = []
    for k in range(max_k, 1, -1):
        rows = [{"code": s["code"], "name": s["name"], "n_etfs": s["n_etfs"],
                 "sum_active": s["sum_active"]}
                for s in breadth if s["n_etfs"] >= k]
        tiers.append({"k": k, "count": len(rows), "stocks": rows[:80]})

    # --- 兩兩重疊矩陣（原始權重版 / 主動權重版）---
    n = len(etfs)
    raw = [[0.0] * n for _ in range(n)]
    act = [[0.0] * n for _ in range(n)]
    for i in range(n):
        hi = fund_holdings[etfs[i]]
        for j in range(i, n):
            hj = fund_holdings[etfs[j]]
            common = set(hi) & set(hj)
            r = sum(min(hi[c]["w"], hj[c]["w"]) for c in common)
            a = sum(min(max(hi[c]["aw"], 0), max(hj[c]["aw"], 0)) for c in common)
            raw[i][j] = raw[j][i] = round(r, 2)
            act[i][j] = act[j][i] = round(a, 2)

    return {
        "as_of": latest.get("generated_at"),
        "trade_date": market.get("trade_date"),
        "coverage": {"total": len(cfg["funds"]), "parsed": len(etfs),
                     "fresh": len(fresh), "etfs": fund_meta},
        "breadth": breadth[:200],
        "tiers": tiers,
        "overlap": {"etfs": etfs, "raw": raw, "active": act},
    }, fund_holdings, fresh


def run_portfolio(analysis, market, cfg, fresh_count):
    st = cfg["settings"]
    cap0 = float(st.get("shadow_initial_capital", 1000000))
    fee_b = float(st.get("buy_fee_pct", 0.0855)) / 100
    fee_s = float(st.get("sell_fee_pct", 0.0855)) / 100
    tax_s = float(st.get("sell_tax_pct", 0.3)) / 100
    top_n = int(st.get("top_n", 5))
    exit_rank = int(st.get("exit_rank", 8))

    pf = jload("portfolio.json", {"inited": False, "cash": cap0, "positions": {},
                                  "last_check_month": None, "inception_date": None})
    nav_hist = jload("nav_history.json", [])
    trades = jload("trade_log.json", [])
    closes = market.get("closes", {})
    trade_date = market.get("trade_date")
    today = datetime.now(TPE).date().isoformat()
    is_trading_today = (trade_date == today)
    month = today[:7]

    ranks = {s["code"]: s["rank"] for s in analysis["breadth"]}
    names = {s["code"]: s["name"] for s in analysis["breadth"]}
    top_list = [s for s in analysis["breadth"][:top_n]]

    def price(code):
        return closes.get(code)

    def record(action, code, shares, px, fee, tax, reason):
        nm = names.get(code) or pf["positions"].get(code, {}).get("name", "")
        trades.append({"date": trade_date, "action": action, "code": code,
                       "name": nm, "shares": shares,
                       "price": px, "fee": round(fee), "tax": round(tax),
                       "reason": reason})

    do_check = False
    reason = None
    if is_trading_today and fresh_count >= MIN_FRESH_FUNDS:
        if not pf["inited"]:
            do_check, reason = True, "建倉"
        elif pf.get("last_check_month") != month:
            do_check, reason = True, "每月第一個交易日檢查"

    if do_check:
        # 賣出：不在榜上或排名跌出緩衝帶
        for code in list(pf["positions"].keys()):
            r = ranks.get(code)
            if r is None or r > exit_rank:
                px = price(code)
                if px is None:
                    continue   # 無報價暫不處理，下次再試
                sh = pf["positions"][code]["shares"]
                gross = sh * px
                fee = gross * fee_s
                tax = gross * tax_s
                pf["cash"] += gross - fee - tax
                record("SELL", code, sh, px, fee, tax,
                       f"{reason}：排名 {r if r else '落榜'} 跌出前{exit_rank}")
                del pf["positions"][code]
        # 買進：前N名中尚未持有者，平均分配現金
        buys = [s["code"] for s in top_list
                if s["code"] not in pf["positions"] and price(s["code"])]
        room = max(top_n - len(pf["positions"]), 0)
        buys = buys[:room]
        if buys:
            budget = pf["cash"] / len(buys)
            for code in buys:
                px = price(code)
                sh = int(budget / (px * (1 + fee_b)))
                if sh <= 0:
                    continue
                cost = sh * px
                fee = cost * fee_b
                pf["cash"] -= cost + fee
                pf["positions"][code] = {"shares": sh, "name": names.get(code, ""),
                                         "avg_cost": px}
                record("BUY", code, sh, px, fee, 0,
                       f"{reason}：廣度排名 {ranks.get(code)}")
        pf["last_check_month"] = month
        if not pf["inited"] and pf["positions"]:
            pf["inited"] = True
            pf["inception_date"] = trade_date

    # 每個交易日記一次淨值
    if pf["inited"] and trade_date and not any(x["date"] == trade_date for x in nav_hist):
        nav = pf["cash"]
        for code, pos in pf["positions"].items():
            px = price(code) or pos.get("avg_cost", 0)
            nav += pos["shares"] * px
        nav_hist.append({"date": trade_date, "nav": round(nav),
                         "taiex": market.get("taiex")})

    pf["cash"] = round(pf["cash"], 2)
    pf["as_of"] = trade_date
    pf["target_top"] = [{"code": s["code"], "name": s["name"], "rank": s["rank"],
                         "n_etfs": s["n_etfs"], "sum_active": s["sum_active"]}
                        for s in top_list]
    jsave("portfolio.json", pf)
    jsave("nav_history.json", nav_hist)
    jsave("trade_log.json", trades)
    return pf


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    latest = jload("latest.json", None)
    market = jload("market.json", None)
    if not latest or not market:
        print("[compute] 缺 latest.json 或 market.json，先跑 scraper 與 market_weights")
        sys.exit(1)

    analysis, fund_holdings, fresh = build_analysis(latest, market, cfg)
    jsave("analysis.json", analysis)
    print(f"[compute] 分析完成：解析 {analysis['coverage']['parsed']} 檔ETF、"
          f"{len(analysis['breadth'])} 檔個股入榜、新鮮 {len(fresh)} 檔")

    pf = run_portfolio(analysis, market, cfg, len(fresh))
    pos = ", ".join(f"{c}×{p['shares']}" for c, p in pf["positions"].items()) or "（空手）"
    print(f"[compute] 影子投組：現金 {pf['cash']:.0f}，持倉 {pos}")


if __name__ == "__main__":
    main()
