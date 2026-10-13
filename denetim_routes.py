"""
DYS — Denetim Yönetimi ekranları (2026)
  /audits/automation[?tur=Süreç|Proses|Ürün|Saha]   Denetim Yönetimi: tür sekmeleri (süreç / proses / ürün / saha İSG–5S),
                                tür bazında plan kalemleri, denetimler ve hatırlatmalar; 'şimdi çalıştır', ayarlar, plan / soru listesi içe aktarma
  /audits/auditors              iç denetçi yetkinlik kayıtları (F06 kalifikasyon matrisi)
  /audits/auditors/<id>         denetçi kartı (nitelikler, bağımsızlık, geçerlilik, eğitim kayıtları, denetim yükü)
  /audits/program/item/<id>/ac  plan kalemi için denetimi hemen aç
app.py sonunda import edilir.
"""
import json
from datetime import date, datetime

from flask import render_template, request, redirect, url_for, flash, abort, session

from app_runtime import host as _host
_h = _host()
app = _h.app
login_required = _h.login_required
get_db = _h.get_db
log_action = _h.log_action

from models import (  # noqa: E402
    User, Process, InternalAudit, AuditProgram, AuditProgramItem, DenetciYetkinlik, TrainingRecord,
    DENETCI_NITELIKLERI,
)
import denetim_otomasyon as OT  # noqa: E402

ROLLER = ("Baş Denetçi", "Denetçi", "Stajer")


def _editor():
    return session.get("rol") in ("Admin", "Doküman Kontrol")


def _tarih(v):
    try:
        return datetime.strptime(v, "%Y-%m-%d").date() if v else None
    except ValueError:
        return None


_ROZET = {}  # user_id → (zaman, sayı): menü rozeti her sayfada yeniden hesaplanmasın


@app.context_processor
def _denetim_ctx():
    def denetim_hatirlatma_sayisi():
        uid = session.get("user_id")
        if not uid:
            return 0
        onbellek = _ROZET.get(uid)
        if onbellek and (datetime.now() - onbellek[0]).total_seconds() < 300:
            return onbellek[1]
        db = get_db()
        try:
            n = len([h for h in OT.hatirlatmalar(db, user_id=uid) if h["seviye"] != "bilgi"])
        except Exception:  # noqa: BLE001
            n = 0
        finally:
            db.close()
        _ROZET[uid] = (datetime.now(), n)
        return n
    return {"denetim_hatirlatma_sayisi": denetim_hatirlatma_sayisi}


