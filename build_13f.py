"""
13F 배치 — funds.csv 의 펀드를 돌며 최신 분기 보유를 받아 JSON 두 개를 만든다.

실행:
    python build_13f.py              # 새 분기만 (없으면 몇 초로 끝난다)
    python build_13f.py --force      # 이미 있어도 다시 받는다
    python build_13f.py --limit 5    # 앞 5곳만 (시험용)

만드는 것:
    out/f13_v1.json      목록용 — 변동만
    out/f13_hold.json    상세용 — 전체 보유 (펀드당 상한 100)
    out/state.json       마지막으로 받은 분기 (다음 실행이 이걸 보고 건너뛴다)

⚠ Firestore 로 올리는 것은 이 스크립트가 하지 않는다. JSON 만 만든다.
⚠ 요청 간격 1.2초를 지킨다. 줄이면 SEC 가 막는다.
⚠ 펀드마다 제출일이 며칠씩 다르다. 없는 펀드는 건너뛰고 다음 실행에서 채운다.
"""
import argparse, csv, json, os, re, sys, time
from datetime import datetime, timezone

try:
    from edgar import Company, set_identity
except ImportError:
    sys.exit("⚠ edgartools 가 없다.  pip install edgartools")

UA   = "stockfolio-batch goldenboykr@gmail.com"   # ⚠ 본인 이메일
GAP  = 1.2      # SEC 요청 간격 (초) — 줄이지 마라
CAP  = 100      # ⚠ 저장 상한만이다. 비교는 전체 보유로 한다 (안 그러면 신규=청산 이 된다)
OUT  = "out"

set_identity(UA)


