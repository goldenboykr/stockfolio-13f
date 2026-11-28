"""
out/valhist/*.json 을 Firestore shared/ 로 올린다 (build_valhist.py 가 만든 것).

실행:
    python upload_valhist.py          # 올린다
    python upload_valhist.py --dry    # 크기 · 값만 보고 올리지 않는다

문서:
    shared/valagg_<이름>     합산 PBR · PER (+ fw = 예상 PER 기록)
    shared/valco_v1_<0…7>    종목별 계단 값 (+ fd · fw = 예상 PER 기록)
    shared/valfwd_v1         합산 예상 PER 을 날마다 쌓는 곳      {이름: [[기준일, 예상 PER, 종목 수, 실제 PER]]}
    shared/valfwdco_v1_<n>   종목별 예상 PER 을 날마다 쌓는 곳    {"d": [기준일], "v": {티커: [값 …]}}

⚠ 예상 PER 은 과거 기록을 구할 곳이 없어 날마다 하나씩 쌓는다 (upload_semipb.py 와 같은 방식).
   종목별 값은 매일 배치가 올려 둔 shared/market_data 의 screener 행에서 읽는다 — 이 저장소(공개)에는 그 값을 받는 코드를 두지 않고,
   값은 Firestore 에서 Firestore 로만 옮긴다. 기록 · 로그 · 보관 파일에는 종목별 값을 남기지 않는다 (개수만).
⚠ 예상 PER 쪽이 실패해도 PBR · 실제 PER 은 올린다. 쌓인 기록은 읽지 못했으면 건드리지 않는다.
"""
import argparse, glob, json, os, sys

from upload_13f import LIMIT, clean, credentials

OUT = os.path.join("out", "valhist")
NB = 8


def body_of(obj):
    b = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return b, len(b.encode("utf-8"))


