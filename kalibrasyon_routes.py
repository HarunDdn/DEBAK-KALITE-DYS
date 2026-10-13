"""
DYS — Kalibrasyon: cihaz kartı, ölçü aletine özel sertifika yükleme, D04 F06 listesi içe aktarma (2026)
  /iatf/calibration/<id>                     cihaz kartı + sertifika geçmişi (+ düzenleme, sertifika yükleme)
  /iatf/calibration/cert/<cid>/download      sertifika indir / görüntüle
  /iatf/calibration/import                   D04 F06 Excel (.xlsx/.xlsm) içe aktarma + sertifika klasörü eşleştirme
app.py sonunda import edilir.
"""
import os
from datetime import date, datetime

from flask import render_template, request, redirect, url_for, flash, abort, session, send_file
from werkzeug.utils import secure_filename

from app_runtime import host as _host
_h = _host()
app = _h.app
login_required = _h.login_required
get_db = _h.get_db
log_action = _h.log_action

from config import Config  # noqa: E402
from models import CalibrationEquipment, KalibrasyonSertifika, User, KALIBRASYON_DURUMLARI  # noqa: E402
from helpers import resolve_file_path  # noqa: E402
import kalibrasyon_aktar as KA  # noqa: E402

SERTIFIKA_UZANTILARI = {".pdf", ".jpg", ".jpeg", ".png"}


def _can_edit():
    return session.get("rol") in ("Admin", "Doküman Kontrol")  # iatf_routes ile aynı yetki


def _tarih(v):
    try:
        return datetime.strptime(v, "%Y-%m-%d").date() if v else None
    except ValueError:
        return None


def _ay_ekle(t, ay):
    if not t or not ay:
        return None
    y, m = divmod(t.month - 1 + ay, 12)
    import calendar
    gun = min(t.day, calendar.monthrange(t.year + y, m + 1)[1])
    return date(t.year + y, m + 1, gun)


@app.route("/iatf/calibration/<int:eid>", methods=["GET", "POST"])
@login_required
def calibration_detail(eid):
    db = get_db()
    try:
        e = db.get(CalibrationEquipment, eid)
        if not e:
            abort(404)
        if request.method == "POST":
            if not _can_edit():
                abort(403)
            action = request.form.get("action")
            if action == "sertifika":
                f = request.files.get("dosya")
                if not f or not f.filename:
                    flash("Sertifika dosyası seçilmedi.", "error")
                    return redirect(url_for("calibration_detail", eid=eid))
                uz = os.path.splitext(f.filename)[1].lower()
                if uz not in SERTIFIKA_UZANTILARI:
                    flash("Sertifika PDF ya da görüntü (JPG/PNG) olmalıdır.", "error")
                    return redirect(url_for("calibration_detail", eid=eid))
                kt = _tarih(request.form.get("kalibrasyon_tarihi"))
                sonraki = _tarih(request.form.get("sonraki_kalibrasyon")) or _ay_ekle(kt, e.kalibrasyon_araligi_ay)
                sonuc = request.form.get("sonuc") or "Uygun"
                gecici = os.path.join(Config.UPLOAD_FOLDER, "kalibrasyon", str(e.id))
                os.makedirs(gecici, exist_ok=True)
                ad = secure_filename(f.filename) or f"sertifika{uz}"
                if not os.path.splitext(ad)[1]:
                    ad += uz
                yol = os.path.join(gecici, f"{datetime.now():%Y%m%d%H%M%S}_{ad}")
                f.save(yol)
                s = KA.sertifika_ekle(db, e, yol, yukleyen_id=session.get("user_id"),
                                      aciklama=(request.form.get("aciklama") or "").strip() or None,
                                      sertifika_no=(request.form.get("sertifika_no") or "").strip()[:120] or None,
                                      kalibrasyon_tarihi=kt, sonraki_kalibrasyon=sonraki,
                                      kurum=(request.form.get("kurum") or "").strip()[:150] or None, sonuc=sonuc,
                                      sapma=(request.form.get("sapma") or "").strip()[:200] or None)
                if request.form.get("kaydi_guncelle"):
                    if kt:
                        e.son_kalibrasyon = kt
                    if sonraki:
                        e.sonraki_kalibrasyon = sonraki
                    e.sertifika_no = s.sertifika_no or e.sertifika_no
                    e.kalibrasyon_sapma = s.sapma or e.kalibrasyon_sapma
                    e.durum = "Geçerli" if sonuc == "Uygun" else "Kullanım Dışı"
                db.commit()
                log_action(db, "Oluşturma", detay=f"Kalibrasyon sertifikası yüklendi: {e.ekipman_no} — {s.sertifika_no or s.dosya_adi}")
                flash("Sertifika yüklendi" + (" ve cihaz kaydı güncellendi." if request.form.get("kaydi_guncelle") else "."), "success")
            elif action == "sertifika_durum":
                c = db.get(KalibrasyonSertifika, request.form.get("cid", type=int))
                if c and c.ekipman_id == e.id:
                    if request.form.get("gecerli") == "1":
                        for x in e.sertifikalar:
                            x.gecerli = False
                        c.gecerli = True
                    else:
                        c.gecerli = False
                    db.commit()
                    log_action(db, "Güncelleme", detay=f"Kalibrasyon sertifikası durumu: {e.ekipman_no} — {c.dosya_adi} → {'geçerli' if c.gecerli else 'arşiv'}")
                    flash("Sertifika durumu güncellendi.", "success")
            elif action == "duzenle":
                for alan in ("ad", "tip", "konum", "seri_no", "imalatci", "kullanici_bolum", "hassasiyet", "olcum_araligi",
                             "kullanim_araligi", "birim", "sertifika_no", "izin_verilen_hata", "kalibrasyon_sapma", "msa_sonucu",
                             "msa_tipi", "dogrulama_periyodu", "dogrulama_yontemi", "karar"):
                    if alan in request.form:
                        v = (request.form.get(alan) or "").strip()
                        if alan == "ad" and not v:
                            continue
                        setattr(e, alan, v or None)
                e.kalibrasyon_araligi_ay = request.form.get("kalibrasyon_araligi_ay", type=int)
                e.son_kalibrasyon = _tarih(request.form.get("son_kalibrasyon"))
                e.sonraki_kalibrasyon = _tarih(request.form.get("sonraki_kalibrasyon")) or _ay_ekle(e.son_kalibrasyon, e.kalibrasyon_araligi_ay)
                if request.form.get("durum") in KALIBRASYON_DURUMLARI:
                    e.durum = request.form["durum"]
                e.sorumlu_id = request.form.get("sorumlu_id", type=int) or None
                db.commit()
                log_action(db, "Güncelleme", detay=f"Kalibrasyon cihazı güncellendi: {e.ekipman_no}")
                flash("Cihaz kaydı güncellendi.", "success")
            return redirect(url_for("calibration_detail", eid=eid))
        users = db.query(User).filter_by(aktif=True).order_by(User.ad_soyad).all()
        return render_template("calibration_detail.html", e=e, users=users, durumlar=KALIBRASYON_DURUMLARI,
                               can_edit=_can_edit(), bugun=date.today())
    finally:
        db.close()


