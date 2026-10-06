"""Eski kurgudaki (11 süreç öncesi) süreç / doküman kayıtlarını DYS veritabanından siler; yalnız SON SİSTEM kalır.
KULLANIM:
  python scripts/eski_yapi_temizle.py            → KURU ÇALIŞMA: ne yapılacağını listeler, hiçbir şey değiştirmez
  python scripts/eski_yapi_temizle.py --uygula   → uygular (ÖNCE VERİTABANI YEDEĞİ ALIN)
  --dokuman-sil                                   → eski süreçlere bağlı dokümanları da (bağlı kayıtlarıyla) siler
Ortam: DYS_DATABASE_URL (staging için SQLite; canlıda canlı veritabanı — yalnız yedek alındıktan sonra, DBA / yönetici onayıyla).
Önce eski sürecin KPI / bağlı kayıtları yeni karşılığına TAŞINIR (veri kaybı olmasın): D07→D05, D08→D06, M05→M04, Y02→Y01, Y03→Y01; '-E' ekli (Eski) süreçler ve D01.3 → karşılık yoksa
bağlı kayıt kalmıyorsa silinir, kalıyorsa işlem durur ve listelenir.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text, inspect  # noqa: E402
from models import SessionLocal, Process, Document, Base  # noqa: E402

TASIMA = {"D07": "D05", "D08": "D06", "M05": "M04", "Y02": "Y01", "Y03": "Y01", "D01.3": "D06.1",
          "D02-E": "D03", "D05-E": "D02", "D06-E": "D04", "M03-E": "M04", "M04-E": "M03", "Y01-E": "Y01"}
UYGULA = "--uygula" in sys.argv
DOKUMAN_SIL = "--dokuman-sil" in sys.argv


def fk_sutunlari(tablo):
    """Verilen tabloya FK ile bağlı (tablo, sütun) çiftleri."""
    out = []
    for t in Base.metadata.sorted_tables:
        for fk in t.foreign_keys:
            if fk.column.table.name == tablo:
                out.append((t.name, fk.parent.name, fk.column.name))
    return out


def bagli_sil(db, tablo, pk_sutun, idler, derin=0):
    """tablo'da id'si listede olan satırları, onlara bağlı satırlarla birlikte siler (özyinelemeli)."""
    if not idler:
        return 0
    n = 0
    for t, c, pk in fk_sutunlari(tablo):
        if derin > 4 or t == tablo:
            continue
        pkcols = [x.name for x in Base.metadata.tables[t].primary_key.columns]
        if len(pkcols) != 1:
            db.execute(text(f"DELETE FROM {t} WHERE {c} IN ({','.join(str(int(i)) for i in idler)})"))
            continue
        alt = [r[0] for r in db.execute(text(f"SELECT {pkcols[0]} FROM {t} WHERE {c} IN ({','.join(str(int(i)) for i in idler)})")).fetchall()]
        n += bagli_sil(db, t, pkcols[0], alt, derin + 1)
    db.execute(text(f"DELETE FROM {tablo} WHERE {pk_sutun} IN ({','.join(str(int(i)) for i in idler)})"))
    return n + len(idler)


def main():
    db = SessionLocal()
    try:
        yeni = {p.kod: p for p in db.query(Process).filter(Process.aktif.is_(True)).all()}
        eski = [p for p in db.query(Process).filter(Process.aktif.is_(False)).all()]
        print(f"aktif süreç: {len(yeni)} · eski (pasif) süreç: {len(eski)}")
        for p in eski:
            hedef = yeni.get(TASIMA.get(p.kod, ""))
            ref = []
            for t, c, _ in fk_sutunlari("processes"):
                n = db.execute(text(f"SELECT COUNT(*) FROM {t} WHERE {c} = :i"), {"i": p.id}).scalar()
                if n:
                    ref.append((t, c, n))
            print(f"  {p.kod:6s} {p.ad[:44]:44s} bağlı kayıt: {ref or '—'} → taşınacağı süreç: {hedef.kod if hedef else '(yok)'}")
            if not ref:
                continue
            if not hedef:
                print("    !! bağlı kayıt var ama taşıma hedefi tanımsız — işlem atlandı"); continue
            if UYGULA:
                for t, c, n in ref:
                    if t == "process_kpis":                          # sıra no / kod yeniden numaralanır
                        mx = db.execute(text("SELECT COALESCE(MAX(sira_no),0) FROM process_kpis WHERE surec_id = :h"), {"h": hedef.id}).scalar()
                        for i, (kid,) in enumerate(db.execute(text("SELECT id FROM process_kpis WHERE surec_id = :e ORDER BY sira_no, id"), {"e": p.id}).fetchall(), 1):
                            db.execute(text("UPDATE process_kpis SET surec_id = :h, surec_kod = :k, sira_no = :s WHERE id = :i"), {"h": hedef.id, "k": hedef.kod, "s": mx + i, "i": kid})
                    else:
                        db.execute(text(f"UPDATE {t} SET {c} = :h WHERE {c} = :e"), {"h": hedef.id, "e": p.id})
        # eski süreçlere bağlı dokümanlar
        eid = [p.id for p in eski]
        docs = [d.id for d in db.query(Document).filter(Document.surec_id.in_(eid)).all()] if eid else []
        print(f"eski süreçlere bağlı doküman: {len(docs)}", "(silinecek)" if DOKUMAN_SIL else "(--dokuman-sil verilmedi: dokunulmaz; silinmeyen doküman varsa süreç silinemez)")
        if UYGULA and DOKUMAN_SIL and docs:
            n = bagli_sil(db, "documents", "id", docs)
            print("silinen doküman + bağlı kayıt:", n)
        kalan_doc = db.query(Document).filter(Document.surec_id.in_(eid)).count() if eid else 0
        if UYGULA:
            if kalan_doc:
                print("!! eski süreçlerde doküman kaldı; süreçler silinmedi (--dokuman-sil ile tekrar çalıştırın)")
            else:
                # alt süreci olan eski süreç varsa önce alt
                for p in sorted(eski, key=lambda x: x.ust_surec_id is None):
                    db.execute(text("DELETE FROM processes WHERE id = :i"), {"i": p.id})
                db.commit()
                print("eski süreçler silindi:", len(eski))
        else:
            print("\nKURU ÇALIŞMA — hiçbir şey değişmedi. Uygulamak için --uygula (önce yedek!).")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
