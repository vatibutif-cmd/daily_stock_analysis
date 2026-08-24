# -*- coding: utf-8 -*-
"""
监控池盘中关键位提醒(通用版) —— 纯云端跑(GitHub Actions), 电脑关机也能收。
动态读取同目录 levels.py(单一数据源), 覆盖全部持仓(HOLDINGS)+自选(WATCH):

触发规则(触关键位才推, 其余静默):
  持仓 HOLDINGS:
    - 卖出位(止损/清仓/减仓/高抛/减半/生命线/破位离场/锁利/目标): 现价 ≤ 价位 + 容差 → 推
    - 买入位(买/低吸/回踩/补/加仓): 现价 ≤ 价位 + 容差 → 推(低成本补仓机会)
    - 较成本浮亏超 10%: 推
  自选 WATCH:
    - 仅买入位(买/低吸/回踩/补/加仓): 现价 ≤ 价位 + 容差 → 推(买入建议, 不推卖出/止损)
    - 回避票(价位≥90 如 99.99)整个标的跳过
  通用:
    - |涨跌幅| ≥ 7% 异动 → 推

容差: 相对 2% 关键区(与本地 smart_monitor 口径一致): tol = max(0.12, 价位*0.02)
行情源: 腾讯 qt.gtimg.cn (GBK)。推送: PushPlus 微信。
"""
import os
import sys
import io
import time
import urllib.request
import urllib.parse

# 动态读取同目录 levels.py(单一数据源, 之后改池只需同步 levels.py)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from levels import HOLDINGS, WATCH, COST  # noqa: E402

# 触发容差: 相对 2% 关键区 (smart_monitor.py PROXIMITY_PCT=2.0 同口径)
def tol_of(level):
    return max(0.12, round(level * 0.02, 2))

# 腾讯行情字段索引(逗号分隔, GBK): 3=现价 4=昨收 5=开盘 32=涨跌幅%
IDX_PRICE, IDX_PREV, IDX_OPEN, IDX_PCT = 3, 4, 5, 32


