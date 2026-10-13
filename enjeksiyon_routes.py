"""
DYS — M03 F08 Ürün Enjeksiyon Parametreleri (2026)
Müşteri bazlı 14 Excel formu (M03 F08–F21) yerine tek form: üstte ürün seçilir, ürünün geçerli parametre kartı
(değer ± sapma, makine, malzeme) ve ayar geçmişi açılır; yeni ayar önceki değerlerle dolu gelir, tolerans dışı
değişiklikler işaretlenir; onaylanan kayıt geçerli kart olur (görevler ayrılığı: kaydı giren onaylayamaz).
  /uretim/enjeksiyon-parametreleri                 ürün seçimi + kart + geçmiş + yeni ayar
  /uretim/enjeksiyon-parametreleri/kayit/<id>      onay / red
  /uretim/enjeksiyon-parametreleri/yeni-urun       şablondan yeni ürün
  /uretim/enjeksiyon-parametreleri/<urun>/yazdir   sahaya parametre kartı çıktısı
  /uretim/enjeksiyon-parametreleri/aktar           'DYS Aktarım/Enjeksiyon/enjeksiyon_parametreleri.json' içe aktarma
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

from models import EnjeksiyonUrun, EnjeksiyonParametreKaydi  # noqa: E402
from enjeksiyon_veri import tolerans, sayi, karsilastir, sablonlar, paketten_yukle  # noqa: E402,F401

YETKILI = ("Admin", "Doküman Kontrol")


def _onaylayabilir():
    return session.get("rol") in YETKILI


def _girebilir():
    return session.get("rol") != "Sadece Görüntüleme"


def _form_parametreler(form):
    ps = []
    i = 0
    while f"p_ad_{i}" in form:
        ad = (form.get(f"p_ad_{i}") or "").strip()
        if ad:
            ps.append({"ad": ad[:120], "deger": (form.get(f"p_deger_{i}") or "").strip()[:40], "sapma": (form.get(f"p_sapma_{i}") or "").strip()[:20]})
        i += 1
        if i > 300:
            break
    return ps


@app.route("/uretim/enjeksiyon-parametreleri", methods=["GET", "POST"])
@login_required
def enjeksiyon_parametreleri():
    db = get_db()
    try:
        if request.method == "POST":
            if not _girebilir():
                abort(403)
            u = db.get(EnjeksiyonUrun, request.form.get("urun_id", type=int))
            if not u:
                abort(404)
            ps = _form_parametreler(request.form)
            if not any(p["deger"] for p in ps):
                flash("En az bir parametre değeri girilmelidir.", "error")
                return redirect(url_for("enjeksiyon_parametreleri", urun=u.id, yeni=1))
            try:
                t = datetime.strptime(request.form.get("tarih") or "", "%Y-%m-%d").date()
            except ValueError:
                t = date.today()
            onceki = u.gecerli
            k = EnjeksiyonParametreKaydi(urun_id=u.id, tarih=t, makina=(request.form.get("makina") or "").strip()[:60] or None,
                                         malzeme=(request.form.get("malzeme") or "").strip()[:100] or None,
                                         parametreler_json=json.dumps(ps, ensure_ascii=False), kaynak="DYS", durum="Onayda",
                                         aciklama=(request.form.get("aciklama") or "").strip() or None, kaydeden_id=session.get("user_id"))
            db.add(k); db.commit()
            disi = [p["ad"] for p in karsilastir(onceki, ps) if p["tolerans_disi"]]
            log_action(db, "Oluşturma", detay=f"Enjeksiyon parametre kaydı: {u.urun_adi} ({u.musteri}) — {len(ps)} parametre"
                       + (f"; tolerans dışı değişiklik: {', '.join(disi)}" if disi else ""))
            flash("Parametre kaydı onaya gönderildi." + (f" Önceki ayara göre tolerans dışı değişen: {', '.join(disi[:6])}." if disi else ""),
                  "warning" if disi else "success")
            return redirect(url_for("enjeksiyon_parametreleri", urun=u.id))

        urunler = db.query(EnjeksiyonUrun).filter(EnjeksiyonUrun.aktif.__eq__(True)).order_by(EnjeksiyonUrun.musteri, EnjeksiyonUrun.urun_adi).all()
        musteriler = sorted({u.musteri or "—" for u in urunler})
        secili = db.get(EnjeksiyonUrun, request.args.get("urun", type=int)) if request.args.get("urun") else None
        bekleyen = db.query(EnjeksiyonParametreKaydi).filter(EnjeksiyonParametreKaydi.durum == "Onayda").count()
        ctx = {"urunler": urunler, "musteriler": musteriler, "secili": secili, "bekleyen": bekleyen,
               "f_musteri": request.args.get("musteri") or (secili.musteri if secili else ""), "can_approve": _onaylayabilir(),
               "can_fill": _girebilir(), "sablonlar": sablonlar(db), "bugun": date.today()}
        if secili:
            g = secili.gecerli
            gecmis = sorted(secili.kayitlar, key=lambda k: (k.tarih or date.min, k.id), reverse=True)
            yeni = request.args.get("yeni") == "1"
            taban = g.parametreler if g else next(([{"ad": p["ad"], "deger": "", "sapma": p.get("sapma", "")} for p in s["parametreler"]]
                                                   for s in ctx["sablonlar"] if s["ad"] == secili.sablon), [])
            ctx.update({"gecerli": g, "gecmis": gecmis, "yeni": yeni or not g, "taban": taban,
                        "bekleyenler": [k for k in gecmis if k.durum == "Onayda"],
                        "karsilastirmalar": {k.id: karsilastir(next((x for x in gecmis if x.durum == "Onaylandı" and (x.tarih or date.min, x.id) < (k.tarih or date.min, k.id)), None), k.parametreler) for k in gecmis[:40]}})
        return render_template("enjeksiyon_parametreleri.html", **ctx)
    finally:
        db.close()


@app.route("/uretim/enjeksiyon-parametreleri/kayit/<int:kid>", methods=["POST"])
@login_required
def enjeksiyon_kayit_onay(kid):
    if not _onaylayabilir():
        abort(403)
    db = get_db()
    try:
        k = db.get(EnjeksiyonParametreKaydi, kid)
        if not k or k.durum != "Onayda":
            abort(404)
        if k.kaydeden_id == session.get("user_id") and _h.app.config.get("ENFORCE_SEGREGATION_OF_DUTIES", True):
            flash("Görevler ayrılığı: kendi girdiğiniz parametre kaydını onaylayamazsınız.", "error")
            return redirect(url_for("enjeksiyon_parametreleri", urun=k.urun_id))
        islem = request.form.get("islem")
        k.durum = "Onaylandı" if islem == "onayla" else "Reddedildi"
        k.onaylayan_id = session.get("user_id"); k.onay_tarihi = datetime.now()
        if request.form.get("not"):
            k.aciklama = ((k.aciklama or "") + f"\nOnay notu: {request.form['not'].strip()}").strip()
        db.commit()
        log_action(db, "Onay" if k.durum == "Onaylandı" else "Red", detay=f"Enjeksiyon parametre kaydı {k.urun.urun_adi}: {k.durum}")
        flash(f"Kayıt {k.durum.lower()}." + (" Ürünün geçerli parametre kartı güncellendi." if k.durum == "Onaylandı" else ""), "success")
        return redirect(url_for("enjeksiyon_parametreleri", urun=k.urun_id))
    finally:
        db.close()


@app.route("/uretim/enjeksiyon-parametreleri/yeni-urun", methods=["POST"])
@login_required
def enjeksiyon_yeni_urun():
    if not _onaylayabilir():
        abort(403)
    db = get_db()
    try:
        ad = (request.form.get("urun_adi") or "").strip()
        if not ad:
            flash("Ürün adı zorunludur.", "error")
            return redirect(url_for("enjeksiyon_parametreleri"))
        u = EnjeksiyonUrun(urun_adi=ad[:200], musteri=(request.form.get("musteri") or "").strip()[:100] or None,
                           tanim=(request.form.get("tanim") or "").strip()[:250] or None, sablon=request.form.get("sablon") or None, aktif=True)
        db.add(u); db.commit()
        log_action(db, "Oluşturma", detay=f"Enjeksiyon parametre kartı açıldı: {u.urun_adi} ({u.musteri})")
        flash("Ürün eklendi; şablondaki parametrelerle ilk ayarı girin.", "success")
        return redirect(url_for("enjeksiyon_parametreleri", urun=u.id, yeni=1))
    finally:
        db.close()


@app.route("/uretim/enjeksiyon-parametreleri/<int:uid>/yazdir")
@login_required
def enjeksiyon_yazdir(uid):
    db = get_db()
    try:
        u = db.get(EnjeksiyonUrun, uid)
        if not u:
            abort(404)
        cikti_no = f"ÇKT-M03F08-{datetime.now():%y%m%d%H%M%S}"
        log_action(db, "Çıktı", detay=f"Enjeksiyon parametre kartı çıktısı {cikti_no}: {u.urun_adi}")
        return render_template("enjeksiyon_yazdir.html", u=u, k=u.gecerli, cikti_no=cikti_no, simdi=datetime.now())
    finally:
        db.close()


@app.route("/uretim/enjeksiyon-parametreleri/aktar", methods=["POST"])
@login_required
def enjeksiyon_aktar():
    if session.get("rol") != "Admin":
        abort(403)
    db = get_db()
    try:
        f = request.files.get("dosya")
        if not f or not f.filename.lower().endswith(".json"):
            flash("enjeksiyon_parametreleri.json dosyasını seçin.", "error")
            return redirect(url_for("enjeksiyon_parametreleri"))
        try:
            paket = json.loads(f.read().decode("utf-8-sig"))
        except ValueError:
            flash("JSON okunamadı.", "error")
            return redirect(url_for("enjeksiyon_parametreleri"))
        R = paketten_yukle(db, paket)
        log_action(db, "Oluşturma", detay=f"Enjeksiyon parametreleri aktarıldı: {R}")
        flash(f"Aktarıldı: {R['urun_yeni']} yeni ürün, {R['urun_var']} mevcut ürün, {R['kayit']} geçmiş ayar kaydı.", "success")
        return redirect(url_for("enjeksiyon_parametreleri"))
    finally:
        db.close()
