"""
DYS — İç Tetkik (dashboard, liste)
==================================
app_runtime üzerinden app'e route kaydı.
CRUD (/audits/new ve detay) app.py'de kalır.
"""

from sqlalchemy import case as _sa_case
from datetime import date, timedelta

from flask import render_template, request, session

from app_runtime import host as _host

_h = _host()
app = _h.app
login_required = _h.login_required
get_db = _h.get_db

from models import (
    InternalAudit, AuditFinding, AuditAnswer, AuditChecklist,
    AuditProgram, AuditProgramItem,
    AUDIT_DURUMLARI, AUDIT_TIPLERI,
)


def _is_editor():
    return session.get("rol") in ("Admin", "Doküman Kontrol")


@app.route("/audits")
@login_required
def audit_dashboard():
    """İç tetkik paneli."""
    db = get_db()
    try:
        tum = (
            db.query(InternalAudit)
            .order_by(InternalAudit.olusturma_tarihi.desc())
            .all()
        )
        bugun = date.today()
        yil = bugun.year
        yaklasan_esik = bugun + timedelta(days=14)
        acik_durum = {"Planlandı", "Devam Ediyor"}

        durum_dagilim = {d: 0 for d in AUDIT_DURUMLARI}
        tip_dagilim = {t: 0 for t in AUDIT_TIPLERI}
        for a in tum:
            durum_dagilim[a.durum] = durum_dagilim.get(a.durum, 0) + 1
            tip = a.denetim_tipi or "Sistem Denetimi"
            tip_dagilim[tip] = tip_dagilim.get(tip, 0) + 1

        aciklar = [a for a in tum if a.durum in acik_durum]
        geciken = [a for a in aciklar if a.planlanan_tarih and a.planlanan_tarih < bugun]
        yaklasan = [
            a for a in aciklar
            if a.planlanan_tarih and bugun <= a.planlanan_tarih <= yaklasan_esik
        ]
        bu_yil = [
            a for a in tum
            if (a.planlanan_tarih and a.planlanan_tarih.year == yil)
            or (a.olusturma_tarihi and a.olusturma_tarihi.year == yil)
        ]

        findings = db.query(AuditFinding).order_by(AuditFinding.id.desc()).all()
        major = [f for f in findings if f.bulgu_tipi and "Majör" in f.bulgu_tipi]
        minor = [f for f in findings if f.bulgu_tipi and "Minör" in f.bulgu_tipi]
        dof_bekleyen = [
            f for f in findings
            if not f.capa_id and f.bulgu_tipi and "Uygunsuzluk" in f.bulgu_tipi
        ]

        uygunsuz_cevap = (
            db.query(AuditAnswer)
            .filter(AuditAnswer.sonuc.in_(["Uygunsuz", "Kısmen Uygun"]))
            .count()
        )
        checklist_sayisi = db.query(AuditChecklist).count()

        program_kalem = (
            db.query(AuditProgramItem)
            .join(AuditProgram, AuditProgramItem.program_id == AuditProgram.id)
            .filter(AuditProgram.yil == yil)
            .count()
        )

        aylik = [0] * 12
        for a in bu_yil:
            t = a.planlanan_tarih or (a.olusturma_tarihi.date() if a.olusturma_tarihi else None)
            if t and t.year == yil:
                aylik[t.month - 1] += 1

        stats = {
            "toplam": len(tum),
            "bu_yil": len(bu_yil),
            "planlandi": durum_dagilim.get("Planlandı", 0),
            "devam": durum_dagilim.get("Devam Ediyor", 0),
            "tamamlandi": durum_dagilim.get("Tamamlandı", 0),
            "kapatildi": durum_dagilim.get("Kapatıldı", 0),
            "acik": len(aciklar),
            "geciken": len(geciken),
            "yaklasan": len(yaklasan),
            "bulgu": len(findings),
            "major": len(major),
            "minor": len(minor),
            "dof_bekleyen": len(dof_bekleyen),
            "uygunsuz_cevap": uygunsuz_cevap,
            "checklist": checklist_sayisi,
            "program": program_kalem,
            "yil": yil,
        }

        return render_template(
            "audit_dashboard.html",
            stats=stats,
            geciken=geciken[:8],
            yaklasan=yaklasan[:8],
            son_kayitlar=tum[:10],
            dof_bekleyen=dof_bekleyen[:8],
            durum_labels=list(durum_dagilim.keys()),
            durum_values=list(durum_dagilim.values()),
            tip_labels=list(tip_dagilim.keys()),
            tip_values=list(tip_dagilim.values()),
            aylik_labels=["Oca", "Şub", "Mar", "Nis", "May", "Haz",
                          "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"],
            aylik_values=aylik,
            is_editor=_is_editor(),
        )
    finally:
        db.close()


@app.route("/audits/list")
@login_required
def audit_list():
    db = get_db()
    try:
        query = db.query(InternalAudit)
        durum = request.args.get("durum")
        tip = request.args.get("tip")
        if durum:
            query = query.filter(InternalAudit.durum == durum)
        if tip:
            query = query.filter(InternalAudit.denetim_tipi == tip)
        kayitlar = query.order_by(_sa_case((InternalAudit.planlanan_tarih.is_(None), 1), else_=0), InternalAudit.planlanan_tarih.desc()).all()
        return render_template(
            "audit_list.html",
            kayitlar=kayitlar,
            durumlar=AUDIT_DURUMLARI,
            tipler=AUDIT_TIPLERI,
            is_editor=_is_editor(),
        )
    finally:
        db.close()
