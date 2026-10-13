"""
DYS — İç denetim otomasyonu (2026)
===================================
Y01.6 F06 İç & Dış Denetim Planı + Y01.6 F01 süreç soru listeleri + iç denetçi yetkinlikleri.

"Kendi kendine denetim" döngüsü (günlük zamanlayıcı ya da 'Şimdi çalıştır'):
  1) Plan kalemi tarihi yaklaşınca (varsayılan 14 gün önce) iç tetkik kaydı OTOMATİK açılır,
     doğru soru listesi (11 süreçlik yapı) bağlanır.
  2) Denetim ekibi plandaki denetçi kodlarından atanır; yetkin değilse ya da kendi sürecini
     denetleyecekse (bağımsızlık — IATF 16949 § 9.2.2.1 / ISO 19011) yetkin + bağımsız + yükü en az
     denetçiyle değiştirilir.
  3) Soru listesinde 'Uygunsuz' / 'Kısmen Uygun' işaretlenen her soru otomatik bulgu olur;
     denetim 'Tamamlandı' olunca uygunsuzluk bulguları için DÖF otomatik açılır.
  4) Hatırlatmalar: yaklaşan / geciken denetim, açılmamış plan kalemi, denetçisi olmayan kalem,
     DÖF'süz uygunsuzluk, sonuç özeti eksik, denetçi yetkinlik süresi, planda olmayan süreç.
Hiçbir adım eski kayıtları silmez; ayarlar 'sistem_ayarlari' tablosundadır.
"""
import json
import logging
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta

logger = logging.getLogger("dys")

AYAR_VARSAYILAN = {
    "denetim_otomatik": "1",        # plandan otomatik tetkik aç
    "denetim_gun_once": "14",       # plan tarihinden kaç gün önce açılsın
    "denetim_dof_otomatik": "1",    # tamamlanan denetimde uygunsuzluk → DÖF
    "denetim_hatirlatma_gun": "7",  # yaklaşan denetim uyarısı (gün)
    "denetim_eposta": "1",          # SMTP tanımlıysa e-posta gönder
    # "denetim_baslangic": ilk plan yüklemesinde bugünün tarihi yazılır; bundan önceki kalemler (kâğıtta yapılmış
    # denetimler) otomatik açılmaz, yalnız bilgi olarak gösterilir ve istenirse elle açılır.
}


def baslangic(db):
    v = ayar(db, "denetim_baslangic")
    try:
        return datetime.strptime(v, "%Y-%m-%d").date() if v else None
    except ValueError:
        return None
IC_KATEGORILER = ("Süreç", "Saha", "Proses", "Ürün")   # DYS'nin kendisinin açtığı denetimler
TUR_HARF = {"Süreç": "S", "Saha": "S", "Proses": "P", "Ürün": "Ü"}
# süreç → ek aranan nitelik (standart özel)
SUREC_NITELIK = {"D05": "iso14001", "D06": "iso27001"}
# kullanıcı departmanı → sahibi olduğu süreç (bağımsızlık kontrolü; denetçi kaydında elle düzeltilebilir)
DEPARTMAN_SUREC = [
    ("insan kaynak", "D01"), ("isg", "D01"), ("iş sağlığı", "D01"), ("satın", "D02"), ("satin", "D02"), ("tedarik", "D02"),
    ("bakım", "D03"), ("bakim", "D03"), ("kalıphane", "D03"), ("kalite", "D04"), ("laboratuvar", "D04"), ("metroloji", "D04"),
    ("çevre", "D05"), ("bilgi işlem", "D06"), ("bilgi islem", "D06"), ("bt", "D06"), ("it", "D06"),
    ("satış", "M01"), ("satis", "M01"), ("proje", "M02"), ("ar-ge", "M02"), ("arge", "M02"), ("mühendislik", "M02"),
    ("üretim", "M03"), ("uretim", "M03"), ("enjeksiyon", "M03"), ("planlama", "M04"), ("sevkiyat", "M04"), ("depo", "M04"),
    ("dış ticaret", "M04"), ("genel müdür", "Y01"), ("yönetim", "Y01"),
]
AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


# ─────────────────────────── yardımcılar ───────────────────────────
def ayar(db, anahtar):
    from models import SistemAyar
    kayit = db.get(SistemAyar, anahtar)
    return kayit.deger if kayit and kayit.deger is not None else AYAR_VARSAYILAN.get(anahtar)


def ayar_yaz(db, anahtar, deger):
    from models import SistemAyar
    kayit = db.get(SistemAyar, anahtar)
    if kayit:
        kayit.deger = str(deger); kayit.guncelleme_tarihi = datetime.now()
    else:
        db.add(SistemAyar(anahtar=anahtar, deger=str(deger), guncelleme_tarihi=datetime.now()))


def _norm(s):
    s = (s or "").replace("İ", "i").replace("I", "ı").lower()
    s = unicodedata.normalize("NFKD", s.replace("ı", "i"))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z ]+", " ", s).split()


