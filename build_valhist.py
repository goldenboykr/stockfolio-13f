"""
종목별 · 지수별 PBR · PER 기록 — S&P 500 · 나스닥 100 · semis.csv 종목.

실행:
    python build_valhist.py                    # out/valhist/*.json
    python build_valhist.py --only AAPL,MSFT   # 몇 종목만 (시험용 · 합산은 만들지 않는다)

만드는 것 (out/valhist/):
    valco_v1_0 … 7.json   종목별 계단 값  {"co": {티커: [[공시일, 주당 자기자본, 주당 순이익(지난 4분기)], …]}}
                          → 앱이 그날 종가를 나눠 PBR · PER 을 그린다 (값은 지금 분할 기준 · 야후 종가와 같은 기준)
                          → 묶음 번호 = 티커 글자 코드 합 % 8  (앱의 valBucket 과 같아야 한다)
    valagg_<이름>.json    합산  {"d": [날짜], "pb": [Σ시가총액 ÷ Σ자기자본], "pe": [Σ시가총액 ÷ Σ순이익], "k": [종목 수], "ke": [종목 수]}
                          이름 = SPY (S&P 500) · QQQ (나스닥 100) · XLK … (S&P 500 의 GICS 업종 11개)

계산 방식 · 주의는 build_semipb.py 와 같다 (공시일 기준 · 지금 구성 종목으로 과거를 계산 · 해외 공시 기업은 빠진다).
다른 점: 합산에 자기자본이 음수인 회사도 넣는다 (지수 방식 — 500개 합이라 분모가 뒤집히지 않는다).

⚠ 이 저장소는 공개다 — 여기서 받는 것은 SEC 공시 · 야후 주가 · 구성 종목 목록뿐이다.
⚠ Firestore 로 올리는 것은 upload_valhist.py 다.
"""
import argparse, bisect, csv, html, json, os, re, statistics, sys, time
from datetime import date, datetime, timezone, timedelta

import requests

from build_semipb import GAP, STALE, START, at, get_json, sec_company, sec_ua, yahoo

OUT = os.path.join("out", "valhist")
NB = 8                      # 종목 묶음 수 — 앱의 valBucket 과 같아야 한다
COVER = 0.6                 # 합산은 구성 종목의 이 비율 넘게 값이 있는 날만
UA = {"User-Agent": "research-script"}
# 주식 종류마다 값어치가 달라 '주식 수 × 한 종류 주가' 로 시가총액을 낼 수 없는 회사 — 종목별 · 합산 모두에서 뺀다
EXCL = {"BRK-B": "A주 1주 = B주 1500주 — 주식 수 합 × B주 가격이 시가총액이 아니다", "BRK-A": "같음"}
SECTOR = {"Information Technology": "XLK", "Financials": "XLF", "Health Care": "XLV", "Consumer Discretionary": "XLY",
          "Communication Services": "XLC", "Industrials": "XLI", "Consumer Staples": "XLP", "Energy": "XLE",
          "Utilities": "XLU", "Real Estate": "XLRE", "Materials": "XLB"}
HERE = os.path.dirname(os.path.abspath(__file__))


def bucket(t):
    return sum(ord(c) for c in t) % NB


def read_csv(name):
    p = os.path.join(HERE, name)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if (r.get("ticker") or "").strip()]


