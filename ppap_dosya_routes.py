"""
DYS — PPAP dosyası (2026): ürün ağacı (elle) → operasyonlar → iş akış şeması OTOMATİK + FMEA + kontrol planı + canlı şikayet / mühendislik değişikliği + revizyon.
  /urun-proses/<id>/agac            ürün ağacı: ürün → yarı mamul → hammadde; her düğüme operasyon (kütüphaneden) eklenir
  /urun-proses/<id>/ppap            yaşayan PPAP dosyası: iş akışı, P-FMEA, kontrol planı, müşteri şikayetleri, mühendislik değişiklikleri, revizyonlar
  /urun-proses/<id>/ppap/rev/<no>   revizyon anlık görüntüsü
Operasyonun FMEA / kontrol planı kütüphanesi urun_proses_routes'tadır (bir kez hazırlanır); burada yalnız ürüne uygulanır ve derlenir.
Yetki: ağaç ve revizyon oluşturma Admin / Doküman Kontrol; görüntüleme herkes.
"""
import hashlib
import json
from datetime import datetime

from flask import render_template, request, redirect, url_for, flash, abort, session

from app_runtime import host as _host
_h = _host()
app = _h.app
login_required = _h.login_required
get_db = _h.get_db
log_action = _h.log_action

from models import (UrunProsesi, UrunProsesAdimi, UrunAgacDugumu, PpapRevizyon, ProsesAdimTanim, ProsesFmeaSatiri, ProsesKpSatiri,  # noqa: E402
                    OlcumKaydi, MusteriSikayeti, TeknikResim, TeknikResimRevizyon, PPAPSubmission, User)
import fmea_core as FC  # noqa: E402
import urun_proses_routes as UP  # noqa: E402

TURLER = ("Yarı Mamul", "Hammadde")


def _kok(db, u):
    """Ürünün kök düğümünü döndürür (yoksa oluşturur); dugum_id'si boş eski adımları köke bağlar."""
    k = db.query(UrunAgacDugumu).filter_by(urun_proses_id=u.id, ust_id=None).first()
    if not k:
        k = UrunAgacDugumu(urun_proses_id=u.id, ust_id=None, tur="Ürün", kod=u.urun_kodu, ad=u.urun_adi, sira=0)
        db.add(k); db.flush()
    for a in db.query(UrunProsesAdimi).filter_by(urun_proses_id=u.id, dugum_id=None).all():
        a.dugum_id = k.id
    db.flush()
    return k


def _agac(db, u):
    """İç içe ağaç: {id, tur, kod, ad, miktar, cocuklar[], adimlar[{uid, ad}]}; operasyon zinciri sıraya göre."""
    kok = _kok(db, u)
    dugumler = db.query(UrunAgacDugumu).filter_by(urun_proses_id=u.id).order_by(UrunAgacDugumu.sira, UrunAgacDugumu.id).all()
    adlar = {t.id: t.ad for t in db.query(ProsesAdimTanim).all()}
    adimlar = db.query(UrunProsesAdimi).filter_by(urun_proses_id=u.id).order_by(UrunProsesAdimi.sira, UrunProsesAdimi.id).all()
    cocuk = {}
    for d in dugumler:
        cocuk.setdefault(d.ust_id, []).append(d)

    def kur(d, derin=0):
        if derin > 12:
            return None
        return {"id": d.id, "tur": d.tur, "kod": d.kod or "", "ad": d.ad, "miktar": d.miktar or "",
                "adimlar": [{"uid": a.id, "tanim_id": a.adim_tanim_id, "ad": adlar.get(a.adim_tanim_id, "?")} for a in adimlar if a.dugum_id == d.id],
                "cocuklar": [x for x in (kur(c, derin + 1) for c in cocuk.get(d.id, [])) if x]}
    return kur(kok)


def _duz(n, out=None):
    out = [] if out is None else out
    out.append(n)
    for c in n["cocuklar"]:
        _duz(c, out)
    return out


