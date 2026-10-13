"""
DYS — Kalite modülü (2026): Tedarikçi Uygunsuzlukları (D02 F11, SCAR) ve Müşteri Şikayetleri (Y01.7 F03, 8D)
  /kalite/<tur>                       liste (tur = tedarikci | musteri): durum / önem / yıl / arama, açık – geciken sayaçları, Excel
  /kalite/<tur>/yeni, /kalite/<tur>/<id>   kayıt (TR / EN başlıklı form, tablolar, görseller)
  /kalite/<tur>/<id>/gorsel           görsel yükle (çoklu, sürükle-bırak, panodan yapıştır; işaretlenmiş görsel de buraya gelir)
  /kalite/gorsel/<gid>[/sil|/duzenle] görseli göster / sil / etiket – açıklama – rapora ekle
  /kalite/<tur>/<id>/pdf              A4 PDF rapor (TR + EN başlıklar, fotoğraflar, imza alanları) — tedarikçiye / müşteriye gönderilir
  /kalite/<tur>/<id>/gonderildi       raporun gönderildiğini kaydeder (tarih, kime)
  /kalite/<tur>/<id>/dof              kayıttan DYS DÖF açar ve bağlar
Yetki: kayıt / görsel: tüm kullanıcılar (Sadece Görüntüleme hariç); silme: Admin / Doküman Kontrol.
"""
import io
import json
import os
from datetime import date, datetime

from flask import render_template, request, redirect, url_for, flash, abort, session, send_file
import dosya_depo as _dd
from werkzeug.utils import secure_filename

from app_runtime import host as _host
_h = _host()
app = _h.app
login_required = _h.login_required
get_db = _h.get_db
log_action = _h.log_action

from models import TedarikciUygunsuzluk, MusteriSikayeti, KaliteGorsel, Supplier, User, CorrectiveAction  # noqa: E402
from helpers import next_sequence_no  # noqa: E402
import kalite_spec as KS  # noqa: E402

TURLER = {"tedarikci": ("TU", TedarikciUygunsuzluk, "Tedarikçi Uygunsuzlukları", "Supplier Nonconformances"),
          "musteri": ("MS", MusteriSikayeti, "Müşteri Şikayetleri", "Customer Complaints")}
GORSEL_UZANTI = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}
KAPALI = ("Kapatıldı", "İptal")
DURUM_EN = {"Açık": "Open", "Tedarikçi yanıtı bekleniyor": "Awaiting supplier response", "Değerlendirmede": "Under review",
            "Etkinlik doğrulamada": "Effectiveness verification", "Kapatıldı": "Closed", "İptal": "Cancelled", "Geçici önlem (D3)": "Containment (D3)",
            "Kök neden analizi (D4)": "Root cause analysis (D4)", "Aksiyonlar (D5–D6)": "Actions (D5–D6)", "Etkinlik doğrulamada (D7)": "Verification (D7)"}


def _yazabilir():
    return session.get("rol") != "Sadece Görüntüleme"


def _yonetici():
    return session.get("rol") in ("Admin", "Doküman Kontrol")


def _tur(tur):
    if tur not in TURLER:
        abort(404)
    return TURLER[tur]


