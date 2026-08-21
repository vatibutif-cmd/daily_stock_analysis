# -*- coding: utf-8 -*-
"""
竞价强弱核对(云端版) —— 关机也能收到盘前竞价判断。
9:25 集合竞价结束后运行。拉取监控标的今开/昨收/量比/换手, 计算竞价强弱标签,
每天推一条快照到 PushPlus(微信)。零重依赖, 仅腾讯行情 API。

与本地 auction_check.py 的区别: 不依赖 levels.py, 内置标的; 输出走 PushPlus 而非 console。
"""
import os
import sys
import io
import time
import urllib.request
import urllib.parse

# ---------- 配置 ----------
# 监控标的: 代码 -> (名称, 关键位[选填: 止损/买点/目标])
WATCH = {
    "600272": {"name": "开开实业", "stop": 17.45, "buy": 18.30, "target": 19.60},
}
TRIGGER_TOL = 0.25

# 腾讯行情字段: 3=现价 4=昨收 5=开盘 32=涨跌幅% 38=换手% 49=量比
IDX_PRICE, IDX_PRE, IDX_OPEN, IDX_PCT = 3, 4, 5, 32
IDX_TURNOVER, IDX_VR = 38, 49


def _f(x):
    if x is None:
        return 0.0
    s = str(x).strip()
    if s in ("", "-", "--", "nan", "None", "N/A"):
        return 0.0
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def fetch_quotes(codes):
    syms = [("sh" if c.startswith("6") else "sz") + c for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(syms)
    req = urllib.request.Request(url, headers={"Referer": "https://gu.qq.com", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
    try:
        text = raw.decode("gbk", errors="replace")
    except Exception:
        text = raw.decode("utf-8", errors="replace")
    out = {}
    for part in text.split(";"):
        if "v_" not in part:
            continue
        f = part.split("~")
        if len(f) < 50:
            continue
        price = _f(f[3])
        if price <= 0:
            continue
        out[f[2]] = {"open": _f(f[5]), "pre_close": _f(f[4]), "price": price,
                     "pct": _f(f[32]), "vol_ratio": _f(f[49]), "turnover": _f(f[38])}
    return out


def auction_label(open_gap, vr):
    tags = []
    if open_gap >= 5.0:
        tags.append("大幅高开(防兑现)")
    elif open_gap >= 3.0:
        tags.append("高开偏强")
    elif open_gap >= 1.0:
        tags.append("高开")
    elif open_gap > -1.0:
        tags.append("平开")
    elif open_gap > -3.0:
        tags.append("低开偏弱")
    else:
        tags.append("大幅低开(警惕)")
    if vr >= 3.0:
        tags.append("竞价放量")
    elif vr >= 1.5:
        tags.append("竞价温和放量")
    elif vr < 0.5:
        tags.append("竞价缩量")
    return " · ".join(tags)


def pushplus_send(title, content):
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


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower().startswith("ascii"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    # 北京时间窗口守卫: 9:25-9:32 (9:30后按连续竞价价算, 不再准)
    force_send = os.environ.get("FORCE_SEND", "").strip().lower() in ("1", "true", "yes")
    now_t = time.localtime()
    hhmm = now_t.tm_hour * 100 + now_t.tm_min
    if not force_send and not (925 <= hhmm <= 932):
        print(f"[guard] 北京时间 {now_t.tm_hour:02d}:{now_t.tm_min:02d} 不在 9:25-9:32 窗口, 静默退出", flush=True)
        return 0

    quotes = fetch_quotes(list(WATCH.keys()))
    if not quotes:
        print("[warn] 行情拉取失败, 静默退出", flush=True)
        return 0

    lines = []
    for code, cfg in WATCH.items():
        if code not in quotes:
            lines.append(f"{cfg['name']}: 无有效行情")
            continue
        q = quotes[code]
        pre = q["pre_close"]
        gap = (q["open"] / pre - 1) * 100 if pre else 0.0
        label = auction_label(gap, q["vol_ratio"])
        lines.append(f"<b>{cfg['name']} ({code})</b><br>"
                     f"今开 <b>{q['open']:.2f}</b> ({gap:+.1f}%) | 昨收 {pre:.2f}<br>"
                     f"量比 {q['vol_ratio']:.1f} · 换手 {q['turnover']:.1f}% · 现价 {q['price']:.2f} ({q['pct']:+.1f}%)<br>"
                     f"竞价强弱: <b>{label}</b>")

        # 关键位状态(竞价就能看的)
        kp = [f"止损{cfg['stop']}" if q["price"] <= cfg["stop"] + TRIGGER_TOL else None,
              f"买点区{cfg['buy']}" if abs(q["price"] - cfg["buy"]) <= TRIGGER_TOL else None,
              f"目标{cfg['target']}" if q["price"] >= cfg["target"] - TRIGGER_TOL else None]
        kp = [x for x in kp if x]
        if kp:
            lines[-1] += f"<br>🔑 竞价已触: {', '.join(kp)}"

    now = time.strftime("%m-%d %H:%M")
    title = f"🌅 竞价强弱 {now}"
    content = "".join(f"<div style='margin-bottom:12px;'>{x}</div>" for x in lines)
    content += "<div style='color:#999;font-size:12px;'>云端自动·仅供参考·非荐股</div>"
    pushplus_send(title, content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
