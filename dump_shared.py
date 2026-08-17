"""
Firestore shared/* 를 파일로 뽑는다.

실행:
    python dump_shared.py

인증:
    같은 폴더에 serviceAccount.json 이 있어야 한다.

만드는 것:
    dump/market_data.json
    dump/market_data_russell_a.json
    dump/market_data_russell_b.json
    dump/ark_trades.json
    dump/f13_v1.json
    dump/f13_hold.json

⚠ 읽기만 한다. 아무것도 쓰지 않는다.
⚠ market_data 는 790KB 쯤이다. 파일이 크니 zip 으로 묶어 보내는 게 편하다.
"""
import json, os, sys

DOCS = ["market_data", "market_data_russell_a", "market_data_russell_b",
        "ark_trades", "f13_v1", "f13_hold"]
OUT = "dump"


def main():
    if not os.path.exists("serviceAccount.json"):
        sys.exit("⚠ serviceAccount.json 이 없다.")
    try:
        from google.cloud import firestore
        from google.oauth2 import service_account
    except ImportError:
        sys.exit("⚠ pip install google-cloud-firestore")

    with open("serviceAccount.json", encoding="utf-8") as f:
        sa = json.load(f)
    cred = service_account.Credentials.from_service_account_info(sa)
    db = firestore.Client(project=sa["project_id"], credentials=cred)

    os.makedirs(OUT, exist_ok=True)
    print(f"프로젝트 = {sa['project_id']}")
    print("-" * 66)

    for name in DOCS:
        try:
            snap = db.collection("shared").document(name).get()
        except Exception as e:
            print(f"{name:<26} ⚠ {type(e).__name__}: {e}")
            continue
        if not snap.exists:
            print(f"{name:<26} — 없음")
            continue
        data = snap.to_dict() or {}
        # Firestore 타임스탬프 등 JSON 으로 안 되는 값은 문자열로 바꾼다
        def safe(o):
            if isinstance(o, dict):
                return {k: safe(v) for k, v in o.items()}
            if isinstance(o, list):
                return [safe(v) for v in o]
            if isinstance(o, (str, int, float, bool)) or o is None:
                return o
            return str(o)
        body = json.dumps(safe(data), ensure_ascii=False, separators=(",", ":"))
        p = os.path.join(OUT, name + ".json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"{name:<26} {len(body.encode('utf-8'))/1024:>8.1f} KB → {p}")

    print("-" * 66)
    print("dump/ 폴더를 zip 으로 묶어 보내면 된다.")


if __name__ == "__main__":
    main()