def kullanici_bul(db, ad):
    """'F.Ünal', 'Güllü İNCİ', 'Fevzi ÜNAL' → User (tam ad ya da baş harf + soyad)."""
    from models import User
    t = _norm(ad)
    if not t:
        return None
    adaylar = db.query(User).filter(User.aktif.__eq__(True)).all()
    for u in adaylar:
        if _norm(u.ad_soyad) == t:
            return u
    soyad = t[-1]
    uyan = [u for u in adaylar if _norm(u.ad_soyad) and _norm(u.ad_soyad)[-1] == soyad
            and (len(t) == 1 or _norm(u.ad_soyad)[0].startswith(t[0][:1]))]
    return uyan[0] if len(uyan) == 1 else None


def departman_sureci(departman):
    d = (departman or "").replace("İ", "i").replace("I", "ı").lower()
    for anahtar, kod in DEPARTMAN_SUREC:
        son = r"(\W|$)" if len(anahtar) <= 3 else ""   # 'bt', 'it', 'isg' tam kelime; diğerleri kelime başı (kaynakları, satınalma)
        if re.search(rf"(^|\W){re.escape(anahtar)}{son}", d):
            return kod
    return None


def ekip_oku(nesne):
    try:
        return json.loads(getattr(nesne, "ekip_json", None) or "[]") or []
    except ValueError:
        return []


def kalem_tarihi(item, yil):
    if not item.planlanan_ay:
        return None
    gun = item.plan_gunu or 1
    for g in (gun, 28):
        try:
            return date(yil, item.planlanan_ay, g)
        except ValueError:
            continue
    return None


def ust_kod(kod):
    return (kod or "").split(".")[0].upper()


# ─────────────────────────── içe aktarma ───────────────────────────
def soru_listelerini_yukle(db, listeler):
    """DYS Aktarım/Denetim/soru_listeleri.json → AuditChecklist (Y01.6 F01-XX). Cevap almış liste değiştirilmez."""
    from models import AuditChecklist, AuditChecklistQuestion, AuditAnswer
    rapor = {"yeni": 0, "guncellenen": 0, "korunan": 0, "soru": 0}
    for L in listeler:
        cl = db.query(AuditChecklist).filter_by(kod=L["kod"]).first()
        if cl:
            qids = [q.id for q in cl.questions]
            if qids and db.query(AuditAnswer).filter(AuditAnswer.question_id.in_(qids)).count():
                rapor["korunan"] += 1
                # cevaplı liste korunur; yalnız kaynaktan çıkarılmış ve hiç cevap almamış sorular kaldırılır, sıra yeniden verilir
                yeni_metin = {" ".join(q["soru"].split()) for q in L["sorular"]}
                cevapli = {a for (a,) in db.query(AuditAnswer.question_id).filter(AuditAnswer.question_id.in_(qids)).all()}
                for q in list(cl.questions):
                    if " ".join(q.soru_metin.split()) not in yeni_metin and q.id not in cevapli:
                        cl.questions.remove(q); db.delete(q); rapor["kaldirilan"] = rapor.get("kaldirilan", 0) + 1
                for i, q in enumerate(sorted(cl.questions, key=lambda x: (x.sira or 0, x.id)), 1):
                    q.sira = i
                continue
            for q in list(cl.questions):
                db.delete(q)
            db.flush()
            rapor["guncellenen"] += 1
        else:
            cl = AuditChecklist(kod=L["kod"]); db.add(cl); rapor["yeni"] += 1
        cl.ad = L["ad"][:200]; cl.denetim_tipi = L.get("denetim_tipi") or "Sistem Denetimi"
        cl.surec_kod = L.get("surec_kod"); cl.aktif = True
        cl.kaynak_dosya = "; ".join(k["dosya"] for k in L.get("kaynaklar", []))[:260]
        db.flush()
        for q in L["sorular"]:
            ipucu = [f"İlgili doküman: {q['dokuman']}"] if q.get("dokuman") else []
            ipucu += [f"Kaynak liste: {q.get('kaynak')}"] if q.get("kaynak") else []
            if q.get("onceki_gozlem"):
                ipucu.append(f"Önceki denetim gözlemi{(' (' + q['onceki_sonuc'] + ')') if q.get('onceki_sonuc') else ''}: {q['onceki_gozlem']}")
            db.add(AuditChecklistQuestion(
                checklist_id=cl.id, sira=q["sira"], bolum=(q.get("bolum") or "Genel")[:120], soru_no=(q.get("soru_no") or "")[:20],
                soru_metin=q["soru"], sart_no=(q.get("sart_no") or "")[:40] or None,
                kime=(q.get("kime") or "")[:200] or None, ipucu="\n".join(ipucu) or None))
            rapor["soru"] += 1
    # eski 16 süreçlik sistem listeleri (Y03-F21-Dxx…) yeni süreç koduna denk gelmiyorsa pasif kalır
    yeni = {L.get("surec_kod") for L in listeler} - {None}
    for cl in db.query(AuditChecklist).filter(AuditChecklist.denetim_tipi == "Sistem Denetimi").all():
        if not cl.kod.startswith("Y01.6 F01-") and cl.surec_kod in yeni:
            cl.aktif = False
    db.flush()
    rapor["saha_baglanan"] = saha_listelerini_bagla(db)
    db.commit()
    return rapor


