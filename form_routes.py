"""
DYS — Dinamik Formlar (2026)
Mevcut modüllere uymayan formlar DYS'de elektronik olarak doldurulur, onaylanır ve saklanır.
Form alanları JSON ile tanımlanır; taslak tanımlar 'Dinamik Form Taslakları' JSON dosyalarından içe aktarılır.
"""
import io
import json
from datetime import datetime

from flask import render_template, request, redirect, url_for, flash, abort, session, send_file

from app_runtime import host as _host

_h = _host()
app = _h.app
login_required = _h.login_required
get_db = _h.get_db
log_action = _h.log_action

from models import FormTanim, FormKayit, Process, Document, User, FORM_ALAN_TIPLERI  # noqa: E402
from form_hesap import hesapla, hesap_alanlari_var, tanim_dogrula  # noqa: E402
import form_erisim as FE  # noqa: E402  (KVKK — kısıtlı form kayıtları)


def _uid_rol():
    return session.get("user_id"), session.get("rol")


def _gorebilir(k):
    uid, rol = _uid_rol()
    return FE.kayit_gorebilir(k.tanim, k, uid, rol)


def _can_manage():
    return session.get("rol") in ("Admin", "Doküman Kontrol")


def _can_fill():
    return session.get("rol") != "Sadece Görüntüleme"


def _alanlar(tanim):
    try:
        data = json.loads(tanim.alanlar_json or "[]")
        return [a for a in data if isinstance(a, dict) and a.get("ad")]
    except (TypeError, ValueError):
        return []


def _alan_anahtar(i):
    return f"alan_{i}"


def _surec_bul(db, kod):
    if not kod:
        return None
    kod = kod.split()[0]
    p = db.query(Process).filter(Process.kod == kod).first()
    if not p and "." in kod:
        p = db.query(Process).filter(Process.kod == kod.split(".")[0]).first()
    return p


def _ek_klasoru(kayit_no):
    from config import Config
    import os
    from werkzeug.utils import secure_filename
    d = os.path.join(Config.UPLOAD_FOLDER, "form_kayitlari", secure_filename(kayit_no.replace(" ", "_")))
    os.makedirs(d, exist_ok=True)
    return d


def _ekleri_kaydet(kayit_no, files):
    """Form kaydına eklenen dosyalar (izin verilen uzantılar) → UPLOAD_FOLDER/form_kayitlari/<kayit_no>/"""
    import os
    from config import Config
    from werkzeug.utils import secure_filename
    kayitli = []
    for f in files or []:
        if not f or not f.filename:
            continue
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in Config.ALLOWED_UPLOAD_EXTENSIONS:
            continue
        ad = secure_filename(f.filename) or f"ek.{ext}"
        f.save(os.path.join(_ek_klasoru(kayit_no), ad))
        kayitli.append(ad)
    return kayitli


def _next_kayit_no(db, tanim):
    yil = datetime.now().year
    prefix = f"{tanim.form_kodu}-{yil}-"
    mevcut = db.query(FormKayit.kayit_no).filter(FormKayit.kayit_no.like(f"{prefix}%")).all()
    n = 0
    for (k,) in mevcut:
        try:
            n = max(n, int(k.rsplit("-", 1)[1]))
        except (ValueError, IndexError):
            continue
    return f"{prefix}{n + 1:04d}"


@app.route("/forms")
@login_required
def forms_list():
    db = get_db()
    try:
        q = (request.args.get("q") or "").strip()
        surec = (request.args.get("surec") or "").strip()
        query = db.query(FormTanim)
        if q:
            query = query.filter((FormTanim.form_kodu.ilike(f"%{q}%")) | (FormTanim.ad.ilike(f"%{q}%")) | (FormTanim.eski_kod.ilike(f"%{q}%")))
        if surec:
            query = query.filter(FormTanim.form_kodu.like(f"{surec}%"))
        tanimlar = query.order_by(FormTanim.form_kodu).all()
        uid, rol = _uid_rol()
        sayilar = {t.id: len(FE.gorunur(t.kayitlar or [], uid, rol)) for t in tanimlar}
        bekleyen = sum(1 for k in db.query(FormKayit).filter(FormKayit.durum == "Onayda").all()
                       if FE.onaylayabilir(k.tanim, uid, rol) and FE.kayit_gorebilir(k.tanim, k, uid, rol))
        from config import Config
        return render_template("forms_list.html", tanimlar=tanimlar, sayilar=sayilar, q=q, surec=surec,
                               bekleyen=bekleyen, can_manage=_can_manage(), veri_klasoru=Config.FORM_VERI_KLASORU)
    finally:
        db.close()


