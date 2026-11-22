"""
반도체 합산 PBR — semis.csv 종목의 시가총액 합 ÷ 자기자본 합 을 날마다 계산해 JSON 하나를 만든다.

실행:
    python build_semipb.py                 # out/semipb_v1.json
    python build_semipb.py --out <폴더>    # 다른 폴더에 (시험용)

무엇을 어디서:
    자기자본 · 주식 수   SEC XBRL companyfacts (공문서)   us-gaap:StockholdersEquity · dei:EntityCommonStockSharesOutstanding
    주가 · 분할         야후 chart (분할만 반영된 종가 · 배당 반영 아님)

계산:
    그날 시가총액 = (그날까지 공시된 가장 최근 주식 수 × 그 뒤 분할 배수) × 그날 종가
    그날 자기자본 = 그날까지 공시된 가장 최근 분기 값          ← ⚠ 공시일(filed) 기준이다. 분기말 기준이면 그때 모르던 숫자를 쓰게 된다
    합산 PBR     = Σ 시가총액 ÷ Σ 자기자본                    (셋 다 있는 종목만 · 자기자본 ≤ 0 은 뺀다)
    정규화       = 합산 PBR ÷ 지난 3 · 5 · 10년(거래일 756 · 1260 · 2520) 가운데값   ← 창이 다 차야 값이 나온다

⚠ 지금 구성 종목으로 과거를 계산한다 (살아남은 종목만 본 값). 상장 전인 종목은 그때 합산에 없다 — 들어오는 날 값이 조금 뛴다.
⚠ us-gaap 자기자본이 없는 해외 공시 기업(TSM · ASML 등)은 저절로 빠진다. 환율 · 회계기준을 추측해 섞지 않는다.
⚠ Firestore 로 올리는 것은 upload_semipb.py 다. 이 스크립트는 JSON 만 만든다.
"""
import argparse, bisect, csv, json, os, re, statistics, sys, time
from datetime import datetime, timezone, timedelta

import requests

OUT = "out"
GAP = 0.35            # SEC 요청 간격 (초) — SEC 한도는 초당 10회
START = "2010-06-01"  # XBRL 표지 주식 수가 이 무렵부터 있다
MIN_NAMES = 15        # 합산 종목이 이보다 적은 날은 버린다
STALE = 400           # 자기자본 기말이 이보다 오래됐으면(일) 그날 그 종목은 뺀다 — 멈춘 숫자로 조용히 계산하지 않는다
WINS = (("n3", 756), ("n5", 1260), ("n10", 2520))
P1 = 946684800       # 야후 일봉 시작 (2000-01-01) — ⚠ range=max 는 일봉을 달 단위로 솎아 준다. 기간을 초로 줘야 날마다 온다. 분할은 이 뒤 것만 온다 (주식 수 보정은 2009 뒤 것만 쓰므로 충분)


def sec_ua():
    """SEC 는 연락처가 든 User-Agent 를 요구한다 — build_13f.py 의 UA 를 그대로 쓴다 (한 곳에서만 정의)."""
    v = os.environ.get("SEC_UA")
    if v:
        return v
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_13f.py"), encoding="utf-8") as f:
        m = re.search(r'^UA\s*=\s*"([^"]+)"', f.read(), re.M)
    if not m:
        sys.exit("⚠ build_13f.py 에서 UA 를 못 찾았다. 환경변수 SEC_UA 를 넣어라.")
    return m.group(1)


def get_json(url, headers, tries=4, wait=2.0):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers, timeout=40)
            if r.status_code == 200:
                return r.json()
            last = "HTTP %d" % r.status_code
            if r.status_code == 404:
                break
        except Exception as e:      # noqa: BLE001 — 네트워크 오류는 다시 해 본다
            last = repr(e)[:120]
        time.sleep(wait * (i + 1))
    raise RuntimeError("%s → %s" % (url, last))


# ── SEC ───────────────────────────────────────────────────────────────
def step_series(units):
    """XBRL 값 목록 → 공시일 순 [(filed, end, val)] — 그날 알려진 가장 최근 기말(end)의 값만 남긴다.
    같은 기말이 뒤에 다시 실리면(다음 해 10-K 의 비교 열) 처음 공시된 값을 쓴다."""
    first = {}
    for u in units:
        end, filed, val, accn = u.get("end"), u.get("filed"), u.get("val"), u.get("accn")
        if not end or not filed or val is None:
            continue
        k = first.get(end)
        if k is None or filed < k[0]:
            first[end] = [filed, accn, {val}]
        elif filed == k[0] and accn == k[1]:
            k[2].add(val)             # 한 공시에 같은 날 값이 둘 = 주식 종류가 둘 → 더한다
    ev = sorted((v[0], end, sum(v[2])) for end, v in first.items())
    out, top = [], ""
    for filed, end, val in ev:
        if end > top:
            top = end
            out.append((filed, end, val))
    return out