SAHA_LISTELERI = (("kalıphane", "Y01.6 F07-KLP"), ("kalite", "Y01.6 F07-KAL"), ("ara işlem", "Y01.6 F07-KAL"), ("üretim", "Y01.6 F07-URT"))


def saha_listesi_kodu(ad):
    """Saha plan kalemi adı → saha soru listesi kodu. Bölüm adı geçmiyorsa (ör. 'Saha Denetimi') İSG ve Çevre saha listesi."""
    a = (ad or "").replace("İ", "i").replace("I", "ı").lower()
    return next((k for s, k in SAHA_LISTELERI if s in a), "Y01.6 F07-ISG")


def saha_listelerini_bagla(db):
    """Soru listesi olmadan açılmış saha / proses / ürün denetimlerine (henüz cevap yoksa) uygun listeyi bağlar
    (saha: kalem adına göre Y01.6 F07-xx; proses: Y01.6 F08; ürün: Y01.6 F10)."""
    from models import AuditProgramItem, InternalAudit, AuditAnswer
    n = 0
    for it in db.query(AuditProgramItem).filter(AuditProgramItem.kategori.in_(("Saha", "Proses", "Ürün")), AuditProgramItem.audit_id.isnot(None)).all():
        a = db.get(InternalAudit, it.audit_id)
        if not a or db.query(AuditAnswer).filter_by(audit_id=a.id).count():
            continue
        if a.checklist_id and not (a.otomatik and a.checklist and a.checklist.denetim_tipi != (_checklist(db, it) or a.checklist).denetim_tipi):
            continue                          # elle seçilmiş ya da türüne uygun liste korunur; otomatik denetimde yanlış türdeki liste düzeltilir
        cl = _checklist(db, it)
        if cl and cl.id != a.checklist_id:
            a.checklist_id = cl.id; n += 1
    return n


def denetcileri_yukle(db, plan):
    """F06 denetçi kalifikasyon matrisi → DenetciYetkinlik. Mevcut kayıtta yalnız nitelikleri günceller."""
    from models import DenetciYetkinlik, DENETCI_NITELIKLERI
    rapor = {"yeni": 0, "guncellenen": 0, "eslesmeyen": []}
    anahtarlar = [k for k, _ in DENETCI_NITELIKLERI]
    for d in plan.get("denetciler", []):
        kayit = db.query(DenetciYetkinlik).filter_by(denetci_no=d["no"]).first() or \
            db.query(DenetciYetkinlik).filter_by(ad_soyad=d["ad_soyad"]).first()
        if not kayit:
            kayit = DenetciYetkinlik(denetci_no=d["no"], ad_soyad=d["ad_soyad"], aktif=True); db.add(kayit); rapor["yeni"] += 1
        else:
            rapor["guncellenen"] += 1
        kayit.nitelikler_json = json.dumps({k: bool(d.get(k)) for k in anahtarlar}, ensure_ascii=False)
        kayit.denetim_turleri = ",".join(d.get("denetim_turleri") or [])
        kayit.gecen_yil_denetim = d.get("gecen_yil")
        kayit.yillik_hedef = d.get("hedef") or kayit.yillik_hedef
        kayit.rol = kayit.rol or ("Denetçi" if d.get("iso19011") and d.get("iatf") else "Stajer")
        if not kayit.user_id:
            u = kullanici_bul(db, d["ad_soyad"])
            if u:
                kayit.user_id = u.id
                kayit.kendi_surecleri = kayit.kendi_surecleri or departman_sureci(u.departman)
            else:
                rapor["eslesmeyen"].append(d["ad_soyad"])
        if plan.get("kalifikasyon_guncelleme") and not kayit.notlar:
            kayit.notlar = f"F06 kalifikasyon matrisi — {plan['kalifikasyon_guncelleme']}"
    db.commit()
    return rapor


