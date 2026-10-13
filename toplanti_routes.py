"""
DYS — Toplantı Tutanakları (2026) — Y01.4 İletişim
Y01.4 F02 Günlük Toplantı Üretim & Bakım, F03 İç Paydaş Toplantı Tutanağı, F04 (Aylık Kalite) Toplantı Tutanağı yerine.
  /toplantilar                      liste (tür, yıl, arama) + açık aksiyon sayısı
  /toplantilar/yeni?tur=…           yeni tutanak (Aylık Kalite Toplantısında süreçler madde olarak hazır gelir)
  /toplantilar/<id>                 görüntüle / düzenle (katılımcılar, gündem, maddeler: konu, görüşülen, karar, sorumlu, termin, durum)
  /toplantilar/<id>/yazdir          imzaya hazır çıktı
  /toplantilar/aksiyonlar           tüm açık karar / aksiyonlar (gecikenler işaretli), hızlı kapatma
  /toplantilar/madde/<id>/dof       maddeden DÖF açar ve bağlar
Proje toplantıları / QRQC (Y01.4 F01) için DYS QRQC modülü kullanılır.
"""
from sqlalchemy import case as _sa_case
import json
from datetime import date, datetime

from flask import render_template, request, redirect, url_for, flash, abort, session

from app_runtime import host as _host
_h = _host()
app = _h.app
login_required = _h.login_required
get_db = _h.get_db
log_action = _h.log_action

from models import (ToplantiTutanagi, ToplantiMaddesi, User, Process, CorrectiveAction,  # noqa: E402
                    TOPLANTI_TURLERI, TOPLANTI_MADDE_DURUMLARI)
from helpers import next_sequence_no  # noqa: E402

TUR_BILGI = {
    "Günlük Toplantı – Üretim & Bakım": {"form": "Y01.4 F02", "ek": [("isg", "İş sağlığı ve güvenliği ile ilgili yaşanan kaza / olay"), ("musteri", "Müşteri kalite vakası"),
                                                                     ("tedarikci", "Tedarikçi kalite vakası"), ("isletme", "İşletme içi kalite vakası")],
                                          "konu": "Toplantı konu başlığı", "gorusme": "Görüşülen", "karar": "Sonuç / karar"},
    "Aylık Kalite Toplantısı": {"form": "Y01.4 F04", "ek": [], "konu": "Süreç / konu", "gorusme": "Gözden geçirme sonucu", "karar": "Açıklama / karar", "surecler": True},
    "İç Paydaş Toplantısı": {"form": "Y01.4 F03", "ek": [], "konu": "Öneri / şikâyet sahibi", "gorusme": "Öneri & şikâyet ve açıklama", "karar": "Karar"},
    "Proje Toplantısı": {"form": "Y01.4 F01", "ek": [], "konu": "Proje / konu", "gorusme": "Durum", "karar": "Karar / aksiyon"},
    "Diğer Toplantı": {"form": "—", "ek": [], "konu": "Konu", "gorusme": "Görüşülen", "karar": "Karar"},
}


def _yazabilir():
    return session.get("rol") != "Sadece Görüntüleme"


