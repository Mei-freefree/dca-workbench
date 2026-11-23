#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定投补仓信号检查（收盘口径）

规则（与飞飞手写纪律一致）：
  基准 = 最近 252 个完整收盘里的最高收盘价（52 周滚动高）
  回撤 = (基准 - 最近完整收盘) / 基准
  只补「当日首次跌破且尚未消化」的档位；反弹站上该档则该档复活；回撤归零全档复位
  每只标的月度封顶；额度按自然月自动重置
输出：Markdown 摘要 + 可选推送（Server酱 / PushPlus / Bark / Telegram）
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)
TODAY = NOW.strftime("%Y-%m-%d")
def _find_state():
    """state.json 可能和脚本同目录（仓库根目录），也可能在脚本上一级（scripts/ 布局）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.dirname(here)):
        p = os.path.join(d, "state.json")
        if os.path.exists(p):
            return p
    return os.path.join(here, "state.json")


STATE_PATH = _find_state()
ROOT = os.path.dirname(STATE_PATH)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

TARGETS = [
    {
        "key": "a500", "label": "中证A500", "fund": "022459", "cap": 400,
        "levels": [(6, 100), (12, 100), (20, 200), (30, 200), (40, 200)],
        # 东财在 GitHub Actions 里基本取不到数（被限流/不可达），只留新浪源，避免每次多一条噪音告警
        "prefer": "sina", "em": None, "sina": ("cn", "sh000510"),
    },
    {
        # 注意：东财 secid=100.NDX 返回的其实是「纳斯达克综合指数(IXIC)」的数值
        # （2026-11-20 收 22617.73，与新浪 .IXIC 完全一致），不是纳斯达克100。
        # 纳斯达克100 用新浪 .NDX：2026-11-20 收 25028.98，同期 QQQ 608.45，
        # NDX/QQQ≈41.1，与真实指数关系吻合。故本标的只用新浪源。
        "key": "ndx", "label": "纳斯达克100", "fund": "019172", "cap": 40,
        "levels": [(25, 10), (30, 10), (35, 10), (40, 10)],
        "prefer": "sina", "em": None, "sina": ("us", ".NDX"),
    },
]
LOOKBACK = 252
REBOUND = 1.0      # 反弹超过该档 1.00 个百分点 → 该档复活
NEWHIGH = 0.20     # 回撤回落到 0.20% 以内 → 视为创新高，全档复位


def http_get(url, referer=None, timeout=25, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": referer or "https://finance.sina.com.cn",
                "Accept": "*/*",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + 2 * i)
    raise RuntimeError("fetch failed: %s (%s)" % (url, last))


def closes_em(secid):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           "secid=%s&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=0"
           "&beg=%s&end=20500101" % (secid, (NOW - timedelta(days=760)).strftime("%Y%m%d")))
    d = json.loads(http_get(url, "https://quote.eastmoney.com/"))
    rows = d["data"]["klines"]
    out = []
    for s in rows:
        p = s.split(",")
        out.append((p[0], float(p[1])))
    return out


def closes_sina(kind, code):
    if kind == "cn":
        url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=520" % code)
        raw = http_get(url)
        rows = json.loads(raw)
        return [(r["day"], float(r["close"])) for r in rows]
    url = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/x/"
           "US_MinKService.getDailyK?symbol=%s&___qn=3" % urllib.parse.quote(code))
    raw = http_get(url)
    m = re.search(r"\((\[.*\])\)\s*$", raw, re.S)
    if not m:
        m = re.search(r"\((\[.*\])\)", raw, re.S)
    rows = json.loads(m.group(1))
    return [(r["d"], float(r["c"])) for r in rows]


def drop_today(rows):
    """14:30 检查时当天这根 K 线还没收盘，必须丢掉（收盘口径）"""
    return [r for r in rows if r[0] != TODAY]


def stats(rows):
    rows = drop_today(sorted(rows))
    win = rows[-LOOKBACK:]
    hi_date, hi = max(win, key=lambda x: x[1])
    cur_date, cur = rows[-1]
    dd = (hi - cur) / hi * 100.0
    return {
        "n": len(win), "high": hi, "high_date": hi_date,
        "cur": cur, "cur_date": cur_date, "dd": dd,
    }


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def write_data_json(state):
    """给工作台页面（GitHub Pages 同仓库）看的只读数据：收盘、52 周高、回撤、下一档、本月额度。"""
    snap = state.get("snapshot", {})
    sts = state.get("targets", {})
    out = {"generated": NOW.strftime("%Y-%m-%d %H:%M UTC"),
           "lastRun": state.get("lastRun", ""), "targets": {}}
    for t in TARGETS:
        key = t["key"]
        s = snap.get(key)
        st = sts.get(key, {})
        if not s:
            continue
        nxt, nxt_price, drop = None, None, None
        for lv, amt in t["levels"]:
            if lv not in (st.get("consumed") or []):
                nxt = lv
                nxt_price = round(s["high"] * (1 - lv / 100.0), 2)
                drop = round((s["close"] - nxt_price) / s["close"] * 100.0, 2)
                break
        out["targets"][key] = {
            "label": t["label"], "fund": t["fund"],
            "asOf": s["asOf"], "close": s["close"],
            "high": round(s["high"], 2), "highDate": s.get("highDate", ""),
            "dd": round(s["dd"], 2),
            "next": nxt, "nextPrice": nxt_price, "dropToNext": drop,
            "used": st.get("used", 0), "cap": t["cap"],
            "consumed": st.get("consumed") or [],
        }
        if s["asOf"] > out.get("asOf", ""):
            out["asOf"] = s["asOf"]
    path = os.path.join(ROOT, "data.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError as e:
        print("WARN: 写 data.json 失败: %s" % e)
        return None
    return path


def ensure(state, key):
    st = state.setdefault("targets", {}).setdefault(
        key, {"consumed": [], "used_month": "", "used": 0})
    st.setdefault("consumed", [])
    st.setdefault("used_month", "")
    st.setdefault("used", 0)
    return st


def main():
    state = load_state()
    alerts, lines, warns = [], [], []

    for t in TARGETS:
        key = t["key"]
        st = ensure(state, key)
        if st["used_month"] != TODAY[:7]:
            st["used_month"] = TODAY[:7]
            st["used"] = 0

        order = []
        for n in (t.get("prefer"), "sina", "em"):
            if t.get(n) and n not in order:
                order.append(n)
        got = {}
        for name in order:
            try:
                rows = closes_em(t["em"]) if name == "em" else closes_sina(*t["sina"])
                got[name] = stats(rows)
            except Exception as e:  # noqa: BLE001
                warns.append("%s: %s 源取数失败（%s）" % (t["label"], name, str(e)[:60]))

        if not got:
            lines.append("| %s | 取数失败 | - | - | - |" % t["label"])
            continue

        pref = t.get("prefer") if t.get("prefer") in got else sorted(got)[0]
        s = got[pref]
        dd = s["dd"]
        conflict = False
        if len(got) == 2:
            d2 = got["sina"]["dd"] if pref == "em" else got["em"]["dd"]
            if abs(d2 - dd) > 0.5:
                conflict = True
                warns.append("%s 两源回撤相差 %.2f 个百分点（%.2f%% vs %.2f%%），本次不发信号，请人工核对"
                             % (t["label"], abs(d2 - dd), dd, d2))

        # 创新高 → 全档复位
        if dd <= NEWHIGH and st["consumed"]:
            st["consumed"] = []
            lines.append("| %s | %.2f%% | 创新高，全档已复位 | - | - |" % (t["label"], dd))
            continue

        # 反弹站上浅档 → 该档复活
        for lv in [x for x in t["levels"] if x[0] in st["consumed"]]:
            if dd < lv[0] - REBOUND:
                st["consumed"].remove(lv[0])

        nxt = None
        for lv, amt in t["levels"]:
            if dd >= lv and lv not in st["consumed"]:
                nxt = (lv, amt)
                break

        if nxt and not conflict:
            lv, amt = nxt
            room = max(0, t["cap"] - st["used"])
            if room <= 0:
                lines.append("| %s | %.2f%% | 已跌破 %d%% 档，但本月 %d/%d 已满 | - | - |"
                             % (t["label"], dd, lv, st["used"], t["cap"]))
            else:
                real = min(amt, room)
                st["consumed"].append(lv)
                st["used"] += real
                alerts.append({
                    "label": t["label"], "fund": t["fund"], "level": lv,
                    "amount": real, "dd": dd, "room": room - real,
                })
                lines.append("| %s | %.2f%% | **触发 %d%% 档** | %d 元 | 本月剩 %d/%d |"
                             % (t["label"], dd, lv, real, t["cap"] - st["used"], t["cap"]))
        else:
            gap_txt = "-"
            for lv, _ in t["levels"]:
                if lv not in st["consumed"]:
                    lv_price = s["high"] * (1 - lv / 100.0)
                    gap_txt = ("%d%% 档还差 %.0f 点（再跌 %.2f%%）"
                               % (lv, s["cur"] - lv_price,
                                  (s["cur"] - lv_price) / s["cur"] * 100.0))
                    break
            lines.append("| %s | %.2f%% | 无信号 | - | %s |" % (t["label"], dd, gap_txt))

        state.setdefault("snapshot", {})[key] = {
            "asOf": s["cur_date"], "close": round(s["cur"], 2),
            "high": round(s["high"], 2), "highDate": s["high_date"],
            "dd": round(dd, 2), "src": pref,
        }

    state["lastRun"] = NOW.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    write_data_json(state)

    head = "### 补仓信号检查 · %s %s\n\n" % (TODAY, NOW.strftime("%H:%M"))
    if alerts:
        head += "**今日有加仓信号：" + "；".join(
            "%s 跌破 %d%% 档，可补 %d 元" % (a["label"], a["level"], a["amount"]) for a in alerts
        ) + "**\n\n"
        head += "> 15:00 前下单按今日净值。补完记得在 `state.json` 里已自动记账，不用再改。\n\n"
    else:
        head += "**今日无加仓信号** —— 按纪律：不动手。\n\n"

    table = "| 标的 | 当前回撤 | 状态 | 该补 | 备注 |\n|---|---|---|---|---|\n" + "\n".join(lines) + "\n"
    foot = "\n以上为个人纪律执行提醒与信息整理，不构成投资建议，市场有风险，投资需谨慎。\n"
    if warns:
        foot = "\n**提醒**\n" + "\n".join("- " + w for w in warns) + "\n" + foot

    report = head + table + foot
    print(report)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)

    should_push = bool(alerts) or os.environ.get("ALWAYS_NOTIFY") == "1"
    if should_push:
        push(push_title(alerts), push_body(alerts, state, warns))
    return 0


def push_title(alerts):
    if alerts:
        t = "需要补仓：" + "、".join("%s %d元" % (a["label"], a["amount"]) for a in alerts)
        return t[:32]
    return "定投检查 %s：今日无信号" % NOW.strftime("%m-%d")


def push_body(alerts, state, warns):
    """手机上看的一段话，不搬表格。"""
    out = []
    if alerts:
        out.append("## 今天要补")
        out.append("")
        for a in alerts:
            out.append("**%s（%s）　%d 元**" % (a["label"], a["fund"], a["amount"]))
            out.append("")
            out.append("回撤 %.2f%%，跌破 %d%% 档" % (a["dd"], a["level"]))
            out.append("")
            out.append("本月还剩 %d 元额度" % a["room"])
        out.append("")
        out.append("> 15:00 前下单按今日净值。已经自动记账，不用再改文件。")
    else:
        out.append("## 今天没有信号")
        out.append("")
        out.append("按纪律：不动手，明天再看。")

    out.append("")
    out.append("---")
    out.append("")
    out.append("**两个标的当前状态**")
    out.append("")
    for t in TARGETS:
        snap = (state.get("snapshot") or {}).get(t["key"])
        if not snap:
            out.append("- %s（%s）：今天没取到数" % (t["label"], t["fund"]))
            continue
        consumed = (state.get("targets", {}).get(t["key"]) or {}).get("consumed", [])
        nxt = next((lv for lv, _ in t["levels"] if lv not in consumed), None)
        if nxt:
            lv_price = snap["high"] * (1 - nxt / 100.0)
            gap_pts = snap["close"] - lv_price
            drop = gap_pts / snap["close"] * 100.0
            tail = ("下一档 %d%% 在 %.0f 点，还差 %.0f 点（再跌 %.2f%%）"
                    % (nxt, lv_price, gap_pts, drop))
        else:
            tail = "档位已全部消化"
        out.append("- **%s**（%s）：回撤 **%.2f%%**，%s" % (t["label"], t["fund"], snap["dd"], tail))

    tstate = state.get("targets") or {}
    cap_txt = []
    for t in TARGETS:
        st = tstate.get(t["key"]) or {}
        used = st.get("used", 0)
        cap_txt.append("%s %d/%d" % (t["label"], used, t["cap"]))
    if cap_txt:
        out.append("")
        out.append("**本月额度**：" + " · ".join(cap_txt))

    if warns:
        out.append("")
        out.append("**提醒**")
        for w in warns:
            out.append("- " + w)

    out.append("")
    out.append("以上为个人纪律执行提醒与信息整理，不构成投资建议，市场有风险，投资需谨慎。")
    return "\n".join(out)


def push(title, body):
    """推送：只认环境变量里配置了哪一个。Server酱 支持 Markdown，直接发原文。"""
    title = title.replace("\n", " ")
    md = body.strip()
    plain = re.sub(r"[*#>]|^\s*---\s*$", "", md, flags=re.M).strip()
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    plain = re.sub(r"[ \t]+\n", "\n", plain)

    sct = os.environ.get("SCT_KEY")
    if sct:
        try:
            key = sct if sct.startswith("sctp") else sct
            url = "https://sctapi.ftqq.com/%s.send" % key
            data = urllib.parse.urlencode({"title": title[:32], "desp": md}).encode()
            print("push(serverchan):", urllib.request.urlopen(
                urllib.request.Request(url, data=data, headers={"User-Agent": UA}), timeout=20
            ).read()[:120])
        except Exception as e:  # noqa: BLE001
            print("push(serverchan) failed:", e)

    pplus = os.environ.get("PUSHPLUS_TOKEN")
    if pplus:
        try:
            data = urllib.parse.urlencode({
                "token": pplus, "title": title[:32], "content": plain,
                "template": "txt",
            }).encode()
            print("push(pushplus):", urllib.request.urlopen(
                urllib.request.Request("https://www.pushplus.plus/send", data=data,
                                       headers={"User-Agent": UA}), timeout=20
            ).read()[:120])
        except Exception as e:  # noqa: BLE001
            print("push(pushplus) failed:", e)

    bark = os.environ.get("BARK_URL")
    if bark:
        try:
            url = bark.rstrip("/") + "/" + urllib.parse.quote(title[:60]) + "/" + \
                  urllib.parse.quote(plain[:1500])
            print("push(bark):", urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20
            ).read()[:120])
        except Exception as e:  # noqa: BLE001
            print("push(bark) failed:", e)

    tg_token, tg_chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if tg_token and tg_chat:
        try:
            data = urllib.parse.urlencode({
                "chat_id": tg_chat, "text": title + "\n\n" + plain[:3500],
            }).encode()
            print("push(telegram):", urllib.request.urlopen(
                urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % tg_token,
                                       data=data, headers={"User-Agent": UA}), timeout=20
            ).read()[:120])
        except Exception as e:  # noqa: BLE001
            print("push(telegram) failed:", e)


if __name__ == "__main__":
    sys.exit(main())