def screener(db):
    """→ (기준일, {티커: (시가총액, 예상 PER, 실제 PER)})"""
    md = db.collection("shared").document("market_data").get().to_dict() or {}
    scr = md.get("screener") or {}
    asof = str((scr.get("_meta") or {}).get("asof") or "")[:10]
    out = {}
    for r in scr.get("rows") or []:
        try:
            out[str(r.get("t") or "").upper().replace(".", "-")] = (float(r.get("mc") or 0), float(r.get("fwd_pe") or 0), float(r.get("pe") or 0))
        except (TypeError, ValueError):
            continue
    return asof, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    aggs, cos = {}, {}
    for p in sorted(glob.glob(os.path.join(OUT, "valagg_*.json"))):
        with open(p, encoding="utf-8") as f:
            aggs[os.path.basename(p)[7:-5]] = clean(json.load(f))
    for b in range(NB):
        p = os.path.join(OUT, "valco_v1_%d.json" % b)
        if not os.path.exists(p):
            sys.exit(f"⚠ {p} 가 없다. build_valhist.py 를 먼저 돌려라.")
        with open(p, encoding="utf-8") as f:
            cos[b] = clean(json.load(f))
    groups = {}
    gp = os.path.join(OUT, "_groups.json")
    if os.path.exists(gp):
        with open(gp, encoding="utf-8") as f:
            groups = json.load(f)
    # ⚠ 이상한 결과는 올리지 않는다
    for k in ("SPY", "QQQ"):
        g = aggs.get(k)
        if not g or len(g["d"]) < 1000 or not (1 < g["pb"][-1] < 40) or not (g["pe"][-1] and 5 < g["pe"][-1] < 100):
            sys.exit(f"⚠ 합산 {k} 가 없거나 값이 이상하다 ({g and g['pb'][-1]} · {g and g['pe'][-1]}). 올리지 않는다.")
    nco = sum(len(c["co"]) for c in cos.values())
    if nco < 400:
        sys.exit(f"⚠ 종목이 {nco}개뿐이다. 올리지 않는다.")
    for k, g in aggs.items():
        print(f"valagg_{k:5s} {body_of(g)[1]/1024:6.1f} KB · {len(g['d'])}일 · PBR {g['pb'][-1]} · PER {g['pe'][-1]} · 종목 {g['k'][-1]}")
    for b, c in cos.items():
        print(f"valco_v1_{b} {body_of(c)[1]/1024:6.1f} KB · {len(c['co'])}종목")
    if a.dry:
        print("--dry 라 올리지 않았다."); return

    from google.cloud import firestore
    from google.oauth2 import service_account
    sa, where = credentials()
    print(f"인증 = {where} · 프로젝트 = {sa.get('project_id')}")
    db = firestore.Client(project=sa["project_id"], credentials=service_account.Credentials.from_service_account_info(sa))
    sh = db.collection("shared")

    # 예상 PER — 합산
    try:
        asof, scr = screener(db)
        if len(asof) != 10:
            raise RuntimeError("기준일 없음")
        ref = sh.document("valfwd_v1")
        hist = json.loads((ref.get().to_dict() or {}).get("json") or "{}")
        made = 0
        for k, mem in groups.items():
            m = e = m2 = e2 = 0.0; n = 0
            for t in mem:
                mc, fw, tt = scr.get(t, (0, 0, 0))
                if mc > 0 and fw > 0:
                    m += mc; e += mc / fw; n += 1
                if mc > 0 and tt > 0:
                    m2 += mc; e2 += mc / tt
            if n < max(8, len(mem) * 0.6) or e <= 0:
                print(f"  예상 PER {k}: 종목 {n} / {len(mem)} — 모자라 건너뛴다"); continue
            row = [asof, round(m / e, 2), n, round(m2 / e2, 2) if e2 > 0 else None]
            hist[k] = sorted([x for x in hist.get(k, []) if x[0] != asof] + [row]); made += 1
            print(f"  예상 PER {k}: {row[1]}배 (실제 {row[3]}배 · {n}종목 · 기준일 {asof}) · 쌓인 날 {len(hist[k])}")
        if made:
            ref.set({"json": json.dumps(hist, separators=(",", ":")), "asof": asof})
        for k, g in aggs.items():
            if hist.get(k):
                g["fw"] = hist[k]
    except Exception as e:      # noqa: BLE001
        print(f"⚠ 합산 예상 PER 기록 실패 (PBR · 실제 PER 은 그대로 올린다): {str(e)[:120]}")
        asof, scr = "", {}

    # 예상 PER — 종목별 (값은 로그에 남기지 않는다)
    for b, c in cos.items():
        try:
            if len(asof) != 10:
                raise RuntimeError("기준일 없음")
            ref = sh.document("valfwdco_v1_%d" % b)
            h = json.loads((ref.get().to_dict() or {}).get("json") or '{"d":[],"v":{}}')
            if asof in h["d"]:
                i = h["d"].index(asof)
            else:
                h["d"].append(asof); i = len(h["d"]) - 1
                if h["d"] != sorted(h["d"]):
                    raise RuntimeError("기준일 순서가 어긋났다")
            n = 0
            for t in c["co"]:
                v = h["v"].setdefault(t, [])
                v += [None] * (i + 1 - len(v))
                fw = scr.get(t, (0, 0, 0))[1]
                v[i] = round(fw, 2) if fw > 0 else None
                n += v[i] is not None
            ref.set({"json": json.dumps(h, separators=(",", ":")), "asof": asof})
            c["fd"] = h["d"]; c["fw"] = {t: h["v"][t] for t in c["co"] if any(x is not None for x in h["v"].get(t, []))}
            print(f"  종목별 예상 PER 묶음 {b}: {n} / {len(c['co'])}종목 · 쌓인 날 {len(h['d'])}")
        except Exception as e:      # noqa: BLE001
            print(f"⚠ 종목별 예상 PER 묶음 {b} 실패 (PBR · 실제 PER 은 그대로 올린다): {str(e)[:120]}")

    for k, g in aggs.items():
        body, size = body_of(g)
        if size > LIMIT:
            print(f"⚠ valagg_{k} 한도 초과 — 건너뛴다"); continue
        sh.document("valagg_" + k).set({"built": g.get("built"), "asof": g.get("asof"), "json": body})
    for b, c in cos.items():
        body, size = body_of(c)
        if size > LIMIT:
            c.pop("fd", None); c.pop("fw", None); body, size = body_of(c)
            print(f"⚠ valco_v1_{b} 한도 초과 — 예상 PER 기록을 빼고 올린다")
        sh.document("valco_v1_%d" % b).set({"built": c.get("built"), "asof": c.get("asof"), "json": body})
    print(f"→ 합산 {len(aggs)}개 · 종목 묶음 {len(cos)}개 올렸다")
    try:
        sh.document("versions").set({"valhist": cos[0].get("built")}, merge=True)
    except Exception as e:      # noqa: BLE001
        print(f"⚠ versions 기록 실패(본 저장은 끝났다): {e}")


if __name__ == "__main__":
    main()
