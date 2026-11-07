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
def latest_two(cik):
    """그 펀드의 최신 13F 와 직전 분기 13F → (cur, prev). 없으면 (None, None).
    ⚠ 펀드마다 자기 최신 분기다 (2026-11-07) — 일찍 낸 곳은 일찍 보인다."""
    fs = Company(cik).get_filings(form="13F-HR")
    if not fs or len(fs) == 0:
        return None, None
    cur = fs[0]
    cp = str(getattr(cur, "period_of_report", "") or "")
    prev = None
    for i in range(1, min(len(fs), 8)):
        p = str(getattr(fs[i], "period_of_report", "") or "")
        if p and p < cp:                  # ⚠ 같은 분기 재제출은 건너뛴다
            prev = fs[i]
            break
    return cur, prev


def fetch(target):
    """13F 한 건 → (period, filed, [{t,name,w,sh,val}])"""
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
        # ⚠ CUSIP 으로 티커를 못 찾으면 NaN 이 온다. NaN 은 참이라 'or cusip' 이 안 먹는다.
        if not isinstance(tkr, str):
            tkr = "" if (tkr is None or tkr != tkr) else str(tkr)
        tkr = tkr.strip()

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

    total = sum(r["val"] for r in rows)
    if not total or total != total:      # 0 이거나 NaN
        total = 1.0
    for r in rows:
        w = r["val"] / total * 100
        r["w"] = 0.0 if w != w else round(w, 2)     # ⚠ NaN 은 JS 가 못 읽는다
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
            if d != d:                  # NaN
                r["st"], r["chg"] = "—", None
                continue
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
    # active=0 = 13F 를 더는 안 내는 곳 (Greenlight · Omega · Aquamarine, 2026-11-07).
    # ⚠ 지우지 말고 쉬게 둔다 — 다시 1억 달러 문턱을 넘으면 1 로 되살린다.
    rest = [fd["fund"] for fd in funds if (fd.get("active") or "1").strip() == "0"]
    funds = [fd for fd in funds if (fd.get("active") or "1").strip() != "0"]
    if rest:
        print(f"쉬는 곳 {len(rest)}: {', '.join(rest)}")
    if a.limit:
        funds = funds[:a.limit]

    st = load_state()
    done = st.get("funds", {})            # 펀드 → 받은 분기
    print(f"펀드 {len(funds)}곳 · 지난 실행 최신 분기 {st.get('period') or '없음'}")
    print("─" * 78)

    # ⚠ 이전 결과를 읽어 이어쓴다 — 새 분기를 안 낸 펀드는 전 분기 그대로 남는다.
    #   (2026-11-07 전 파일은 줄에 분기 p 가 없다 → meta.period 로 채운다)
    v1, hold, skipped, errs = [], {}, [], []
    for path, box in ((f"{OUT}/f13_v1.json", "v1"), (f"{OUT}/f13_hold.json", "hold")):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            mp = old.get("meta", {}).get("period")
            if box == "v1":
                v1 = old.get("rows", [])
                for x in v1:
                    x.setdefault("p", mp)
                    fdv = str(x.get("fd") or "")
                    if len(fdv) == 5 and x.get("p"):   # 옛 줄은 제출일이 MM-DD → 해를 붙인다 (4분기는 이듬해 제출)
                        y = int(x["p"][:4]) + (1 if x["p"][5:7] == "12" else 0)
                        x["fd"] = f"{y}-{fdv}"
            else:
                hold = old.get("hold", {})
        except Exception as e:
            print(f"⚠ {path} 를 못 읽었다: {e}")
    # ⚠ 상태는 있는데 결과 파일이 비었으면 건너뛰면 안 된다.
    #   실제로 Actions 가 캐시된 상태만 복원해 빈 결과를 올린 적이 있다.
    if not v1:
        done = {}
        print("→ 결과 파일이 없거나 비었다. 처음부터 받는다.")
    else:
        print(f"→ 이전 결과 {len(v1)}곳 이어쓴다.")
    have = {x["n"]: x for x in v1}

    for i, fd in enumerate(funds, 1):
        name, cik = fd["fund"], fd["cik"]
        time.sleep(GAP)
        try:
            cf, pf = latest_two(cik)
        except Exception as e:
            print(f"{i:>3}. {name:<26} ⚠ {type(e).__name__}: {e}")
            errs.append(name); continue
        if cf is None:
            print(f"{i:>3}. {name:<26} — 13F 기록 없음")
            skipped.append(name); continue
        period = str(getattr(cf, "period_of_report", "") or "")
        if done.get(name) == period and name in have and name in hold and not a.force:
            print(f"{i:>3}. {name:<26} · {period} 이미 받음")
            continue
        time.sleep(GAP)
        try:
            _, filed, rows = fetch(cf)
            prev_rows, prev_period = [], None
            if pf is not None:
                time.sleep(GAP)
                try:
                    prev_period, _, prev_rows = fetch(pf)
                except Exception:
                    prev_rows = []
        except Exception as e:
            print(f"{i:>3}. {name:<26} ⚠ {type(e).__name__}: {e}")
            errs.append(name); continue

        rows = diff(rows, prev_rows)
        full = len(rows)
        nw = sum(1 for r in rows if r["st"] == "NEW")
        ad = sum(1 for r in rows if r["st"] == "ADD")
        dn = sum(1 for r in rows if r["st"] == "TRIM")
        ex = sum(1 for r in rows if r["st"] == "EXIT")

        buys = sorted([r for r in rows if r["st"] in ("NEW", "ADD")],
                      key=lambda r: -r["w"])[:6]

        v1 = [x for x in v1 if x["n"] != name]
        v1.append({"n": name, "g": fd["group"], "corp": int(fd.get("corp", 0) or 0),
                   "p": period, "pp": prev_period, "fd": filed,   # 분기 · 직전 분기 · 제출일 (YYYY-MM-DD)
                   "nw": nw, "ad": ad, "dn": dn, "ex": ex,
                   "hold": sum(1 for r in rows if r["st"] != "EXIT"),
                   "cut": full if full > CAP else 0,   # 상한에 걸린 펀드는 전체 개수를 남긴다
                   "buys": [[r["t"] or r["cusip"], r["w"], 1 if r["st"] == "NEW" else 0]
                            for r in buys]})
        # ⚠ 저장은 여기서만 자른다.
        #   100개를 넘으면 무엇을 남길지가 문제다 → 우선순위를 못 박는다.
        #   ① 신규  ② 증액  ③ 감소  ④ 청산  ⑤ 변동 없음
        #   같은 등급 안에서는 비중 큰 것 우선 (rows 가 이미 비중순이다)
        PRIO = {"NEW": 0, "ADD": 1, "TRIM": 2, "EXIT": 3, "—": 4}
        keep = sorted(rows, key=lambda r: PRIO.get(r["st"], 9))
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

    order = {fd["fund"]: i for i, fd in enumerate(funds)}
    v1 = [x for x in v1 if x["n"] in order]          # 쉬게 한 곳 · 명단에서 빠진 곳은 버린다
    hold = {k: v for k, v in hold.items() if k in order}
    v1.sort(key=lambda x: order.get(x["n"], 999))

    # 가장 최근 분기 = 지금 화면의 기준 · 그 분기를 아직 안 낸 곳 = 미제출 (전 분기로 남아 있음)
    newest = max((x.get("p") or "" for x in v1), default="")
    late = [x["n"] for x in v1 if (x.get("p") or "") < newest]
    skipped = late + [n for n in skipped if n not in late]
    prev_newest = next((x.get("pp") for x in v1 if x.get("p") == newest and x.get("pp")), None)
    per_cnt = {}
    for x in v1:
        per_cnt[x.get("p") or ""] = per_cnt.get(x.get("p") or "", 0) + 1

    os.makedirs(OUT, exist_ok=True)
    meta = {"period": newest, "prev": prev_newest, "periods": per_cnt, "errors": errs,
            "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "funds": len(v1), "skipped": skipped}
    with open(f"{OUT}/f13_v1.json", "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "rows": v1}, f, ensure_ascii=False, separators=(",", ":"))   # ⚠ upload_13f.clean() 이 NaN 을 다시 걸른다
    with open(f"{OUT}/f13_hold.json", "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "hold": hold}, f, ensure_ascii=False, separators=(",", ":"))

    st["period"] = newest
    st["funds"] = done
    save_state(st)

    s1 = os.path.getsize(f"{OUT}/f13_v1.json") / 1024
    s2 = os.path.getsize(f"{OUT}/f13_hold.json") / 1024
    print("─" * 78)
    print(f"받음 {len(v1)}곳 · 분기별 {per_cnt} · {newest} 미제출 {len(skipped)}곳 · 오류 {len(errs)}곳")
    print(f"f13_v1.json    {s1:>7.1f} KB   (한도 1024KB 의 {s1/1024*100:.0f}%)")
    print(f"f13_hold.json  {s2:>7.1f} KB   (한도 1024KB 의 {s2/1024*100:.0f}%)")
    if s2 > 900:
        print("⚠ 상세가 900KB 를 넘었다. 문서를 a/b 로 쪼개야 한다.")
    if skipped:
        print("미제출:", ", ".join(skipped))
    if errs:
        print("오류:", ", ".join(errs))


if __name__ == "__main__":
    main()
