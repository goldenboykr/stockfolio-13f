"""
SEC 접근 진단 — 어느 경로가 막히고 어느 경로가 열려 있는지 하나씩 확인한다.

실행:
    pip install edgartools requests
    python sec_probe.py

⚠ 목적은 데이터를 받는 게 아니라 **어디서 막히는지 위치를 특정하는 것**이다.
⚠ 로컬에서 되고 Actions 에서 안 되면 IP 문제, 둘 다 안 되면 경로/헤더 문제다.
"""
import time, sys, json, traceback
import requests

# SEC 가 요구하는 UA — 이메일 필수. 여기를 반드시 본인 것으로 바꿀 것.
UA = "goldenboykr@gmail.com"
HDR = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
GAP = 1.2          # SEC 권고 10 req/s 보다 훨씬 보수적으로
TIMEOUT = 20

CIK_GATES = "0001166559"   # Gates Foundation — 24종목, 가벼움
CIK_VF    = "0001697868"   # Valley Forge — 7종목, 가장 가벼움

results = []

def probe(name, fn):
    """한 경로를 시험하고 상태를 기록한다. 예외도 결과로 남긴다."""
    time.sleep(GAP)
    t0 = time.time()
    try:
        code, note = fn()
        ok = code == 200
    except Exception as e:
        code, note, ok = None, f"{type(e).__name__}: {e}", False
    dt = round(time.time() - t0, 2)
    results.append((name, code, ok, dt, note))
    mark = "OK " if ok else "XX "
    print(f"{mark} {name:<42} {str(code):<5} {dt:>5}s  {note[:90]}")
    return ok


# ── ① 기본 경로 4종 ────────────────────────────────────────────────
def p_submissions():
    r = requests.get(f"https://data.sec.gov/submissions/CIK{CIK_GATES}.json",
                     headers=HDR, timeout=TIMEOUT)
    if r.status_code == 200:
        d = r.json()
        return 200, f"{d.get('name')} · 최근폼 {d['filings']['recent']['form'][:3]}"
    return r.status_code, r.text[:120]

def p_browse_edgar():
    # 핸드오프에서 200회 이후 403 이 났던 경로
    r = requests.get("https://www.sec.gov/cgi-bin/browse-edgar",
                     params={"action": "getcompany", "CIK": CIK_GATES,
                             "type": "13F-HR", "dateb": "", "owner": "include",
                             "count": "5", "output": "atom"},
                     headers=HDR, timeout=TIMEOUT)
    return r.status_code, r.text[:120].replace("\n", " ")

def p_files_json():
    # 핸드오프에서 UA 를 바꿔도 403 이던 경로
    r = requests.get("https://www.sec.gov/files/company_tickers.json",
                     headers=HDR, timeout=TIMEOUT)
    return r.status_code, (f"{len(r.json())}건" if r.status_code == 200 else r.text[:120])

def p_full_index():
    # 일별 인덱스 — Form 4 배치가 쓸 경로
    r = requests.get("https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/",
                     headers=HDR, timeout=TIMEOUT)
    return r.status_code, r.text[:120].replace("\n", " ")


