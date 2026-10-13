"""Eski canlı DBKDYS → yeni DBKDYS_YENI: seçilmiş kayıtları anahtar eşleştirerek ekler.
Aynı SQL Server üzerinde iki veritabanı; bağlantı bilgisi .env (DYS_MSSQL_*) dosyasından.

KULLANIM (debakdys klasöründe):
  python scripts\\eski_canli_aktar.py                                   → KURU ÇALIŞMA (yazar, sayar, GERİ ALIR)
  python scripts\\eski_canli_aktar.py --uygula --onay DBKDYS_YENI       → KALICI yazar

Aktarılan (2026-10-09 kararı):
 • ISO 27001 SoA kontrolleri (kontrol_no ile)
 • Standart sürümleri, gereklilikler ve gereklilik durumları (standart+sürüm, madde_no ile)
 • Doğrulama kartları (tür+grup+no ile)
 • İşlem kayıtları (audit_logs): içerik aynen; yeni hash zincirinin sonuna eklenir, hash'ler yeniden hesaplanır
Aktarılmayan: eski yapı dokümanları, Temmuz deneme DÖF/QRQC/FMEA/risk/denetim kayıtları.

Kurallar:
 • Mevcut kaydı ezmez; tekrar çalıştırmak güvenlidir (yalnız yeni olanları ekler).
 • Kimlikler yeniden üretilir; kullanıcı=e-posta, doküman=doküman no (yazım farkı yok sayılır). Eşleşmeyen → NULL.
 • Eski veritabanı VARCHAR olduğu için Türkçe harfler bozuk gelir (ş→s, ı→i...). Sabit listeli alanlar
   (durumlar, gereklilik başlıkları) kanonik değere düzeltilir; serbest metin ve log içeriği değiştirilmez.
 • Tek transaction: hata olursa hiçbir şey yazılmaz.
"""
import argparse
import hashlib
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from config import Config  # noqa: E402
import models  # noqa: E402

TR =str.maketrans("şŞıİğĞüÜöÖçÇ", "sSiIgGuUoOcC")
UYARI = {}
SAY = {}


def _audit_record_hash(onceki_hash, kullanici_id, islem_tipi, document_id, detay, tarih):
    """app._audit_record_hash ile birebir aynı (app'i import etmek başlangıç işlerini tetikler)."""
    ham = f"{onceki_hash or ''}|{kullanici_id}|{islem_tipi}|{document_id}|{detay or ''}|{tarih.isoformat()}"
    return hashlib.sha256(ham.encode("utf-8")).hexdigest()


def katla(s):
    return re.sub(r"[\s\-_]+", " ", str(s).translate(TR)).strip().lower() if s is not None else None


def uyar(k, n=1):
    UYARI[k] = UYARI.get(k, 0) + n


def say(tablo, ne, n=1):
    SAY.setdefault(tablo, {"eklendi": 0, "atlandı(mevcut)": 0, "düzeltilen değer": 0})
    SAY[tablo][ne] += n


def duzelt(deger, adaylar, tablo):
    """Bozuk Türkçe değeri, katlanmış hali tek bir adayla eşleşiyorsa kanonik değere çevirir."""
    if not isinstance(deger, str):
        return deger
    es = [a for a in adaylar if katla(a) == katla(deger)]
    if len(es) == 1 and es[0] != deger:
        say(tablo, "düzeltilen değer")
        return es[0]
    return deger


def satirlar(kon, tablo, where=""):
    return [dict(r._mapping) for r in kon.execute(text(f"SELECT * FROM [{tablo}] {where} ORDER BY id"))]


def ekle(kh, tablo, d):
    d = {k: v for k, v in d.items() if k != "id"}
    cols = ", ".join(f"[{k}]" for k in d)
    vals = ", ".join(f":{k}" for k in d)
    return kh.execute(text(f"INSERT INTO [{tablo}] ({cols}) OUTPUT INSERTED.id VALUES ({vals})"), d).scalar()


def dagilim(kh, tablo, kolon):
    return [r[0] for r in kh.execute(text(f"SELECT DISTINCT [{kolon}] FROM [{tablo}] WHERE [{kolon}] IS NOT NULL"))]


class Eslem:
    def __init__(self, ks, kh):
        self.u_eski = {r["id"]: (r["eposta"] or "").strip().lower() for r in satirlar(ks, "users")}
        self.u_yeni = {(r["eposta"] or "").strip().lower(): r["id"] for r in satirlar(kh, "users")}
        self.d_eski = {r[0]: katla(r[1]) for r in ks.execute(text("SELECT id, dokuman_no FROM documents"))}
        self.d_yeni = {katla(r[1]): r[0] for r in kh.execute(text("SELECT id, dokuman_no FROM documents"))}

    def kullanici(self, kid, ctx):
        if kid is None:
            return None
        h = self.u_yeni.get(self.u_eski.get(kid))
        if h is None:
            uyar(f"{ctx}: kullanıcı eşleşmedi → NULL")
        return h

    def dokuman(self, did, ctx):
        if did is None:
            return None
        h = self.d_yeni.get(self.d_eski.get(did))
        if h is None:
            uyar(f"{ctx}: doküman yeni yapıda yok → NULL (detay metni korunur)")
        return h