def plani_yukle(db, plan, degistir=False):
    """F06 yıl sayfası (JSON) → AuditProgram + AuditProgramItem. Denetimi açılmış kalemlere dokunmaz."""
    from models import AuditProgram, AuditProgramItem, Process, DenetciYetkinlik
    yil = int(plan["yil"])
    baslik = f"{yil} İç & Dış Denetim Planı (Y01.6 F06)"
    prog = db.query(AuditProgram).filter_by(yil=yil, baslik=baslik).first()
    if prog and not degistir:
        return {"program_id": prog.id, "kalem": 0, "atlandi": True}
    if not baslangic(db):
        ayar_yaz(db, "denetim_baslangic", date.today().isoformat())
    if not prog:
        prog = AuditProgram(yil=yil, baslik=baslik, risk_temelli_mi=True, durum="Planlandı",
                            kapsam=(plan.get("kurallar") or "")[:4000] or None)
        db.add(prog); db.flush()
    else:
        for it in list(prog.items):
            if not it.audit_id:
                db.delete(it)
        db.flush()
    surecler = {p.kod: p.id for p in db.query(Process).all()}
    denetci = {d.denetci_no: d for d in db.query(DenetciYetkinlik).all() if d.denetci_no is not None}
    n = 0
    for k in plan["kalemler"]:
        ekip = []
        for i, no in enumerate(k.get("denetci_kodlari") or []):
            d = denetci.get(no)
            ekip.append({"denetci_no": no, "denetci_id": d.id if d else None, "user_id": d.user_id if d else None,
                         "ad": d.ad_soyad if d else f"Denetçi #{no}", "rol": "Baş Denetçi" if i == 0 else "Denetçi"})
        if not ekip and k.get("denetci_metin"):
            d = next((x for x in denetci.values() if _norm(x.ad_soyad)[-1:] == _norm(k["denetci_metin"])[-1:]), None) if _norm(k["denetci_metin"]) else None
            ekip.append({"denetci_no": d.denetci_no if d else None, "denetci_id": d.id if d else None, "user_id": d.user_id if d else None,
                         "ad": k["denetci_metin"], "rol": "Baş Denetçi"})
        kat = k.get("kategori") or "Diğer"
        db.add(AuditProgramItem(
            program_id=prog.id, planlanan_ay=k.get("ay"), plan_gunu=k.get("gun"), kategori=kat,
            kalem_adi=(k.get("ad") or "")[:200], denetim_tipi=k.get("denetim_tipi") or "Sistem Denetimi",
            surec_id=surecler.get(k.get("yeni_surec")) if k.get("yeni_surec") else None,
            ilgili_standart=(k["ad"][:50] if kat == "Sertifikasyon" else None),
            urun_adi=(k.get("ad") or "")[:200] if kat == "Ürün" else ((k.get("denetim_turu") or "")[:200] if kat == "Proses" and k.get("denetim_turu") not in ("Normal", "") else None),
            denetci_metin=(k.get("denetci_metin") or "&".join(str(x) for x in k.get("denetci_kodlari") or []))[:200] or None,
            tetkik_eden_id=next((e["user_id"] for e in ekip if e.get("user_id")), None),
            ekip_json=json.dumps(ekip, ensure_ascii=False) if ekip else None,
            notlar="; ".join(x for x in [f"Eski süreç: {k['eski_surec']}" if k.get("eski_surec") else "", k.get("fonksiyonlar") and f"Standart maddeleri/fonksiyon: {k['fonksiyonlar']}",
                                         k.get("musteri_istegi") and f"Müşteri isteği: {k['musteri_istegi']}", k.get("tarih_metin") == "x" and "Her ay (gün serbest)"] if x) or None,
            durum="Planlandı" if kat in IC_KATEGORILER else "Planlandı",
        ))
        n += 1
    db.commit()
    return {"program_id": prog.id, "kalem": n, "atlandi": False}


def paketten_yukle(db, klasor, yil=None, degistir=False):
    """'YENİ YAPI/DYS Aktarım/Denetim' klasöründen soru listeleri + denetçiler + plan."""
    import glob, os
    sonuc = {}
    for ad in ("soru_listeleri", "saha_soru_listeleri", "proses_urun_soru_listeleri"):
        p = os.path.join(klasor, f"{ad}.json")
        if os.path.isfile(p):
            sonuc[ad] = soru_listelerini_yukle(db, json.load(open(p, encoding="utf-8")))
    planlar = sorted(glob.glob(os.path.join(klasor, "denetim_plani_*.json")))
    if yil:
        planlar = [x for x in planlar if x.endswith(f"_{yil}.json")]
    for pp in planlar:
        plan = json.load(open(pp, encoding="utf-8"))
        sonuc.setdefault("denetciler", denetcileri_yukle(db, plan))
        sonuc[f"plan_{plan['yil']}"] = plani_yukle(db, plan, degistir=degistir)
    return sonuc


# ─────────────────────────── ekip atama ───────────────────────────
def denetci_uygun_mu(d, kategori, surec_kod, bugun=None):
    """(uygun_mu, neden) — yetkinlik + bağımsızlık + geçerlilik."""
    bugun = bugun or date.today()
    if not d.aktif:
        return False, "pasif denetçi"
    if not d.user_id:
        return False, "DYS kullanıcısıyla eşleşmemiş"
    if d.gecerlilik_tarihi and d.gecerlilik_tarihi < bugun:
        return False, f"yetkinlik süresi dolmuş ({d.gecerlilik_tarihi:%d.%m.%Y})"
    harf = TUR_HARF.get(kategori)
    if harf and d.turler and harf not in d.turler:
        return False, f"'{harf}' denetim türünde yetkili değil"
    nit = d.nitelikler
    if kategori in ("Süreç", "Proses", "Ürün") and not nit.get("iatf"):
        return False, "IATF 16949 iç denetçi eğitimi yok"
    if kategori == "Proses" and not (nit.get("fmea") and nit.get("spc")):
        return False, "proses denetimi için FMEA/SPC yetkinliği yok"
    ek = SUREC_NITELIK.get(ust_kod(surec_kod))
    if ek and not nit.get(ek):
        return False, f"{ek.upper()} yetkinliği yok"
    if surec_kod and ust_kod(surec_kod) in [ust_kod(x) for x in d.surec_listesi]:
        return False, f"kendi sürecini ({ust_kod(surec_kod)}) denetleyemez — bağımsızlık"
    return True, ""