@app.route("/forms/import", methods=["POST"])
@login_required
def forms_import():
    """Dinamik Form Taslakları (JSON) içe aktarma. Aynı form kodu varsa alanlar güncellenmez (üzerine yazmaz)."""
    if not _can_manage():
        abort(403)
    files = request.files.getlist("dosyalar")
    db = get_db()
    eklenen = atlanan = hatali = 0
    try:
        for f in files:
            try:
                d = json.load(f.stream)
                kod = (d.get("form_kodu") or "").strip()
                if not kod:
                    hatali += 1
                    continue
                if db.query(FormTanim).filter(FormTanim.form_kodu == kod).first():
                    atlanan += 1
                    continue
                surec = _surec_bul(db, kod)
                doc = db.query(Document).filter(Document.dokuman_no == kod).first()
                t = FormTanim(
                    form_kodu=kod, eski_kod=d.get("eski_kod"), ad=(d.get("form_adi") or kod)[:200],
                    surec_id=surec.id if surec else None, document_id=doc.id if doc else None,
                    revizyon_no=doc.revizyon_no if doc else 0,
                    alanlar_json=json.dumps(d.get("alanlar") or [], ensure_ascii=False),
                    onay_akisi=",".join(d.get("onay_akisi") or ["Dolduran", "Onaylayan"]),
                    saklama_suresi_ay=d.get("saklama_suresi_ay"), aktif=True,
                )
                db.add(t)
                eklenen += 1
            except (ValueError, UnicodeDecodeError):
                hatali += 1
        db.commit()
        log_action(db, "Oluşturma", detay=f"Dinamik form içe aktarma: {eklenen} eklendi, {atlanan} mevcut, {hatali} hatalı")
        flash(f"{eklenen} form tanımı eklendi, {atlanan} zaten vardı, {hatali} dosya okunamadı.", "success" if eklenen else "info")
        return redirect(url_for("forms_list"))
    finally:
        db.close()


@app.route("/forms/<int:tanim_id>", methods=["GET", "POST"])
@login_required
def form_detail(tanim_id):
    db = get_db()
    try:
        t = db.get(FormTanim, tanim_id)
        if not t:
            abort(404)
        if request.method == "POST" and request.form.get("islem") == "erisim":
            if session.get("rol") != "Admin":
                abort(403)
            kisitli = bool(request.form.get("kisitli"))
            kul = sorted({int(x) for x in request.form.getlist("erisim_kullanicilar") if str(x).isdigit()})
            t.erisim_json = json.dumps({"kisitli": kisitli, "kullanicilar": kul, "gerekce": (request.form.get("gerekce") or "").strip()[:300]}, ensure_ascii=False) if kisitli else None
            db.commit()
            if kisitli:
                from form_veri import kisitli_dosyayi_kaldir
                kisitli_dosyayi_kaldir(t)
            log_action(db, "Düzenleme", detay=f"Form erişimi: {t.form_kodu} " + (f"KISITLI (KVKK) · yetkili kullanıcı id {kul}" if kisitli else "herkese açık"))
            flash("Erişim ayarı kaydedildi." + (" Kayıtları yalnız kaydı giren, yetkililer ve Admin görür." if kisitli else ""), "success")
            return redirect(url_for("form_detail", tanim_id=t.id))
        if request.method == "POST":
            if not _can_manage():
                abort(403)
            try:
                alanlar = json.loads(request.form.get("alanlar_json") or "[]")
                assert isinstance(alanlar, list)
                for a in alanlar:
                    assert isinstance(a, dict) and a.get("ad")
                    if a.get("tip") not in FORM_ALAN_TIPLERI:
                        a["tip"] = "metin"
            except (ValueError, AssertionError):
                flash("Alan tanımı geçerli bir JSON listesi olmalı: [{\"ad\": \"...\", \"tip\": \"metin\"}]", "error")
                return redirect(url_for("form_detail", tanim_id=t.id))
            hatalar = tanim_dogrula(alanlar)
            if hatalar:
                flash("Form tanımı kaydedilmedi — " + " · ".join(hatalar[:6]), "error")
                return redirect(url_for("form_detail", tanim_id=t.id))
            t.alanlar_json = json.dumps(alanlar, ensure_ascii=False)
            t.ad = (request.form.get("ad") or t.ad)[:200]
            t.onay_akisi = request.form.get("onay_akisi") or t.onay_akisi
            t.aktif = bool(request.form.get("aktif"))
            db.commit()
            log_action(db, "Düzenleme", detay=f"Dinamik form tanımı güncellendi: {t.form_kodu}")
            flash("Form tanımı kaydedildi.", "success")
            return redirect(url_for("form_detail", tanim_id=t.id))
        uid, rol = _uid_rol()
        kayitlar = FE.gorunur(db.query(FormKayit).filter(FormKayit.tanim_id == t.id).order_by(FormKayit.olusturma_tarihi.desc()).limit(500).all(), uid, rol)[:200]
        erisim_kullanicilar = db.query(User).filter(User.aktif.__eq__(True)).order_by(User.ad_soyad).all() if rol == "Admin" else []
        return render_template("form_detail.html", t=t, alanlar=_alanlar(t), kayitlar=kayitlar,
                               erisim_kullanicilar=erisim_kullanicilar, yetkililer=FE.yetkililer(t),
                               yetkili_adlari=[u.ad_soyad for u in db.query(User).filter(User.id.in_(FE.yetkililer(t))).all()] if t.kisitli else [],
                               alanlar_json=json.dumps(_alanlar(t), ensure_ascii=False, indent=1),
                               tipler=FORM_ALAN_TIPLERI, can_manage=_can_manage(), can_fill=_can_fill())
    finally:
        db.close()