def write_csv(name, cols, rows):
    with open(os.path.join(HERE, name), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(cols); w.writerows(rows)


def sp500():
    """→ [(티커, CIK, 업종)] · 위키백과 표 (티커 · 업종 · CIK 가 한 표에 있다). 못 받으면 sp500.csv"""
    try:
        r = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=UA, timeout=40)
        s = r.text
        i = s.find('id="constituents"')
        if r.status_code != 200 or i < 0:
            raise RuntimeError("HTTP %d · 표 없음" % r.status_code)
        out = []
        for row in s[i:s.find("</table>", i)].split("<tr")[2:]:
            c = [html.unescape(re.sub(r"<[^>]+>", "", x)).strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(c) >= 7 and re.fullmatch(r"[A-Z][A-Z.]{0,5}", c[0]) and re.fullmatch(r"\d{1,10}", c[6]):
                out.append((c[0].replace(".", "-"), int(c[6]), c[2]))
        if len(out) < 480:
            raise RuntimeError("%d줄뿐" % len(out))
        write_csv("sp500.csv", ["ticker", "cik", "sector"], out)
        print("S&P 500 — 위키백과 %d종목" % len(out))
        return out
    except Exception as e:      # noqa: BLE001
        rows = read_csv("sp500.csv")
        print("S&P 500 — 못 받았다 (%s) → sp500.csv %d종목" % (str(e)[:60], len(rows)))
        return [(r["ticker"].strip().upper(), int(r["cik"]), r.get("sector") or "") for r in rows]


def ndx100():
    """→ [티커] · 나스닥 공식 목록. 못 받으면 ndx100.csv"""
    try:
        j = requests.get("https://api.nasdaq.com/api/quote/list-type/nasdaq100", headers=dict(UA, Accept="application/json"), timeout=40).json()
        out = [str(r.get("symbol") or "").strip().upper().replace(".", "-").replace("/", "-") for r in j["data"]["data"]["rows"]]
        out = [t for t in out if re.fullmatch(r"[A-Z][A-Z-]{0,5}", t)]
        if len(out) < 95:
            raise RuntimeError("%d줄뿐" % len(out))
        write_csv("ndx100.csv", ["ticker"], [[t] for t in out])
        print("나스닥 100 — 나스닥 %d종목" % len(out))
        return out
    except Exception as e:      # noqa: BLE001
        rows = read_csv("ndx100.csv")
        print("나스닥 100 — 못 받았다 (%s) → ndx100.csv %d종목" % (str(e)[:60], len(rows)))
        return [r["ticker"].strip().upper() for r in rows]


def ordn(d, _c={}):
    v = _c.get(d)
    if v is None:
        v = _c[d] = date(int(d[:4]), int(d[5:7]), int(d[8:10])).toordinal()
    return v


def company(t, ciks, hdr):
    """한 종목 → {eq, sh, ni, px, days} · 못 만들면 사유 문자열 (build_semipb.py main 의 종목 처리와 같은 순서)"""
    eq, sh, ename, ni = sec_company(ciks, hdr)
    if not eq:
        return "us-gaap 자기자본 없음 · " + ename[:30]
    if not sh:
        return "주식 수 없음"
    px, sp = yahoo(t)
    time.sleep(0.3)
    sh2 = []
    for filed, end, val in sh:          # 주식 수를 지금 분할 기준으로
        f = 1.0
        for d, k in sp:
            if d > end:
                f *= k
        sh2.append((filed, end, val * f))
    vals = [v for _, _, v in sh2]; keep = []
    for i, row in enumerate(sh2):       # 단위가 틀린 공시 걷기 (앞뒤 3건 가운데값의 3배 밖)
        nb = vals[max(0, i - 3):i] + vals[i + 1:i + 4]
        med = statistics.median(nb) if nb else row[2]
        if med > 0 and 1 / 3 <= row[2] / med <= 3:
            keep.append(row)
    if not keep or len(px) < 30:
        return "주식 수 · 일봉 모자람"
    return {"eq": eq, "sh": keep, "ni": ni, "px": px, "days": sorted(px), "name": ename}


def steps(c, today):
    """종목별 계단 값 [[공시일, 주당 자기자본, 주당 순이익]] — 공시가 나온 날마다 한 줄 · 오래된(STALE 일 넘은) 값은 비운다"""
    ev = sorted({x[0] for x in c["eq"]} | {x[0] for x in c["sh"]} | {x[0] for x in c["ni"]})
    out = []
    for d in ev:
        s = at(c["sh"], d)
        if not s or s[1] <= 0:
            continue
        e, n = at(c["eq"], d), at(c["ni"], d)
        bv = round(e[1] / s[1], 3) if e and ordn(d) - ordn(e[0]) <= STALE else None
        ep = round(n[1] / s[1], 3) if n and ordn(d) - ordn(n[0]) <= STALE else None
        row = [max(d, START), bv, ep]
        if d < START and out:
            out[-1] = row               # START 전 것은 마지막 한 줄만 남긴다
        elif not out or out[-1][1:] != row[1:]:
            out.append(row)
    if out:                             # 공시가 끊긴 회사 — 멈춘 숫자로 조용히 이어 그리지 않게 끝을 비운다
        e, n = at(c["eq"], today), at(c["ni"], today)
        bv = out[-1][1] if e and ordn(today) - ordn(e[0]) <= STALE else None
        ep = out[-1][2] if n and ordn(today) - ordn(n[0]) <= STALE else None
        if [bv, ep] != out[-1][1:]:
            out.append([today, bv, ep])
    return out


def aggregate(co, members, days):
    """합산 PBR · PER → {"d", "pb", "pe", "k", "ke"} · 못 만들면 None"""
    members = [t for t in members if t in co]
    n = len(days); need = max(8, int(len(members) * COVER))
    M = [0.0] * n; E = [0.0] * n; K = [0] * n; M2 = [0.0] * n; E2 = [0.0] * n; K2 = [0] * n
    dord = [ordn(d) for d in days]
    for t in members:
        c = co[t]; px = c["px"]; cd = c["days"]; eq, sh, ni = c["eq"], c["sh"], c["ni"]
        i0 = bisect.bisect_left(cd, days[0]) - 1
        lp = (ordn(cd[i0]), px[cd[i0]]) if i0 >= 0 else None
        ie = is_ = ini = -1
        for j, day in enumerate(days):
            p = px.get(day)
            if p is not None:
                lp = (dord[j], p)
            while ie + 1 < len(eq) and eq[ie + 1][0] <= day:
                ie += 1
            while is_ + 1 < len(sh) and sh[is_ + 1][0] <= day:
                is_ += 1
            while ini + 1 < len(ni) and ni[ini + 1][0] <= day:
                ini += 1
            if lp is None or dord[j] - lp[0] > 7 or is_ < 0 or sh[is_][2] <= 0:
                continue
            mc = sh[is_][2] * lp[1]
            if ie >= 0 and dord[j] - ordn(eq[ie][1]) <= STALE:
                M[j] += mc; E[j] += eq[ie][2]; K[j] += 1
            if ini >= 0 and dord[j] - ordn(ni[ini][1]) <= STALE:
                M2[j] += mc; E2[j] += ni[ini][2]; K2[j] += 1
    D, PB, PE, KK, KE = [], [], [], [], []
    for j in range(n):
        if K[j] < need or E[j] <= 0:
            continue
        ok = K2[j] >= need and E2[j] > 0 and M2[j] / E2[j] < 500
        D.append(days[j]); PB.append(round(M[j] / E[j], 3)); KK.append(K[j])
        PE.append(round(M2[j] / E2[j], 2) if ok else None); KE.append(K2[j])
    if len(D) < 500:
        return None
    return {"d": D, "pb": PB, "pe": PE, "k": KK, "ke": KE, "n": len(members)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    hdr = {"User-Agent": sec_ua(), "Accept-Encoding": "gzip, deflate"}
    sp = sp500(); nd = ndx100(); semis = read_csv("semis.csv")
    cik = {}                                  # 티커 → [CIK …] (첫째가 지금 법인)
    for t, c, _ in sp:
        cik[t] = [c]
    ref = {}
    try:
        tk = get_json("https://www.sec.gov/files/company_tickers.json", hdr, tries=2)
        ref = {str(v["ticker"]).upper(): int(v["cik_str"]) for v in tk.values()}
    except Exception as e:      # noqa: BLE001
        print("SEC 명단을 못 받았다 (%s)" % str(e)[-40:])
    for r in semis:                           # 법인이 바뀐 회사(AVGO)는 semis.csv 의 CIK 묶음을 쓴다
        if (r.get("cik") or "").strip():
            cik[r["ticker"].strip().upper()] = [int(x) for x in r["cik"].split("|")]
    for r in read_csv("valhist_cik.csv"):     # 법인 번호가 바뀐 회사 — 옛 법인 공시를 잇는다 (손으로 적는다 · 첫째가 지금 법인이고 목록 · SEC 명단과 같아야 한다)
        cik[r["ticker"].strip().upper()] = [int(x) for x in r["cik"].split("|")]
    universe = list(dict.fromkeys([t for t, _, _ in sp] + nd + [r["ticker"].strip().upper() for r in semis]))
    if a.only:
        universe = [t.strip().upper() for t in a.only.split(",") if t.strip()]
    skipped, bad = [], set()
    for t in universe:                        # ⚠ 식별자는 두 출처로 — 목록의 CIK 와 SEC 명단이 다르면 뺀다 · 목록에 CIK 가 없으면 SEC 명단 것
        if t in ref and t in cik and ref[t] != cik[t][0]:
            bad.add(t); skipped.append([t, "CIK 불일치 — 목록 %d · SEC 명단 %d" % (cik[t][0], ref[t])])
        elif t not in cik and t in ref:
            cik[t] = [ref[t]]
    co, t0 = {}, time.time()
    for i, t in enumerate(universe):
        if t in bad:
            continue
        if t in EXCL:
            skipped.append([t, EXCL[t]]); continue
        if t not in cik:
            skipped.append([t, "CIK 없음"]); continue
        try:
            c = company(t, cik[t], hdr)
        except Exception as e:      # noqa: BLE001
            skipped.append([t, "실패 " + str(e)[-70:]]); time.sleep(GAP); continue
        if isinstance(c, str):
            skipped.append([t, c]); continue
        co[t] = c
        if (i + 1) % 25 == 0:
            print("  %d / %d · %.0f초" % (i + 1, len(universe), time.time() - t0), flush=True)
    print("받은 종목 %d · 빠진 것 %d" % (len(co), len(skipped)))
    for s in skipped:
        print("  빠짐 %-6s %s" % (s[0], s[1]))
    for t, c in co.items():                   # 법인 번호가 바뀌었을 수 있는 회사 — 눈으로 보고 valhist_cik.csv 에 옛 번호를 적는다
        if c["eq"][0][0] > "2013" and c["days"][0] < c["eq"][0][0][:4]:
            print("  기록 짧음 %-6s 첫 공시 %s · 일봉은 %s 부터 · %s" % (t, c["eq"][0][0], c["days"][0], c["name"][:40]))
    if not a.only and len(co) < 400:
        sys.exit("⚠ 받은 종목이 %d개뿐이다. 만들지 않는다." % len(co))

    os.makedirs(a.out, exist_ok=True)
    built = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")
    today = max(c["days"][-1] for c in co.values())

    def dump(name, obj):
        p = os.path.join(a.out, name + ".json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        return os.path.getsize(p) / 1024

    # 종목별
    packs = [{} for _ in range(NB)]
    for t, c in co.items():
        st = steps(c, today)
        if len(st) >= 4:
            packs[bucket(t)][t] = st
    for b, pk in enumerate(packs):
        kb = dump("valco_v1_%d" % b, {"built": built, "asof": today, "co": pk})
        print("valco_v1_%d · %d종목 · %.0f KB" % (b, len(pk), kb))
    for t in ("AAPL", "MSFT", "NVDA", "JPM", "XOM"):
        if t in co:
            st = packs[bucket(t)].get(t) or []; p = co[t]["px"][co[t]["days"][-1]]
            if st and st[-1][1] and st[-1][2]:
                print("  %-5s 종가 %.2f · PBR %.2f · PER %.1f (주당 자기자본 %s · 주당 순이익 %s · %s)" % (t, p, p / st[-1][1], p / st[-1][2], st[-1][1], st[-1][2], st[-1][0]))
    if a.only:
        return

    # 합산 — 같은 회사의 다른 주식 종류(GOOG · GOOGL)는 한 번만
    days = sorted({d for c in co.values() for d in c["days"] if d >= START})
    seen = set(); uniq = {}
    for t in universe:
        if t in co:
            k = cik[t][0]
            uniq[t] = k not in seen; seen.add(k)
    groups = {"SPY": [t for t, _, _ in sp], "QQQ": nd}
    for t, _, sec in sp:
        if sec in SECTOR:
            groups.setdefault(SECTOR[sec], []).append(t)
    for name, mem in groups.items():
        g = aggregate(co, [t for t in mem if uniq.get(t)], days)
        if not g:
            print("⚠ 합산 %s — 못 만들었다" % name); continue
        g["built"] = built; g["asof"] = g["d"][-1]
        kb = dump("valagg_" + name, g)
        print("valagg_%-4s %d일 (%s ~ %s) · PBR %.2f · PER %s · 종목 %d / %d · %.0f KB" % (name, len(g["d"]), g["d"][0], g["d"][-1], g["pb"][-1], g["pe"][-1], g["k"][-1], g["n"], kb))
    dump("_groups", {k: v for k, v in groups.items()})


if __name__ == "__main__":
    main()