def _tarih(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except ValueError:
        return None


def _sayi(s):
    """Türkçe biçim de okunur: 1.250,50 → 1250.5 · 5.000 → 5000 · 4,31 → 4.31"""
    import re
    s = (s or "").strip().replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _gorsel_klasoru(tk, no):
    from config import Config
    d = os.path.join(Config.UPLOAD_FOLDER, "kalite", tk, secure_filename(no))
    os.makedirs(d, exist_ok=True)
    return d


def _gorseller(db, tk, kid):
    return db.query(KaliteGorsel).filter_by(tur=tk, kayit_id=kid).order_by(KaliteGorsel.sira, KaliteGorsel.id).all()


def gecikmis(k, tk):
    bugun = date.today()
    if k.durum in KAPALI:
        return False
    return bool((k.termin_8d and k.termin_8d < bugun) or (tk == "MS" and k.termin_d3 and k.termin_d3 < bugun and k.durum in ("Açık",)))


def _formdan(db, tk, k, f):
    spec, kolon = KS.SPEC[tk], KS.KOLON[tk]
    veri = k.veri
    for a in KS.alanlar(tk):
        anahtar, tip = a[0], a[3]
        if tip == "salt":
            continue
        if tip == "tablo":
            sut = a[4]; satirlar = []
            for i in range(int(f.get(f"{anahtar}__say") or 0)):
                s = {c: (f.get(f"{anahtar}__{i}__{j}") or "").strip() for j, c in enumerate(sut)}
                if any(s.values()):
                    satirlar.append(s)
            veri[anahtar] = satirlar; continue
        if tip == "coklu":
            veri[anahtar] = f.getlist(anahtar); continue
        ham = (f.get(anahtar) or "").strip()
        if tip == "tarih":
            v = _tarih(ham)
        elif tip == "sayi":
            v = _sayi(ham)
        elif tip in ("kullanici", "tedarikci"):
            v = int(ham) if ham.isdigit() else None
        elif tip == "durum":
            v = ham if ham in spec["durumlar"] else (k.durum or "Açık")
        else:
            v = ham or None
        if anahtar in kolon:
            setattr(k, anahtar, v)
        else:
            veri[anahtar] = v.isoformat() if isinstance(v, date) else v
    if tk == "TU":
        s = db.get(Supplier, k.tedarikci_id) if k.tedarikci_id else None
        k.tedarikci_ad = s.ad if s else ((f.get("tedarikci_ad") or "").strip() or k.tedarikci_ad)
    if k.durum == "Kapatıldı" and not k.kapanis_tarihi:
        k.kapanis_tarihi = date.today()
    k.veri_json = json.dumps(veri, ensure_ascii=False)


def _deger(k, anahtar):
    return getattr(k, anahtar) if hasattr(k, anahtar) and anahtar not in ("veri",) else k.veri.get(anahtar)


@app.route("/kalite")
@login_required
def kalite_ana():
    return redirect(url_for("kalite_liste", tur="musteri"))


@app.route("/kalite/<tur>")
@login_required
def kalite_liste(tur):
    tk, M, ad, ad_en = _tur(tur)
    db = get_db()
    try:
        q = db.query(M)
        durum, onem, yil, ara = request.args.get("durum") or "acik", request.args.get("onem") or "", request.args.get("yil", type=int), (request.args.get("q") or "").strip()
        tum = q.order_by(M.tarih.desc(), M.id.desc()).all()
        def uyar(k):
            if durum == "acik" and k.durum in KAPALI: return False
            if durum not in ("acik", "hepsi") and k.durum != durum: return False
            if onem and k.onem != onem: return False
            if yil and k.tarih.year != yil: return False
            if ara:
                metin = " ".join(str(x) for x in (k.no, k.urun_no, k.urun_adi, getattr(k, "tedarikci_ad", None), getattr(k, "musteri", None),
                                                   getattr(k, "musteri_ref", None), k.veri.get("problem")) if x).lower()
                if ara.lower() not in metin: return False
            return True
        liste = [k for k in tum if uyar(k)]
        bu_yil = [k for k in tum if k.tarih.year == date.today().year]
        ozet = {"acik": sum(1 for k in tum if k.durum not in KAPALI), "geciken": sum(1 for k in tum if gecikmis(k, tk)),
                "bu_yil": len(bu_yil), "hatali": sum(k.hatali_miktar or 0 for k in bu_yil), "kritik": sum(1 for k in tum if k.onem == "Kritik" and k.durum not in KAPALI)}
        if request.args.get("excel"):
            from openpyxl import Workbook
            wb = Workbook(); ws = wb.active; ws.title = ad[:30]
            ws.append(["No", "Tarih / Date", "Tedarikçi / Supplier" if tk == "TU" else "Müşteri / Customer", "Ürün No / Part No", "Ürün / Part",
                       "Hata Türü / Defect", "Önem / Severity", "Hatalı / NOK Qty", "8D Termini / Due", "Durum / Status", "Sorumlu / Responsible", "Kapanış / Closed"])
            for k in liste:
                ws.append([k.no, k.tarih, k.tedarikci_ad if tk == "TU" else k.musteri, k.urun_no, k.urun_adi, k.veri.get("hata_turu"), k.onem,
                           k.hatali_miktar, k.termin_8d, k.durum, k.sorumlu.ad_soyad if k.sorumlu else "", k.kapanis_tarihi])
            buf = io.BytesIO(); wb.save(buf); buf.seek(0)
            log_action(db, "İndirme", detay=f"Kalite listesi Excel: {ad} ({len(liste)})")
            return send_file(buf, as_attachment=True, download_name=f"{ad} {date.today():%Y%m%d}.xlsx",
                             mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        yillar = sorted({k.tarih.year for k in tum} | {date.today().year}, reverse=True)
        return render_template("kalite_liste.html", tur=tur, tk=tk, ad=ad, ad_en=ad_en, liste=liste, ozet=ozet, spec=KS.SPEC[tk],
                               durum=durum, onem=onem, yil=yil, q=ara, yillar=yillar, gecikmis=gecikmis, yazabilir=_yazabilir(), bugun=date.today())
    finally:
        db.close()


@app.route("/api/canias/firmalar")
@login_required
def canias_firmalar():
    """CANIAS IASCUSTOMER onaylı firma araması (JSON) — müşteri/tedarikçi seçicisi için."""
    import canias
    from flask import jsonify
    q = (request.args.get("q") or "").strip()
    try:
        limit = min(max(int(request.args.get("limit", 30)), 1), 100)
    except (TypeError, ValueError):
        limit = 30
    return jsonify({"aktif": canias.aktif(), "firmalar": canias.ara(q, limit)})


def _musteriler(db):
    from models import PPAPSubmission, CustomerSpecificRequirement
    s = set()
    for M, alan in ((MusteriSikayeti, "musteri"), (PPAPSubmission, "musteri"), (CustomerSpecificRequirement, "musteri")):
        try:
            s |= {r[0] for r in db.query(getattr(M, alan)).distinct().all() if r[0]}
        except Exception:  # noqa: BLE001 — eski şemada tablo yoksa atlanır
            pass
    return sorted(s)


@app.route("/kalite/<tur>/yeni", methods=["GET", "POST"])
@app.route("/kalite/<tur>/<int:kid>", methods=["GET", "POST"])
@login_required
def kalite_kayit(tur, kid=None):
    tk, M, ad, ad_en = _tur(tur)
    db = get_db()
    try:
        k = db.get(M, kid) if kid else None
        if kid and not k:
            abort(404)
        if request.method == "POST":
            if not _yazabilir():
                abort(403)
            if request.form.get("islem") == "sil":
                if not _yonetici():
                    abort(403)
                for g in _gorseller(db, tk, k.id):
                    try:
                        os.remove(g.dosya_yolu)
                    except OSError:
                        pass
                    db.delete(g)
                no = k.no; db.delete(k); db.commit()
                log_action(db, "Silme", detay=f"{ad}: {no} silindi"); flash(f"{no} silindi.", "success")
                return redirect(url_for("kalite_liste", tur=tur))
            yeni = k is None
            if yeni:
                k = M(no=next_sequence_no(db, M, "no", KS.SPEC[tk]["onek"]), tarih=date.today(), durum="Açık", olusturan_id=session.get("user_id"),
                      sorumlu_id=session.get("user_id"))
                db.add(k)
            _formdan(db, tk, k, request.form)
            k.tarih = k.tarih or date.today()
            db.commit()
            log_action(db, "Oluşturma" if yeni else "Düzenleme", detay=f"{ad}: {k.no} · {k.durum}")
            flash(f"{k.no} kaydedildi." + (" Görselleri aşağıdan ekleyebilirsiniz." if yeni else ""), "success")
            return redirect(url_for("kalite_kayit", tur=tur, kid=k.id))
        if not k:
            k = M(no="(yeni)", tarih=date.today(), durum="Açık", sorumlu_id=session.get("user_id"))
            if tk == "MS":
                from datetime import timedelta
                k.termin_d3 = date.today() + timedelta(days=1); k.termin_8d = date.today() + timedelta(days=10)
        return render_template("kalite_kayit.html", tur=tur, tk=tk, ad=ad, ad_en=ad_en, k=k, spec=KS.SPEC[tk], deger=_deger,
                               gorseller=_gorseller(db, tk, k.id) if k.id else [], etiketler=KS.ETIKET, gecikmis=gecikmis(k, tk) if k.id else False,
                               kullanicilar=db.query(User).filter(User.aktif.__eq__(True)).order_by(User.ad_soyad).all(),
                               tedarikciler=db.query(Supplier).order_by(Supplier.ad).all() if tk == "TU" else [],
                               musteriler=_musteriler(db) if tk == "MS" else [], yazabilir=_yazabilir(), yonetici=_yonetici(),
                               canias_aktif=__import__("canias").aktif())
    finally:
        db.close()


@app.route("/kalite/<tur>/<int:kid>/gorsel", methods=["POST"])
@login_required
def kalite_gorsel_yukle(tur, kid):
    tk, M, ad, _ = _tur(tur)
    if not _yazabilir():
        abort(403)
    db = get_db()
    try:
        k = db.get(M, kid) or abort(404)
        n = 0; sira = len(_gorseller(db, tk, kid))
        for f in request.files.getlist("gorsel"):
            if not f or not f.filename:
                continue
            ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else "png"
            if ext not in GORSEL_UZANTI:
                flash(f"{f.filename}: yalnız görsel dosyası (jpg, png, gif, bmp, webp) eklenebilir.", "error"); continue
            ad_ = secure_filename(f.filename) or f"gorsel.{ext}"
            ad_ = f"{datetime.now():%Y%m%d%H%M%S%f}_{ad_}"
            yol = os.path.join(_gorsel_klasoru(tk, k.no), ad_)
            f.save(yol)
            try:   # gerçekten görsel mi (içerik kontrolü)
                from PIL import Image
                with Image.open(yol) as im:
                    im.verify()
            except Exception:  # noqa: BLE001
                os.remove(yol); flash(f"{f.filename}: görsel dosyası okunamadı.", "error"); continue
            sira += 1
            db.add(KaliteGorsel(tur=tk, kayit_id=kid, dosya_adi=ad_, dosya_yolu=yol, depo_id=_dd.ekle(db, yol=yol, ad=ad_), sira=sira, pdfte=True,
                                etiket=request.form.get("etiket") or "Hatalı (NOK)", aciklama=(request.form.get("aciklama") or "").strip()[:300] or None,
                                yukleyen_id=session.get("user_id")))
            n += 1
        db.commit()
        if n:
            log_action(db, "Oluşturma", detay=f"{ad}: {k.no} — {n} görsel eklendi")
        if request.headers.get("X-Requested-With") == "fetch":
            return {"ok": True, "eklenen": n}
        flash(f"{n} görsel eklendi." if n else "Görsel eklenmedi.", "success" if n else "info")
        return redirect(url_for("kalite_kayit", tur=tur, kid=kid) + "#gorseller")
    finally:
        db.close()


def _gorsel_ve_kayit(db, gid):
    g = db.get(KaliteGorsel, gid) or abort(404)
    M = TedarikciUygunsuzluk if g.tur == "TU" else MusteriSikayeti
    return g, db.get(M, g.kayit_id), ("tedarikci" if g.tur == "TU" else "musteri")


@app.route("/kalite/gorsel/<int:gid>")
@login_required
def kalite_gorsel(gid):
    db = get_db()
    try:
        g, k, tur = _gorsel_ve_kayit(db, gid)
        p = _dd.yol_coz(db, g.depo_id, g.dosya_adi, g.dosya_yolu)
        if not p:
            abort(404)
        return send_file(p, download_name=g.dosya_adi, max_age=3600)
    finally:
        db.close()


@app.route("/kalite/gorsel/<int:gid>/duzenle", methods=["POST"])
@login_required
def kalite_gorsel_duzenle(gid):
    if not _yazabilir():
        abort(403)
    db = get_db()
    try:
        g, k, tur = _gorsel_ve_kayit(db, gid)
        if request.form.get("islem") == "sil":
            try:
                os.remove(g.dosya_yolu)
            except OSError:
                pass
            db.delete(g); db.commit(); log_action(db, "Silme", detay=f"Kalite görseli silindi: {k.no if k else ''} / {g.dosya_adi}")
        else:
            g.etiket = request.form.get("etiket") or g.etiket
            g.aciklama = (request.form.get("aciklama") or "").strip()[:300] or None
            g.pdfte = bool(request.form.get("pdfte"))
            if request.form.get("sira", type=int) is not None:
                g.sira = request.form.get("sira", type=int)
            db.commit()
        return redirect(url_for("kalite_kayit", tur=tur, kid=g.kayit_id) + "#gorseller")
    finally:
        db.close()


@app.route("/kalite/<tur>/<int:kid>/pdf")
@login_required
def kalite_pdf(tur, kid):
    tk, M, ad, _ = _tur(tur)
    db = get_db()
    try:
        k = db.get(M, kid) or abort(404)
        import kalite_pdf as KP
        _gl = _gorseller(db, tk, kid)
        for _g in _gl:                                        # dosya yoksa depodan yerel kopya (kalıcı yazılmaz)
            _g._yerel = _dd.yol_coz(db, _g.depo_id, _g.dosya_adi, _g.dosya_yolu)
        buf = KP.rapor(k, tk, _gl, hazirlayan=k.sorumlu.ad_soyad if k.sorumlu else (session.get("ad_soyad") or ""))
        log_action(db, "Çıktı", detay=f"{ad}: {k.no} A4 PDF raporu")
        kime = (k.tedarikci_ad if tk == "TU" else k.musteri) or ""
        dosya = secure_filename(f"{k.no} {kime}".strip()) or k.no
        return send_file(buf, mimetype="application/pdf", as_attachment=bool(request.args.get("indir")), download_name=f"{dosya}.pdf")
    finally:
        db.close()


@app.route("/kalite/<tur>/<int:kid>/gonderildi", methods=["POST"])
@login_required
def kalite_gonderildi(tur, kid):
    tk, M, ad, _ = _tur(tur)
    if not _yazabilir():
        abort(403)
    db = get_db()
    try:
        k = db.get(M, kid) or abort(404)
        v = k.veri
        g = v.get("gonderimler") or []
        g.append({"tarih": datetime.now().isoformat(timespec="minutes"), "kime": (request.form.get("kime") or "").strip()[:200],
                  "gonderen": session.get("ad_soyad") or "", "not": (request.form.get("not") or "").strip()[:300]})
        v["gonderimler"] = g; k.veri_json = json.dumps(v, ensure_ascii=False); k.gonderim_tarihi = datetime.now()
        if tk == "TU" and k.durum == "Açık":
            k.durum = "Tedarikçi yanıtı bekleniyor"
        db.commit()
        log_action(db, "Düzenleme", detay=f"{ad}: {k.no} raporu gönderildi → {g[-1]['kime']}")
        flash("Gönderim kaydedildi.", "success")
        return redirect(url_for("kalite_kayit", tur=tur, kid=kid))
    finally:
        db.close()


@app.route("/kalite/<tur>/<int:kid>/dof", methods=["POST"])
@login_required
def kalite_dof(tur, kid):
    tk, M, ad, _ = _tur(tur)
    if not _yazabilir():
        abort(403)
    db = get_db()
    try:
        k = db.get(M, kid) or abort(404)
        if not k.dof_id:
            v = k.veri
            c = CorrectiveAction(dof_no=next_sequence_no(db, CorrectiveAction, "dof_no", "DÖF"),
                                 baslik=f"{k.no} {k.urun_adi or k.urun_no or ''} — {v.get('hata_turu') or 'uygunsuzluk'}"[:200],
                                 kaynak_tipi="Müşteri Şikayeti" if tk == "MS" else "Uygunsuzluk", tespit_tarihi=k.tarih,
                                 acan_id=session.get("user_id"), sorumlu_id=k.sorumlu_id, planlanan_tarih=k.termin_8d, durum="Açık",
                                 tanim=f"{ad} {k.no} — {(k.musteri if tk == 'MS' else k.tedarikci_ad) or ''}\n{v.get('problem') or ''}")
            if tk == "MS":
                c.sikayet_musteri = (k.musteri or "")[:200]
            db.add(c); db.flush(); k.dof_id = c.id; db.commit()
            log_action(db, "Oluşturma", detay=f"{ad}: {k.no} → DÖF {c.dof_no}")
            flash(f"{c.dof_no} açıldı.", "success")
        return redirect(url_for("kalite_kayit", tur=tur, kid=kid))
    finally:
        db.close()