def fetch_quotes(codes):
    """拉取腾讯实时行情, 返回 {code: {price, prev, open, pct}}。"""
    syms = [("sh" if c.startswith("6") else "sz") + c for c in codes if c]
    if not syms:
        return {}
    url = "https://qt.gtimg.cn/q=" + ",".join(syms)
    req = urllib.request.Request(url, headers={"Referer": "https://gu.qq.com", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"[err] fetch_quotes: {e}", flush=True)
        return {}
    try:
        text = raw.decode("gbk", errors="replace")
    except Exception:
        text = raw.decode("utf-8", errors="replace")
    out = {}
    for part in text.split(";"):
        if "v_" not in part:
            continue
        f = part.split("~")
        if len(f) < 35:
            continue
        try:
            price = float(f[IDX_PRICE])
        except (ValueError, TypeError):
            price = 0.0
        if price <= 0:
            continue
        code = f[2]
        out[code] = {
            "price": price,
            "prev": float(f[IDX_PREV] or 0 or 0.0),
            "open": float(f[IDX_OPEN] or 0 or 0.0),
            "pct": float(f[IDX_PCT] or 0 or 0.0),
        }
    return out


def pushplus_send(title: str, content: str) -> bool:
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        print("[err] PUSHPLUS_TOKEN 未配置", flush=True)
        return False
    data = {"token": token, "title": title, "content": content, "template": "html"}
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request("https://www.pushplus.plus/send", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            out = resp.read().decode("utf-8", errors="ignore")
        ok = '"code":200' in out or '"code": 200' in out
        print(f"[pushplus] ok={ok} resp={out[:200]}", flush=True)
        return ok
    except Exception as e:
        print(f"[err] pushplus: {e}", flush=True)
        return False


def is_sell_level(name):
    return any(k in name for k in ("止损", "清仓", "减仓", "减半", "高抛", "生命线", "破位", "离场", "锁利", "目标", "止盈"))


def is_buy_level(name):
    return any(k in name for k in ("买", "低吸", "回踩", "补", "加仓"))


def skip_stock(levels):
    """回避票(价位≥90 如 99.99 标记)整个跳过。"""
    return any(v >= 90 for v in levels.values() if isinstance(v, (int, float)))


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower().startswith("ascii"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    # 北京时间窗口守卫 (9:00-15:00 交易时段; 留 schedule 漂移余量)
    force_send = os.environ.get("FORCE_SEND", "").strip().lower() in ("1", "true", "yes")
    now_t = time.localtime()
    hhmm = now_t.tm_hour * 100 + now_t.tm_min
    if not force_send and not (855 <= hhmm <= 1505):
        print(f"[guard] 当前北京时间 {now_t.tm_hour:02d}:{now_t.tm_min:02d} 不在 9:00-15:00 窗口, 静默退出", flush=True)
        return 0

    # 组装监控池: 持仓 + 自选(跳过回避票)
    pool = {}
    for code, (name, tag, levels) in HOLDINGS.items():
        pool[code] = {"name": name, "tag": tag, "levels": levels, "cost": COST.get(code), "kind": "hold"}
    for code, (name, tag, levels) in WATCH.items():
        if skip_stock(levels):
            print(f"[skip] {name}({code}) 回避票, 不监控", flush=True)
            continue
        pool[code] = {"name": name, "tag": tag, "levels": levels, "cost": None, "kind": "watch"}

    if not pool:
        print("[warn] 监控池为空, 静默退出", flush=True)
        return 0

    quotes = fetch_quotes(list(pool.keys()))
    if not quotes:
        print("[warn] 行情拉取失败或非交易时段, 静默退出", flush=True)
        return 0

    now = time.strftime("%H:%M:%S")
    triggers = []      # 全局触发列表
    summary = []       # 每只票一行现价摘要

    for code, cfg in pool.items():
        if code not in quotes:
            continue
        q = quotes[code]
        price, prev, oprice, pct = q["price"], q["prev"], q["open"], q["pct"]
        name, tag, levels, cost = cfg["name"], cfg["tag"], cfg["levels"], cfg["cost"]
        summary.append(f"{tag}{name} 现价 <b>{price:.2f}</b> ({pct:+.1f}%)")

        for lname, lv in levels.items():
            if not isinstance(lv, (int, float)) or lv <= 0 or lv >= 90:
                continue
            tol = tol_of(lv)
            hit = None
            if cfg["kind"] == "hold":
                if is_sell_level(lname) and price <= lv + tol:
                    hit = f"⚠️ 触及/跌破 {lname} {lv:.2f} (现价 {price:.2f})"
                elif is_buy_level(lname) and price <= lv + tol:
                    hit = f"🟢 触及 {lname} {lv:.2f} 附近 (现价 {price:.2f})"
            else:  # watch: 只给买入建议
                if is_buy_level(lname) and price <= lv + tol:
                    hit = f"🟢 {name} 触及 {lname} {lv:.2f} (现价 {price:.2f}) — 买入建议"
            if hit:
                triggers.append(f"<b>{tag}{name}({code})</b><br>{hit}")
                break  # 每只票同一次只回一个触发, 避免刷屏

        # 持仓成本浮亏超 10%
        if cost and price <= cost * 0.90:
            pnl = (price / cost - 1) * 100
            triggers.append(f"<b>{tag}{name}({code})</b><br>🔻 较成本 {cost:.2f} 浮亏 {pnl:.1f}% (现价 {price:.2f}) — 注意风险")

        # 大幅异动
        if abs(pct) >= 7:
            triggers.append(f"<b>{tag}{name}({code})</b><br>🔥 大幅异动 {pct:+.1f}% (现价 {price:.2f})")

    print(f"[{now}] 池内 {len(summary)} 只: " + " | ".join(summary), flush=True)

    if not triggers and not force_send:
        print("[no-trigger] 未触及关键位, 静默退出", flush=True)
        return 0
    if force_send and not triggers:
        triggers = ["🧪 测试消息: 云端监控池提醒链路已打通。后续交易时段触关键位才会推, 平时静默。"]

    content = (
        f"<b>监控池关键位提醒</b><br>"
        f"时间: {now}<br>"
        f"{'<br>'.join(summary)}<br><br>"
        f"{'<br><br>'.join(triggers)}"
    )
    title = f"监控池 {len(summary)}只 {'⚠️' if any('止损' in t or '浮亏' in t for t in triggers) else '提醒'}"
    pushplus_send(title, content)
    return 0


if __name__ == "__main__":
    sys.exit(main())