"""
CIK 를 이름으로 찾는다 — 74곳 중 CIK 가 없는 21곳용.

실행:
    python resolve_ciks.py

⚠ 이름 검색은 동명이인·유사명이 걸린다. 결과를 눈으로 확인해야 한다.
⚠ 13F-HR 을 낸 적 없는 곳은 안 나온다 (해외 운용사·비상장 지분 중심).
"""
import re, time, requests

UA = "stockfolio-probe goldenboykr@gmail.com"   # ⚠ 본인 이메일
HDR = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
GAP = 1.2

# FolioObs 전용 21곳 — Bridgewater 제외
NAMES = [
    "Greenlight Capital",
    "Paulson",
    "Oaktree Capital Management",
    "Omega Advisors",
    "Point72 Asset Management",
    "Fairholme Capital Management",
    "Trian Fund Management",
    "Fundsmith",
    "Miller Value Partners",
    "H&H International Investment",
    "Fairfax Financial Holdings",
    "Ruane Cunniff",
    "Aquamarine Capital",
    "Abrams Capital Management",
    "Giverny Capital",
    "Temasek Holdings",
    "Public Investment Fund",
    "National Pension Service",
    "Baillie Gifford",
    "Baron Capital Group",
    "Markel Group",
]

URL = ("https://www.sec.gov/cgi-bin/browse-edgar"
       "?action=getcompany&company={}&type=13F-HR&dateb=&owner=include"
       "&count=10&output=atom")

def find(name):
    r = requests.get(URL.format(requests.utils.quote(name)), headers=HDR, timeout=20)
    if r.status_code != 200:
        return [(f"{r.status_code} 오류", "")]
    x = r.text
    # 결과가 1건이면 회사 페이지로 바로 간다 → CIK 가 단독으로 나온다
    single = re.search(r"<cik>(\d{10})</cik>", x)
    names  = re.findall(r"<conformed-name>([^<]+)</conformed-name>", x)
    ciks   = re.findall(r"<CIK>(\d{10})</CIK>|<cik>(\d{10})</cik>", x)
    ciks   = [a or b for a, b in ciks]
    if names and ciks:
        return list(zip(names, ciks))[:5]
    if single:
        nm = re.search(r"<conformed-name>([^<]+)</conformed-name>", x)
        return [(nm.group(1) if nm else "?", single.group(1))]
    return [("검색 결과 없음", "")]

if __name__ == "__main__":
    print(f"UA = {UA}")
    print("=" * 78)
    for name in NAMES:
        time.sleep(GAP)
        print(f"\n▶ {name}")
        try:
            for nm, cik in find(name):
                print(f"    {cik or '        '}  {nm}")
        except Exception as e:
            print(f"    ⚠ {type(e).__name__}: {e}")
    print("\n" + "=" * 78)
    print("⚠ 여러 건 나오면 13F 를 내는 본체를 골라야 한다.")
    print("⚠ 결과를 그대로 붙여 보내면 마스터 엑셀에 채워 넣는다.")