@app.route("/forms/<int:tanim_id>/new", methods=["GET", "POST"])
@login_required
def form_fill(tanim_id):
    if not _can_fill():
        abort(403)
    db = get_db()
    try:
        t = db.get(FormTanim, tanim_id)
        if not t or not (t.aktif is None or t.aktif):
            abort(404)
        alanlar = _alanlar(t)
        if request.method == "POST":
            veri, eksik = {}, []
            for i, a in enumerate(alanlar):
                k = _alan_anahtar(i)
                tip = a.get("tip")
                if tip in ("baslik", "hesap"):   # hesap alanları aşağıda sunucuda hesaplanır (istemci değeri kullanılmaz)
                    continue
                if tip == "coklu_secim":
                    v = request.form.getlist(k)
                elif tip == "evet_hayir":
                    v = "Evet" if request.form.get(k) else "Hayır"
                elif tip == "kontrol_listesi":
                    v = []
                    acik_zor = a.get("aciklama_zorunlu") or []
                    for j, madde in enumerate(a.get("maddeler") or []):
                        sonuc = (request.form.get(f"{k}__{j}") or "").strip()
                        acik = (request.form.get(f"{k}__{j}__a") or "").strip()
                        if a.get("zorunlu") and not sonuc:
                            eksik.append(f"{a['ad']} › {madde[:40]}")
                        if sonuc in acik_zor and not acik:
                            eksik.append(f"{a['ad']} › {madde[:40]} (açıklama)")
                        v.append({"madde": madde, "sonuc": sonuc, "aciklama": acik})
                elif tip == "tablo":
                    v = []
                    sut = a.get("sutunlar") or ["Değer"]
                    r = 0
                    while f"{k}__r{r}__c0" in request.form or r < int(a.get("satir") or 5):
                        satir = {s: (request.form.get(f"{k}__r{r}__c{c}") or "").strip() for c, s in enumerate(sut)}
                        if any(satir.values()):
                            v.append(satir)
                        r += 1
                        if r > 500:
                            break
                else:
                    v = (request.form.get(k) or "").strip()
                if a.get("zorunlu") and not v and tip != "kontrol_listesi":
                    eksik.append(a["ad"])
                veri[a["ad"]] = v
            if hesap_alanlari_var(alanlar):
                hesapla(alanlar, veri)
            kagit = bool(request.form.get("kagittan"))
            if kagit:
                # Kağıttan aktarımda taranmış kağıt zorunlu; alanlar kağıtta olduğu için zorunluluk aranmaz
                if not any(f and f.filename for f in request.files.getlist("ekler")):
                    flash("Kağıt formdan aktarımda taranmış kağıdın eklenmesi zorunludur.", "error")
                    return render_template("form_fill.html", t=t, alanlar=alanlar, veri=veri, users=db.query(User).filter_by(aktif=True).all())
                eksik = []
                veri["_kaynak"] = "Kağıt formdan aktarıldı" + (f" · çıktı no: {request.form.get('cikti_no').strip()}" if request.form.get("cikti_no") else "")
            if eksik:
                flash("Zorunlu alanlar boş: " + ", ".join(eksik), "error")
                return render_template("form_fill.html", t=t, alanlar=alanlar, veri=veri, users=db.query(User).filter_by(aktif=True).all())
            kayit_no = _next_kayit_no(db, t)
            ekler = _ekleri_kaydet(kayit_no, request.files.getlist("ekler"))
            if ekler:
                veri["_ekler"] = ekler
            kayit = FormKayit(tanim_id=t.id, kayit_no=kayit_no, form_revizyon_no=t.revizyon_no,
                              veriler_json=json.dumps(veri, ensure_ascii=False),
                              durum="Taslak" if request.form.get("taslak") else "Onayda",
                              olusturan_id=session.get("user_id"))
            db.add(kayit)
            db.commit()
            log_action(db, "Oluşturma", detay=f"Form kaydı: {kayit.kayit_no} ({t.form_kodu})")
            _veri_dosyasini_yenile(db, t)
            flash(f"Kayıt oluşturuldu: {kayit.kayit_no}", "success")
            return redirect(url_for("form_record", kayit_id=kayit.id))
        veri, kaynak = {}, None
        kid = request.args.get("kaynak", type=int)   # 'Bu kayıttan yeni hesap': önceki kaydın değerleriyle başla
        if kid:
            kaynak = db.get(FormKayit, kid)
            if kaynak and kaynak.tanim_id == t.id and _gorebilir(kaynak):
                try:
                    veri = {kk: vv for kk, vv in json.loads(kaynak.veriler_json or "{}").items() if not kk.startswith("_")}
                except ValueError:
                    veri = {}
            else:
                kaynak = None
        return render_template("form_fill.html", t=t, alanlar=alanlar, veri=veri, kaynak=kaynak,
                               users=db.query(User).filter_by(aktif=True).all())
    finally:
        db.close()