# ------------------------------------------------------------------ gruplar
def g_soa(ks, kh, E):
    mevcut = {katla(r[0]) for r in kh.execute(text("SELECT kontrol_no FROM soa_controls"))}
    for d in satirlar(ks, "soa_controls"):
        if katla(d["kontrol_no"]) in mevcut:
            say("soa_controls", "atlandı(mevcut)")
            continue
        d["uygulama_durumu"] = duzelt(d["uygulama_durumu"], models.SOA_DURUMLARI, "soa_controls")
        d["kontrol_sahibi_id"] = E.kullanici(d["kontrol_sahibi_id"], "soa_controls.kontrol_sahibi_id")
        ekle(kh, "soa_controls", d)
        say("soa_controls", "eklendi")


def g_gereklilik(ks, kh, E):
    ed_yeni = {(katla(r["standart"]), katla(r["surum"])): r["id"] for r in satirlar(kh, "standard_editions")}
    ed_map, ed_std = {}, {}
    for d in satirlar(ks, "standard_editions"):
        std = duzelt(d["standart"], models.STANDART_MADDELERI.keys(), "standard_editions")
        ed_std[d["id"]] = std
        k = (katla(std), katla(d["surum"]))
        if k in ed_yeni:
            ed_map[d["id"]] = ed_yeni[k]
            say("standard_editions", "atlandı(mevcut)")
            continue
        d["standart"] = std
        d["aciklama"] = duzelt(d["aciklama"], [f"{std}:{d['surum']} yapısal referans"], "standard_editions")
        ed_map[d["id"]] = ed_yeni[k] = ekle(kh, "standard_editions", d)
        say("standard_editions", "eklendi")

    req_yeni = {(r["edition_id"], katla(r["madde_no"])): r["id"] for r in satirlar(kh, "requirements")}
    req_map = {}
    for d in satirlar(ks, "requirements"):
        hedef_ed = ed_map.get(d["edition_id"])
        k = (hedef_ed, katla(d["madde_no"]))
        if k in req_yeni:
            req_map[d["id"]] = req_yeni[k]
            say("requirements", "atlandı(mevcut)")
            continue
        basliklar = [b for m, b in models.STANDART_MADDELERI.get(ed_std.get(d["edition_id"]), []) if m == d["madde_no"]]
        d["baslik"] = duzelt(d["baslik"], basliklar, "requirements")
        d["edition_id"] = hedef_ed
        req_map[d["id"]] = req_yeni[k] = ekle(kh, "requirements", d)
        say("requirements", "eklendi")

    rs_yeni = {r[0] for r in kh.execute(text("SELECT requirement_id FROM requirement_status"))}
    for d in satirlar(ks, "requirement_status"):
        rid = req_map.get(d["requirement_id"])
        if rid is None or rid in rs_yeni:
            say("requirement_status", "atlandı(mevcut)")
            continue
        d["requirement_id"] = rid
        d["uygulanabilirlik"] = duzelt(d["uygulanabilirlik"], models.IMS_UYGULANABILIRLIK, "requirement_status")
        d["uygulama_durumu"] = duzelt(d["uygulama_durumu"], models.IMS_UYGULAMA_DURUMLARI, "requirement_status")
        d["etkinlik"] = duzelt(d["etkinlik"], models.IMS_ETKINLIK_DURUMLARI, "requirement_status")
        d["sorumlu_id"] = E.kullanici(d["sorumlu_id"], "requirement_status.sorumlu_id")
        ekle(kh, "requirement_status", d)
        rs_yeni.add(rid)
        say("requirement_status", "eklendi")


