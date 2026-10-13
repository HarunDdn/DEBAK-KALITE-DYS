"""Dosya bazlı dokümanların (talimat, form, politika, görev tanımı, el kitabı) içeriğini okunur HTML olarak veritabanına (documents.govde_html) alır.
KULLANIM:  python scripts/icerik_aktar.py                → yalnız içeriği henüz çıkarılmamış dokümanlar
           python scripts/icerik_aktar.py --yenile       → hepsini yeniden çıkarır
           python scripts/icerik_aktar.py --tip Talimat  → yalnız bu tip
           python scripts/icerik_aktar.py --kuru         → ne yapılacağını sayar, yazmaz
Ortam: DYS_DATABASE_URL (staging SQLite / canlı MSSQL). Dosya yolları dokümanın dosya_yolu alanından okunur (paylaşım erişilebilir olmalı).
Yalnız OKUR (kaynak dosyalara dokunmaz); .doc dosyaları için sunucuda / bilgisayarda Word kurulu olmalı (yoksa o dosyalar 'okunamadı' notu alır)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import SessionLocal, Document  # noqa: E402
import icerik_cikar as I  # noqa: E402


def main():
    yenile, kuru = "--yenile" in sys.argv, "--kuru" in sys.argv
    tip = sys.argv[sys.argv.index("--tip") + 1] if "--tip" in sys.argv else None
    db = SessionLocal()
    try:
        q = db.query(Document).filter((Document.icerik_json.is_(None)) | (Document.icerik_json == ""))
        if tip:
            q = q.filter(Document.dokuman_tipi == tip)
        docs = [d for d in q.order_by(Document.id).all() if d.dosya_yolu and (yenile or not d.govde_html)]
        print(f"işlenecek doküman: {len(docs)}")
        if kuru:
            return
        ok = yok = 0
        t0 = time.time()
        notlar = {}
        for i, d in enumerate(docs, 1):
            if I.doldur(db, d):
                ok += 1
            else:
                yok += 1
                notlar[d.dokuman_no] = d.govde_not
            if i % 25 == 0:
                db.commit()
                print(f"  {i}/{len(docs)} · {time.time() - t0:.0f} sn · çıkarıldı {ok} · çıkarılamadı {yok}", flush=True)
        db.commit()
        print(f"BİTTİ: çıkarıldı {ok} · çıkarılamadı {yok} · {time.time() - t0:.0f} sn")
        for k, v in list(notlar.items())[:40]:
            print("  çıkarılamadı:", k, "→", v)
    finally:
        db.close()


if __name__ == "__main__":
    main()