@app.route("/audits/automation", methods=["GET", "POST"])
@login_required
def audit_automation():
    db = get_db()
    try:
        if request.method == "POST":
            if not _editor():
                abort(403)
            action = request.form.get("action")
            if action == "calistir":
                r = OT.otomatik_calistir(db, zorla=True)
                log_action(db, "Güncelleme", detay=f"Denetim Yönetimi — plan çalıştırıldı: {len(r['acilan'])} tetkik açıldı, {r['bulgu']} bulgu, {len(r['dof'])} DÖF")
                flash(f"Çalıştırıldı: {len(r['acilan'])} denetim açıldı ({', '.join(r['acilan'][:8])}{'…' if len(r['acilan']) > 8 else ''}), "
                      f"{r['bulgu']} yeni bulgu, {len(r['dof'])} DÖF.", "success")
                for u in r["uyarilar"][:10]:
                    flash(u, "warning")
            elif action == "ayarlar":
                for k in OT.AYAR_VARSAYILAN:
                    if k in ("denetim_gun_once", "denetim_hatirlatma_gun"):
                        v = request.form.get(k, "").strip()
                        if v.isdigit():
                            OT.ayar_yaz(db, k, v)
                    else:
                        OT.ayar_yaz(db, k, "1" if request.form.get(k) else "0")
                db.commit()
                log_action(db, "Güncelleme", detay="Denetim Yönetimi ayarları değiştirildi")
                flash("Ayarlar kaydedildi.", "success")
            elif action == "ice_aktar":
                degistir = bool(request.form.get("degistir"))
                sonuc = []
                for f in request.files.getlist("dosyalar"):
                    if not f or not f.filename.lower().endswith(".json"):
                        continue
                    try:
                        veri = json.loads(f.read().decode("utf-8-sig"))
                    except ValueError:
                        flash(f"{f.filename}: JSON okunamadı", "error"); continue
                    if isinstance(veri, list):
                        r = OT.soru_listelerini_yukle(db, veri)
                        sonuc.append(f"Soru listeleri: {r['yeni']} yeni, {r['guncellenen']} güncellendi, {r['korunan']} cevaplı liste korundu, {r['soru']} soru")
                    elif isinstance(veri, dict) and "kalemler" in veri:
                        r1 = OT.denetcileri_yukle(db, veri)
                        r2 = OT.plani_yukle(db, veri, degistir=degistir)
                        sonuc.append(f"{veri['yil']} planı: {r2['kalem']} kalem" + (" (program zaten var — 'Yeniden yükle' seçin)" if r2["atlandi"] else "")
                                     + f"; denetçi {r1['yeni']} yeni / {r1['guncellenen']} güncellendi"
                                     + (f"; kullanıcıyla eşleşmeyen: {', '.join(r1['eslesmeyen'])}" if r1["eslesmeyen"] else ""))
                log_action(db, "Oluşturma", detay="İç denetim içe aktarma: " + " | ".join(sonuc))
                for s in sonuc:
                    flash(s, "success")
                if not sonuc:
                    flash("Dosya seçilmedi.", "warning")
            return redirect(url_for("audit_automation", tur=request.args.get("tur") or None))

        TURLER = [t[0] for t in OT.DENETIM_TURLERI]
        tur = request.args.get("tur") if request.args.get("tur") in TURLER else ""
        tum_hepsi = OT.hatirlatmalar(db)
        tum = [h for h in tum_hepsi if not tur or h.get("kategori") == tur]
        benim = [h for h in tum if session.get("user_id") in h["user_ids"]]
        ayarlar = {k: OT.ayar(db, k) for k in OT.AYAR_VARSAYILAN}
        yil = date.today().year
        progs = db.query(AuditProgram).filter(AuditProgram.yil == yil).all()
        kalemler = [i for p in progs for i in p.items]
        ozet = {
            "kalem": len(kalemler),
            "ic": len([i for i in kalemler if (i.kategori or "Süreç") in OT.IC_KATEGORILER]),
            "acilan": len([i for i in kalemler if i.audit_id]),
            "oto": db.query(InternalAudit).filter(InternalAudit.otomatik.__eq__(True)).count(),
            "denetci": db.query(DenetciYetkinlik).filter(DenetciYetkinlik.aktif.__eq__(True)).count(),
            "son": OT.ayar(db, "denetim_son_calisma") or "—",
        }
        # tür sekmeleri: sayaçlar + seçili türün plan kalemleri ve denetimleri
        kat_of = OT.denetim_kategorileri(db)
        sekmeler = []
        for k, baslik, aciklama in OT.DENETIM_TURLERI:
            ki = [i for i in kalemler if (i.kategori or "Süreç") == k]
            sekmeler.append({"kod": k, "baslik": baslik, "aciklama": aciklama, "kalem": len(ki), "acilan": sum(1 for i in ki if i.audit_id),
                             "kritik": sum(1 for h in tum_hepsi if h.get("kategori") == k and h["seviye"] == "kritik")})
        secili = next((s for s in sekmeler if s["kod"] == tur), None)
        plan, denetimler = [], []
        if tur:
            bugun = date.today()
            for p in progs:
                for i in sorted((i for i in p.items if (i.kategori or "Süreç") == tur), key=lambda i: (i.planlanan_ay or 0, i.plan_gunu or 0)):
                    t = OT.kalem_tarihi(i, p.yil)
                    plan.append({"i": i, "tarih": t, "gec": bool(t and t < bugun and not i.audit_id and i.durum not in ("Tamamlandı", "İptal"))})
            denetimler = sorted((a for a in db.query(InternalAudit).all() if kat_of.get(a.id) == tur),
                                key=lambda a: (a.planlanan_tarih or date.max), reverse=False)
            ozet.update({"kalem": len(plan), "acilan": sum(1 for x in plan if x["i"].audit_id),
                         "tamam": sum(1 for a in denetimler if a.durum == "Tamamlandı"), "geciken": sum(1 for x in plan if x["gec"])
                         + sum(1 for a in denetimler if a.durum in ("Planlandı", "Devam Ediyor") and a.planlanan_tarih and a.planlanan_tarih < bugun),
                         "bulgu": sum(1 for a in denetimler for f in a.findings if (f.bulgu_tipi or "").startswith("Uygunsuzluk") and not f.capa_id)})
            benim = [h for h in benim if h.get("kategori") == tur]
        return render_template("audit_automation.html", tum=tum, benim=benim, ayarlar=ayarlar, ozet=ozet,
                               can_edit=_editor(), yil=yil, tur=tur, sekmeler=sekmeler, secili=secili, plan=plan, denetimler=denetimler,
                               toplam_kritik=sum(1 for h in tum_hepsi if h["seviye"] == "kritik"))
    finally:
        db.close()


