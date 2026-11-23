"""
out/semipb_v1.json 을 Firestore shared/semipb_v1 로 올린다.

실행:
    python upload_semipb.py          # 올린다
    python upload_semipb.py --dry    # 크기 · 값만 보고 올리지 않는다

⚠ 인증 · NaN 걷기는 upload_13f.py 것을 그대로 쓴다 (한 곳에서만 정의).
⚠ 본문은 JSON 문자열 한 덩어리 (f13_v1 과 같은 모양) — 앱이 snap.data().json 을 JSON.parse 한다.
"""
import argparse, csv, json, os, sys

from upload_13f import LIMIT, OUT, clean, credentials


def fwd_today(db):
    """예상 이익 기준 합산 PER — 오늘 값 하나 (2026-11-23 "실제 pe · fwd pe 둘 다").
    과거 예상치는 구할 곳이 없어 날마다 하나씩 쌓는다 (shared/semifwd_v1 · 같은 기준일은 덮어쓴다).
    종목별 예상 PER · 시가총액은 매일 배치가 올려 둔 shared/market_data 의 screener 행에서 읽는다 (fwd_pe · pe · mc = 백만 달러)
    — 이 저장소에는 그 값을 받는 코드를 두지 않는다 (공개 저장소). 여기서 하는 일은 합산뿐이고 기록 · 로그에 남는 것도 합산 값뿐이다.
    합산 = Σ시가총액 ÷ Σ(시가총액 ÷ PER) · 값이 없는 종목(적자 등)은 뺀다. → [기준일, 예상 PER, 종목 수, 실제 PER(같은 출처 · 견줄 값)] 또는 None"""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "semis.csv"), encoding="utf-8") as f:
        want = {(r.get("ticker") or "").strip().upper() for r in csv.DictReader(f)} - {""}
    md = db.collection("shared").document("market_data").get().to_dict() or {}
    scr = md.get("screener") or {}
    asof = str((scr.get("_meta") or {}).get("asof") or "")[:10]
    m = e = m2 = e2 = 0.0; k = 0
    for r in scr.get("rows") or []:
        if str(r.get("t") or "").upper() not in want:
            continue
        try:
            mc, fw, tt = float(r.get("mc") or 0), float(r.get("fwd_pe") or 0), float(r.get("pe") or 0)
        except (TypeError, ValueError):
            continue
        if mc > 0 and fw > 0:
            m += mc; e += mc / fw; k += 1
        if mc > 0 and tt > 0:
            m2 += mc; e2 += mc / tt
    if len(asof) != 10 or k < 15 or e <= 0:
        print(f"⚠ 예상 PER — 오늘 값을 못 만들었다 (기준일 {asof!r} · 종목 {k})"); return None
    return [asof, round(m / e, 2), k, round(m2 / e2, 2) if e2 > 0 else None]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    p = os.path.join(OUT, "semipb_v1.json")
    if not os.path.exists(p):
        sys.exit(f"⚠ {p} 가 없다. build_semipb.py 를 먼저 돌려라.")
    with open(p, encoding="utf-8") as f:
        data = clean(json.load(f))
    d, pb, meta = data.get("d") or [], data.get("pb") or [], data.get("meta") or {}
    # ⚠ 이상한 결과는 올리지 않는다 — 앱의 선이 통째로 틀어진다
    if len(d) < 1000 or len(d) != len(pb):
        sys.exit(f"⚠ 날 수가 이상하다 ({len(d)} · {len(pb)}). 올리지 않는다.")
    if not (1 < pb[-1] < 60) or (meta.get("n") or 0) < 15:
        sys.exit(f"⚠ 마지막 값이 이상하다 (PBR {pb[-1]} · 종목 {meta.get('n')}). 올리지 않는다.")
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    size = len(body.encode("utf-8"))
    print(f"semipb_v1 {size/1024:.1f} KB · 한도의 {size/LIMIT*100:.0f}% · {len(d)}일 ({d[0]} ~ {d[-1]}) · PBR {pb[-1]} · 종목 {meta.get('n')}")
    if size > LIMIT:
        sys.exit("⚠ 한도 초과")
    if a.dry:
        print("--dry 라 올리지 않았다."); return

    from google.cloud import firestore
    from google.oauth2 import service_account
    sa, where = credentials()
    print(f"인증 = {where} · 프로젝트 = {sa.get('project_id')}")
    db = firestore.Client(project=sa["project_id"], credentials=service_account.Credentials.from_service_account_info(sa))
    # 예상 PER 기록 — 쌓인 것(shared/semifwd_v1)에 오늘 값을 더해 본문에도 같이 싣는다 (앱은 문서 하나만 읽는다)
    #   ⚠ 여기서 실패해도 PBR · 실제 PER 은 올린다. 기록은 읽지 못했으면 건드리지 않는다 (빈 목록으로 덮어 지우지 않는다)
    try:
        ref = db.collection("shared").document("semifwd_v1")
        hist = [list(x) for x in json.loads((ref.get().to_dict() or {}).get("json") or "[]")]
        t = fwd_today(db)
        if t:
            hist = [x for x in hist if x[0] != t[0]] + [t]; hist.sort()
            ref.set({"json": json.dumps(hist, separators=(",", ":")), "asof": t[0]})
            print(f"예상 PER {t[1]}배 (실제 {t[3]}배 · {t[2]}종목 · 기준일 {t[0]}) · 쌓인 날 {len(hist)}")
        if hist:
            data["fw"] = hist
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if len(body.encode("utf-8")) > LIMIT:
                sys.exit("⚠ 한도 초과 (예상 PER 기록 포함)")
    except SystemExit:
        raise
    except Exception as e:      # noqa: BLE001
        print(f"⚠ 예상 PER 기록 실패 (PBR · 실제 PER 은 그대로 올린다): {str(e)[:120]}")
    db.collection("shared").document("semipb_v1").set({"built": meta.get("built"), "asof": meta.get("asof"), "json": body})
    print("→ shared/semipb_v1 올렸다")
    try:
        db.collection("shared").document("versions").set({"semipb": meta.get("built")}, merge=True)
    except Exception as e:      # noqa: BLE001
        print(f"⚠ versions 기록 실패(본 저장은 끝났다): {e}")


if __name__ == "__main__":
    main()