def _yuk(db, yil):
    from models import InternalAudit
    say = Counter()
    for a in db.query(InternalAudit).filter(InternalAudit.planlanan_tarih >= date(yil, 1, 1), InternalAudit.planlanan_tarih <= date(yil, 12, 31)).all():
        for e in ekip_oku(a) or ([{"user_id": a.tetkik_eden_id}] if a.tetkik_eden_id else []):
            if e.get("user_id"):
                say[e["user_id"]] += 1
    return say


def ekip_belirle(db, item, yil, bugun=None):
    """Plan kalemi için ekip + uyarılar. Plandaki uygun denetçiler korunur, uygun olmayanın yerine en az yüklü uygun denetçi."""
    from models import DenetciYetkinlik
    surec_kod = item.surec.kod if item.surec else None
    tum = db.query(DenetciYetkinlik).filter(DenetciYetkinlik.aktif.__eq__(True)).all()
    by_id = {d.id: d for d in tum}
    plan_ekip = ekip_oku(item)
    ekip, uyarilar = [], []
    for e in plan_ekip:
        d = by_id.get(e.get("denetci_id"))
        if not d:
            if e.get("ad") and not e.get("denetci_no"):     # 'İSG UZMANI' gibi rol yazımı → olduğu gibi (kullanıcı eşleşmesi yok)
                ekip.append({**e, "not": "plandaki yazım"})
            else:
                uyarilar.append(f"{e.get('ad')}: denetçi kaydı bulunamadı")
            continue
        ok, neden = denetci_uygun_mu(d, item.kategori, surec_kod, bugun)
        if ok:
            ekip.append({"denetci_id": d.id, "user_id": d.user_id, "ad": d.ad_soyad, "rol": e.get("rol") or "Denetçi"})
        else:
            uyarilar.append(f"{d.ad_soyad}: {neden}")
    if not any(e.get("user_id") for e in ekip) and item.kategori in IC_KATEGORILER:
        yuk = _yuk(db, yil)
        adaylar = [d for d in tum if denetci_uygun_mu(d, item.kategori, surec_kod, bugun)[0]]
        adaylar.sort(key=lambda d: (yuk.get(d.user_id, 0) - (d.yillik_hedef or 0), 0 if d.rol == "Baş Denetçi" else 1, d.ad_soyad))
        if adaylar:
            d = adaylar[0]
            ekip.insert(0, {"denetci_id": d.id, "user_id": d.user_id, "ad": d.ad_soyad, "rol": "Baş Denetçi", "not": "otomatik atandı"})
            if plan_ekip:
                uyarilar.append(f"Yerine {d.ad_soyad} otomatik atandı")
        else:
            uyarilar.append("Yetkin ve bağımsız denetçi bulunamadı")
    if ekip and not any(e.get("rol") == "Baş Denetçi" for e in ekip):
        ekip[0]["rol"] = "Baş Denetçi"
    return ekip, uyarilar


# ─────────────────────────── otomatik döngü ───────────────────────────
def _checklist(db, item):
    from audit_checklist_import import find_checklist_for_audit
    if item.checklist_id:
        from models import AuditChecklist
        return db.get(AuditChecklist, item.checklist_id)
    if item.kategori == "Saha":
        from models import AuditChecklist
        return db.query(AuditChecklist).filter_by(kod=saha_listesi_kodu(item.kalem_adi), aktif=True).first()
    kod = ust_kod(item.surec.kod) if item.surec else None
    if item.kategori == "Süreç" and not kod:
        return None
    return find_checklist_for_audit(db, item.denetim_tipi or "Sistem Denetimi", kod)


def denetim_ac(db, item, yil, bugun=None):
    """Plan kalemi için iç tetkik kaydını oluşturur (commit etmez)."""
    from helpers import next_sequence_no
    from models import InternalAudit
    ekip, uyarilar = ekip_belirle(db, item, yil, bugun)
    cl = _checklist(db, item)
    t = kalem_tarihi(item, yil)
    ad = item.kalem_adi or (item.surec.ad if item.surec else item.urun_adi) or item.denetim_tipi
    tur = f"{item.kategori} denetimi" if item.kategori else (item.denetim_tipi or "Denetim")
    baslik = f"{AYLAR[item.planlanan_ay - 1] if item.planlanan_ay else ''} {yil} — {tur}: {ad}".strip()
    if item.surec and (item.kategori or "Süreç") == "Süreç" and item.surec.kod not in baslik:
        baslik += f" → {item.surec.kod} {item.surec.ad}"
    a = InternalAudit(
        tetkik_no=next_sequence_no(db, InternalAudit, "tetkik_no", "IT"), baslik=baslik[:200],
        denetim_tipi=item.denetim_tipi or "Sistem Denetimi", planlanan_tarih=t,
        tetkik_eden_id=next((e["user_id"] for e in ekip if e.get("user_id")), None),
        denetlenen_surec_id=item.surec_id, ilgili_standart=item.ilgili_standart,
        urun_adi=item.urun_adi if (item.denetim_tipi == "Ürün Denetimi") else None,
        kapsam="\n".join(x for x in [f"Plan: {item.program.baslik} (kalem #{item.id})", item.notlar or "",
                                    ("Atama uyarıları: " + "; ".join(uyarilar)) if uyarilar else ""] if x),
        durum="Planlandı", checklist_id=cl.id if cl else None,
        ekip_json=json.dumps(ekip, ensure_ascii=False) if ekip else None, otomatik=True,
    )
    db.add(a); db.flush()
    item.audit_id = a.id
    item.ekip_json = a.ekip_json
    item.tetkik_eden_id = a.tetkik_eden_id
    item.durum = "Atandı" if a.tetkik_eden_id else "Planlandı"
    return a, uyarilar


