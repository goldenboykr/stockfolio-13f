"""
사업회사가 13F 를 내는지 확인한다.

실행:
    python check_13f_filers.py

⚠ 13F 의무는 '13(f) 대상 증권을 1억 달러 이상 재량 운용' 할 때 생긴다.
   전략적 지분이 비상장이면 대상이 아니라 아예 안 낸다.
⚠ 그래서 회사가 크다고 13F 를 내는 게 아니다. 확인해야 안다.
"""
import time, requests

UA = "stockfolio-probe goldenboykr@gmail.com"   # ⚠ 본인 이메일
HDR = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
GAP = 1.2

# 확인 대상 — 사업회사 + 대조용으로 이미 아는 곳 둘
TARGETS = [
    ("NVIDIA",        "0001045810"),
    ("Microsoft",     "0000789019"),
    ("Amazon",        "0001018724"),
    ("Meta",          "0001326801"),
    ("Alphabet",      "0001652044"),
    ("Apple",         "0000320193"),
    ("Tesla",         "0001318605"),
    ("Berkshire",     "0001067983"),   # 대조 — 반드시 나와야 함
    ("Valley Forge",  "0001697868"),   # 대조 — 반드시 나와야 함
]

def check(name, cik):
    r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                     headers=HDR, timeout=20)
    if r.status_code != 200:
        return f"{r.status_code} 오류"
    d = r.json()
    rec = d["filings"]["recent"]
    forms = rec["form"]

    # 최근 목록에서 13F-HR 을 찾는다
    hits = [(f, rec["reportDate"][i], rec["filingDate"][i], rec["accessionNumber"][i])
            for i, f in enumerate(forms) if f.startswith("13F-HR")]

    real_name = d.get("name", "?")
    if not hits:
        # 최근 목록(최대 1,000건)에 없을 뿐일 수도 있다 → 과거 파일까지 확인
        older = d["filings"].get("files", [])
        hint = " (최근 1,000건에 없음 · 과거 파일 있음)" if older else ""
        return f"❌ 13F 없음{hint} · {real_name}"

    form, period, filed, acc = hits[0]
    # 종목 수를 보려면 원문을 받아야 한다 — 여기서는 건수만
    return f"✅ {form} · 최근 {period} 기준 · {filed} 제출 · 총 {len(hits)}건 · {real_name}"

if __name__ == "__main__":
    print(f"UA = {UA}")
    print("─" * 78)
    for name, cik in TARGETS:
        time.sleep(GAP)
        try:
            print(f"{name:<14} {check(name, cik)}")
        except Exception as e:
            print(f"{name:<14} ⚠ {type(e).__name__}: {e}")
    print("─" * 78)
    print("⚠ Berkshire · Valley Forge 가 ✅ 여야 결과를 신뢰할 수 있다.")
    print("⚠ ❌ 는 '안 낸다' 는 뜻이다 — 지분이 비상장이거나 1억 달러 미만.")