def g_dogrulama_kart(ks, kh, E):
    anahtar = lambda d: (katla(d["tur"]), katla(d["grup"]), katla(d["no"]))
    mevcut = {anahtar(d) for d in satirlar(kh, "dogrulama_kartlari")}
    ekipman_yeni = {katla(r[1]): r[0] for r in kh.execute(text("SELECT id, ekipman_no FROM calibration_equipment"))}
    ekipman_eski = {r[0]: katla(r[1]) for r in ks.execute(text("SELECT id, ekipman_no FROM calibration_equipment"))}
    secimli = ("tur", "grup", "durum", "ortam", "yontem", "kullanim_yeri", "dogrulama_ay", "kalibrasyon_siklik")
    adaylar = {k: dagilim(kh, "dogrulama_kartlari", k) for k in secimli}
    for d in satirlar(ks, "dogrulama_kartlari"):
        if anahtar(d) in mevcut:
            say("dogrulama_kartlari", "atlandı(mevcut)")
            continue
        for k in secimli:
            d[k] = duzelt(d[k], adaylar[k], "dogrulama_kartlari")
        if d["ekipman_id"] is not None:
            d["ekipman_id"] = ekipman_yeni.get(ekipman_eski.get(d["ekipman_id"]))
            if d["ekipman_id"] is None:
                uyar("dogrulama_kartlari.ekipman_id: ekipman eşleşmedi → NULL")
        ekle(kh, "dogrulama_kartlari", d)
        mevcut.add(anahtar(d))
        say("dogrulama_kartlari", "eklendi")


def g_loglar(ks, kh, E):
    """İçerik aynen; yeni zincirin sonuna eklenir ve hash'ler yeniden hesaplanır (bkz. app._audit_record_hash)."""
    anahtar = lambda tarih, tip, detay, eposta: (tarih, katla(tip), detay or "", eposta or "")
    u_yeni_ters = {v: k for k, v in E.u_yeni.items()}
    mevcut = {anahtar(r[0], r[1], r[2], u_yeni_ters.get(r[3]))
              for r in kh.execute(text("SELECT tarih, islem_tipi, detay, kullanici_id FROM audit_logs"))}
    son = kh.execute(text("SELECT TOP 1 kayit_hash FROM audit_logs ORDER BY id DESC")).scalar()
    for d in satirlar(ks, "audit_logs"):
        if anahtar(d["tarih"], d["islem_tipi"], d["detay"], E.u_eski.get(d["kullanici_id"])) in mevcut:
            say("audit_logs", "atlandı(mevcut)")
            continue
        d["kullanici_id"] = E.kullanici(d["kullanici_id"], "audit_logs.kullanici_id")
        d["document_id"] = E.dokuman(d["document_id"], "audit_logs.document_id")
        d["onceki_hash"] = son
        d["kayit_hash"] = son = _audit_record_hash(son, d["kullanici_id"], d["islem_tipi"],
                                                   d["document_id"], d["detay"], d["tarih"])
        ekle(kh, "audit_logs", d)
        say("audit_logs", "eklendi")


GRUPLAR = [("SoA", g_soa), ("Gereklilikler", g_gereklilik), ("Doğrulama kartı", g_dogrulama_kart),
           ("İşlem kayıtları", g_loglar)]


def main():
    p = argparse.ArgumentParser(description="Eski canlı DBKDYS → DBKDYS_YENI seçilmiş kayıt aktarımı")
    p.add_argument("--kaynak-db", default="DBKDYS")
    p.add_argument("--hedef-db", default="DBKDYS_YENI")
    p.add_argument("--uygula", action="store_true", help="Kalıcı yaz (yoksa yazıp geri alır)")
    p.add_argument("--onay", help="--uygula için hedef veritabanı adı")
    a = p.parse_args()
    if a.uygula and a.onay != a.hedef_db:
        sys.exit(f"[DUR] --uygula için --onay {a.hedef_db} gerekli.")
    if a.kaynak_db == a.hedef_db:
        sys.exit("[DUR] Kaynak ve hedef aynı veritabanı.")

    url = make_url(Config.DATABASE_URL)
    if not url.drivername.startswith("mssql"):
        sys.exit("[HATA] .env MSSQL göstermiyor.")
    kaynak = create_engine(url.set(database=a.kaynak_db))
    hedef = create_engine(url.set(database=a.hedef_db))
    print(f"[..] Kaynak: {url.host}/{a.kaynak_db}  →  Hedef: {url.host}/{a.hedef_db}  "
          f"({'UYGULA' if a.uygula else 'KURU ÇALIŞMA: sonunda geri alınır'})")

    with kaynak.connect() as ks, hedef.connect() as kh:
        tx = kh.begin()
        kh.execute(text("SET XACT_ABORT ON"))
        E = Eslem(ks, kh)
        for ad, fn in GRUPLAR:
            print(f"[..] {ad}")
            fn(ks, kh, E)
        for tablo, s in SAY.items():
            print(f"  {tablo:22} " + "  ".join(f"{k}={v}" for k, v in s.items()))
        for k, v in sorted(UYARI.items()):
            print(f"  [uyarı] {k}: {v}")
        if a.uygula:
            tx.commit()
            print(f"[OK] Yazıldı ({datetime.now():%Y-%m-%d %H:%M}).")
        else:
            tx.rollback()
            print("[OK] KURU ÇALIŞMA bitti, geri alındı. Kalıcı yazmak için: --uygula --onay " + a.hedef_db)


if __name__ == "__main__":
    main()
