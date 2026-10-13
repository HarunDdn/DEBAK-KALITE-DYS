"""
DYS — Doküman yönetim etkinliği (2026): tek iş kutusu ve atıf bütünlüğü
  /islerim                    kişiye ait tüm bekleyen işler tek listede: doküman onayları, okunup anlaşılacak dokümanlar
                              (yeni revizyonlar dahil), form / enjeksiyon kaydı onayları, taslak ve reddedilen form kayıtları,
                              DÖF'ler, iç denetimler, yaklaşan / geciken doküman gözden geçirmeleri, kalibrasyonu yaklaşan cihazlar
  /reports/atiflar            atıf bütünlüğü raporu: DYS'de olmayan, Eskimiş / İptal ya da hariç tutulan dokümana verilen atıflar
  /reports/atiflar.csv        aynı rapor (Excel'de açılır CSV)
Şema değişikliği yoktur; mevcut tablolardan okunur. app.py sonunda import edilir.
"""
from sqlalchemy import case as _sa_case
import csv
import io
import json
from datetime import date, datetime, timedelta

from flask import render_template, request, session, Response
from sqlalchemy import or_

from app_runtime import host as _host
_h = _host()
app = _h.app
login_required = _h.login_required
get_db = _h.get_db

from models import (  # noqa: E402
    Document, DocumentApproval, DocumentDistribution, FormKayit, FormTanim, EnjeksiyonParametreKaydi,
    CorrectiveAction, InternalAudit, CalibrationEquipment,
)
import dokuman_atif  # noqa: E402

YETKILI = ("Admin", "Doküman Kontrol")
GUN = 30   # "yaklaşan" ufku


def _ekipte(ekip_json, uid):
    try:
        return any(isinstance(e, dict) and e.get("user_id") == uid for e in json.loads(ekip_json or "[]"))
    except ValueError:
        return False


def _satir(metin, link, alt="", tarih=None, gecikme=False, eylem=None):
    return {"metin": metin, "link": link, "alt": alt, "tarih": tarih, "gecikme": gecikme, "eylem": eylem}