def sec_company(ciks, hdr):
    """CIK 여럿(법인이 바뀐 회사 — 옛 법인 공시를 잇는다)의 값을 한데 모아 → (자기자본, 주식 수, 회사 이름들)"""
    equ, shu, shu2, names = [], [], [], []
    for cik in ciks:
        j = get_json("https://data.sec.gov/api/xbrl/companyfacts/CIK%010d.json" % cik, hdr)
        names.append(str(j.get("entityName") or ""))
        facts = j.get("facts") or {}
        g = facts.get("us-gaap") or {}
        # 자기자본 — 꼬리표를 중간에 바꾸는 회사가 있다 (브로드컴은 2020 부터 '비지배 포함' 만 쓴다) → 기말마다 첫째 꼬리표가 없을 때만 둘째 것으로 채운다
        u1 = ((g.get("StockholdersEquity") or {}).get("units") or {}).get("USD") or []
        u2 = ((g.get("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest") or {}).get("units") or {}).get("USD") or []
        have = {u.get("end") for u in u1}
        equ += u1 + [u for u in u2 if u.get("end") not in have]
        shu += (((facts.get("dei") or {}).get("EntityCommonStockSharesOutstanding") or {}).get("units") or {}).get("shares") or []
        shu2 += ((g.get("CommonStockSharesOutstanding") or {}).get("units") or {}).get("shares") or []
        time.sleep(GAP)
    return step_series(equ), step_series(shu) or step_series(shu2), " + ".join(names)


# ── 야후 ──────────────────────────────────────────────────────────────
def yahoo(t):
    """→ ({날짜: 종가}, [(분할일, 배수)]) · 종가는 분할만 반영된 값"""
    hdr = {"User-Agent": "Mozilla/5.0"}
    last = None
    for host in ("query1", "query2"):
        try:
            j = get_json("https://%s.finance.yahoo.com/v8/finance/chart/%s?period1=%d&period2=%d&interval=1d&events=split" % (host, t, P1, int(time.time()) + 86400), hdr, tries=3)
            r = j["chart"]["result"][0]
            ft = (r.get("meta") or {}).get("firstTradeDate")
            if len(r.get("timestamp") or []) < 50 and ft and ft > P1:      # 상장 전 날짜부터 달라고 하면 한 봉만 주는 종목이 있다 (QRVO) → 상장일부터 다시
                j = get_json("https://%s.finance.yahoo.com/v8/finance/chart/%s?period1=%d&period2=%d&interval=1d&events=split" % (host, t, ft, int(time.time()) + 86400), hdr, tries=3)
                r = j["chart"]["result"][0]
            off = (r.get("meta") or {}).get("gmtoffset", -18000)
            ts = r.get("timestamp") or []
            cl = r["indicators"]["quote"][0]["close"]
            px = {}
            for a, c in zip(ts, cl):
                if c is not None and c > 0:
                    px[datetime.fromtimestamp(a + off, timezone.utc).strftime("%Y-%m-%d")] = c
            sp = []
            for s in ((r.get("events") or {}).get("splits") or {}).values():
                n, d = s.get("numerator"), s.get("denominator")
                if n and d:
                    sp.append((datetime.fromtimestamp(s["date"] + off, timezone.utc).strftime("%Y-%m-%d"), n / d))
            return px, sorted(sp)
        except Exception as e:      # noqa: BLE001
            last = e
    raise RuntimeError("야후 %s: %s" % (t, last))


def at(series, day):
    """[(filed, end, val)] 에서 그날까지 공시된 마지막 것 → (end, val) · 없으면 None"""
    i = bisect.bisect_right(series, (day, "~", 0)) - 1
    return None if i < 0 else series[i][1:]


def rolling_median(v, w):
    out, win = [None] * len(v), []
    for i, x in enumerate(v):
        bisect.insort(win, x)
        if i >= w:
            win.pop(bisect.bisect_left(win, v[i - w]))
        if i >= w - 1:
            m = len(win) // 2
            med = win[m] if len(win) % 2 else (win[m - 1] + win[m]) / 2
            out[i] = round(x / med, 4) if med > 0 else None
    return out


