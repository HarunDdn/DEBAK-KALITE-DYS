"""audit_logs hash zincirini veritabanında SAKLANAN değerlerle yeniden hesaplar (yeniden mühürleme).

Neden: SQLite → MSSQL aktarımında ve eski log_action'da saat mikrosaniyeyle hash'lenip
DATETIME (3 ms) olarak saklandığı için /admin/audit-integrity kayıtları bozuk gösteriyordu.
Kayıt içeriği DEĞİŞMEZ; yalnız onceki_hash / kayit_hash güncellenir. Sona, işlemi belgeleyen
hash'li bir "Zincir Mühürleme" kaydı eklenir.

KULLANIM (debakdys klasöründe; hedef .env'deki veritabanı veya --db):
  python scripts\\audit_zincir_muhurle.py --db DBKDYS_YENI                          → KURU ÇALIŞMA (sayar)
  python scripts\\audit_zincir_muhurle.py --db DBKDYS_YENI --uygula --onay DBKDYS_YENI
Uygulama durdurulmuşken çalıştırın (çalışırken yazılan log zincire yarış koşulu yaratır).
"""
import argparse
import hashlib
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from config import Config  # noqa: E402


def _audit_record_hash(onceki_hash, kullanici_id, islem_tipi, document_id, detay, tarih):
    """app._audit_record_hash ile birebir aynı."""
    ham = f"{onceki_hash or ''}|{kullanici_id}|{islem_tipi}|{document_id}|{detay or ''}|{tarih.isoformat()}"
    return hashlib.sha256(ham.encode("utf-8")).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", help="Veritabanı adı (verilmezse .env)")
    p.add_argument("--uygula", action="store_true")
    p.add_argument("--onay", help="--uygula için veritabanı adı")
    a = p.parse_args()

    url = make_url(Config.DATABASE_URL)
    if a.db:
        url = url.set(database=a.db)
    db_adi = url.database
    if a.uygula and a.onay != db_adi:
        sys.exit(f"[DUR] --uygula için --onay {db_adi} gerekli.")
    eng = create_engine(url, fast_executemany=url.drivername.startswith("mssql"))

    with eng.connect() as c:
        tx = c.begin()
        rows = c.execute(text("SELECT id, kullanici_id, islem_tipi, document_id, detay, tarih, onceki_hash, kayit_hash "
                              "FROM audit_logs ORDER BY id")).fetchall()
        onceki, bozuk, guncel = None, 0, []
        for r in rows:
            if not r.kayit_hash:          # hash'siz eski kayıt: doğrulayıcı da atlar
                continue
            beklenen = _audit_record_hash(onceki, r.kullanici_id, r.islem_tipi, r.document_id, r.detay, r.tarih)
            if r.onceki_hash != onceki or r.kayit_hash != beklenen:
                bozuk += 1
                guncel.append({"i": r.id, "o": onceki, "k": beklenen})
            onceki = beklenen
        print(f"[..] {db_adi}: {len(rows)} kayıt, yeniden hesaplanacak {bozuk}")
        if not guncel:
            tx.rollback()
            print("[OK] Zincir zaten sağlam.")
            return

        for i in range(0, len(guncel), 2000):
            c.execute(text("UPDATE audit_logs SET onceki_hash = :o, kayit_hash = :k WHERE id = :i"), guncel[i:i + 2000])

        tarih = datetime.now().replace(microsecond=0)
        detay = (f"Zincir Mühürleme: {bozuk} kaydın hash'i saklanan değerlerle yeniden hesaplandı "
                 f"(SQLite→MSSQL DATETIME yuvarlaması). İçerik değişmedi.")
        c.execute(text("INSERT INTO audit_logs (kullanici_id, islem_tipi, document_id, detay, ip_adresi, tarih, "
                       "onceki_hash, kayit_hash) VALUES (NULL, :t, NULL, :d, NULL, :ta, :o, :k)"),
                  {"t": "Sistem", "d": detay, "ta": tarih, "o": onceki,
                   "k": _audit_record_hash(onceki, None, "Sistem", None, detay, tarih)})
        if a.uygula:
            tx.commit()
            print(f"[OK] Yazıldı: {bozuk} kayıt güncellendi, mühürleme kaydı eklendi.")
        else:
            tx.rollback()
            print(f"[OK] KURU ÇALIŞMA, geri alındı. Yazmak için: --uygula --onay {db_adi}")


if __name__ == "__main__":
    main()