BULGU_VARSAYILAN = {"Uygunsuz": "Uygunsuzluk (Minör)", "Kısmen Uygun": "Gözlem"}


def bulgulari_esitle(db, audit):
    """Uygunsuz / Kısmen Uygun cevaplar → AuditFinding (soru başına bir tane, derece değişirse günceller)."""
    from models import AuditFinding
    mevcut = {f.answer_id: f for f in audit.findings if f.answer_id}
    n = 0
    for ans in audit.answers or []:
        if ans.sonuc not in BULGU_VARSAYILAN:
            f = mevcut.get(ans.id)
            if f and not f.capa_id:          # cevap düzeltilmişse otomatik bulgu kaldırılır
                db.delete(f)
            continue
        tip = ans.bulgu_derecesi or BULGU_VARSAYILAN[ans.sonuc]
        q = ans.question
        metin = f"[Soru {q.sira}] {q.soru_metin}\nGözlem/delil: {ans.gozlem or '—'}" if q else (ans.gozlem or "")
        f = mevcut.get(ans.id)
        if f:
            if not f.capa_id:
                f.bulgu_tipi, f.aciklama, f.ilgili_madde = tip, metin, (q.sart_no if q else None)
        else:
            db.add(AuditFinding(audit_id=audit.id, answer_id=ans.id, bulgu_tipi=tip, aciklama=metin,
                                ilgili_madde=(q.sart_no if q else None)))
            n += 1
    if audit.durum == "Planlandı" and any(a.sonuc != "Değerlendirilmedi" for a in audit.answers or []):
        audit.durum = "Devam Ediyor"
    return n


def _surec_sorumlusu(db, surec):
    if not surec or not surec.sorumlu:
        return None
    u = kullanici_bul(db, surec.sorumlu)
    return u.id if u else None


def dof_ac(db, audit, acan_id=None):
    """Tamamlanan denetimdeki DÖF'süz uygunsuzluk bulguları → CorrectiveAction."""
    from helpers import next_sequence_no
    from models import CorrectiveAction
    acilan = []
    for f in audit.findings:
        if f.capa_id or not (f.bulgu_tipi or "").startswith("Uygunsuzluk"):
            continue
        capa = CorrectiveAction(
            dof_no=next_sequence_no(db, CorrectiveAction, "dof_no", "DÖF"),
            baslik=f"{audit.tetkik_no} bulgusu: {f.bulgu_tipi}"[:200], kaynak_tipi="İç Tetkik",
            ilgili_surec_id=audit.denetlenen_surec_id, tespit_tarihi=audit.gerceklesen_tarih or date.today(),
            acan_id=acan_id or audit.tetkik_eden_id, sorumlu_id=_surec_sorumlusu(db, audit.denetlenen_surec),
            tanim=f.aciklama + (f"\nİlgili madde: {f.ilgili_madde}" if f.ilgili_madde else ""),
            planlanan_tarih=date.today() + timedelta(days=30 if "Minör" in f.bulgu_tipi else 15), durum="Açık",
        )
        db.add(capa); db.flush()
        f.capa_id = capa.id
        acilan.append(capa)
    return acilan


def otomatik_calistir(db, bugun=None, zorla=False):
    """Günlük döngü. Dönüş: rapor sözlüğü."""
    from models import AuditProgram, InternalAudit
    bugun = bugun or date.today()
    rapor = {"acilan": [], "uyarilar": [], "bulgu": 0, "dof": [], "tarih": bugun.isoformat()}
    oto = ayar(db, "denetim_otomatik") == "1" or zorla
    gun_once = int(ayar(db, "denetim_gun_once") or 14)
    bas = baslangic(db)
    if oto:
        for prog in db.query(AuditProgram).filter(AuditProgram.yil.in_([bugun.year, bugun.year + 1])).all():
            for item in sorted(prog.items, key=lambda i: (i.planlanan_ay or 0, i.plan_gunu or 0, i.id)):
                if item.audit_id or item.durum in ("Tamamlandı", "İptal") or (item.kategori or "Süreç") not in IC_KATEGORILER:
                    continue
                t = kalem_tarihi(item, prog.yil)
                if not t or t - timedelta(days=gun_once) > bugun:
                    continue
                if bas and t < bas:        # DYS öncesi dönem — kâğıtta yapılmış olabilir, otomatik açılmaz
                    continue
                a, uy = denetim_ac(db, item, prog.yil, bugun)
                rapor["acilan"].append(a.tetkik_no)
                rapor["uyarilar"] += [f"{a.tetkik_no}: {u}" for u in uy]
    for a in db.query(InternalAudit).filter(InternalAudit.durum != "Kapatıldı").all():
        rapor["bulgu"] += bulgulari_esitle(db, a)
        if a.durum == "Tamamlandı" and ayar(db, "denetim_dof_otomatik") == "1":
            db.flush()
            rapor["dof"] += [c.dof_no for c in dof_ac(db, a)]
    ayar_yaz(db, "denetim_son_calisma", datetime.now().strftime("%Y-%m-%d %H:%M"))
    db.commit()
    return rapor