def is_kutusu(db, uid, rol):
    """[{anahtar, baslik, ikon, aciklama, satirlar, tumu}] — boş bölümler de döner (sayaç 0)."""
    bugun = date.today(); ufuk = bugun + timedelta(days=GUN); yetkili = rol in YETKILI
    b = []

    q = db.query(DocumentApproval).filter(DocumentApproval.durum == "Bekliyor")
    if not yetkili:
        q = q.filter(DocumentApproval.kullanici_id == uid)
    b.append({"anahtar": "onay", "baslik": "Doküman onaylarım", "ikon": "✅", "tumu": "/approvals",
              "aciklama": "Onay akışında sizin adımınızda bekleyen dokümanlar.",
              "satirlar": [_satir(f"{a.document.dokuman_no} — {a.document.baslik}", f"/documents/{a.document_id}",
                                  f"{a.onay_adimi} adımı · Rev. {a.document.revizyon_no}", a.tarih.date() if a.tarih else None)
                           for a in q.order_by(DocumentApproval.tarih).limit(200).all() if a.document]})

    dd = (db.query(DocumentDistribution).filter(DocumentDistribution.kullanici_id == uid, DocumentDistribution.okundu_mu.__eq__(False))
          .order_by(DocumentDistribution.dagitim_tarihi).limit(200).all())
    b.append({"anahtar": "oku", "baslik": "Okuyup anlamam gereken dokümanlar", "ikon": "📨", "tumu": "/distributions",
              "aciklama": "Size dağıtılan ya da yeni revizyonu yayınlanan dokümanlar. Okuduktan sonra 'Okudum, anladım' ile onaylayın (okuma kaydı).",
              "satirlar": [_satir(f"{d.document.dokuman_no} — {d.document.baslik}", f"/documents/{d.document_id}",
                                  f"Rev. {d.document.revizyon_no}", d.dagitim_tarihi.date() if d.dagitim_tarihi else None,
                                  eylem={"url": f"/distributions/{d.id}/mark-read", "etiket": "Okudum, anladım"})
                           for d in dd if d.document]})

    fs = []
    import form_erisim as FE   # kısıtlı (KVKK) formlarda onay yetkilisi formda tanımlı kişilerdir (ör. İK)
    for k in (db.query(FormKayit).filter(FormKayit.durum == "Onayda", or_(FormKayit.olusturan_id.is_(None), FormKayit.olusturan_id != uid))
              .order_by(FormKayit.olusturma_tarihi).limit(500).all()):
        if not (FE.onaylayabilir(k.tanim, uid, rol) and FE.kayit_gorebilir(k.tanim, k, uid, rol)):
            continue
        fs.append(_satir(f"{k.kayit_no} — {k.tanim.ad if k.tanim else ''}", f"/forms/records/{k.id}",
                         f"onay bekliyor · {k.olusturan.ad_soyad if k.olusturan else ''}", k.olusturma_tarihi.date() if k.olusturma_tarihi else None))
    if yetkili:
        for k in (db.query(EnjeksiyonParametreKaydi).filter(EnjeksiyonParametreKaydi.durum == "Onayda",
                  or_(EnjeksiyonParametreKaydi.kaydeden_id.is_(None), EnjeksiyonParametreKaydi.kaydeden_id != uid)).limit(200).all()):
            fs.append(_satir(f"Enjeksiyon parametresi — {k.urun.ad if k.urun else ''}", f"/uretim/enjeksiyon-parametreleri?urun={k.urun_id}",
                             f"onay bekliyor · {k.makina or ''}", k.olusturma_tarihi.date() if k.olusturma_tarihi else None))
    b.append({"anahtar": "form_onay", "baslik": "Onayımı bekleyen kayıtlar", "ikon": "🗂️", "tumu": "/forms",
              "aciklama": "Dinamik form ve enjeksiyon parametre kayıtları (görevler ayrılığı: kendi girdiğiniz kayıt burada görünmez).",
              "satirlar": fs})

    kendi = (db.query(FormKayit).filter(FormKayit.olusturan_id == uid,
             or_(FormKayit.durum == "Taslak", (FormKayit.durum == "Reddedildi") & (FormKayit.onay_tarihi >= datetime.now() - timedelta(days=GUN))))
             .order_by(FormKayit.olusturma_tarihi.desc()).limit(100).all())
    b.append({"anahtar": "form_kendi", "baslik": "Tamamlanmamış / reddedilen kayıtlarım", "ikon": "📝", "tumu": "/forms",
              "aciklama": "Taslak bıraktığınız ya da son 30 günde reddedilen form kayıtlarınız.",
              "satirlar": [_satir(f"{k.kayit_no} — {k.tanim.ad if k.tanim else ''}", f"/forms/records/{k.id}",
                                  k.durum + (f" · {k.onay_notu}" if k.durum == "Reddedildi" and k.onay_notu else ""),
                                  k.olusturma_tarihi.date() if k.olusturma_tarihi else None, gecikme=k.durum == "Reddedildi") for k in kendi]})

    dof = (db.query(CorrectiveAction).filter(CorrectiveAction.sorumlu_id == uid, CorrectiveAction.durum != "Kapatıldı")
           .order_by(CorrectiveAction.planlanan_tarih).limit(200).all())
    b.append({"anahtar": "dof", "baslik": "Sorumlu olduğum DÖF'ler", "ikon": "🛠️", "tumu": "/capa",
              "aciklama": "Kapatılmamış düzeltici / önleyici faaliyetler.",
              "satirlar": [_satir(f"{k.dof_no} — {k.baslik}", f"/capa/{k.id}", k.durum, k.planlanan_tarih,
                                  gecikme=bool(k.planlanan_tarih and k.planlanan_tarih < bugun)) for k in dof]})

    from models import ToplantiMaddesi
    tm = (db.query(ToplantiMaddesi).filter(ToplantiMaddesi.sorumlu_id == uid, ToplantiMaddesi.durum == "Açık")
          .order_by(_sa_case((ToplantiMaddesi.termin.is_(None), 1), else_=0), ToplantiMaddesi.termin).limit(200).all())
    b.append({"anahtar": "toplanti", "baslik": "Toplantı kararlarından aksiyonlarım", "ikon": "🗓️", "tumu": "/toplantilar/aksiyonlar?benim=1",
              "aciklama": "Toplantı tutanaklarında sorumlu olarak atandığınız açık kararlar / aksiyonlar.",
              "satirlar": [_satir(m.karar or m.konu or m.gorusme or "Toplantı aksiyonu", f"/toplantilar/{m.tutanak_id}",
                                  f"{m.tutanak.tutanak_no} · {m.tutanak.tur}", m.termin, gecikme=bool(m.termin and m.termin < bugun),
                                  eylem={"url": "/toplantilar/aksiyonlar", "etiket": "Tamamlandı", "alan": {"madde_id": m.id, "geri": "/islerim"}}) for m in tm]})

    from models import TedarikciUygunsuzluk, MusteriSikayeti
    kv = []
    for M, tur, ad in ((MusteriSikayeti, "musteri", "Müşteri şikayeti"), (TedarikciUygunsuzluk, "tedarikci", "Tedarikçi uygunsuzluğu")):
        for k in db.query(M).filter(M.sorumlu_id == uid, ~M.durum.in_(("Kapatıldı", "İptal"))).order_by(_sa_case((M.termin_8d.is_(None), 1), else_=0), M.termin_8d).limit(200).all():
            kv.append(_satir(f"{k.no} — {getattr(k, 'musteri', None) or getattr(k, 'tedarikci_ad', None) or ''} · {k.urun_adi or k.urun_no or ''}", f"/kalite/{tur}/{k.id}",
                             f"{ad} · {k.durum}", k.termin_8d, gecikme=bool(k.termin_8d and k.termin_8d < bugun)))
    b.append({"anahtar": "kalite", "baslik": "Sorumlu olduğum kalite vakaları", "ikon": "📣", "tumu": "/kalite/musteri",
              "aciklama": "Açık müşteri şikayetleri (8D) ve tedarikçi uygunsuzlukları; termin 8D terminidir.", "satirlar": kv})

    den = [a for a in db.query(InternalAudit).filter(InternalAudit.durum.in_(("Planlandı", "Devam Ediyor"))).order_by(InternalAudit.planlanan_tarih).all()
           if a.tetkik_eden_id == uid or _ekipte(a.ekip_json, uid)]
    b.append({"anahtar": "denetim", "baslik": "Denetçi olduğum iç denetimler", "ikon": "🔍", "tumu": "/audits",
              "aciklama": "Baş denetçi ya da ekip üyesi olduğunuz, tamamlanmamış denetimler.",
              "satirlar": [_satir(f"{a.tetkik_no} — {a.baslik}", f"/audits/{a.id}/checklist" if a.checklist_id else f"/audits/{a.id}", a.durum,
                                  a.planlanan_tarih, gecikme=bool(a.planlanan_tarih and a.planlanan_tarih < bugun)) for a in den[:200]]})

    q = db.query(Document).filter(Document.durum == "Onaylı", Document.sonraki_gozden_gecirme.isnot(None), Document.sonraki_gozden_gecirme <= ufuk)
    if not yetkili:
        q = q.filter(or_(Document.hazirlayan_id == uid, Document.onaylayan_id == uid))
    b.append({"anahtar": "gozden", "baslik": "Gözden geçirmesi yaklaşan / geciken dokümanlar", "ikon": "📅", "tumu": "/reports/review-calendar",
              "aciklama": f"Periyodik gözden geçirme tarihi {GUN} gün içinde olan ya da geçmiş yürürlükteki dokümanlar"
                          + (" (tüm dokümanlar)." if yetkili else " (hazırladığınız / onayladığınız)."),
              "satirlar": [_satir(f"{d.dokuman_no} — {d.baslik}", f"/documents/{d.id}", f"Rev. {d.revizyon_no}", d.sonraki_gozden_gecirme,
                                  gecikme=d.sonraki_gozden_gecirme < bugun) for d in q.order_by(Document.sonraki_gozden_gecirme).limit(200).all()]})

    q = db.query(CalibrationEquipment).filter(CalibrationEquipment.sonraki_kalibrasyon.isnot(None), CalibrationEquipment.sonraki_kalibrasyon <= ufuk,
                                             CalibrationEquipment.durum.notin_(("Pasif", "Kullanım Dışı", "Arızalı")))
    if not yetkili:
        q = q.filter(CalibrationEquipment.sorumlu_id == uid)
    b.append({"anahtar": "kalibrasyon", "baslik": "Kalibrasyonu yaklaşan / geçen cihazlar", "ikon": "📏", "tumu": "/iatf/calibration",
              "aciklama": f"Sonraki kalibrasyon / doğrulama tarihi {GUN} gün içinde olan ya da geçmiş cihazlar.",
              "satirlar": [_satir(f"{e.cihaz_kodu or e.ekipman_no} — {e.ad}", f"/iatf/calibration/{e.id}", e.konum or e.kullanici_bolum or "",
                                  e.sonraki_kalibrasyon, gecikme=e.sonraki_kalibrasyon < bugun)
                           for e in q.order_by(CalibrationEquipment.sonraki_kalibrasyon).limit(200).all()]})
    return b


