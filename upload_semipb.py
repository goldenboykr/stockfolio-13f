"""
out/semipb_v1.json 을 Firestore shared/semipb_v1 로 올린다.

실행:
    python upload_semipb.py          # 올린다
    python upload_semipb.py --dry    # 크기 · 값만 보고 올리지 않는다

⚠ 인증 · NaN 걷기는 upload_13f.py 것을 그대로 쓴다 (한 곳에서만 정의).
⚠ 본문은 JSON 문자열 한 덩어리 (f13_v1 과 같은 모양) — 앱이 snap.data().json 을 JSON.parse 한다.
"""
import argparse, json, os, sys

from upload_13f import LIMIT, OUT, clean, credentials


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
    db.collection("shared").document("semipb_v1").set({"built": meta.get("built"), "asof": meta.get("asof"), "json": body})
    print("→ shared/semipb_v1 올렸다")
    try:
        db.collection("shared").document("versions").set({"semipb": meta.get("built")}, merge=True)
    except Exception as e:      # noqa: BLE001
        print(f"⚠ versions 기록 실패(본 저장은 끝났다): {e}")


if __name__ == "__main__":
    main()