@app.route("/forms/records/<int:kayit_id>", methods=["GET", "POST"])
@login_required
def form_record(kayit_id):
    db = get_db()
    try:
        k = db.get(FormKayit, kayit_id)
        if not k:
            abort(404)
        if not _gorebilir(k):
            log_action(db, "Görüntüleme", detay=f"Kısıtlı form kaydına erişim reddedildi: {k.kayit_no}")
            abort(403)
        if request.method == "POST":
            islem = request.form.get("islem")
            if islem in ("onayla", "reddet"):
                if not FE.onaylayabilir(k.tanim, *_uid_rol()):
                    abort(403)
                if k.olusturan_id == session.get("user_id") and _h.app.config.get("ENFORCE_SEGREGATION_OF_DUTIES", True):
                    flash("Görevler ayrılığı: kendi oluşturduğunuz kaydı onaylayamazsınız.", "error")
                    return redirect(url_for("form_record", kayit_id=k.id))
                k.durum = "Onaylandı" if islem == "onayla" else "Reddedildi"
                k.onaylayan_id = session.get("user_id"); k.onay_tarihi = datetime.now()
                k.onay_notu = (request.form.get("not") or "").strip() or None
                db.commit()
                log_action(db, "Onay" if islem == "onayla" else "Red", detay=f"Form kaydı {k.kayit_no}: {k.durum}")
                flash(f"Kayıt {k.durum.lower()}.", "success")
            elif islem == "gonder" and k.durum == "Taslak" and k.olusturan_id == session.get("user_id"):
                k.durum = "Onayda"; db.commit()
                log_action(db, "Düzenleme", detay=f"Form kaydı onaya gönderildi: {k.kayit_no}")
            _veri_dosyasini_yenile(db, k.tanim)
            return redirect(url_for("form_record", kayit_id=k.id))
        try:
            veri = json.loads(k.veriler_json or "{}")
        except ValueError:
            veri = {}
        alanlar = _alanlar(k.tanim)
        if k.tanim.kisitli:
            log_action(db, "Görüntüleme", detay=f"Kısıtlı form kaydı görüntülendi: {k.kayit_no}")
        return render_template("form_record.html", k=k, t=k.tanim, veri=veri, can_manage=FE.onaylayabilir(k.tanim, *_uid_rol()),
                               alanlar=alanlar, hesapli=hesap_alanlari_var(alanlar), can_fill=_can_fill())
    finally:
        db.close()


@app.route("/forms/<int:tanim_id>/print")
@app.route("/forms/records/<int:kayit_id>/print")
@login_required
def form_print(tanim_id=None, kayit_id=None):
    """Sahada kullanım için A4 çıktı: boş form (çıktı numaralı) veya dolu kayıt. Ortak antetli."""
    db = get_db()
    try:
        k = db.get(FormKayit, kayit_id) if kayit_id else None
        t = k.tanim if k else db.get(FormTanim, tanim_id)
        if not t:
            abort(404)
        if k and not _gorebilir(k):
            abort(403)
        veri = {}
        if k:
            try:
                veri = json.loads(k.veriler_json or "{}")
            except ValueError:
                veri = {}
        kullanici = session.get("ad_soyad") or ""
        cikti_no = f"ÇKT-{t.form_kodu.replace(' ', '')}-{datetime.now().strftime('%y%m%d%H%M%S')}"
        log_action(db, "Çıktı", detay=f"Form çıktısı {cikti_no}: {t.form_kodu} " + (f"(kayıt {k.kayit_no})" if k else "(boş form)"))
        return render_template("form_print.html", t=t, k=k, veri=veri, alanlar=_alanlar(t), cikti_no=cikti_no,
                               kullanici=kullanici, simdi=datetime.now())
    finally:
        db.close()


