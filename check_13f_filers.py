"""
어떤 곳이 13F 를 내는지 · 특정 분기를 냈는지 확인한다.

실행:
    python check_13f_filers.py                       # 기본 대상 (아래 DEFAULT)
    python check_13f_filers.py --period 2026-06-30   # 분기 지정
    python check_13f_filers.py "Pershing Square" "Fundsmith"
    python check_13f_filers.py --all                 # funds.csv 전부 (73곳 · 약 3분)
    python check_13f_filers.py --corp                # 사업회사(엔비디아 등) 옛 대상

⚠ 이 도구가 갈라야 하는 것은 셋이고, 셋은 서로 다른 문제다.
    ① CIK 가 딴 데를 가리킨다        → funds.csv 를 고친다
    ② 13F 를 아예 안 낸다             → 목록에서 뺀다 (매 분기 '미제출' 소음이 된다)
    ③ 내긴 하는데 그 분기만 없다      → 늦게 낼 수 있으니 기다린다
  그래서 SEC 가 부르는 **실제 이름**과 **분기 목록**을 같이 찍는다.
  이름을 안 찍으면 ①을 ②로 착각한다
  (2026-08-15 실측: Situational Awareness 를 2045870 으로 적었더니 Virtus Wealth 였다).

⚠ 13F 의무는 '13(f) 대상 증권을 1억 달러 이상 재량 운용' 할 때 생긴다.
   운용사가 크다고 내는 게 아니고, 지분이 비상장이면 아예 대상이 아니다.

⚠ 집 IP 는 SEC 가 403 이다 (2026-08-29 실측 · 네트워크 단위 판정).
   **GitHub 러너에서 돌려야 한다** — .github/workflows/check-filers.yml
"""
import argparse, csv, os, sys, time, requests

UA = "stockfolio-probe goldenboykr@gmail.com"   # ⚠ 본인 이메일 (SEC 가 요구한다)
HDR = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
GAP = 1.2                                        # ⚠ SEC 권고(10 req/s)보다 훨씬 여유
HERE = os.path.dirname(os.path.abspath(__file__))
SUB = "https://data.sec.gov/submissions/CIK{}.json"

# 2026-06-30 분기에서 배치가 건너뛴 곳 (앱 화면의 '아직 미제출' 과 같은 목록).
# ⚠ 손으로 적은 값이다. 다음에 다를 수 있으니 인자로 넘겨 덮어쓸 수 있게 해 뒀다.
DEFAULT = ["Pershing Square", "Greenlight Capital", "Omega Advisors",
           "Trian Fund Management", "Fundsmith", "Sequoia Fund", "Aquamarine Capital"]

# ⚠ 대조군 — 이 둘이 ✅ 가 아니면 결과 전체를 믿으면 안 된다.
#   SEC 가 막혔거나 UA 가 거부당한 것을 '13F 를 안 낸다' 로 읽는 사고를 막는다.
CONTROL = [("Berkshire (대조)", "0001067983"), ("Valley Forge (대조)", "0001697868")]

# 옛 용도 — 사업회사가 13F 를 내는지 (--corp)
CORP = [("NVIDIA", "0001045810"), ("Microsoft", "0000789019"), ("Amazon", "0001018724"),
        ("Meta", "0001326801"), ("Alphabet", "0001652044"), ("Apple", "0000320193"),
        ("Tesla", "0001318605")]


def load_funds():
    """funds.csv → [(이름, CIK)] · 이름으로 찾을 수 있게 사전도 같이"""
    path = os.path.join(HERE, "funds.csv")
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [(r["fund"], r["cik"]) for r in rows]


def filings_13f(cik):
    """(SEC 가 부르는 이름, [(분기, 제출일, 서식)]) · 최신 분기부터"""
    r = requests.get(SUB.format(cik), headers=HDR, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    d = r.json()
    rec = d["filings"]["recent"]
    hits = [(rec["reportDate"][i], rec["filingDate"][i], f)
            for i, f in enumerate(rec["form"]) if f.startswith("13F-HR")]
    hits.sort(reverse=True)
    return d.get("name", "?"), hits, bool(d["filings"].get("files"))


def verdict(name, cik, period):
    """한 곳의 판정. ①②③ 중 어느 것인지가 **한 줄에** 보여야 한다."""
    real, hits, has_old = filings_13f(cik)
    tag = f"CIK {cik.lstrip('0')} = {real}"
    if not hits:
        # ⚠ '최근 1,000건에 없을 뿐' 일 수 있다. 단정하지 않는다.
        hint = " · 과거 파일 있음(최근 1,000건 밖일 수 있다)" if has_old else ""
        return f"❌ 13F 기록 없음{hint}", tag
    per, filed, form = hits[0]
    want = [h for h in hits if h[0] == period]
    if want:
        return f"✅ {period} 제출함 ({want[0][1]} · {want[0][2]})", tag
    # 내긴 내는데 그 분기가 없다 — 늦은 건지, 그만 낸 건지 최신 분기로 판단한다
    return f"⚠ {period} 없음 · 최신은 {per} ({filed} 제출) · 13F 총 {len(hits)}건", tag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="funds.csv 의 펀드 이름 (비우면 DEFAULT)")
    # ⚠ 워크플로 입력용. 이름에 공백이 있어(Pershing Square) 셸 단어분리로는 못 넘긴다.
    ap.add_argument("--csv", default="", help="쉼표로 구분한 이름 목록 (빈 값이면 무시)")
    ap.add_argument("--period", default="2026-06-30", help="확인할 분기말 (YYYY-MM-DD)")
    ap.add_argument("--all", action="store_true", help="funds.csv 전부")
    ap.add_argument("--corp", action="store_true", help="사업회사 목록")
    a = ap.parse_args()

    funds = load_funds()
    by_name = {n: c for n, c in funds}

    if a.corp:
        targets = list(CORP)
    elif a.all:
        targets = list(funds)
    else:
        from_csv = [s.strip() for s in a.csv.split(",") if s.strip()]
        want = a.names or from_csv or DEFAULT
        targets, missing = [], []
        for n in want:
            if n in by_name:
                targets.append((n, by_name[n]))
            else:
                missing.append(n)
        if missing:
            # ⚠ 조용히 건너뛰면 '확인했다' 고 착각한다. 이름이 안 맞는 것도 결과다.
            print("⚠ funds.csv 에 없는 이름:", ", ".join(missing))
    targets += CONTROL

    print(f"UA = {UA}")
    print(f"확인 분기 = {a.period} · 대상 {len(targets)}곳")
    print("-" * 92)
    bad = 0
    for name, cik in targets:
        time.sleep(GAP)
        try:
            line, tag = verdict(name, cik, a.period)
        except Exception as e:
            line, tag = f"⚠ {type(e).__name__}: {e}", ""
            bad += 1
        print(f"{name:<22} {line}")
        if tag:
            print(f"{'':<22}   {tag}")
    print("-" * 92)
    print("⚠ 대조군 둘이 ✅ 여야 나머지를 믿을 수 있다. ❌ 면 SEC 가 막힌 것일 수 있다.")
    print("⚠ 'CIK … = 이름' 이 우리가 아는 그 회사인지 **눈으로** 확인해라 — 이게 핵심이다.")
    print("⚠ ❌ 13F 기록 없음 = 안 낸다(비상장 지분이거나 1억 달러 미만).")
    print("⚠ ⚠ 그 분기만 없음 = 늦게 낼 수 있다. 최신 분기가 언제인지로 판단해라.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
