"""
DYS — D03 Bakım Formu (2026): D03 klasöründeki 50 bakım formu tek ekranda
  /bakim?ekipman=<id>&form=<kod>   üstte ekipman (makine / kalıp) ve bakım formu seçilir; alt kısım şablona göre değişir
                                   (periyodik bakım, günlük kontrol, bakım kartı, kalıp bakımı). Kaydedilince ekipmanın bakım geçmişine düşer.
  /bakim/ekipman                   makine ve kalıp listesi: son bakım kaydı; ekleme; başlangıç listesini yükleme
  /bakim/ekipman/<id>              ekipman kartı: uygun formlar ve son kayıtları, bakım geçmişi (bakım kartı), bilgiler
  /bakim/kayit/<id>                kayıt görünümü / A4 çıktı; Admin silebilir
  /bakim/kayitlar                  tüm kayıtlar (filtre) + Excel
Şablonlar: data/bakim_formlari.json (_calisma/bakim_formlari_hazirla.py). Ekipman türü + marka → uygun şablonlar (marka boşsa türdeki tümü).
Periyot: gün bazlı (günlük / aylık / 3–6 aylık / yıllık / 6 yıllık) → sonraki tarih ve gecikme; saat bazlı → son çalışma saati + periyot;
kalıp → baskı sayısı. Yetki: kayıt / ekipman ekleme Sadece Görüntüleme hariç; silme Admin / Doküman Kontrol.
"""
import io
import json
import os
from datetime import date, datetime, timedelta

from flask import render_template, request, redirect, url_for, flash, abort, session, send_file

from app_runtime import host as _host
_h = _host()
app = _h.app
login_required = _h.login_required
get_db = _h.get_db
log_action = _h.log_action

from models import BakimEkipman, BakimKaydi, User  # noqa: E402
from helpers import next_sequence_no  # noqa: E402

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_CACHE = {"mtime": None, "veri": None}
SONUCLAR = ("Tamamlandı", "Tamamlanmadı — devam edecek", "Arıza / uygunsuzluk tespit edildi")
DURUM_MAKINE = ("Yapıldı", "Uygun Değil", "Kapsam Dışı")
DURUM_KALIP = ("Uygun", "Uygun Değil", "Kapsam Dışı")
BOLUMLER = ("Üretim", "Kalıphane", "Kalite", "Depo", "Genel")


def veri():
    p = os.path.join(_DATA, "bakim_formlari.json")
    mt = os.path.getmtime(p)
    if _CACHE["mtime"] != mt:
        _CACHE["veri"], _CACHE["mtime"] = json.load(open(p, encoding="utf-8")), mt
    return _CACHE["veri"]


def sablon(kod):
    return next((s for s in veri()["sablonlar"] if s["kod"] == kod), None)


def _k(s):
    return " ".join((s or "").replace("İ", "i").replace("I", "ı").lower().split())


def uygun_sablonlar(e):
    """Ekipmanın türüne uyan şablonlar; markası belliyse yalnız o markanın (veya markasız) şablonları."""
    out = []
    for s in veri()["sablonlar"]:
        if e.tur not in s["ekipman_turleri"]:
            continue
        if s.get("marka") and e.marka and _k(s["marka"]) != _k(e.marka):
            continue
        out.append(s)
    return out


def _yazabilir():
    return session.get("rol") != "Sadece Görüntüleme"


def _yonetici():
    return session.get("rol") in ("Admin", "Doküman Kontrol")


def _int(v):
    try:
        return int(str(v).replace(".", "").replace(" ", "")) if str(v or "").strip() else None
    except ValueError:
        return None