# ─────────────────────────── hatırlatmalar ───────────────────────────
DENETIM_TURLERI = (   # Denetim Yönetimi sekmeleri: (kategori, başlık, açıklama)
    ("Süreç", "Süreç Denetimleri", "11 süreçlik yapıda süreç denetimleri (IATF 16949 9.2.2.2, ISO 9001 / 14001 / 45001 9.2) — süreç soru listeleriyle."),
    ("Proses", "Proses Denetimleri", "Üretim prosesi denetimleri (IATF 16949 9.2.2.3) — vardiyalar, PFMEA ve kontrol planı etkinliği, proses parametreleri — Y01.6 F08 Proses Denetim Soru Listesi (52 soru)."),
    ("Ürün", "Ürün Denetimleri", "Ürün denetimleri (IATF 16949 9.2.2.4) — ürün / parça bazında ölçüsel, görsel, fonksiyonel, ambalaj ve etiket uygunluğu — Y01.6 F10 Ürün Denetim Soru Listesi."),
    ("Saha", "Saha Denetimleri (İSG – 5S)", "Saha, İSG, çevre ve 5S denetimleri — Y01.6 F07 saha soru listeleri: İSG ve Çevre (D01.2 F25, D01.2 F27, D05 F18'den), Üretim, Kalıphane, Kalite & Ara İşlem."),
)
TIP_KATEGORI = {"Ürün Denetimi": "Ürün", "Proses Denetimi": "Proses", "Saha Denetimi": "Saha"}


def denetim_kategorileri(db):
    """Denetim (InternalAudit) id → plan kategorisi (Süreç / Proses / Ürün / Saha …); plandan açılmamışsa denetim tipinden."""
    from models import AuditProgramItem, InternalAudit
    m = {it.audit_id: (it.kategori or "Süreç") for it in db.query(AuditProgramItem).filter(AuditProgramItem.audit_id.isnot(None)).all()}
    for a in db.query(InternalAudit).all():
        m.setdefault(a.id, TIP_KATEGORI.get(a.denetim_tipi or "", "Saha" if "saha" in (a.baslik or "").lower() else "Süreç"))
    return m


def denetim_kategorisi(db, audit):
    """Tek denetimin kategorisi (Süreç / Proses / Ürün / Saha …)."""
    from models import AuditProgramItem
    it = db.query(AuditProgramItem).filter_by(audit_id=audit.id).first()
    if it and it.kategori:
        return it.kategori
    return TIP_KATEGORI.get(audit.denetim_tipi or "", "Saha" if "saha" in (audit.baslik or "").lower() else "Süreç")


EK_KANIT_KATEGORILERI = ("Proses", "Ürün")   # ek kanıt dokümanı bölümü görünen denetim türleri


