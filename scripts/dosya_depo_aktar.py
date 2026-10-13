"""Mevcut yüklenmiş dosyaları (kalibrasyon sertifikaları, teknik resim + ekleri, doğrulama görselleri, kalite görselleri) veritabanındaki dosya deposuna alır.
KULLANIM:  python scripts/dosya_depo_aktar.py          → depo_id'si boş olanlar (tekrar çalıştırılabilir)
           python scripts/dosya_depo_aktar.py --kuru   → sayar, yazmaz
Ortam: DYS_DATABASE_URL, DYS_UPLOAD_FOLDER (dosyalar bu klasörden okunur). Kaynak dosyalara dokunmaz; aynı içerik bir kez saklanır (SHA-256)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import SessionLocal, KalibrasyonSertifika, TeknikResimRevizyon, KaliteGorsel, DogrulamaGorsel, DosyaDepo  # noqa: E402
from helpers import resolve_file_path  # noqa: E402
import dosya_depo as DD  # noqa: E402


def yol_bul(p, ad):
    try:
        y = resolve_file_path(p, ad)
    except Exception:  # noqa: BLE001
        y = None
    if y and os.path.isfile(y):
        return y
    return p if p and os.path.isfile(p) else None


def main():
    kuru = "--kuru" in sys.argv
    db = SessionLocal()
    try:
        say = {"yeni": 0, "yok": 0, "buyuk": 0, "zaten": 0}

        def isle(ad, nesne, alan_yol="dosya_yolu", alan_ad="dosya_adi"):
            if getattr(nesne, "depo_id", None):
                say["zaten"] += 1
                return
            y = yol_bul(getattr(nesne, alan_yol, None), getattr(nesne, alan_ad, None))
            if not y:
                say["yok"] += 1
                return
            if kuru:
                say["yeni"] += 1
                return
            i = DD.ekle(db, yol=y, ad=getattr(nesne, alan_ad, None) or os.path.basename(y))
            if i:
                nesne.depo_id = i
                say["yeni"] += 1
            else:
                say["buyuk"] += 1

        for ad, M in (("sertifika", KalibrasyonSertifika), ("teknik resim", TeknikResimRevizyon), ("doğrulama görseli", DogrulamaGorsel), ("kalite görseli", KaliteGorsel)):
            adet0 = dict(say)
            for n, x in enumerate(db.query(M).all(), 1):
                isle(ad, x)
                if n % 200 == 0 and not kuru:
                    db.commit()
            if not kuru:
                db.commit()
            print(f"{ad}: yeni {say['yeni'] - adet0['yeni']} · zaten {say['zaten'] - adet0['zaten']} · dosya yok {say['yok'] - adet0['yok']} · atlanan(büyük) {say['buyuk'] - adet0['buyuk']}", flush=True)
        # teknik resim ek dosyaları (JSON içinde)
        n_ek = 0
        for r in db.query(TeknikResimRevizyon).filter(TeknikResimRevizyon.ek_dosyalar_json.isnot(None)).all():
            try:
                ekler = json.loads(r.ek_dosyalar_json) or []
            except ValueError:
                continue
            degisti = False
            for e in ekler:
                if e.get("depo_id"):
                    continue
                y = yol_bul(e.get("yol"), e.get("ad"))
                if not y:
                    continue
                if not kuru:
                    i = DD.ekle(db, yol=y, ad=e.get("ad"))
                    if i:
                        e["depo_id"] = i; degisti = True
                n_ek += 1
            if degisti:
                r.ek_dosyalar_json = json.dumps(ekler, ensure_ascii=False)
        if not kuru:
            db.commit()
        print("teknik resim ek dosyası:", n_ek)
        if not kuru:
            toplam = db.query(DosyaDepo).count()
            mb = sum(b for (b,) in db.query(DosyaDepo.boyut).all()) / 1048576
            print(f"depo: {toplam} tekil dosya · {mb:.0f} MB")
    finally:
        db.close()


if __name__ == "__main__":
    main()