def _tarih(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except ValueError:
        return None


def son_kayit(kayitlar, form_kod):
    """Şablonun ekipmandaki son bakım kaydı (planlama CANIAS DBKT01'de yapılır; DYS yalnız kontrol listesi kaydı tutar)."""
    ks = sorted((k for k in kayitlar if k.form_kod == form_kod), key=lambda k: (k.tarih, k.id))
    return ks[-1] if ks else None


def _ekipman_ozet(db, ekipmanlar):
    kayitlar = {}
    for k in db.query(BakimKaydi).all():
        kayitlar.setdefault(k.ekipman_id, []).append(k)
    out = []
    for e in ekipmanlar:
        kl = kayitlar.get(e.id, [])
        n_sablon = len(uygun_sablonlar(e))
        son = max(kl, key=lambda k: (k.tarih, k.id)) if kl else None
        out.append({"e": e, "son": son, "sablon": n_sablon, "kayit": len(kl)})
    return out


# ─────────────────────────────── tek bakım formu ───────────────────────────────
@app.route("/bakim", methods=["GET", "POST"])
@login_required
def bakim_form():
    db = get_db()
    try:
        ekipmanlar = db.query(BakimEkipman).filter((BakimEkipman.aktif.is_(None) | BakimEkipman.aktif.__eq__(True))).order_by(BakimEkipman.tur, BakimEkipman.kod).all()
        e = db.get(BakimEkipman, request.values.get("ekipman", type=int) or 0)
        sablonlar = uygun_sablonlar(e) if e else []
        s = sablon(request.values.get("form") or "")
        if s and e and s["kod"] not in {x["kod"] for x in sablonlar}:
            s = None
        if not s and len(sablonlar) == 1:
            s = sablonlar[0]
        if request.method == "POST":
            if not _yazabilir():
                abort(403)
            if not (e and s):
                abort(400)
            f = request.form
            durumlar = DURUM_KALIP if s.get("kalip") else DURUM_MAKINE
            maddeler, eksik, uyg = [], [], 0
            for i, m in enumerate(s["maddeler"]):
                d = f.get(f"durum_{i}") or ""
                if d not in durumlar:
                    eksik.append(m["no"])
                uyg += d == "Uygun Değil"
                maddeler.append({"no": m["no"], "grup": m.get("grup", ""), "madde": m["madde"], "durum": d,
                                 "olcum": (f.get(f"olcum_{i}") or "").strip()[:60], "aciklama": (f.get(f"aciklama_{i}") or "").strip()[:500]})
            tarih = _tarih(f.get("tarih"))
            if eksik or not tarih or not (f.get("bakimci") or "").strip():
                flash(("Durumu seçilmeyen madde: " + ", ".join(eksik) + ". " if eksik else "") + ("Tarih ve bakımcı zorunludur." if not tarih or not (f.get("bakimci") or "").strip() else ""), "error")
                return _form_goster(db, ekipmanlar, e, sablonlar, s, f, maddeler)
            sonuc = f.get("sonuc") if f.get("sonuc") in SONUCLAR else (SONUCLAR[2] if uyg else SONUCLAR[0])
            k = BakimKaydi(kayit_no=next_sequence_no(db, BakimKaydi, "kayit_no", "BK"), ekipman_id=e.id, form_kod=s["kod"], form_ad=s["ad"][:150],
                           kategori=s["kategori"], tarih=tarih, bakimci=f.get("bakimci").strip()[:200], sure_dk=_int(f.get("sure_dk")),
                           calisma_saati=_int(f.get("calisma_saati")), baski_sayisi=_int(f.get("baski_sayisi")),
                           is_emri=(f.get("is_emri") or "").strip()[:60] or None, lot_no=(f.get("lot_no") or "").strip()[:60] or None,
                           urun=(f.get("urun") or "").strip()[:150] or None, sonuc=sonuc, maddeler_json=json.dumps(maddeler, ensure_ascii=False),
                           uygunsuz_sayisi=uyg, degisen_parcalar=(f.get("degisen_parcalar") or "").strip()[:1000] or None,
                           aciklama=(f.get("aciklama") or "").strip()[:1000] or None, olusturan_id=session.get("user_id"))
            db.add(k); db.commit()
            log_action(db, "Oluşturma", detay=f"Bakım kaydı {k.kayit_no}: {e.kod} — {s['kod']} {s['ad']} ({sonuc})")
            flash(f"{k.kayit_no} kaydedildi — {e.kod} / {s['ad']}." + (f" {uyg} madde 'Uygun Değil'." if uyg else ""), "success" if not uyg else "warning")
            return redirect(url_for("bakim_kayit", kid=k.id))
        return _form_goster(db, ekipmanlar, e, sablonlar, s, {}, None)
    finally:
        db.close()


def _form_goster(db, ekipmanlar, e, sablonlar, s, form, maddeler):
    kayitlar = db.query(BakimKaydi).filter_by(ekipman_id=e.id).all() if e else []
    son = son_kayit(kayitlar, s["kod"]) if (e and s) else None
    u = db.get(User, session.get("user_id")) if session.get("user_id") else None
    gruplar = {}
    for x in ekipmanlar:
        gruplar.setdefault(x.tur, []).append(x)
    kategoriler = {}
    for x in sablonlar:
        kategoriler.setdefault(x["kategori"], []).append(x)
    return render_template("bakim_form.html", ekipman_gruplari=gruplar, e=e, sablonlar=sablonlar, kategoriler=kategoriler, s=s, form=form,
                           secili=maddeler, son=son, son_maddeler={m["no"]: m for m in json.loads(son.maddeler_json or "[]")} if son else {},
                           durumlar=(DURUM_KALIP if s and s.get("kalip") else DURUM_MAKINE), sonuclar=SONUCLAR, yazabilir=_yazabilir(),
                           kullanici=u.ad_soyad if u else "", today=date.today(), ekipman_yok=not ekipmanlar)


# ─────────────────────────────── ekipman ───────────────────────────────
@app.route("/bakim/ekipman", methods=["GET", "POST"])
@login_required
def bakim_ekipman():
    db = get_db()
    try:
        if request.method == "POST":
            if not _yazabilir():
                abort(403)
            f = request.form
            if f.get("action") == "baslangic":
                if not _yonetici():
                    abort(403)
                L = json.load(open(os.path.join(_DATA, "bakim_ekipman_baslangic.json"), encoding="utf-8"))["ekipmanlar"]
                var = {_k(x.kod) for x in db.query(BakimEkipman).all()}
                n = 0
                for x in L:
                    if _k(x["kod"]) in var:
                        continue
                    db.add(BakimEkipman(kod=x["kod"], ad=x["ad"], tur=x["tur"], marka=x.get("marka") or None, model=x.get("model") or None,
                                        bolum=x.get("bolum") or None, notlar=x.get("not") or None, aktif=True)); n += 1
                db.commit(); log_action(db, "Oluşturma", detay=f"Bakım ekipmanı başlangıç listesi: {n} ekipman")
                flash(f"{n} ekipman eklendi (D03 F03 listesi ve bakım kartları). Markası boş olanları kontrol edin.", "success")
                return redirect(url_for("bakim_ekipman"))
            kod, ad, tur = (f.get("kod") or "").strip(), (f.get("ad") or "").strip(), (f.get("tur") or "").strip()
            if not (kod and ad and tur in veri()["ekipman_turleri"]):
                flash("Kod, ad ve tür zorunludur.", "error")
                return redirect(url_for("bakim_ekipman"))
            if any(_k(x.kod) == _k(kod) for x in db.query(BakimEkipman).all()):
                flash(f"{kod} kodlu ekipman zaten var.", "error")
                return redirect(url_for("bakim_ekipman"))
            e = BakimEkipman(kod=kod[:40], ad=ad[:150], tur=tur, marka=(f.get("marka") or "").strip()[:60] or None, model=(f.get("model") or "").strip()[:120] or None,
                             seri_no=(f.get("seri_no") or "").strip()[:80] or None, bolum=(f.get("bolum") or "").strip()[:60] or None,
                             baski_omru=_int(f.get("baski_omru")), notlar=(f.get("notlar") or "").strip()[:500] or None, aktif=True)
            db.add(e); db.commit(); log_action(db, "Oluşturma", detay=f"Bakım ekipmanı: {kod} {ad} ({tur})")
            flash(f"{kod} eklendi.", "success")
            return redirect(url_for("bakim_ekipman_detay", eid=e.id))
        tur = request.args.get("tur") or ""
        q = _k(request.args.get("q"))
        pasif = request.args.get("pasif") == "1"
        L = db.query(BakimEkipman).order_by(BakimEkipman.tur, BakimEkipman.kod).all()
        L = [x for x in L if (pasif or x.aktif is not False) and (not tur or x.tur == tur) and (not q or q in _k(f"{x.kod} {x.ad} {x.marka} {x.model} {x.bolum}"))]
        oz = _ekipman_ozet(db, L)
        V = veri()
        return render_template("bakim_ekipman.html", liste=oz, turler=V["ekipman_turleri"], markalar=V["markalar"], bolumler=BOLUMLER,
                               tur=tur, q=request.args.get("q", ""), pasif=pasif, yazabilir=_yazabilir(), yonetici=_yonetici(),
                               toplam=db.query(BakimEkipman).count())
    finally:
        db.close()


@app.route("/bakim/ekipman/<int:eid>", methods=["GET", "POST"])
@login_required
def bakim_ekipman_detay(eid):
    db = get_db()
    try:
        e = db.get(BakimEkipman, eid)
        if not e:
            abort(404)
        if request.method == "POST":
            if not _yazabilir():
                abort(403)
            f = request.form
            if not ((f.get("ad") or "").strip() and f.get("tur") in veri()["ekipman_turleri"]):
                flash("Ad ve tür zorunludur.", "error")
                return redirect(url_for("bakim_ekipman_detay", eid=e.id))
            e.ad, e.tur = f.get("ad").strip()[:150], f.get("tur")
            e.marka = (f.get("marka") or "").strip()[:60] or None
            e.model = (f.get("model") or "").strip()[:120] or None
            e.seri_no = (f.get("seri_no") or "").strip()[:80] or None
            e.bolum = (f.get("bolum") or "").strip()[:60] or None
            e.baski_omru = _int(f.get("baski_omru"))
            e.notlar = (f.get("notlar") or "").strip()[:500] or None
            e.aktif = f.get("aktif") == "1"
            db.commit(); log_action(db, "Güncelleme", detay=f"Bakım ekipmanı: {e.kod}")
            flash("Ekipman bilgileri kaydedildi.", "success")
            return redirect(url_for("bakim_ekipman_detay", eid=e.id))
        kayitlar = sorted(db.query(BakimKaydi).filter_by(ekipman_id=e.id).all(), key=lambda k: (k.tarih, k.id), reverse=True)
        plan = [(s, son_kayit(kayitlar, s["kod"])) for s in uygun_sablonlar(e)]
        V = veri()
        return render_template("bakim_ekipman_detay.html", e=e, plan=plan, kayitlar=kayitlar, turler=V["ekipman_turleri"], markalar=V["markalar"],
                               bolumler=BOLUMLER, yazabilir=_yazabilir())
    finally:
        db.close()


# ─────────────────────────────── kayıtlar ───────────────────────────────
@app.route("/bakim/kayit/<int:kid>", methods=["GET", "POST"])
@login_required
def bakim_kayit(kid):
    db = get_db()
    try:
        k = db.get(BakimKaydi, kid)
        if not k:
            abort(404)
        e = db.get(BakimEkipman, k.ekipman_id)
        if request.method == "POST" and request.form.get("action") == "sil":
            if not _yonetici():
                abort(403)
            no = k.kayit_no
            db.delete(k); db.commit(); log_action(db, "Silme", detay=f"Bakım kaydı silindi: {no} ({e.kod if e else ''} {k.form_kod})")
            flash(f"{no} silindi.", "success")
            return redirect(url_for("bakim_ekipman_detay", eid=k.ekipman_id))
        olusturan = db.get(User, k.olusturan_id) if k.olusturan_id else None
        return render_template("bakim_kayit.html", k=k, e=e, s=sablon(k.form_kod), maddeler=json.loads(k.maddeler_json or "[]"),
                               olusturan=olusturan.ad_soyad if olusturan else "—", yonetici=_yonetici(), cikti=request.args.get("cikti") == "1")
    finally:
        db.close()


def _kayit_filtre(db, a):
    L = db.query(BakimKaydi).order_by(BakimKaydi.tarih.desc(), BakimKaydi.id.desc()).all()
    eq = {e.id: e for e in db.query(BakimEkipman).all()}
    bas, bit = _tarih(a.get("bas")), _tarih(a.get("bit"))
    out = []
    for k in L:
        e = eq.get(k.ekipman_id)
        if a.get("ekipman") and str(k.ekipman_id) != a.get("ekipman"):
            continue
        if a.get("tur") and (not e or e.tur != a.get("tur")):
            continue
        if a.get("kategori") and k.kategori != a.get("kategori"):
            continue
        if a.get("uygunsuz") == "1" and not k.uygunsuz_sayisi:
            continue
        if (bas and k.tarih < bas) or (bit and k.tarih > bit):
            continue
        out.append((k, e))
    return out, eq


@app.route("/bakim/kayitlar")
@login_required
def bakim_kayitlar():
    db = get_db()
    try:
        L, eq = _kayit_filtre(db, request.args)
        V = veri()
        return render_template("bakim_kayitlar.html", liste=L[:500], toplam=len(L), ekipmanlar=sorted(eq.values(), key=lambda e: (e.tur, e.kod)),
                               turler=V["ekipman_turleri"], kategoriler=sorted({s["kategori"] for s in V["sablonlar"]}), a=request.args)
    finally:
        db.close()


@app.route("/bakim/kayitlar/excel")
@login_required
def bakim_kayitlar_excel():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    db = get_db()
    try:
        L, _ = _kayit_filtre(db, request.args)
        wb = Workbook(); ws = wb.active; ws.title = "Bakım Kayıtları"
        bas = ["Kayıt No", "Tarih", "Ekipman Kodu", "Ekipman", "Tür", "Marka", "Form", "Bakım", "Kategori", "Bakımcı", "Süre (dk)", "Çalışma Saati",
               "Baskı", "Sonuç", "Uygun Değil", "Değişen Parçalar", "Açıklama"]
        ws.append(bas)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="1F4E78")
        for k, e in L:
            ws.append([k.kayit_no, k.tarih, e.kod if e else "", e.ad if e else "", e.tur if e else "", (e.marka or "") if e else "", k.form_kod, k.form_ad,
                       k.kategori, k.bakimci, k.sure_dk, k.calisma_saati, k.baski_sayisi, k.sonuc, k.uygunsuz_sayisi or 0, k.degisen_parcalar or "", k.aciklama or ""])
            ws.cell(ws.max_row, 2).number_format = "DD.MM.YYYY"
        for i, w in enumerate([14, 11, 12, 22, 20, 14, 10, 34, 16, 20, 9, 12, 10, 26, 10, 30, 30], 1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = w
        ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
        bio = io.BytesIO(); wb.save(bio); bio.seek(0)
        return send_file(bio, as_attachment=True, download_name=f"D03 Bakım Kayıtları {date.today():%Y%m%d}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    finally:
        db.close()