# ── ② 실제 13F 원문 (이게 핵심) ────────────────────────────────────
def p_infotable():
    """제출 목록 → 최근 13F-HR → infotable XML 까지 3단계를 다 밟는다."""
    r = requests.get(f"https://data.sec.gov/submissions/CIK{CIK_VF}.json",
                     headers=HDR, timeout=TIMEOUT)
    if r.status_code != 200:
        return r.status_code, "submissions 단계에서 막힘"
    rec = r.json()["filings"]["recent"]
    idx = next((i for i, f in enumerate(rec["form"]) if f == "13F-HR"), None)
    if idx is None:
        return 200, "13F-HR 없음"
    acc = rec["accessionNumber"][idx].replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK_VF)}/{acc}"

    time.sleep(GAP)
    r2 = requests.get(base + "/index.json", headers=HDR, timeout=TIMEOUT)
    if r2.status_code != 200:
        return r2.status_code, "index.json 단계에서 막힘"
    names = [i["name"] for i in r2.json()["directory"]["item"]]
    xmls = [n for n in names if n.endswith(".xml")]
    if not xmls:
        return 200, f"xml 없음 · {names[:5]}"

    # infotable 은 보통 primary_doc 이 아닌 쪽
    target = next((n for n in xmls if "primary" not in n.lower()), xmls[-1])
    time.sleep(GAP)
    r3 = requests.get(f"{base}/{target}", headers=HDR, timeout=TIMEOUT)
    if r3.status_code != 200:
        return r3.status_code, f"{target} 단계에서 막힘"
    n = r3.text.count("<infoTable")
    return 200, f"{target} · 보유 {n}종목 · {len(r3.text)//1024}KB"


# ── ③ edgartools 경유 ──────────────────────────────────────────────
def p_edgartools_13f():
    from edgar import set_identity, Company
    set_identity(UA)
    f = Company(CIK_VF).get_filings(form="13F-HR").latest(1)
    obj = f.obj()
    h = obj.infotable if hasattr(obj, "infotable") else obj.holdings
    return 200, f"{len(h)}행 · 열 {list(h.columns)[:5]}"

def p_edgartools_form4():
    from edgar import set_identity, Company
    set_identity(UA)
    f = Company("AAPL").get_filings(form="4").latest(1)
    o = f.obj()
    return 200, f"{getattr(o, 'reporting_owner_name', '?')} · {getattr(o, 'issuer_ticker', '?')}"


# ── ④ 연속 요청 내성 — 몇 번째에서 막히나 ──────────────────────────
def p_burst(n=30):
    """핸드오프의 '200회 이후 403' 이 재현되는지. n 회 연속으로 두드린다."""
    print(f"\n── 연속 {n}회 시험 (간격 {GAP}s) ─────────────────────")
    fail_at = None
    for i in range(1, n + 1):
        time.sleep(GAP)
        try:
            r = requests.get(f"https://data.sec.gov/submissions/CIK{CIK_GATES}.json",
                             headers=HDR, timeout=TIMEOUT)
            if r.status_code != 200:
                fail_at = (i, r.status_code)
                break
        except Exception as e:
            fail_at = (i, type(e).__name__)
            break
        if i % 10 == 0:
            print(f"   {i}회 통과")
    if fail_at:
        print(f"   ⚠ {fail_at[0]}회째에서 {fail_at[1]}")
    else:
        print(f"   {n}회 전부 통과")
    return fail_at


if __name__ == "__main__":
    print(f"UA = {UA}")
    if "goldenboykr" not in UA and "@" not in UA:
        print("⚠ UA 에 이메일을 넣어라. SEC 가 거부한다.")
    print("─" * 78)

    probe("① data.sec.gov/submissions",      p_submissions)
    probe("① www.sec.gov/cgi-bin/browse-edgar", p_browse_edgar)
    probe("① www.sec.gov/files/*.json",      p_files_json)
    probe("① Archives/daily-index",          p_full_index)
    probe("② Archives infotable.xml (13F 원문)", p_infotable)
    probe("③ edgartools 13F",                p_edgartools_13f)
    probe("③ edgartools Form 4",             p_edgartools_form4)

    burst = p_burst(30)

    print("\n" + "─" * 78)
    ok = [r for r in results if r[2]]
    print(f"통과 {len(ok)} / {len(results)}")
    out = {
        "ua": UA,
        "results": [{"name": n, "code": c, "ok": o, "sec": d, "note": t}
                    for n, c, o, d, t in results],
        "burst_fail_at": burst,
    }
    with open("sec_probe_result.json", "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=2)
    print("→ sec_probe_result.json 저장")
    sys.exit(0 if len(ok) >= 5 else 1)