def hatirlatmalar(db, bugun=None, user_id=None):
    """[{seviye: kritik|uyari|bilgi, tur, mesaj, url, user_ids}] — panel ve e-posta için."""
    from models import (AuditProgram, InternalAudit, DenetciYetkinlik, Process, AuditFinding)
    bugun = bugun or date.today()
    yak = int(ayar(db, "denetim_hatirlatma_gun") or 7)
    L = []

    def ekle(seviye, tur, mesaj, url, kimler=(), kategori=None):
        L.append({"seviye": seviye, "tur": tur, "mesaj": mesaj, "url": url, "user_ids": [k for k in kimler if k], "kategori": kategori})

    kat_of = denetim_kategorileri(db)

    for a in db.query(InternalAudit).filter(InternalAudit.durum.in_(["Planlandı", "Devam Ediyor"])).all():
        kimler = {e.get("user_id") for e in ekip_oku(a)} | {a.tetkik_eden_id}
        k = kat_of.get(a.id)
        if a.planlanan_tarih and a.planlanan_tarih < bugun:
            ekle("kritik", "Geciken denetim", f"{a.tetkik_no} {a.baslik} — plan {a.planlanan_tarih:%d.%m.%Y}, {(bugun - a.planlanan_tarih).days} gün gecikti",
                 f"/audits/{a.id}", kimler, k)
        elif a.planlanan_tarih and a.planlanan_tarih <= bugun + timedelta(days=yak):
            ekle("uyari", "Yaklaşan denetim", f"{a.tetkik_no} {a.baslik} — {a.planlanan_tarih:%d.%m.%Y}", f"/audits/{a.id}", kimler, k)
        if not a.tetkik_eden_id:
            ekle("kritik", "Denetçi atanmamış", f"{a.tetkik_no} {a.baslik}", f"/audits/{a.id}", (), k)
    for a in db.query(InternalAudit).filter(InternalAudit.durum == "Tamamlandı").all():
        if not (a.sonuc_ozeti or "").strip():
            ekle("uyari", "Sonuç özeti eksik", f"{a.tetkik_no} {a.baslik}", f"/audits/{a.id}", [a.tetkik_eden_id], kat_of.get(a.id))
        acik = [f for f in a.findings if (f.bulgu_tipi or "").startswith("Uygunsuzluk") and not f.capa_id]
        if acik:
            ekle("kritik", "DÖF'süz uygunsuzluk", f"{a.tetkik_no}: {len(acik)} uygunsuzluk bulgusu DÖF'e bağlanmamış", f"/audits/{a.id}", [a.tetkik_eden_id], kat_of.get(a.id))
    bas = baslangic(db)
    for prog in db.query(AuditProgram).filter(AuditProgram.yil == bugun.year).all():
        oncesi = 0
        for it in prog.items:
            if it.audit_id or it.durum in ("Tamamlandı", "İptal"):
                continue
            t = kalem_tarihi(it, prog.yil)
            if t and t < bugun and (it.kategori or "Süreç") in IC_KATEGORILER:
                if bas and t < bas:
                    oncesi += 1
                else:
                    ekle("kritik", "Açılmamış plan kalemi", f"{it.kalem_adi or it.denetim_tipi} — {t:%d.%m.%Y}", f"/audits/program/{prog.id}/calendar", (), it.kategori or "Süreç")
            if (it.kategori in ("Sertifikasyon", "Tedarikçi")) and t and bugun <= t <= bugun + timedelta(days=30):
                ekle("bilgi", f"Yaklaşan {it.kategori.lower()} denetimi", f"{it.kalem_adi} — {t:%d.%m.%Y}", "/audits/program", (), it.kategori)
        if oncesi:
            ekle("bilgi", "DYS öncesi plan kalemleri", f"{prog.yil} planında {bas:%d.%m.%Y} öncesine ait {oncesi} kalem DYS'de açılmadı "
                 "(kâğıt kayıtlarla yapıldıysa 'Tamamlandı' / 'İptal' işaretleyin ya da takvimden 'aç' ile kayda alın)", f"/audits/program/{prog.id}/calendar")
        # IATF 9.2.2.1: tüm süreçler programda olmalı
        planli = {ust_kod(it.surec.kod) for it in prog.items if it.surec}
        for p in db.query(Process).filter(Process.ust_surec_id.is_(None)).all():
            if p.aktif_mi and re.fullmatch(r"[DMY]\d\d", p.kod or "") and p.kod not in planli:
                ekle("uyari", "Planda olmayan süreç", f"{p.kod} {p.ad} — {prog.yil} programında süreç denetimi yok", "/audits/program", (), "Süreç")
    for d in db.query(DenetciYetkinlik).filter(DenetciYetkinlik.aktif.__eq__(True)).all():
        if not d.user_id:
            ekle("bilgi", "Denetçi eşleşmedi", f"{d.ad_soyad}: DYS kullanıcısı seçilmemiş (otomatik atamada kullanılamaz)", f"/audits/auditors/{d.id}")
        if d.gecerlilik_tarihi and d.gecerlilik_tarihi < bugun:
            ekle("kritik", "Denetçi yetkinliği doldu", f"{d.ad_soyad}: {d.gecerlilik_tarihi:%d.%m.%Y}", f"/audits/auditors/{d.id}", [d.user_id])
        elif d.gecerlilik_tarihi and d.gecerlilik_tarihi <= bugun + timedelta(days=60):
            ekle("uyari", "Denetçi yetkinliği bitiyor", f"{d.ad_soyad}: {d.gecerlilik_tarihi:%d.%m.%Y}", f"/audits/auditors/{d.id}", [d.user_id])
        elif not d.gecerlilik_tarihi:
            ekle("bilgi", "Yetkinlik geçerlilik tarihi yok", d.ad_soyad, f"/audits/auditors/{d.id}")
    sira = {"kritik": 0, "uyari": 1, "bilgi": 2}
    L.sort(key=lambda x: (sira[x["seviye"]], x["tur"]))
    if user_id:
        L = [x for x in L if user_id in x["user_ids"]]
    return L


def gunluk_gorev():
    """Zamanlayıcı: otomatik döngü + kişiye özel hatırlatma e-postası (SMTP tanımlıysa)."""
    from models import SessionLocal, User
    db = SessionLocal()
    try:
        rapor = otomatik_calistir(db)
        if ayar(db, "denetim_eposta") != "1":
            return rapor
        import notifications
        if not notifications.is_configured():
            return rapor
        from config import Config
        taban = (getattr(Config, "APP_BASE_URL", "") or "").rstrip("/")
        kisi = {}
        for h in hatirlatmalar(db):
            if h["seviye"] == "bilgi":
                continue
            for uid in h["user_ids"]:
                kisi.setdefault(uid, []).append(h)
        for uid, liste in kisi.items():
            u = db.get(User, uid)
            if not u or not u.eposta:
                continue
            govde = "\n".join(f"- [{h['tur']}] {h['mesaj']}  {taban}{h['url']}" for h in liste)
            notifications.send_mail(u.eposta, f"DYS İç Denetim hatırlatması ({len(liste)})",
                                    f"Sayın {u.ad_soyad},\n\nİç denetim hatırlatmalarınız:\n\n{govde}\n\nDYS")
        return rapor
    except Exception:  # noqa: BLE001
        logger.exception("İç denetim günlük görevi başarısız")
        db.rollback()
    finally:
        db.close()