def is_sayisi(db, uid, rol):
    return sum(len(x["satirlar"]) for x in is_kutusu(db, uid, rol))


@app.route("/islerim")
@login_required
def islerim():
    db = get_db()
    try:
        bolumler = is_kutusu(db, session.get("user_id"), session.get("rol"))
        return render_template("islerim.html", bolumler=bolumler, toplam=sum(len(x["satirlar"]) for x in bolumler),
                               geciken=sum(1 for x in bolumler for s in x["satirlar"] if s["gecikme"]), bugun=date.today())
    finally:
        db.close()


@app.route("/reports/atiflar")
@login_required
def rapor_atiflar():
    db = get_db()
    try:
        satirlar = dokuman_atif.sorunlu_atiflar(db)
        tur = (request.args.get("sorun") or "").strip()
        turler = sorted({s["sorun"] for s in satirlar})
        if tur:
            satirlar = [s for s in satirlar if s["sorun"] == tur]
        return render_template("rapor_atiflar.html", satirlar=satirlar, turler=turler, tur=tur, ozet=dokuman_atif.ozet(db))
    finally:
        db.close()


@app.route("/reports/atiflar.csv")
@login_required
def rapor_atiflar_csv():
    db = get_db()
    try:
        out = io.StringIO(); w = csv.writer(out, delimiter=";")
        w.writerow(["Kaynak doküman", "Başlık", "Kaynak durumu", "Atıf yapılan kod", "Sorun", "Öneri"])
        for s in dokuman_atif.sorunlu_atiflar(db):
            w.writerow([s["kaynak"].dokuman_no, s["kaynak"].baslik, s["kaynak"].durum, s["kod"], s["sorun"], s["oneri"]])
        return Response("﻿" + out.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=atif_butunlugu_{date.today():%Y%m%d}.csv"})
    finally:
        db.close()