@app.route("/forms/records/<int:kayit_id>/ek/<path:ad>")
@login_required
def form_record_attachment(kayit_id, ad):
    import os
    from werkzeug.utils import secure_filename
    db = get_db()
    try:
        k = db.get(FormKayit, kayit_id)
        if not k:
            abort(404)
        if not _gorebilir(k):
            abort(403)
        ekler = (json.loads(k.veriler_json or "{}") or {}).get("_ekler") or []
        if ad not in ekler or secure_filename(ad) != ad:
            abort(404)
        p = os.path.join(_ek_klasoru(k.kayit_no), ad)
        if not os.path.isfile(p):
            abort(404)
        log_action(db, "İndirme", detay=f"Form kaydı eki: {k.kayit_no} / {ad}")
        return send_file(p, as_attachment=True, download_name=ad)
    finally:
        db.close()


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _kayit_sorgusu(db, tanim_ids=None):
    """Toplu dışa aktarma filtreleri: baslangic, bitis (YYYY-AA-GG), durum, surec."""
    q = db.query(FormKayit)
    if tanim_ids is not None:
        q = q.filter(FormKayit.tanim_id.in_(tanim_ids))
    bas, bit, durum = request.args.get("baslangic"), request.args.get("bitis"), request.args.get("durum")
    try:
        if bas:
            q = q.filter(FormKayit.olusturma_tarihi >= datetime.strptime(bas, "%Y-%m-%d"))
        if bit:
            q = q.filter(FormKayit.olusturma_tarihi < datetime.strptime(bit, "%Y-%m-%d").replace(hour=23, minute=59, second=59))
    except ValueError:
        pass
    if durum:
        q = q.filter(FormKayit.durum == durum)
    return q.order_by(FormKayit.kayit_no)


@app.route("/forms/<int:tanim_id>/export")
@login_required
def form_export(tanim_id):
    from form_veri import form_calisma_kitabi
    db = get_db()
    try:
        t = db.get(FormTanim, tanim_id)
        if not t:
            abort(404)
        wb = form_calisma_kitabi([(t, FE.gorunur(_kayit_sorgusu(db, [t.id]).all(), *_uid_rol()))])
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        log_action(db, "İndirme", detay=f"Form kayıtları dışa aktarıldı: {t.form_kodu}")
        return send_file(buf, as_attachment=True, download_name=f"{t.form_kodu} kayitlar.xlsx", mimetype=XLSX_MIME)
    finally:
        db.close()


@app.route("/forms/export")
@login_required
def forms_export_all():
    """Toplu veri: filtrelere uyan tüm formların kayıtları tek Excel'de (uzun 'Tüm Veriler' + form başına sayfa)."""
    from form_veri import form_calisma_kitabi
    db = get_db()
    try:
        tanimlar = db.query(FormTanim).order_by(FormTanim.form_kodu).all()
        surec = (request.args.get("surec") or "").strip()
        if surec:
            tanimlar = [t for t in tanimlar if t.form_kodu.startswith(surec)]
        secili = request.args.getlist("form")
        if secili:
            tanimlar = [t for t in tanimlar if str(t.id) in secili]
        kayitlar = FE.gorunur(_kayit_sorgusu(db, [t.id for t in tanimlar]).all(), *_uid_rol())
        gruplu = [(t, [k for k in kayitlar if k.tanim_id == t.id]) for t in tanimlar]
        gruplu = [g for g in gruplu if g[1]]
        wb = form_calisma_kitabi(gruplu, tek_form=False)
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        log_action(db, "İndirme", detay=f"Toplu form verisi: {len(gruplu)} form, {len(kayitlar)} kayıt ({request.query_string.decode()[:150]})")
        return send_file(buf, as_attachment=True, download_name=f"Form_Verileri_{datetime.now():%Y%m%d_%H%M}.xlsx", mimetype=XLSX_MIME)
    finally:
        db.close()


def _veri_dosyasini_yenile(db, tanim):
    try:
        from form_veri import otomatik_disa_aktar
        otomatik_disa_aktar(db, tanim)
    except Exception:  # noqa: BLE001 — veri dosyası hatası kaydı engellemez
        pass
