"""Staging (SQLite) → canlı veritabanı: YENİ MODÜL VERİLERİNİ anahtar eşleştirerek ekler.
Hedef: ortam değişkeni / .env ile gösterilen veritabanı (models.engine). Kaynak: --kaynak-db <staging .db kopyası>.

KULLANIM (sunucuda, .venv ile, ÖNCE DBKDYS TAM YEDEĞİ ve mssql_yeni_yapi.sql çalışmış olmalı):
  python scripts\\canli_veri_aktar.py --kaynak-db D:\\aktar\\dys_deneme.db                       → KURU ÇALIŞMA (yazar, sonra GERİ ALIR; sayım + sorun raporu)
  python scripts\\canli_veri_aktar.py --kaynak-db ... --grup dogrulama,yeniden,ppap,depo        → yalnız seçilen gruplar (varsayılan: hepsi)
  python scripts\\canli_veri_aktar.py --kaynak-db ... --uygula --onay-db DBKDYS                 → KALICI yazar (veritabanı adı doğrulanır)

Kurallar:
 • Mevcut kaydı ASLA ezmez/silmez; yalnız canlıda OLMAYAN kayıtları ekler (kaynak anahtarıyla). Tekrar çalıştırmak güvenlidir (ikinci çalıştırmada 0 ekler).
 • Üst kayıt canlıda zaten varsa alt kayıtlar da atlanır (karışık/yinelenen veri oluşmaz).
 • Kimlikler (id) canlıda yeniden üretilir; ilişkiler anahtarla eşlenir: kullanıcı=e-posta, ekipman=ekipman_no, ürün prosesi=ürün kodu,
   operasyon tanımı=ad, teknik resim=resim_no, dosya=sha256. Eşleşmeyen dış başvurular NULL olur ve raporlanır.
 • Test kaydı (ürün kodu 5555) aktarılmaz.
 • Her grup tek işlemde yazılır; hata olursa o grup geri alınır.
Gruplar: dogrulama (kart, oturum, ölçüm, görsel + görsellerin başvurduğu dosya_depo kayıtları) · yeniden (yeniden işlem + DF) · ppap (operasyon kütüphanesi, ürün prosesi, ağaç, adımlar, FMEA, KP, Valeo)
Sertifika / teknik resim / kalite görselleri bu betikle DEĞİL, scripts\dosya_depo_aktar.py ile (canlıdaki kayıtlara dosya bağlar). Doküman, onay, eski yapı: yeni_yapi_aktar.py ve diğer adım-7 betikleri.
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import create_engine, select, insert, update  # noqa: E402
import models  # noqa: E402

M = models.Base.metadata
T = M.tables
HEDEF = models.engine
SOHBET = []


def yaz(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    SOHBET.append(s)


class Say:
    def __init__(self):
        self.d = {}

    def art(self, tablo, ne, n=1):
        self.d.setdefault(tablo, {"eklendi": 0, "atlandi(mevcut)": 0, "NULL'lanan_baslik": 0})
        self.d[tablo][ne] = self.d[tablo].get(ne, 0) + n


SAY = Say()
UYARI = {}


def uyar(k, n=1):
    UYARI[k] = UYARI.get(k, 0) + n


def satirlar(kon, tablo, sirala=True):
    t = T[tablo]
    q = select(t)
    if sirala:
        q = q.order_by(t.c.id)
    return [dict(r._mapping) for r in kon.execute(q)]


def satir_yaz(kon, tablo, d):
    """id hariç ekler, yeni id döner"""
    t = T[tablo]
    veri = {k: v for k, v in d.items() if k != "id" and k in t.c}
    r = kon.execute(insert(t).values(veri))
    return r.inserted_primary_key[0]


def toplu_yaz(kon, tablo, liste):
    if not liste:
        return
    t = T[tablo]
    for i in range(0, len(liste), 500):
        kon.execute(insert(t), [{k: v for k, v in d.items() if k != "id" and k in t.c} for d in liste[i:i + 500]])


class Eslem:
    """kaynak-DB ve hedef-DB arasındaki anahtar eşlemeleri"""

    def __init__(self, ks, kh):
        ku = lambda kon: {(r["eposta"] or "").strip().lower(): r["id"] for r in satirlar(kon, "users", False) if r.get("eposta")}
        self.u_ks_id = {r["id"]: (r["eposta"] or "").strip().lower() for r in satirlar(ks, "users", False) if r.get("eposta")}
        self.u_hd = ku(kh)
        self.e_ks_id = {r["id"]: (r["ekipman_no"] or "").strip() for r in satirlar(ks, "calibration_equipment", False) if r.get("ekipman_no")}
        self.e_hd = {}
        for r in satirlar(kh, "calibration_equipment", False):
            if r.get("ekipman_no"):
                self.e_hd.setdefault(r["ekipman_no"].strip(), r["id"])
        self.tr_ks_id = {r["id"]: ((r["resim_no"] or "").strip(), (r["urun_kodu"] or "").strip()) for r in satirlar(ks, "teknik_resimler", False)}
        self.tr_hd = {(r["resim_no"] or "").strip(): r["id"] for r in satirlar(kh, "teknik_resimler", False)}

    def kullanici(self, kid, ctx):
        if kid is None:
            return None
        ep = self.u_ks_id.get(kid)
        h = self.u_hd.get(ep) if ep else None
        if h is None:
            uyar(f"{ctx}: kullanıcı eşleşmedi → NULL")
        return h

    def ekipman(self, eid, ctx):
        if eid is None:
            return None
        no = self.e_ks_id.get(eid)
        h = self.e_hd.get(no) if no else None
        if h is None:
            uyar(f"{ctx}: ekipman eşleşmedi → NULL")
        return h

    def teknik_resim(self, tid, ctx):
        if tid is None:
            return None
        no = (self.tr_ks_id.get(tid) or ("", ""))[0]
        h = self.tr_hd.get(no) if no else None
        if h is None:
            uyar(f"{ctx}: teknik resim eşleşmedi → NULL")
        return h


# ------------------------------------------------------------------ gruplar
def g_depo(ks, kh, E, depo_map, gerekli_idler=None):
    """dosya_depo: yalnız başvurulanlar (gerekli_idler) sha256 ile eklenir. depo_map: kaynak_id → hedef_id"""
    mevcut = {r[0]: r[1] for r in kh.execute(select(T["dosya_depo"].c.sha256, T["dosya_depo"].c.id))}
    t = T["dosya_depo"]
    ids = sorted(gerekli_idler or [])
    for i in ids:
        r = ks.execute(select(t).where(t.c.id == i)).first()
        if not r:
            uyar("depo: kaynakta dosya yok (depo_id=%s)" % i)
            continue
        d = dict(r._mapping)
        if d["sha256"] in mevcut:
            depo_map[i] = mevcut[d["sha256"]]
            SAY.art("dosya_depo", "atlandi(mevcut)")
            continue
        yeni = satir_yaz(kh, "dosya_depo", d)
        mevcut[d["sha256"]] = yeni
        depo_map[i] = yeni
        SAY.art("dosya_depo", "eklendi")


def g_dogrulama(ks, kh, E, depo_map):
    anahtar = lambda d: d.get("kaynak") or f"{d['tur']}|{d['grup']}|{d['no']}"
    mevcut = {anahtar(r) for r in satirlar(kh, "dogrulama_kartlari", False)}
    kart_map = {}
    for d in satirlar(ks, "dogrulama_kartlari"):
        if anahtar(d) in mevcut:
            SAY.art("dogrulama_kartlari", "atlandi(mevcut)")
            continue
        d2 = dict(d)
        d2["ekipman_id"] = E.ekipman(d["ekipman_id"], "dogrulama_kartlari.ekipman_id")
        kart_map[d["id"]] = satir_yaz(kh, "dogrulama_kartlari", d2)
        SAY.art("dogrulama_kartlari", "eklendi")
    oturum_map = {}
    for d in satirlar(ks, "dogrulama_oturumlari"):
        if d["kart_id"] not in kart_map:
            SAY.art("dogrulama_oturumlari", "atlandi(mevcut)")
            continue
        d2 = dict(d)
        d2["kart_id"] = kart_map[d["kart_id"]]
        d2["yapan_id"] = E.kullanici(d["yapan_id"], "dogrulama_oturumlari.yapan_id")
        oturum_map[d["id"]] = satir_yaz(kh, "dogrulama_oturumlari", d2)
        SAY.art("dogrulama_oturumlari", "eklendi")
    liste = []
    for d in satirlar(ks, "dogrulama_olcumleri"):
        if d["oturum_id"] not in oturum_map:
            SAY.art("dogrulama_olcumleri", "atlandi(mevcut)")
            continue
        d2 = dict(d)
        d2["oturum_id"] = oturum_map[d["oturum_id"]]
        liste.append(d2)
    toplu_yaz(kh, "dogrulama_olcumleri", liste)
    SAY.art("dogrulama_olcumleri", "eklendi", len(liste))
    gor = [d for d in satirlar(ks, "dogrulama_gorselleri") if d["kart_id"] in kart_map]
    g_depo(ks, kh, E, depo_map, {d["depo_id"] for d in gor if d.get("depo_id")})
    liste = []
    for d in gor:
        d2 = dict(d)
        d2["kart_id"] = kart_map[d["kart_id"]]
        if d.get("depo_id"):
            d2["depo_id"] = depo_map.get(d["depo_id"])
            if d2["depo_id"] is None:
                uyar("dogrulama_gorselleri: depo_id eşleşmedi → NULL")
        liste.append(d2)
    toplu_yaz(kh, "dogrulama_gorselleri", liste)
    SAY.art("dogrulama_gorselleri", "eklendi", len(liste))
    SAY.art("dogrulama_gorselleri", "atlandi(mevcut)", len(satirlar(ks, "dogrulama_gorselleri")) - len(gor))


def g_yeniden(ks, kh, E, depo_map):
    mevcut = {r["kaynak"] for r in satirlar(kh, "yeniden_islem_kayitlari", False) if r.get("kaynak")}
    liste = []
    for d in satirlar(ks, "yeniden_islem_kayitlari"):
        if d.get("kaynak") in mevcut:
            SAY.art("yeniden_islem_kayitlari", "atlandi(mevcut)")
            continue
        d2 = dict(d)
        if d.get("dof_id") is not None:
            uyar("yeniden_islem_kayitlari.dof_id: DÖF numaraları ortamlar arası eşleşmez → NULL")
        d2["dof_id"] = None
        d2["olusturan_id"] = E.kullanici(d["olusturan_id"], "yeniden_islem_kayitlari.olusturan_id")
        liste.append(d2)
    toplu_yaz(kh, "yeniden_islem_kayitlari", liste)
    SAY.art("yeniden_islem_kayitlari", "eklendi", len(liste))
    mevcut = {r["kaynak"] for r in satirlar(kh, "yeniden_islem_df", False) if r.get("kaynak")}
    liste = [d for d in satirlar(ks, "yeniden_islem_df") if d.get("kaynak") not in mevcut]
    toplu_yaz(kh, "yeniden_islem_df", liste)
    SAY.art("yeniden_islem_df", "eklendi", len(liste))
    SAY.art("yeniden_islem_df", "atlandi(mevcut)", len(satirlar(ks, "yeniden_islem_df", False)) - len(liste))


def g_ppap(ks, kh, E, depo_map):
    # 1) operasyon kütüphanesi
    tanim_h = {r["ad"]: r["id"] for r in satirlar(kh, "proses_adim_tanimlari", False)}
    tanim_map, tanim_yeni = {}, set()
    for d in satirlar(ks, "proses_adim_tanimlari"):
        if d["ad"] in tanim_h:
            tanim_map[d["id"]] = tanim_h[d["ad"]]
            SAY.art("proses_adim_tanimlari", "atlandi(mevcut)")
        else:
            tanim_map[d["id"]] = satir_yaz(kh, "proses_adim_tanimlari", d)
            tanim_yeni.add(d["id"])
            SAY.art("proses_adim_tanimlari", "eklendi")
    # 2) ürün prosesleri
    mevcut_urun = {(r["urun_kodu"] or "").strip() for r in satirlar(kh, "urun_prosesleri", False)}
    up_map = {}
    for d in satirlar(ks, "urun_prosesleri"):
        kod = (d["urun_kodu"] or "").strip()
        if kod == "5555":
            continue
        if kod in mevcut_urun:
            SAY.art("urun_prosesleri", "atlandi(mevcut)")
            continue
        d2 = dict(d)
        d2["teknik_resim_id"] = E.teknik_resim(d["teknik_resim_id"], "urun_prosesleri.teknik_resim_id")
        d2["olusturan_id"] = E.kullanici(d["olusturan_id"], "urun_prosesleri.olusturan_id")
        up_map[d["id"]] = satir_yaz(kh, "urun_prosesleri", d2)
        SAY.art("urun_prosesleri", "eklendi")
    # 3) ürün ağacı (kendine başvuru: önce ust_id NULL, sonra güncelle)
    dugum_map, bekle = {}, []
    for d in satirlar(ks, "urun_agac_dugumleri"):
        if d["urun_proses_id"] not in up_map:
            continue
        d2 = dict(d)
        d2["urun_proses_id"] = up_map[d["urun_proses_id"]]
        d2["ust_id"] = None
        dugum_map[d["id"]] = satir_yaz(kh, "urun_agac_dugumleri", d2)
        if d["ust_id"] is not None:
            bekle.append((d["id"], d["ust_id"]))
        SAY.art("urun_agac_dugumleri", "eklendi")
    for kid, ust in bekle:
        if ust in dugum_map:
            kh.execute(update(T["urun_agac_dugumleri"]).where(T["urun_agac_dugumleri"].c.id == dugum_map[kid]).values(ust_id=dugum_map[ust]))
    # 4) ürün proses adımları
    adim_map = {}
    for d in satirlar(ks, "urun_proses_adimlari"):
        if d["urun_proses_id"] not in up_map:
            continue
        d2 = dict(d)
        d2["urun_proses_id"] = up_map[d["urun_proses_id"]]
        d2["adim_tanim_id"] = tanim_map.get(d["adim_tanim_id"])
        d2["dugum_id"] = dugum_map.get(d["dugum_id"]) if d.get("dugum_id") is not None else None
        adim_map[d["id"]] = satir_yaz(kh, "urun_proses_adimlari", d2)
        SAY.art("urun_proses_adimlari", "eklendi")
    # 5) FMEA satırları: ürüne bağlı olanlar + YENİ eklenen operasyon tanımlarının kütüphane satırları
    fmea_map = {}
    for d in satirlar(ks, "proses_fmea_satirlari"):
        if d["urun_proses_id"] is not None:
            if d["urun_proses_id"] not in up_map:
                continue
            hedef_up = up_map[d["urun_proses_id"]]
        else:
            if d["adim_tanim_id"] not in tanim_yeni:
                SAY.art("proses_fmea_satirlari", "atlandi(mevcut)")
                continue
            hedef_up = None
        d2 = dict(d)
        d2["urun_proses_id"] = hedef_up
        d2["adim_tanim_id"] = tanim_map.get(d["adim_tanim_id"])
        fmea_map[d["id"]] = satir_yaz(kh, "proses_fmea_satirlari", d2)
        SAY.art("proses_fmea_satirlari", "eklendi")
    # 6) kontrol planı satırları (fmea_satir_id / kaynak_id eşlemesi ikinci geçişte)
    kp_map, bekle = {}, []
    for d in satirlar(ks, "proses_kp_satirlari"):
        if d["urun_proses_id"] is not None:
            if d["urun_proses_id"] not in up_map:
                continue
            hedef_up = up_map[d["urun_proses_id"]]
        else:
            if d["adim_tanim_id"] not in tanim_yeni:
                SAY.art("proses_kp_satirlari", "atlandi(mevcut)")
                continue
            hedef_up = None
        d2 = dict(d)
        d2["urun_proses_id"] = hedef_up
        d2["adim_tanim_id"] = tanim_map.get(d["adim_tanim_id"])
        d2["fmea_satir_id"] = None
        d2["kaynak_id"] = None
        kp_map[d["id"]] = satir_yaz(kh, "proses_kp_satirlari", d2)
        bekle.append((d["id"], d.get("fmea_satir_id"), d.get("kaynak_id")))
        SAY.art("proses_kp_satirlari", "eklendi")
    for kid, f, k in bekle:
        v = {}
        if f is not None and f in fmea_map:
            v["fmea_satir_id"] = fmea_map[f]
        if k is not None and k in kp_map:
            v["kaynak_id"] = kp_map[k]
        if v:
            kh.execute(update(T["proses_kp_satirlari"]).where(T["proses_kp_satirlari"].c.id == kp_map[kid]).values(**v))
    # 7) ölçüm kayıtları / PPAP revizyonları (staging'de boşsa 0)
    for tablo, kolonlar in (("urun_ppap_revizyonlari", ("olusturan_id",)),):
        for d in satirlar(ks, tablo):
            if d["urun_proses_id"] not in up_map:
                continue
            d2 = dict(d)
            d2["urun_proses_id"] = up_map[d["urun_proses_id"]]
            for k in kolonlar:
                d2[k] = E.kullanici(d[k], f"{tablo}.{k}")
            satir_yaz(kh, tablo, d2)
            SAY.art(tablo, "eklendi")
    if satirlar(ks, "proses_olcum_kayitlari"):
        uyar("proses_olcum_kayitlari: kaynakta kayıt VAR ama bu betik aktarmaz (ölçüm girişi canlıda yapılır)")


GRUPLAR = {"dogrulama": g_dogrulama, "yeniden": g_yeniden, "ppap": g_ppap}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kaynak-db", required=True, help="staging SQLite dosyası (kopya)")
    ap.add_argument("--grup", default="dogrulama,yeniden,ppap")
    ap.add_argument("--uygula", action="store_true")
    ap.add_argument("--onay-db", default="", help="--uygula için: hedef veritabanı adını aynen yazın (örn. DBKDYS)")
    ap.add_argument("--izin-sqlite", action="store_true", help="yalnız test: hedef SQLite olabilir")
    a = ap.parse_args()
    if not os.path.exists(a.kaynak_db):
        sys.exit("Kaynak DB yok: " + a.kaynak_db)
    if HEDEF.dialect.name != "mssql" and not a.izin_sqlite:
        sys.exit(f"Hedef '{HEDEF.dialect.name}' (SQL Server değil). .env / DYS_DATABASE_URL canlıyı göstermiyor olabilir. Durduruldu.")
    hedef_ad = HEDEF.url.database or ""
    if HEDEF.dialect.name == "mssql":  # odbc_connect URL'lerinde url.database boş olabilir → gerçek adı sunucudan al
        from sqlalchemy import text
        with HEDEF.connect() as _k:
            hedef_ad = _k.execute(text("SELECT DB_NAME()")).scalar() or hedef_ad
    yaz(f"Hedef: {HEDEF.dialect.name} · veritabanı={hedef_ad} · host={HEDEF.url.host}")
    yaz(f"Kaynak: {a.kaynak_db}")
    if a.uygula and a.onay_db.strip().lower() != hedef_ad.lower():
        sys.exit(f"--uygula için --onay-db {hedef_ad} verin (yanlışlıkla başka veritabanına yazmayı önler).")
    kaynak = create_engine("sqlite:///" + a.kaynak_db.replace("\\", "/"))
    istenen = [g.strip() for g in a.grup.split(",") if g.strip()]
    depo_map = {}
    for g in istenen:
        if g not in GRUPLAR:
            sys.exit("Bilinmeyen grup: " + g)
    for g in [x for x in istenen if x in GRUPLAR]:
        yaz(f"\n=== {g} ===")
        with kaynak.connect() as ks:
            with HEDEF.connect() as kh:
                tr = kh.begin()
                try:
                    E = Eslem(ks, kh)
                    GRUPLAR[g](ks, kh, E, depo_map)
                    if a.uygula:
                        tr.commit()
                        yaz(f"{g}: KALICI YAZILDI")
                    else:
                        tr.rollback()
                        yaz(f"{g}: KURU ÇALIŞMA — geri alındı (hiçbir şey değişmedi)")
                except Exception as e:
                    tr.rollback()
                    yaz(f"{g}: HATA → grup geri alındı: {type(e).__name__}: {str(e)[:600]}")
                    SAY.art(g, "HATA")
    yaz("\n=== ÖZET ===")
    for t, v in SAY.d.items():
        yaz(f"{t:30s} " + " · ".join(f"{k}={n}" for k, n in v.items() if n))
    if UYARI:
        yaz("\n=== UYARILAR (eşleşmeyen başvurular NULL yapıldı) ===")
        for k, n in sorted(UYARI.items()):
            yaz(f"{n:6d} × {k}")
    ad = f"canli_veri_aktar_rapor_{datetime.now():%Y%m%d_%H%M%S}.txt"
    open(ad, "w", encoding="utf-8").write("\n".join(SOHBET))
    yaz(f"\nRapor: {os.path.abspath(ad)}")
    if not a.uygula:
        yaz("KURU ÇALIŞMA — kalıcı değişiklik yok. Uygulamak için: --uygula --onay-db " + hedef_ad)


if __name__ == "__main__":
    main()