def ishares_holdings():
    """SOXX 보유 종목을 iShares 에서 받는다 → [티커] · 못 받으면 None.
    ⚠ 한국에서는 CSV 대신 상품 페이지가 온다 (2026-11-22 실측). 러너에서 되는지는 기록을 볼 것 — 안 되면 semis.csv 가 그대로 쓰인다."""
    url = "https://www.ishares.com/us/products/239705/ishares-semiconductor-etf/1467271812596.ajax?fileType=csv&fileName=SOXX_holdings&dataType=fund"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=40)
        txt = r.text.lstrip("﻿")
        if r.status_code != 200 or txt.lstrip().startswith("<"):
            print("iShares 보유 종목: 못 받았다 (HTTP %d · %s)" % (r.status_code, "페이지가 옴" if txt.lstrip().startswith("<") else "빈 응답")); return None
        lines = txt.splitlines()
        k = next((i for i, l in enumerate(lines) if l.startswith("Ticker,")), None)
        if k is None:
            print("iShares 보유 종목: 표 머리를 못 찾았다"); return None
        out = []
        for row in csv.DictReader(lines[k:]):
            t = (row.get("Ticker") or "").strip().upper()
            if (row.get("Asset Class") or "").strip() == "Equity" and re.fullmatch(r"[A-Z][A-Z.]{0,5}", t):
                out.append(t.replace(".", "-"))
        if len(out) < 20:
            print("iShares 보유 종목: %d개뿐이라 쓰지 않는다" % len(out)); return None
        return out
    except Exception as e:      # noqa: BLE001
        print("iShares 보유 종목: 못 받았다 (%s)" % repr(e)[:80]); return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "semis.csv"), encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("ticker") or "").strip()]
    tickers = [r["ticker"].strip().upper() for r in rows]
    cik = {r["ticker"].strip().upper(): [int(x) for x in r["cik"].split("|")] for r in rows if (r.get("cik") or "").strip()}   # 첫째가 지금 법인
    hdr = {"User-Agent": sec_ua(), "Accept-Encoding": "gzip, deflate"}
    # 구성 종목 — iShares 에서 받아지면 그 목록 (새로 든 종목의 CIK 는 아래 SEC 명단에서) · 못 받으면 semis.csv 그대로
    src = "semis.csv"; live = ishares_holdings()
    if live:
        add = [t for t in live if t not in tickers]; gone = [t for t in tickers if t not in live]
        print("iShares 보유 종목 %d개 · semis.csv 에 없는 것 %s · iShares 에 없는 것 %s" % (len(live), add, gone))
        tickers = live; src = "iShares"

    # CIK 는 semis.csv 에 적어 둔다. SEC 명단이 받아지면 대조해서 다르면 그 종목을 뺀다 (⚠ 식별자는 두 출처로 — 한쪽만 믿지 않는다)
    bad = set()
    try:
        tk = get_json("https://www.sec.gov/files/company_tickers.json", hdr, tries=2)
        ref = {str(v["ticker"]).upper(): int(v["cik_str"]) for v in tk.values()}
        for t in tickers:
            if t in ref and t in cik and ref[t] != cik[t][0]:
                bad.add(t); print("⚠ %s CIK 불일치 — csv %d · SEC 명단 %d" % (t, cik[t][0], ref[t]))
            elif t in ref and t not in cik:
                cik[t] = [ref[t]]
        print("SEC 명단과 CIK 대조 끝 · 불일치 %d" % len(bad))
    except Exception as e:      # noqa: BLE001
        print("SEC 명단을 못 받았다 (%s) — semis.csv 의 CIK 만 쓴다. 아래 회사 이름을 눈으로 확인할 것" % str(e)[-40:])

    co, skipped = {}, []
    for t in tickers:
        if t in bad:
            skipped.append([t, "CIK 불일치"]); continue
        if t not in cik:
            skipped.append([t, "CIK 없음"]); continue
        try:
            eq, sh, ename = sec_company(cik[t], hdr)
        except Exception as e:      # noqa: BLE001
            skipped.append([t, "SEC 실패 " + str(e)[-60:]]); time.sleep(GAP); continue
        time.sleep(GAP)
        if not eq:
            skipped.append([t, "us-gaap 자기자본 없음 (해외 공시) · " + ename[:30]]); continue
        if not sh:
            skipped.append([t, "주식 수 없음"]); continue
        try:
            px, sp = yahoo(t)
        except Exception as e:      # noqa: BLE001
            skipped.append([t, str(e)[:80]]); continue
        time.sleep(0.5)
        # 주식 수를 지금 분할 기준으로 — 그 기준일(end) 뒤의 분할 배수를 곱한다
        sh2 = []
        for filed, end, val in sh:
            f = 1.0
            for d, k in sp:
                if d > end:
                    f *= k
            sh2.append((filed, end, val * f))
        # ⚠ 표지 주식 수에 단위가 틀린 공시가 섞여 있다 (천 주 단위 · 금액을 적은 것 등 — 2011~2013 에 합산 PBR 이 수백~수만 배로 튀었다)
        #   분할을 맞춘 뒤라 이웃 값과 비슷해야 한다 → 앞뒤 3건씩의 가운데값에서 3배 넘게 벗어난 건은 버린다
        vals = [v for _, _, v in sh2]; keep = []
        for i, row in enumerate(sh2):
            nb = vals[max(0, i - 3):i] + vals[i + 1:i + 4]
            med = statistics.median(nb) if nb else row[2]
            if med > 0 and 1 / 3 <= row[2] / med <= 3:
                keep.append(row)
        dropped = len(sh2) - len(keep); sh2 = keep
        co[t] = {"eq": eq, "sh": sh2, "px": px, "days": sorted(px), "drop": dropped}
        print("%-5s %-40s 자기자본 %3d건 (%s~) · 주식 수 %3d건 · 일봉 %5d · 분할 %s" % (t, ename[:40], len(eq), eq[0][0], len(sh2), len(px), sp[-2:]) + (" · 주식 수 버림 %d" % dropped if dropped else ""))
    if len(co) < MIN_NAMES:
        sys.exit("⚠ 받은 종목이 %d개뿐이다 (최소 %d). 만들지 않는다. 빠진 것: %s" % (len(co), MIN_NAMES, skipped))

    days = sorted({d for c in co.values() for d in c["days"] if d >= START})
    D, PB, K = [], [], []
    lastpx = {t: None for t in co}
    for t, c in co.items():       # START 전 마지막 종가
        i = bisect.bisect_left(c["days"], START) - 1
        if i >= 0:
            lastpx[t] = (c["days"][i], c["px"][c["days"][i]])
    cur = []
    for day in days:
        m = e = 0.0; k = 0; row = []
        for t, c in co.items():
            p = c["px"].get(day)
            if p is not None:
                lastpx[t] = (day, p)
            lp = lastpx[t]
            if lp is None or (datetime.strptime(day, "%Y-%m-%d") - datetime.strptime(lp[0], "%Y-%m-%d")).days > 7:
                continue              # 일주일 넘게 거래가 없으면 그날은 뺀다
            q, s = at(c["eq"], day), at(c["sh"], day)
            if not q or not s or q[1] <= 0 or s[1] <= 0:
                continue
            if (datetime.strptime(day, "%Y-%m-%d") - datetime.strptime(q[0], "%Y-%m-%d")).days > STALE:
                continue
            mc = s[1] * lp[1]
            m += mc; e += q[1]; k += 1
            row.append((t, mc, q[1], q[0]))
        if k >= MIN_NAMES and e > 0:
            D.append(day); PB.append(round(m / e, 4)); K.append(k); cur = (m, e, row)
    if len(D) < 1000:
        sys.exit("⚠ 계산된 날이 %d일뿐이다. 만들지 않는다." % len(D))

    out = {"d": D, "pb": PB, "k": K}
    for key, w in WINS:
        out[key] = rolling_median(PB, w)
    m, e, row = cur
    names = sorted(([t, round(mc / 1e9, 1), round(q / 1e9, 2), round(mc / q, 2), round(mc / m * 100, 1), end] for t, mc, q, end in row), key=lambda r: -r[1])
    out["meta"] = {
        "built": datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds"),
        "asof": D[-1], "n": len(row), "skipped": skipped, "src": src,
        "cols": ["ticker", "시가총액(십억$)", "자기자본(십억$)", "PBR", "시총 비중(%)", "자기자본 기말"],
        "names": names,
    }
    os.makedirs(a.out, exist_ok=True)
    p = os.path.join(a.out, "semipb_v1.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("\n%s · %d일 (%s ~ %s) · %.0f KB" % (p, len(D), D[0], D[-1], os.path.getsize(p) / 1024))
    print("오늘 합산 PBR %.2f · 종목 %d · 정규화 3년 %s · 5년 %s · 10년 %s" % (PB[-1], len(row), out["n3"][-1], out["n5"][-1], out["n10"][-1]))
    print("빠진 것:", skipped)
    for r in names[:8]:
        print("  %-5s 시총 %8.1f · 자기자본 %7.2f · PBR %6.2f · 비중 %4.1f%% · 기말 %s" % tuple(r))


if __name__ == "__main__":
    main()
