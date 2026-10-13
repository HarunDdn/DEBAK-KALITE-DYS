"""Taslak durumundaki TÜM dokümanları Onaylı yapar (toplu onay). Yalnız ortam değişkeniyle gösterilen veritabanına yazar.
KULLANIM: python scripts/toplu_onay.py            → kuru çalışma (sayım)
          python scripts/toplu_onay.py --uygula   → uygular (ÖNCE VERİTABANI YEDEĞİ ALIN)
Her doküman için Hazırlama / Kontrol / Onay adımları 'Onaylandı' ve açıklama 'Toplu onay (Admin kararı)' olarak kaydedilir; yürürlük tarihi bugün,
sonraki gözden geçirme +365 gün (boşsa), geçerli revizyon kaydı 'Onaylandı'. Görevler ayrılığı bilinçli olarak atlanır (tek seferlik geçiş kararı) — denetimde bu açıklama izlenebilir."""
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import SessionLocal, Document, DocumentApproval, DocumentRevision, User, AuditLog  # noqa: E402

UYGULA = "--uygula" in sys.argv
ACIKLAMA = f"Toplu onay (Admin kararı {date.today():%d.%m.%Y}) — yeni 11 süreçlik yapıya geçiş"


def main():
    db = SessionLocal()
    try:
        adm = db.query(User).filter(User.rol == "Admin", User.aktif.__eq__(True)).order_by(User.id).first()
        liste = db.query(Document).filter(Document.durum == "Taslak").all()
        print(f"Taslak doküman: {len(liste)} · onaylayan: {adm.ad_soyad if adm else '-'}")
        if not UYGULA:
            print("KURU ÇALIŞMA — hiçbir şey değişmedi. Uygulamak için --uygula (önce yedek!).")
            return
        simdi = datetime.now()
        for d in liste:
            wf = (db.query(DocumentApproval).filter_by(document_id=d.id).count() and
                  max(a.workflow_no or 1 for a in db.query(DocumentApproval).filter_by(document_id=d.id).all()) + 1) or 1
            for adim in ("Hazırlama", "Kontrol", "Onay"):
                db.add(DocumentApproval(document_id=d.id, onay_adimi=adim, kullanici_id=adm.id if adm else None, durum="Onaylandı", aciklama=ACIKLAMA, tarih=simdi, workflow_no=wf))
            d.durum = "Onaylı"
            d.yururluk_tarihi = d.yururluk_tarihi or date.today()
            if not d.sonraki_gozden_gecirme:
                d.sonraki_gozden_gecirme = date.today() + timedelta(days=365)
            rev = db.query(DocumentRevision).filter_by(document_id=d.id, revizyon_no=d.revizyon_no).order_by(DocumentRevision.id.desc()).first()
            if rev:
                rev.onay_durumu = "Onaylandı"
        db.add(AuditLog(kullanici_id=adm.id if adm else None, islem_tipi="Onay", detay=f"Toplu onay: {len(liste)} doküman Taslak → Onaylı"))
        db.commit()
        print("onaylandı:", len(liste))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