@app.route("/iatf/calibration/cert/<int:cid>/download")
@login_required
def calibration_cert_download(cid):
    db = get_db()
    try:
        c = db.get(KalibrasyonSertifika, cid)
        if not c:
            abort(404)
        import dosya_depo as _dd
        yol = _dd.yol_coz(db, c.depo_id, c.dosya_adi, resolve_file_path(c.dosya_yolu, c.dosya_adi))
        if not yol or not os.path.isfile(yol):
            flash("Sertifika dosyası bulunamadı.", "error")
            return redirect(url_for("calibration_detail", eid=c.ekipman_id))
        indir = request.args.get("indir") == "1" or not c.dosya_adi.lower().endswith((".pdf", ".jpg", ".jpeg", ".png"))
        return send_file(yol, as_attachment=indir, download_name=c.dosya_adi)
    finally:
        db.close()


@app.route("/iatf/calibration/import", methods=["POST"])
@login_required
def calibration_import():
    if not _can_edit():
        abort(403)
    db = get_db()
    try:
        f = request.files.get("dosya")
        if not f or not f.filename.lower().endswith((".xlsx", ".xlsm")):
            flash("D04 F06 listesini .xlsx ya da .xlsm olarak seçin.", "error")
            return redirect(url_for("iatf_calibration"))
        d = os.path.join(Config.TEMP_FOLDER if getattr(Config, "TEMP_FOLDER", None) else Config.UPLOAD_FOLDER, "kalibrasyon_aktarim")
        os.makedirs(d, exist_ok=True)
        yol = os.path.join(d, f"{datetime.now():%Y%m%d%H%M%S}_{secure_filename(f.filename) or 'liste.xlsx'}")
        f.save(yol)
        try:
            R = KA.excel_aktar(db, yol)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            flash(f"Liste okunamadı: {exc}", "error")
            return redirect(url_for("iatf_calibration"))
        log_action(db, "Oluşturma", detay=f"D04 F06 kalibrasyon listesi aktarıldı: {R['yeni']} yeni, {R['guncellenen']} güncellendi ({R['sayfa']})")
        flash(f"'{R['sayfa']}' aktarıldı: {R['toplam']} cihaz — {R['yeni']} yeni, {R['guncellenen']} güncellendi"
              + (f", {R['ortak_kod']} ortak kodlu cihaz ayrı numaralandı" if R["ortak_kod"] else "")
              + (f". Önceki listede olup bu listede olmayan {R['listede_yok']} cihaz silinmedi." if R["listede_yok"] else "."), "success")
        for u in R["uyari"][:5]:
            flash(u, "warning")
        return redirect(url_for("iatf_calibration"))
    finally:
        db.close()