def _derle(db, u):
    """PPAP dosyasının canlı içeriği: ağaç + operasyon bazlı FMEA / kontrol planı."""
    agac = _agac(db, u)
    veri = UP._urun_verisi(db, u)
    op = {v["uid"]: v for v in veri}
    for n in _duz(agac):
        for a in n["adimlar"]:
            v = op.get(a["uid"])
            a["fmea"] = (v["genel"] + v["ozel"]) if v else []
            a["kp"] = v["kp"] if v else []
            a["ozet"] = v["ozet"]["guncel"] if v else {}
    return agac


def _hash(agac):
    def sade(n):
        return {"ad": n["ad"], "kod": n["kod"], "tur": n["tur"], "m": n["miktar"],
                "a": [{"ad": a["ad"], "f": [(f["fn"], f["mode"], f["cause"], f["S"], f["O"], f["D"], f["act"]) for f in a["fmea"]],
                       "k": [(k["karakteristik"], k["nominal"], k["alt_limit"], k["ust_limit"], k["olcum_araci"], k["siklik"], k["numune"]) for k in a["kp"]]} for a in n["adimlar"]],
                "c": [sade(c) for c in n["cocuklar"]]}
    return hashlib.sha256(json.dumps(sade(agac), sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _sayilar(agac):
    d = _duz(agac)
    ops = [a for n in d for a in n["adimlar"]]
    return {"dugum": len(d), "operasyon": len(ops), "fmea": sum(len(a["fmea"]) for a in ops), "kp": sum(len(a["kp"]) for a in ops)}


def _ilgili(db, u):
    """Şikayetler (ürün kodu / adı eşleşmesi), mühendislik değişiklikleri (teknik resim revizyonları), PPAP başvuruları."""
    kodk = UP._k(u.urun_kodu or "")
    adk = UP._k(u.urun_adi)

    def esles(kod, ad):
        return bool((kodk and UP._k(kod or "") == kodk) or (adk and UP._k(ad or "") == adk))
    sik = [s for s in db.query(MusteriSikayeti).order_by(MusteriSikayeti.tarih.desc()).all() if esles(s.urun_no, s.urun_adi)]
    resimler = [t for t in db.query(TeknikResim).all() if t.id == u.teknik_resim_id or esles(t.urun_kodu, t.urun_adi)]
    degisim = []
    for t in resimler:
        for r in db.query(TeknikResimRevizyon).filter_by(resim_id=t.id).order_by(TeknikResimRevizyon.revizyon_tarihi.desc(), TeknikResimRevizyon.id.desc()).all():
            try:
                dv = json.loads(r.degerlendirme_json or "{}") or {}
            except ValueError:
                dv = {}
            degisim.append({"resim": t, "rev": r, "ppap": dv.get("ppap") or ""})
    ppaplar = [p for p in db.query(PPAPSubmission).order_by(PPAPSubmission.id.desc()).all() if (kodk and UP._k(p.parca_no) == kodk) or (adk and UP._k(p.parca_adi) == adk)]
    return sik, degisim, ppaplar


# ─────────────────────────── ürün ağacı ───────────────────────────
@app.route("/urun-proses/<int:uid>/agac", methods=["GET", "POST"])
@login_required
def urun_proses_agac(uid):
    db = get_db()
    try:
        u = db.get(UrunProsesi, uid)
        if not u:
            abort(404)
        kok = _kok(db, u); db.commit()
        git = url_for("urun_proses_agac", uid=u.id)
        if request.method == "POST":
            if not UP._yonetici():
                abort(403)
            f = request.form
            a = f.get("action")
            if a == "dugum_ekle":
                ust = db.get(UrunAgacDugumu, f.get("ust_id", type=int) or 0)
                ad = (f.get("ad") or "").strip()
                tur = f.get("tur") if f.get("tur") in TURLER else "Yarı Mamul"
                if not ust or ust.urun_proses_id != u.id or ust.tur == "Hammadde":
                    flash("Üst düğüm geçersiz (hammaddenin altına eklenemez).", "error")
                elif not ad:
                    flash("Ad zorunludur.", "error")
                else:
                    n = db.query(UrunAgacDugumu).filter_by(urun_proses_id=u.id, ust_id=ust.id).count()
                    db.add(UrunAgacDugumu(urun_proses_id=u.id, ust_id=ust.id, tur=tur, kod=(f.get("kod") or "").strip()[:80] or None, ad=ad[:200],
                                          miktar=(f.get("miktar") or "").strip()[:40] or None, sira=n + 1))
                    db.commit(); flash("Düğüm eklendi.", "success")
            elif a == "dugum_sil":
                d = db.get(UrunAgacDugumu, f.get("dugum_id", type=int) or 0)
                if not d or d.urun_proses_id != u.id or d.ust_id is None:
                    abort(404)
                if db.query(UrunAgacDugumu).filter_by(ust_id=d.id).count():
                    flash("Altında düğüm var; önce onları silin.", "error")
                elif db.query(UrunProsesAdimi).filter_by(urun_proses_id=u.id, dugum_id=d.id).count():
                    flash("Düğümde operasyon var; önce operasyonları çıkarın.", "error")
                else:
                    db.delete(d); db.commit(); flash("Silindi.", "success")
            elif a == "op_ekle":
                d = db.get(UrunAgacDugumu, f.get("dugum_id", type=int) or 0)
                tid = f.get("adim_tanim_id", type=int)
                if not d or d.urun_proses_id != u.id:
                    abort(404)
                if not (tid and db.get(ProsesAdimTanim, tid)):
                    flash("Operasyon seçin.", "error")
                else:
                    ua = UP._adim_ekle(db, u, tid); ua.dugum_id = d.id; db.commit()
                    log_action(db, "Güncelleme", detay=f"PPAP {u.urun_adi}: {d.ad} düğümüne operasyon eklendi")
                    flash("Operasyon eklendi: FMEA ve kontrol planı kütüphaneden geldi.", "success")
            elif a == "op_sil":
                ua = db.get(UrunProsesAdimi, f.get("uadim", type=int) or 0)
                if not ua or ua.urun_proses_id != u.id:
                    abort(404)
                if db.query(OlcumKaydi).filter_by(adim_id=ua.id).count():
                    flash("Bu operasyonda ölçü kaydı var; çıkarılamaz.", "error")
                else:
                    for r in db.query(ProsesKpSatiri).filter_by(adim_tanim_id=ua.adim_tanim_id, urun_proses_id=u.id).all():
                        db.delete(r)
                    for r in db.query(ProsesFmeaSatiri).filter_by(adim_tanim_id=ua.adim_tanim_id, urun_proses_id=u.id).all():
                        db.delete(r)
                    db.delete(ua); db.commit(); flash("Operasyon çıkarıldı.", "success")
            elif a in ("op_yukari", "op_asagi"):
                ua = db.get(UrunProsesAdimi, f.get("uadim", type=int) or 0)
                if not ua or ua.urun_proses_id != u.id:
                    abort(404)
                L = db.query(UrunProsesAdimi).filter_by(urun_proses_id=u.id, dugum_id=ua.dugum_id).order_by(UrunProsesAdimi.sira, UrunProsesAdimi.id).all()
                i = L.index(ua); j = i - 1 if a == "op_yukari" else i + 1
                if 0 <= j < len(L):
                    L[i].sira, L[j].sira = L[j].sira, L[i].sira
                    if L[i].sira == L[j].sira:
                        L[j].sira += 1
                    db.commit()
            return redirect(git)
        agac = _agac(db, u)
        katalog = [k for k in UP._katalog(db) if k.aktif is not False]
        return render_template("urun_agac.html", u=u, agac=agac, dugumler=_duz(agac), katalog=katalog, yonetici=UP._yonetici(), turler=TURLER)
    finally:
        db.close()


# ─────────────────────────── PPAP dosyası ───────────────────────────
@app.route("/urun-proses/<int:uid>/ppap", methods=["GET", "POST"])
@login_required
def urun_ppap(uid):
    db = get_db()
    try:
        u = db.get(UrunProsesi, uid)
        if not u:
            abort(404)
        _kok(db, u); db.commit()
        agac = _derle(db, u)
        h = _hash(agac)
        revler = db.query(PpapRevizyon).filter_by(urun_proses_id=u.id).order_by(PpapRevizyon.rev_no.desc()).all()
        if request.method == "POST":
            if not UP._yonetici():
                abort(403)
            neden = (request.form.get("neden") or "").strip()[:500]
            son = revler[0] if revler else None
            if son and son.ozet_hash == h:
                flash("Son revizyondan beri değişiklik yok; yeni revizyon oluşturulmadı.", "info")
            elif son and not neden:
                flash("Revizyon nedeni zorunludur.", "error")
            else:
                no = (son.rev_no + 1) if son else 0
                db.add(PpapRevizyon(urun_proses_id=u.id, rev_no=no, neden=neden or "İlk yayın", olusturan_id=session.get("user_id"), ozet_hash=h,
                                    veri_json=json.dumps({"agac": agac, "sayi": _sayilar(agac), "std": UP._std(u)}, ensure_ascii=False, default=str)))
                db.commit()
                log_action(db, "Oluşturma", detay=f"PPAP dosyası {u.urun_adi}: Rev {no}")
                flash(f"Rev {no} oluşturuldu.", "success")
            return redirect(url_for("urun_ppap", uid=u.id))
        sik, degisim, ppaplar = _ilgili(db, u)
        kim = {x.id: x.ad_soyad for x in db.query(User).all()}
        degisti = bool(revler) and revler[0].ozet_hash != h
        # son revizyondan bu yana fark (operasyon / sayı bazında)
        fark = []
        if revler:
            try:
                eski = json.loads(revler[0].veri_json or "{}").get("sayi", {})
            except ValueError:
                eski = {}
            for k, ad in (("dugum", "ürün ağacı düğümü"), ("operasyon", "operasyon"), ("fmea", "FMEA satırı"), ("kp", "kontrol planı satırı")):
                yeni = _sayilar(agac)[k]
                if eski.get(k) is not None and eski.get(k) != yeni:
                    fark.append(f"{ad}: {eski.get(k)} → {yeni}")
        return render_template("urun_ppap.html", u=u, agac=agac, sayi=_sayilar(agac), revler=revler, degisti=degisti, fark=fark, sikayetler=sik, degisim=degisim,
                               ppaplar=ppaplar, kim=kim, yonetici=UP._yonetici(), rev=None, AP_AD=FC.AP_AD, duz=_duz(agac), std=UP._std(u), standartlar=FC.STANDARTLAR)
    finally:
        db.close()


@app.route("/urun-proses/<int:uid>/ppap/rev/<int:no>")
@login_required
def urun_ppap_rev(uid, no):
    db = get_db()
    try:
        u = db.get(UrunProsesi, uid)
        r = db.query(PpapRevizyon).filter_by(urun_proses_id=uid, rev_no=no).first()
        if not u or not r:
            abort(404)
        try:
            v = json.loads(r.veri_json or "{}")
        except ValueError:
            v = {}
        kim = {x.id: x.ad_soyad for x in db.query(User).all()}
        return render_template("urun_ppap.html", u=u, agac=v.get("agac"), sayi=v.get("sayi", {}), revler=db.query(PpapRevizyon).filter_by(urun_proses_id=uid).order_by(PpapRevizyon.rev_no.desc()).all(),
                               degisti=False, fark=[], sikayetler=[], degisim=[], ppaplar=[], kim=kim, yonetici=False, rev=r, AP_AD=FC.AP_AD, duz=_duz(v['agac']) if v.get('agac') else [], std=UP._std(v.get('std')), standartlar=FC.STANDARTLAR)
    finally:
        db.close()
