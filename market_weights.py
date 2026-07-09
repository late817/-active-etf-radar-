# -*- coding: utf-8 -*-
"""
大盤權重與市場資料（TWSE OpenAPI，免費官方源，無需金鑰）
輸出：docs/data/market.json
  {
    "trade_date": "2026-07-08",       # 最近交易日
    "taiex": 35123.45,                # 加權指數收盤
    "closes": {"2330": 1085.0, ...},  # 個股收盤價
    "weights": {"2330": 34.12, ...}   # 個股市值權重（%）
  }
資料端點：
  STOCK_DAY_ALL  上市個股日成交（含收盤價）
  t187ap03_L     上市公司基本資料（含已發行普通股數）
  FMTQIK         市場成交統計（含日期與發行量加權股價指數收盤）
"""
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TPE = timezone(timedelta(hours=8))
BASE = "https://openapi.twse.com.tw/v1"


def log(msg):
    print(f"[market] {msg}", flush=True)


def fetch_json(path):
    url = f"{BASE}/{path}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def roc_to_iso(s):
    """民國日期 '1150708' 或 '115/07/08' 轉 ISO。西元格式也容忍。"""
    s = str(s).replace("/", "").replace("-", "").strip()
    if len(s) == 7:      # 民國 YYYMMDD
        y = int(s[:3]) + 1911
        return f"{y}-{s[3:5]}-{s[5:7]}"
    if len(s) == 8:      # 西元 YYYYMMDD
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def to_float(v):
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def main():
    # 1) 最近交易日與加權指數收盤（FMTQIK：當月每日市場成交資訊）
    fmtqik = fetch_json("exchangeReport/FMTQIK")
    if not fmtqik:
        raise RuntimeError("FMTQIK 無資料")
    last = fmtqik[-1]
    trade_date = roc_to_iso(last.get("Date") or last.get("日期"))
    taiex = to_float(last.get("TAIEX") or last.get("發行量加權股價指數"))
    log(f"最近交易日 {trade_date}，加權指數 {taiex}")

    # 2) 個股收盤價
    day_all = fetch_json("exchangeReport/STOCK_DAY_ALL")
    closes = {}
    for row in day_all:
        code = str(row.get("Code") or row.get("證券代號") or "").strip()
        close = to_float(row.get("ClosingPrice") or row.get("收盤價"))
        if code and close and close > 0:
            closes[code] = close
    log(f"收盤價 {len(closes)} 檔")

    # 3) 已發行普通股數 → 市值 → 權重（僅上市普通股，4碼數字代號）
    basics = fetch_json("opendata/t187ap03_L")
    shares_map = {}
    for row in basics:
        code = str(row.get("公司代號") or row.get("Code") or "").strip()
        sh = to_float(row.get("已發行普通股數或TDR原發行股數")
                      or row.get("已發行普通股數或TDR原股發行股數"))
        if code and sh and sh > 0:
            shares_map[code] = sh
    caps = {}
    for code, close in closes.items():
        if len(code) == 4 and code.isdigit() and code in shares_map:
            caps[code] = close * shares_map[code]
    total = sum(caps.values())
    if total <= 0:
        raise RuntimeError("市值加總為零，權重計算失敗")
    weights = {c: round(v / total * 100, 6) for c, v in caps.items()}
    log(f"市值權重 {len(weights)} 檔，總市值 {total/1e12:.2f} 兆")

    out = {
        "generated_at": datetime.now(TPE).strftime("%Y-%m-%d %H:%M"),
        "trade_date": trade_date,
        "taiex": taiex,
        "closes": closes,
        "weights": weights,
    }
    data_dir = ROOT / "docs" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "market.json").write_text(
        json.dumps(out, ensure_ascii=False), encoding="utf-8")
    log("market.json 寫入完成")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[market] 失敗：{e}", file=sys.stderr)
        sys.exit(1)