def _tarih(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except ValueError:
        return None


def _formdan(db, t, f):
    t.tur = f.get("tur") if f.get("tur") in TOPLANTI_TURLERI else (t.tur or "Diğer Toplantı")
    t.tarih = _tarih(f.get("tarih")) or t.tarih or date.today()
    t.saat, t.yer, t.konu = f.get("saat") or None, f.get("yer") or None, f.get("konu") or None
    t.katilimcilar, t.gundem, t.notlar = f.get("katilimcilar") or None, f.get("gundem") or None, f.get("notlar") or None
    t.veri_json = json.dumps({a: f.get(f"ek_{a}") or "" for a, _ in TUR_BILGI[t.tur]["ek"]}, ensure_ascii=False)
    mevcut = {m.id: m for m in t.maddeler}
    kalan = []
    for i in range(int(f.get("madde_say") or 0)):
        mid = f.get(f"m_id_{i}", type=int)
        if f.get(f"m_sil_{i}"):
            continue
        konu, gor, kar = (f.get(f"m_konu_{i}") or "").strip(), (f.get(f"m_gorusme_{i}") or "").strip(), (f.get(f"m_karar_{i}") or "").strip()
        if not (konu or gor or kar):
            continue
        m = mevcut.get(mid) or ToplantiMaddesi()
        m.sira = len(kalan) + 1; m.konu, m.gorusme, m.karar = konu[:250] or None, gor or None, kar or None
        m.sorumlu_id = f.get(f"m_sorumlu_{i}", type=int) or None
        m.sorumlu_metin = (f.get(f"m_sorumlu_metin_{i}") or "").strip()[:120] or None
        m.termin = _tarih(f.get(f"m_termin_{i}"))
        m.durum = f.get(f"m_durum_{i}") if f.get(f"m_durum_{i}") in TOPLANTI_MADDE_DURUMLARI else "Açık"
        m.gerceklesme_tarihi = _tarih(f.get(f"m_gercek_{i}")) or (date.today() if m.durum == "Tamamlandı" and not m.gerceklesme_tarihi else m.gerceklesme_tarihi)
        kalan.append(m)
    t.maddeler = kalan


@app.route("/toplantilar")
@login_required
def toplantilar():
    db = get_db()
    try:
        tur, yil, q = request.args.get("tur") or "", request.args.get("yil", type=int), (request.args.get("q") or "").strip()
        sorgu = db.query(ToplantiTutanagi)
        if tur:
            sorgu = sorgu.filter(ToplantiTutanagi.tur == tur)
        liste = [t for t in sorgu.order_by(ToplantiTutanagi.tarih.desc(), ToplantiTutanagi.id.desc()).all()
                 if (not yil or t.tarih.year == yil) and (not q or q.lower() in " ".join(filter(None, [t.konu, t.katilimcilar, t.gundem, t.tutanak_no])).lower())]
        acik = db.query(ToplantiMaddesi).filter(ToplantiMaddesi.durum == "Açık").count()
        yillar = sorted({t.tarih.year for t in db.query(ToplantiTutanagi).all()} | {date.today().year}, reverse=True)
        return render_template("toplantilar.html", liste=liste, tur=tur, yil=yil, q=q, turler=TOPLANTI_TURLERI, tur_bilgi=TUR_BILGI,
                               acik=acik, yillar=yillar, yazabilir=_yazabilir(), bugun=date.today())
    finally:
        db.close()


@app.route("/toplantilar/yeni", methods=["GET", "POST"])
@app.route("/toplantilar/<int:tid>", methods=["GET", "POST"])
@login_required
def toplanti(tid=None):
    db = get_db()
    try:
        t = db.get(ToplantiTutanagi, tid) if tid else None
        if tid and not t:
            abort(404)
        if request.method == "POST":
            if not _yazabilir():
                abort(403)
            yeni = t is None
            if yeni:
                t = ToplantiTutanagi(tutanak_no=next_sequence_no(db, ToplantiTutanagi, "tutanak_no", "TT"),
                                     olusturan_id=session.get("user_id"), durum="Taslak")
                db.add(t)
            _formdan(db, t, request.form)
            if request.form.get("islem") == "yayinla":
                t.durum = "Yayınlandı"
            db.commit()
            log_action(db, "Oluşturma" if yeni else "Düzenleme", detay=f"Toplantı tutanağı {t.tutanak_no} ({t.tur}) — {len(t.maddeler)} madde")
            flash("Tutanak kaydedildi." + (" Yayınlandı." if t.durum == "Yayınlandı" else ""), "success")
            return redirect(url_for("toplanti", tid=t.id))
        if not t:
            tur = request.args.get("tur") if request.args.get("tur") in TOPLANTI_TURLERI else "Aylık Kalite Toplantısı"
            t = ToplantiTutanagi(tur=tur, tarih=date.today(), tutanak_no="(yeni)", durum="Taslak")
            if TUR_BILGI[tur].get("surecler"):
                ana = [p for p in db.query(Process).order_by(Process.kod).all() if "." not in (p.kod or "") and p.aktif is not False and len(p.kod) == 3]
                t.maddeler = [ToplantiMaddesi(sira=i, konu=f"{p.kod} {p.ad}") for i, p in enumerate(ana, 1)]
        kullanicilar = db.query(User).filter(User.aktif.__eq__(True)).order_by(User.ad_soyad).all()
        return render_template("toplanti.html", t=t, bilgi=TUR_BILGI[t.tur], turler=TOPLANTI_TURLERI, durumlar=TOPLANTI_MADDE_DURUMLARI,
                               kullanicilar=kullanicilar, yazabilir=_yazabilir(), bugun=date.today())
    finally:
        db.close()


@app.route("/toplantilar/<int:tid>/yazdir")
@login_required
def toplanti_yazdir(tid):
    db = get_db()
    try:
        t = db.get(ToplantiTutanagi, tid) or abort(404)
        log_action(db, "İndirme", detay=f"Toplantı tutanağı çıktısı {t.tutanak_no}")
        return render_template("toplanti_yazdir.html", t=t, bilgi=TUR_BILGI[t.tur], simdi=datetime.now(), kullanici=session.get("ad_soyad") or "",
                               surec=db.query(Process).filter(Process.kod == "Y01").first())
    finally:
        db.close()


@app.route("/toplantilar/aksiyonlar", methods=["GET", "POST"])
@login_required
def toplanti_aksiyonlar():
    db = get_db()
    try:
        if request.method == "POST":
            if not _yazabilir():
                abort(403)
            m = db.get(ToplantiMaddesi, request.form.get("madde_id", type=int)) or abort(404)
            m.durum = "Tamamlandı"; m.gerceklesme_tarihi = date.today(); db.commit()
            log_action(db, "Düzenleme", detay=f"Toplantı aksiyonu tamamlandı: {m.tutanak.tutanak_no} #{m.sira}")
            flash("Aksiyon tamamlandı olarak işaretlendi.", "success")
            return redirect(request.form.get("geri") or url_for("toplanti_aksiyonlar"))
        durum = request.args.get("durum") or "Açık"
        q = db.query(ToplantiMaddesi).join(ToplantiTutanagi)
        if durum != "hepsi":
            q = q.filter(ToplantiMaddesi.durum == durum)
        if request.args.get("benim"):
            q = q.filter(ToplantiMaddesi.sorumlu_id == session.get("user_id"))
        maddeler = [m for m in q.order_by(_sa_case((ToplantiMaddesi.termin.is_(None), 1), else_=0), ToplantiMaddesi.termin, ToplantiTutanagi.tarih.desc()).all() if m.karar or m.termin or m.sorumlu_id or m.sorumlu_metin]
        return render_template("toplanti_aksiyonlar.html", maddeler=maddeler, durum=durum, durumlar=TOPLANTI_MADDE_DURUMLARI, bugun=date.today(), yazabilir=_yazabilir())
    finally:
        db.close()


@app.route("/toplantilar/madde/<int:mid>/dof", methods=["POST"])
@login_required
def toplanti_madde_dof(mid):
    if not _yazabilir():
        abort(403)
    db = get_db()
    try:
        m = db.get(ToplantiMaddesi, mid) or abort(404)
        if not m.dof_id:
            c = CorrectiveAction(dof_no=next_sequence_no(db, CorrectiveAction, "dof_no", "DÖF"),
                                 baslik=(m.konu or m.karar or "Toplantı kararı")[:200], kaynak_tipi="Diğer", tespit_tarihi=m.tutanak.tarih,
                                 acan_id=session.get("user_id"), sorumlu_id=m.sorumlu_id, planlanan_tarih=m.termin, durum="Açık",
                                 tanim=f"{m.tutanak.tutanak_no} {m.tutanak.tur} ({m.tutanak.tarih:%d.%m.%Y}) — {m.gorusme or ''}\nKarar: {m.karar or ''}")
            db.add(c); db.flush(); m.dof_id = c.id; db.commit()
            log_action(db, "Oluşturma", detay=f"Toplantı maddesinden DÖF: {c.dof_no} ← {m.tutanak.tutanak_no} #{m.sira}")
            flash(f"{c.dof_no} açıldı.", "success")
        return redirect(url_for("toplanti", tid=m.tutanak_id))
    finally:
        db.close()
