"""
out/*.json 을 Firestore 로 올린다.

실행:
    python upload_13f.py            # 올린다
    python upload_13f.py --dry      # 크기만 재고 올리지 않는다

인증 (둘 중 하나):
    ① 환경변수 FIREBASE_SA_JSON  에 서비스 계정 JSON 전체를 넣는다  ← Actions 용
    ② 같은 폴더에 serviceAccount.json 파일을 둔다                    ← 로컬 시험용
       ⚠ 이 파일을 절대 커밋하지 마라. 공개 저장소다.

쓰는 곳:
    shared/f13_v1     목록용
    shared/f13_hold   상세용

⚠ Firestore 는 배열 안에 배열을 못 넣는다.
   그래서 payload 를 JSON 문자열 한 덩어리로 넣는다. market_data 와 같은 방식이다.
⚠ 서비스 계정 권한은 shared/f13_* 만 쓰게 제한해라.
   기존 키를 그대로 쓰면 공개 저장소가 market_data 도 지울 권한을 갖는다.
"""
import argparse, json, math, os, sys

OUT = "out"
DOCS = [("f13_v1", "f13_v1.json"), ("f13_hold", "f13_hold.json")]
LIMIT = 1024 * 1024          # Firestore 문서 한도 1MiB


def clean(o):
    """NaN·Infinity 를 없앤다.
    ⚠ Python 은 NaN 을 그대로 쓰지만 JS 의 JSON.parse 는 못 읽는다 — 앱이 통째로 깨진다."""
    if isinstance(o, float):
        return 0 if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return o


def credentials():
    raw = os.environ.get("FIREBASE_SA_JSON")
    if raw:
        return json.loads(raw), "환경변수 FIREBASE_SA_JSON"
    if os.path.exists("serviceAccount.json"):
        with open("serviceAccount.json", encoding="utf-8") as f:
            return json.load(f), "serviceAccount.json"
    sys.exit("⚠ 인증 정보가 없다. FIREBASE_SA_JSON 을 넣거나 serviceAccount.json 을 둬라.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    payloads = []
    for doc, fname in DOCS:
        p = os.path.join(OUT, fname)
        if not os.path.exists(p):
            sys.exit(f"⚠ {p} 가 없다. build_13f.py 를 먼저 돌려라.")
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        data = clean(data)
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        size = len(body.encode("utf-8"))
        pct = size / LIMIT * 100
        flag = "  ⚠ 한도 초과 — 문서를 쪼개야 한다" if size > LIMIT else ""
        print(f"{doc:<10} {size/1024:>8.1f} KB   한도의 {pct:>4.0f}%{flag}")
        if size > LIMIT:
            sys.exit(1)
        # ⚠ 빈 결과를 올리면 앱의 표가 통째로 사라진다. 배치가 건너뛴 실행에서 실제로 그랬다.
        if doc == "f13_v1":
            n = len(data.get("rows") or [])
            if n == 0:
                sys.exit("⚠ f13_v1 의 펀드가 0곳이다. 올리지 않는다 — build_13f.py 를 먼저 돌려라.")
            print(f"{'':<10} 펀드 {n}곳")
        if doc == "f13_hold" and not (data.get("hold") or {}):
            sys.exit("⚠ f13_hold 가 비었다. 올리지 않는다.")
        payloads.append((doc, data.get("meta", {}), body))

    if a.dry:
        print("\n--dry 라 올리지 않았다.")
        return

    try:
        from google.cloud import firestore
        from google.oauth2 import service_account
    except ImportError:
        sys.exit("⚠ pip install google-cloud-firestore")

    sa, where = credentials()
    print(f"\n인증 = {where} · 프로젝트 = {sa.get('project_id')}")
    cred = service_account.Credentials.from_service_account_info(sa)
    db = firestore.Client(project=sa["project_id"], credentials=cred)

    for doc, meta, body in payloads:
        db.collection("shared").document(doc).set({
            "period": meta.get("period"),
            "prev": meta.get("prev"),
            "built": meta.get("built"),
            "funds": meta.get("funds"),
            "skipped": meta.get("skipped", []),
            "json": body,                    # ⚠ 본문은 문자열 한 덩어리
        })
        print(f"→ shared/{doc} 올렸다  ({len(body.encode('utf-8'))/1024:.1f} KB)")

    print("\n끝났다. 앱에서 shared/f13_v1 을 읽어 JSON.parse 하면 된다.")


if __name__ == "__main__":
    main()