# ── 상태 ──────────────────────────────────────────────────────────────
def load_state():
    p = os.path.join(OUT, "state.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"period": None, "funds": {}}


def save_state(st):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "state.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ── 13F 한 곳 ─────────────────────────────────────────────────────────
def fetch(cik, want_period=None):
    """(period, filed, [{t,name,w,sh,val}]) 를 돌려준다. 없으면 None."""
    c = Company(cik)
    fs = c.get_filings(form="13F-HR")
    if not fs or len(fs) == 0:
        return None

    # 최신 것부터 훑어 want_period 를 찾는다 (없으면 최신)
    target = None
    for i in range(min(len(fs), 6)):
        f = fs[i]
        per = str(getattr(f, "period_of_report", "") or "")
        if want_period is None or per == want_period:
            target = f
            break
    if target is None:
        return None

    period = str(getattr(target, "period_of_report", "") or "")
    filed  = str(getattr(target, "filing_date", "") or "")

    obj = target.obj()
    df  = getattr(obj, "infotable", None)
    if df is None:
        df = obj.get_holdings() if hasattr(obj, "get_holdings") else None
    if df is None or len(df) == 0:
        return (period, filed, [])

    rows = []
    for r in df.to_dict("records"):
        # 열 이름이 판마다 다르다 — 후보를 순서대로 본다
        def g(*keys):
            for k in keys:
                if k in r and r[k] not in (None, ""):
                    return r[k]
            return None

        put_call = g("PutCall", "putCall", "put_call")
        if put_call:                      # ⚠ 옵션은 버린다. 실제 보유가 아니다
            continue

        name = g("Issuer", "nameOfIssuer", "issuer") or ""
        cus  = g("Cusip", "cusip") or ""
        val  = g("Value", "value") or 0
        sh   = g("SharesPrnAmount", "shares", "sshPrnamt") or 0
        tkr  = g("Ticker", "ticker") or ""

        try:  val = float(str(val).replace(",", ""))
        except Exception: val = 0.0
        try:  sh = int(float(str(sh).replace(",", "")))
        except Exception: sh = 0

        rows.append({"t": tkr, "cusip": cus, "name": str(name)[:40],
                     "val": val, "sh": sh})

    # 같은 종목이 여러 줄로 쪼개져 나온다 (계좌별) → 합친다
    merged = {}
    for r in rows:
        k = r["t"] or r["cusip"]
        if k in merged:
            merged[k]["val"] += r["val"]
            merged[k]["sh"]  += r["sh"]
        else:
            merged[k] = r
    rows = list(merged.values())

    total = sum(r["val"] for r in rows) or 1.0
    for r in rows:
        r["w"] = round(r["val"] / total * 100, 2)
    rows.sort(key=lambda r: -r["val"])
    return (period, filed, rows)          # ⚠ 자르지 않는다. 자르면 비교가 틀린다


# ── 두 분기 비교 ──────────────────────────────────────────────────────
def diff(cur, prev):
    """cur 각 행에 st(NEW/ADD/TRIM/EXIT/—) 와 chg(%) 를 붙이고, 청산 목록을 따로 돌려준다."""
    pmap = {(r["t"] or r["cusip"]): r for r in prev}
    for r in cur:
        k = r["t"] or r["cusip"]
        p = pmap.get(k)
        if p is None:
            r["st"], r["chg"] = "NEW", None
        elif p["sh"] == 0:
            r["st"], r["chg"] = "NEW", None
        else:
            d = (r["sh"] - p["sh"]) / p["sh"] * 100
            r["chg"] = round(d, 1)
            r["st"] = "ADD" if d > 0.5 else "TRIM" if d < -0.5 else "—"

    keys = {(r["t"] or r["cusip"]) for r in cur}
    exits = [{"t": p["t"], "cusip": p["cusip"], "name": p["name"],
              "w": 0, "sh": 0, "chg": -100.0, "st": "EXIT"}
             for k, p in pmap.items() if k not in keys]
    return cur + exits


# ── 메인 ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--funds", default="funds.csv")
    a = ap.parse_args()

    with open(a.funds, encoding="utf-8") as f:
        funds = list(csv.DictReader(f))
    if a.limit:
        funds = funds[:a.limit]

    st = load_state()
    done = st.get("funds", {})
    print(f"펀드 {len(funds)}곳 · 마지막 분기 {st['period'] or '없음'}")
    print("─" * 78)

    # ① 기준 분기를 먼저 정한다 — 첫 펀드의 최신 분기
    time.sleep(GAP)
    probe = fetch(funds[0]["cik"])
    if probe is None:
        sys.exit("⚠ 첫 펀드에서 13F 를 못 찾았다. CIK 를 확인해라.")
    period = probe[0]
    print(f"기준 분기 = {period}")

    have = sum(1 for fd in funds if done.get(fd["fund"]) == period)
    if period == st["period"] and have >= len(funds) and not a.force:
        print(f"→ 이미 받은 분기다 ({have}/{len(funds)}곳 완료). 할 일 없음.")
        return
    if have:
        print(f"→ {have}/{len(funds)}곳 이미 받음. 나머지만 받는다.")

    # ② 전 분기 = 직전 것
    prev_period = None
    fs = Company(funds[0]["cik"]).get_filings(form="13F-HR")
    for i in range(min(len(fs), 6)):
        p = str(getattr(fs[i], "period_of_report", "") or "")
        if p and p != period:
            prev_period = p
            break
    print(f"전 분기 = {prev_period or '없음 (증감 계산 못 함)'}")
    print("─" * 78)

    v1, hold, skipped = [], {}, []

    for i, fd in enumerate(funds, 1):
        name, cik = fd["fund"], fd["cik"]
        if done.get(name) == period and not a.force:
            print(f"{i:>3}. {name:<26} · 이미 받음 (건너뜀)")
            continue
        time.sleep(GAP)
        try:
            cur = fetch(cik, period)
        except Exception as e:
            print(f"{i:>3}. {name:<26} ⚠ {type(e).__name__}: {e}")
            skipped.append(name); continue

        if cur is None:
            print(f"{i:>3}. {name:<26} — {period} 미제출 (다음 실행에서 채운다)")
            skipped.append(name); continue

        _, filed, rows = cur
        prev_rows = []
        if prev_period:
            time.sleep(GAP)
            try:
                pr = fetch(cik, prev_period)
                prev_rows = pr[2] if pr else []
            except Exception:
                pass

        rows = diff(rows, prev_rows)
        full = len(rows)
        nw = sum(1 for r in rows if r["st"] == "NEW")
        ad = sum(1 for r in rows if r["st"] == "ADD")
        dn = sum(1 for r in rows if r["st"] == "TRIM")
        ex = sum(1 for r in rows if r["st"] == "EXIT")

        buys = sorted([r for r in rows if r["st"] in ("NEW", "ADD")],
                      key=lambda r: -r["w"])[:6]

        v1.append({"n": name, "g": fd["group"], "corp": int(fd.get("corp", 0) or 0),
                   "fd": filed[5:] if len(filed) >= 10 else filed,
                   "nw": nw, "ad": ad, "dn": dn, "ex": ex,
                   "hold": sum(1 for r in rows if r["st"] != "EXIT"),
                   "buys": [[r["t"] or r["cusip"], r["w"], 1 if r["st"] == "NEW" else 0]
                            for r in buys]})
        # ⚠ 저장은 여기서만 자른다 — 변동이 있는 것을 먼저, 그다음 비중순
        keep = [r for r in rows if r["st"] != "—"] + [r for r in rows if r["st"] == "—"]
        hold[name] = [[r["t"] or r["cusip"], r["name"], r["w"], r["sh"],
                       r["chg"], r["st"]] for r in keep[:CAP]]

        done[name] = period
        cut = " ✂" if full > CAP else ""
        print(f"{i:>3}. {name:<26} {filed}  신규{nw:>3} 증액{ad:>3} 감소{dn:>3} 청산{ex:>3}  보유{full-ex:>4}{cut}")

    if a.limit:
        print("─" * 78)
        print(f"⚠ --limit 시험 실행이다. 상태를 저장하지 않고 out/ 도 건드리지 않는다.")
        print(f"   받음 {len(v1)}곳 · 건너뜀 {len(skipped)}곳")
        return

    os.makedirs(OUT, exist_ok=True)
    meta = {"period": period, "prev": prev_period,
            "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "funds": len(v1), "skipped": skipped}
    with open(f"{OUT}/f13_v1.json", "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "rows": v1}, f, ensure_ascii=False, separators=(",", ":"))
    with open(f"{OUT}/f13_hold.json", "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "hold": hold}, f, ensure_ascii=False, separators=(",", ":"))

    st["period"] = period
    st["funds"] = done
    save_state(st)

    s1 = os.path.getsize(f"{OUT}/f13_v1.json") / 1024
    s2 = os.path.getsize(f"{OUT}/f13_hold.json") / 1024
    print("─" * 78)
    print(f"받음 {len(v1)}곳 · 건너뜀 {len(skipped)}곳")
    print(f"f13_v1.json    {s1:>7.1f} KB   (한도 1024KB 의 {s1/1024*100:.0f}%)")
    print(f"f13_hold.json  {s2:>7.1f} KB   (한도 1024KB 의 {s2/1024*100:.0f}%)")
    if s2 > 900:
        print("⚠ 상세가 900KB 를 넘었다. 문서를 a/b 로 쪼개야 한다.")
    if skipped:
        print("건너뜀:", ", ".join(skipped))


if __name__ == "__main__":
    main()