@app.route("/audits/auditors")
@login_required
def auditor_list():
    db = get_db()
    try:
        yil = date.today().year
        yuk = OT._yuk(db, yil)
        denetciler = db.query(DenetciYetkinlik).order_by(DenetciYetkinlik.aktif.desc(), DenetciYetkinlik.denetci_no, DenetciYetkinlik.ad_soyad).all()
        return render_template("auditor_list.html", denetciler=denetciler, yuk=yuk, yil=yil, NIT=DENETCI_NITELIKLERI,
                               bugun=date.today(), can_edit=_editor())
    finally:
        db.close()


@app.route("/audits/auditors/<int:did>", methods=["GET", "POST"])
@app.route("/audits/auditors/new", methods=["GET", "POST"], defaults={"did": None})
@login_required
def auditor_detail(did):
    db = get_db()
    try:
        d = db.get(DenetciYetkinlik, did) if did else DenetciYetkinlik(aktif=True)
        if did and not d:
            abort(404)
        if request.method == "POST":
            if not _editor():
                abort(403)
            ad = (request.form.get("ad_soyad") or "").strip()
            uid = request.form.get("user_id", type=int)
            if not ad and uid:
                u = db.get(User, uid); ad = u.ad_soyad if u else ""
            if not ad:
                flash("Ad soyad zorunludur.", "error")
                return redirect(request.url)
            d.ad_soyad = ad[:100]; d.user_id = uid or None
            d.denetci_no = request.form.get("denetci_no", type=int)
            d.nitelikler_json = json.dumps({k: bool(request.form.get(f"nit_{k}")) for k, _ in DENETCI_NITELIKLERI})
            d.denetim_turleri = ",".join(t for t in ("S", "P", "Ü") if request.form.get(f"tur_{t}"))
            d.kendi_surecleri = (request.form.get("kendi_surecleri") or "").upper().replace(" ", "")[:200] or None
            d.rol = request.form.get("rol") if request.form.get("rol") in ROLLER else "Denetçi"
            d.gecerlilik_tarihi = _tarih(request.form.get("gecerlilik_tarihi"))
            d.son_egitim_tarihi = _tarih(request.form.get("son_egitim_tarihi"))
            d.yillik_hedef = request.form.get("yillik_hedef", type=int)
            d.aktif = bool(request.form.get("aktif"))
            d.notlar = (request.form.get("notlar") or "").strip() or None
            if not did:
                db.add(d)
            db.commit()
            log_action(db, "Güncelleme" if did else "Oluşturma", detay=f"İç denetçi yetkinliği: {d.ad_soyad}")
            flash("Denetçi kaydı kaydedildi.", "success")
            return redirect(url_for("auditor_detail", did=d.id))
        users = db.query(User).filter_by(aktif=True).order_by(User.ad_soyad).all()
        egitimler, denetimler, oneri = [], [], None
        if d.user_id:
            egitimler = db.query(TrainingRecord).filter(TrainingRecord.kullanici_id == d.user_id).order_by(TrainingRecord.egitim_tarihi.desc()).all()
            denetimler = [a for a in db.query(InternalAudit).order_by(InternalAudit.planlanan_tarih.desc()).all()
                          if a.tetkik_eden_id == d.user_id or any(e.get("user_id") == d.user_id for e in OT.ekip_oku(a))][:30]
            if not d.kendi_surecleri and d.user:
                oneri = OT.departman_sureci(d.user.departman)
        ilgili = [e for e in egitimler if any(k in (e.egitim_adi or "").lower() for k in ("denet", "16949", "19011", "fmea", "spc", "msa", "ppap", "apqp", "core", "14001", "45001", "27001"))]
        uygunluk = []
        if did:
            for p in db.query(Process).filter(Process.ust_surec_id.is_(None)).order_by(Process.kod).all():
                if p.aktif_mi and len(p.kod) == 3:
                    ok, neden = OT.denetci_uygun_mu(d, "Süreç", p.kod)
                    uygunluk.append((p, ok, neden))
        return render_template("auditor_detail.html", d=d, users=users, NIT=DENETCI_NITELIKLERI, ROLLER=ROLLER,
                               egitimler=egitimler, ilgili=ilgili, denetimler=denetimler, uygunluk=uygunluk, oneri=oneri,
                               can_edit=_editor(), bugun=date.today())
    finally:
        db.close()


