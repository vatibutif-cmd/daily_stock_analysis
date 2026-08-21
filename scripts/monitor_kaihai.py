# -*- coding: utf-8 -*-
"""
开开实业(600272) 盘中关键位提醒脚本 —— 纯云端跑(GitHub Actions)，关机也能收。

用途: 在 9:00-10:00 窗口由 GitHub Actions 定时触发。拉取开开实业实时价,
     只在触及关键价位(止损/买点/目标/破位)或明显异动时, 通过 PushPlus 推微信。
     不触发则静默退出(exit 0), 不打扰。

依赖: 仅标准库 + requests (GitHub runner 自带, 无重依赖)。
行情源: 腾讯实时行情 qt.gtimg.cn (GBK 编码)。

关键价位(以 levels.py 2026-08 为准):
  止损 17.45 / 买点 18.30 / 目标 19.60 ; 持仓成本 20.415(仅供参考)
"""
import os
import sys
import io
import time
import urllib.request
import urllib.parse

# ---------- 配置(以 levels.py 实时价位为准) ----------
THSCODE = "600272"          # 开开实业
STOCK_NAME = "开开实业"
STOP_LOSS   = 17.45         # 止损 生死线
BUY_POINT   = 18.30         # 买点
TARGET      = 19.60         # 目标
COST        = 20.415        # 持仓成本(仅提示用, 不作为触发)
TRIGGER_TOL = 0.25          # 触发阈值: 现价进入该价位±0.25 即视为触发(近似 2% 关键区)

# 腾讯行情字段索引(逗号分隔, GBK)
# 3=现价 4=昨收 5=开盘 6=成交量(手) 32=涨跌幅% 33=最高 34=最低
IDX_PRICE, IDX_PREV, IDX_OPEN, IDX_PCT = 3, 4, 5, 32


def fetch_quote(code: str):
    """拉取腾讯实时行情, 返回字段列表(字符串数组)或 None。"""
    url = f"http://qt.gtimg.cn/q=sh{code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk", errors="ignore")
        # 形如 v_sh600272="1~开开实业~...~"; 
        seg = raw.split("=", 1)[1].strip().strip(";").strip('"')
        return seg.split("~")
    except Exception as e:
        print(f"[err] fetch_quote {code}: {e}", flush=True)
        return None


def pushplus_send(title: str, content: str) -> bool:
    """PushPlus 推微信。token 从环境变量 PUSHPLUS_TOKEN 读。"""
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        print("[err] PUSHPLUS_TOKEN 未配置", flush=True)
        return False
    data = {
        "token": token,
        "title": title,
        "content": content,
        "template": "html",
    }
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            out = resp.read().decode("utf-8", errors="ignore")
        ok = '"code":200' in out or '"code": 200' in out
        print(f"[pushplus] ok={ok} resp={out[:200]}", flush=True)
        return ok
    except Exception as e:
        print(f"[err] pushplus: {e}", flush=True)
        return False


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower().startswith("ascii"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    # ---------- 北京时间窗口守卫 (9:00-10:10) ----------
    # runner 需设 TZ=Asia/Shanghai; 守卫防 schedule 秒级漂移导致窗口外误发。
    now_t = time.localtime()
    hhmm = now_t.tm_hour * 100 + now_t.tm_min
    if not (900 <= hhmm <= 1010):
        print(f"[guard] 当前北京时间 {now_t.tm_hour:02d}:{now_t.tm_min:02d} 不在 9:00-10:10 窗口, 静默退出", flush=True)
        return 0

    q = fetch_quote(THSCODE)
    if not q or len(q) < 5 or q[3] in ("", "0"):
        print("[warn] 行情拉取失败或非交易时段, 静默退出", flush=True)
        return 0

    price   = float(q[IDX_PRICE])
    prev    = float(q[IDX_PREV]) if q[IDX_PREV] else 0.0
    pct     = float(q[IDX_PCT]) if q[IDX_PCT] else 0.0
    oprice  = float(q[IDX_OPEN]) if q[IDX_OPEN] else 0.0

    now = time.strftime("%H:%M:%S")
    print(f"[{now}] {STOCK_NAME} 现价 {price} 涨跌 {pct}% 开 {oprice} 昨收 {prev}", flush=True)

    # ---------- 触发判断 ----------
    triggers = []
    if STOP_LOSS and price <= STOP_LOSS + TRIGGER_TOL:
        triggers.append(f"⚠️ 触及/跌破止损 {STOP_LOSS} (现价 {price}) — 按纪律离场")
    if BUY_POINT and abs(price - BUY_POINT) <= TRIGGER_TOL:
        triggers.append(f"🟢 触及买点 {BUY_POINT} 附近 (现价 {price})")
    if TARGET and price >= TARGET - TRIGGER_TOL:
        triggers.append(f"🎯 触及目标 {TARGET} 附近 (现价 {price}) — 考虑锁利")
    if COST and price <= COST * 0.90:
        triggers.append(f"🔻 较成本 {COST} 浮亏超10% (现价 {price}) — 注意风险")
    if abs(pct) >= 7:
        triggers.append(f"🔥 大幅异动 {pct}% (现价 {price})")

    if not triggers:
        print("[no-trigger] 未触及关键位, 静默退出", flush=True)
        return 0

    content = (
        f"<b>{STOCK_NAME} ({THSCODE}) 关键位提醒</b><br>"
        f"时间: {now}<br>"
        f"现价: <b>{price}</b> ({pct:+.2f}%) | 开: {oprice} | 昨收: {prev}<br>"
        f"<br>{'<br>'.join(triggers)}"
    )
    title = f"{STOCK_NAME} {price} {'⚠️' if '止损' in triggers[0] else '提醒'}"
    pushplus_send(title, content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