@app.route("/audits/program/item/<int:item_id>/ac", methods=["POST"])
@login_required
def audit_program_item_open(item_id):
    if not _editor():
        abort(403)
    db = get_db()
    try:
        it = db.get(AuditProgramItem, item_id)
        if not it:
            abort(404)
        if it.audit_id:
            return redirect(url_for("audit_detail", audit_id=it.audit_id))
        a, uy = OT.denetim_ac(db, it, it.program.yil)
        db.commit()
        log_action(db, "Oluşturma", detay=f"Plandan tetkik açıldı: {a.tetkik_no}")
        flash(f"Denetim açıldı: {a.tetkik_no}", "success")
        for u in uy:
            flash(u, "warning")
        return redirect(url_for("audit_checklist_run", audit_id=a.id))
    finally:
        db.close()


@app.route("/audits/program/<int:pid>/calendar")
@login_required
def audit_program_calendar(pid):
    db = get_db()
    try:
        prog = db.get(AuditProgram, pid)
        if not prog:
            abort(404)
        gruplar = {}
        for it in sorted(prog.items, key=lambda i: (i.kategori or "Süreç", i.kalem_adi or "", i.planlanan_ay or 0)):
            ad = it.kalem_adi or ((it.surec.kod + " " + it.surec.ad) if it.surec else (it.urun_adi or it.denetim_tipi))
            gruplar.setdefault(it.kategori or "Süreç", {}).setdefault(ad, {})[it.planlanan_ay or 0] = it
        sira = ["Süreç", "Saha", "Proses", "Ürün", "Tedarikçi", "Sertifikasyon", "Diğer"]
        gruplar = sorted(gruplar.items(), key=lambda g: sira.index(g[0]) if g[0] in sira else 99)
        return render_template("audit_program_calendar.html", prog=prog, gruplar=gruplar, aylar=OT.AYLAR,
                               ekip_oku=OT.ekip_oku, can_edit=_editor(), bugun=date.today())
    finally:
        db.close()
