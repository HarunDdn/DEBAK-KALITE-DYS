"""
DYS — Performans ve CAPA: kpi.debak.com'dan aktarım ve DYS DÖF eşlemesi (2026)
  aktar(db, kaynak)   kaynak = 'http://kpi.debak.com/api/state' ya da aynı içerikte JSON dosyası / sözlük.
                      Süreç, KPI, ölçüm, NCR ve NCR atlamaları kpi.debak.com kimlikleriyle yazılır (tekrar çalıştırılırsa
                      güncellenir; kpi.debak.com'da olmayan DYS kayıtlarına dokunulmaz). kpi.debak.com'a yazılmaz.
  dof_esle(db, ncr)   NCR'nin DYS DÖF modülündeki eşini (corrective_actions, dis_kimlik = NCR id) oluşturur / günceller.
                      NCR Performans modülünde yönetilir; DÖF kaydı listede ve raporlarda görünür.
  ayar(db) / ayar_yaz(db, **)   yıl, tesis adı, form, kişi listesi (sistem_ayarlari 'pk_ayar')
"""
import json
import urllib.request
from datetime import date, datetime

KAYNAK = "http://kpi.debak.com/api/state"
AYAR = "pk_ayar"
# Y03 F06 süreç kodu → 11 süreçlik yapı (kod eşleştirme tablosundaki çoğunluk eşlemesi)
YENI_SUREC = {"D01": "D01", "D02": "D03", "D03": "D03", "D04": "D04", "D05": "D02", "D06": "D04", "D07": "D05", "D08": "D06",
              "M01": "M01", "M02": "M02", "M03": "M04", "M04": "M03", "M05": "M04", "Y01": "D04", "Y02": "Y01", "Y03": "Y01"}
DOF_DURUM = {"kayit": "Açık", "kok_neden": "Devam Ediyor", "plan": "Devam Ediyor", "uygulama": "Etkinlik Kontrolünde", "kapali": "Kapatıldı"}


def ayar(db):
    from models import SistemAyar
    a = db.get(SistemAyar, AYAR)
    try:
        d = json.loads(a.deger) if a and a.deger else {}
    except ValueError:
        d = {}
    d.setdefault("yil", date.today().year); d.setdefault("tesis", "Debak"); d.setdefault("form", "Y03 F06"); d.setdefault("kisiler", [])
    return d


def ayar_yaz(db, **kw):
    from models import SistemAyar
    d = ayar(db); d.update({k: v for k, v in kw.items() if v is not None})
    a = db.get(SistemAyar, AYAR) or SistemAyar(anahtar=AYAR)
    a.deger = json.dumps(d, ensure_ascii=False, default=str); db.merge(a)
    return d


def _t(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def veri_al(kaynak=None):
    kaynak = kaynak or KAYNAK
    if isinstance(kaynak, dict):
        return kaynak
    if str(kaynak).startswith(("http://", "https://")):
        with urllib.request.urlopen(kaynak, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    return json.load(open(kaynak, encoding="utf-8"))


def _kullanici_id(db, ad):
    from models import User
    if not ad:
        return None
    a = ad.strip().lower()
    for u in db.query(User).filter(User.aktif.__eq__(True)).all():
        if u.ad_soyad and u.ad_soyad.strip().lower() == a:
            return u.id
    return None


def dof_esle(db, n):
    """NCR → DYS DÖF (corrective_actions). NCR'deki alanlar DÖF'e yansıtılır."""
    from models import CorrectiveAction, Process
    with db.no_autoflush:
        yeni = YENI_SUREC.get(n.surec_id or "", n.surec_id)
        p = db.query(Process).filter(Process.kod == yeni).first() if yeni else None
        sorumlu_id = _kullanici_id(db, n.sorumlu)
        c = db.get(CorrectiveAction, n.dof_id) if n.dof_id else None
        c = c or db.query(CorrectiveAction).filter(CorrectiveAction.dis_kimlik == n.id).first()
        if not c:
            no = n.numara
            if db.query(CorrectiveAction).filter(CorrectiveAction.dof_no == no).first():
                no = f"{no}-KPI"
            c = CorrectiveAction(dis_kimlik=n.id, dof_no=no[:30], baslik=(n.baslik or n.numara)[:200]); db.add(c)
    aks = n.aksiyonlar
    satir = lambda a: f"• {a.get('description', '')} — {a.get('owner', '')}, termin {a.get('dueDate', '')}" + (f"; gerçekleşen: {a.get('actual') or a.get('evidence')}" if (a.get('actual') or a.get('evidence')) else "")
    c.baslik = (n.baslik or n.numara)[:200]
    c.kaynak_tipi = "Performans / KPI" if n.tur == "kpi-miss" else {"customer": "Müşteri Şikayeti", "audit": "İç Tetkik", "supplier": "Uygunsuzluk",
                                                                      "internal": "Uygunsuzluk", "process": "Uygunsuzluk"}.get(n.tur, "Diğer")
    c.ilgili_surec_id = p.id if p else None
    c.tespit_tarihi = n.tespit_tarihi; c.planlanan_tarih = n.termin
    c.sorumlu_id = sorumlu_id or c.sorumlu_id
    c.tanim = n.aciklama or ""
    c.kok_neden = n.kok_neden or None
    nedenler = [w for w in n.bes_neden if w]
    if nedenler:
        c.kok_neden_yontemi = "5 Neden"; c.d4_root_cause = "\n".join(f"{i}. Neden: {w}" for i, w in enumerate(nedenler, 1))
    c.d1_team = n.ekip or n.sorumlu
    c.containment_aksiyonu = n.koruma or None
    c.duzeltici_faaliyet = "\n".join(satir(a) for a in aks if a.get("kind") != "preventive") or None
    c.onleyici_faaliyet = "\n".join(satir(a) for a in aks if a.get("kind") == "preventive") or (n.hata_onleme or None)
    c.etkinlik_kontrolu = n.etkinlik_kaniti or None
    c.durum = DOF_DURUM.get(n.asama, "Açık")
    c.kapanma_tarihi = n.kapanis_tarihi if n.asama == "kapali" else None
    c.aksiyonlar_json = n.aksiyonlar_json
    c.benzer_analiz = f"Performans ve CAPA modülünde yönetilir: {n.numara} (KPI {n.kpi_id or '—'}, dönem {n.donem or '—'})."
    db.flush()
    n.dof_id = c.id
    return c


def _ham(o):
    return json.dumps(o, ensure_ascii=False)


def surec_yaz(db, p):
    from models import PkSurec
    e = db.get(PkSurec, p["id"]) or PkSurec(id=p["id"])
    e.ad = p.get("name") or p["id"]; e.baslik = p.get("title"); e.aile = p.get("family"); e.yeni_surec = YENI_SUREC.get(p.get("code") or p["id"])
    e.ham_json = _ham(p)
    return db.merge(e)


def kpi_yaz(db, k):
    from models import PkKpi
    e = db.get(PkKpi, k["id"]) or PkKpi(id=k["id"])
    e.surec_id = k.get("processId") or e.surec_id or ""; e.sira = str(k.get("sira") or ""); e.prosedur = k.get("procedure"); e.ad = (k.get("name") or k["id"])[:300]
    e.periyot = k.get("reviewPeriod") or "1A"; e.sorumlu = k.get("owner") or None; e.hedef_metin = k.get("targetLabel"); e.birim = k.get("unit") or ""
    e.yon = k.get("direction") or "higher"; e.izleme = bool(k.get("monitoring")); e.hedef = k.get("target"); e.sari_esik = k.get("amberThreshold")
    e.bant_alt = k.get("bandLow"); e.bant_ust = k.get("bandHigh"); e.oran_olcek = bool(k.get("ratioScale"))
    e.gecmis_json = json.dumps(k.get("history") or {}, ensure_ascii=False); e.yil = k.get("year"); e.aktif = bool(k.get("active", True))
    e.ham_json = _ham(k)
    return db.merge(e)


def olcum_yaz(db, m):
    from models import PkOlcum
    e = db.get(PkOlcum, m["id"]) or PkOlcum(id=m["id"])
    e.kpi_id = m["kpiId"]; e.donem = m["period"]
    e.gerceklesen = m.get("actual") if isinstance(m.get("actual"), (int, float)) and not isinstance(m.get("actual"), bool) else None
    e.metin = ((m.get("text") or "") or None) and str(m.get("text"))[:250]; e.giren = (m.get("enteredBy") or None) and str(m.get("enteredBy"))[:120]
    e.girilme = _dt(m.get("enteredAt")); e.ham_json = _ham(m)
    return db.merge(e)


def olcum_pencere_yaz(db, m, aylar):
    """kpi.debak.com'daki gibi: aynı KPI'ın aynı yıl içinde pencere aylarındaki ölçümleri kaldırılır, yeni ölçüm yazılır
    (aynı dönemde ölçüm varsa kimliği korunur)."""
    from models import PkOlcum
    yil = str(m["period"])[:4]; aylar = {int(a) for a in (aylar or [])}
    mevcut = db.query(PkOlcum).filter(PkOlcum.kpi_id == m["kpiId"], PkOlcum.donem == m["period"]).first()
    m = dict(m, id=mevcut.id if mevcut else (m.get("id") or f"m-{m['kpiId']}-{m['period']}"))
    for o in db.query(PkOlcum).filter(PkOlcum.kpi_id == m["kpiId"], PkOlcum.donem.like(f"{yil}-%")).all():
        if o.id != m["id"] and int(o.donem[5:7] or 0) in aylar:
            db.delete(o)
    db.flush()
    return olcum_yaz(db, m)


def ncr_yaz(db, n):
    from models import PkNcr, PkNcrAtlama
    e = db.get(PkNcr, n["id"]) or PkNcr(id=n["id"])
    e.numara = (n.get("number") or n["id"])[:30]; e.tur = n.get("type") or "kpi-miss"; e.onem = n.get("severity"); e.baslik = (n.get("title") or e.numara)[:300]
    e.aciklama = n.get("description"); e.kpi_id = n.get("kpiId") or None; e.donem = n.get("period") or None; e.surec_id = n.get("process") or None
    e.iatf_madde = n.get("iatfClause"); e.tespit_tarihi = _t(n.get("detectedAt")); e.tespit_eden = n.get("detectedBy"); e.sorumlu = n.get("owner")
    e.termin = _t(n.get("dueDate")); e.asama = n.get("stage") or "kayit"; e.ekip = n.get("team"); e.koruma = n.get("containment")
    e.koruma_tarihi = _t(n.get("containmentDate")); e.kok_neden = n.get("rootCause"); e.bes_neden_json = json.dumps(n.get("fiveWhys") or [""] * 5, ensure_ascii=False)
    e.hata_onleme = n.get("errorProofing"); e.kys_degisiklik = bool(n.get("qmsChangeNeeded")); e.kys_degisiklik_notu = n.get("qmsChangeNote")
    e.risk_guncelleme = n.get("riskUpdate"); e.etkinlik_kaniti = n.get("effectivenessEvidence"); e.etkinlik_tarihi = _t(n.get("effectivenessDate"))
    e.kapanis_tarihi = _t(n.get("closedAt")); e.kapatan = n.get("closedBy")
    e.aksiyonlar_json = json.dumps(n.get("actions") or [], ensure_ascii=False); e.olaylar_json = json.dumps(n.get("events") or [], ensure_ascii=False)
    e.ham_json = _ham(n)
    e = db.merge(e); db.flush()
    dof_esle(db, e)
    if e.kpi_id and e.donem:      # kayıt açılan dönem artık "atlanmış" değildir
        db.query(PkNcrAtlama).filter_by(kpi_id=e.kpi_id, donem=e.donem).delete()
    return e


def ncr_sil(db, nid):
    """NCR silinir; yansıyan DÖF silinir; KPI dönemi 'atlandı' olarak işaretlenir (otomatik yeniden açılmaz)."""
    from models import PkNcr, PkNcrAtlama, CorrectiveAction
    n = db.get(PkNcr, nid)
    if not n:
        return None
    if n.dof_id:
        c = db.get(CorrectiveAction, n.dof_id)
        n.dof_id = None; db.flush()
        if c and c.dis_kimlik == n.id:
            db.delete(c)
    if n.kpi_id and n.donem and not db.query(PkNcrAtlama).filter_by(kpi_id=n.kpi_id, donem=n.donem).first():
        db.add(PkNcrAtlama(kpi_id=n.kpi_id, donem=n.donem, aciklama="NCR silindi"))
    no = n.numara; db.delete(n)
    return no


def _json(s, varsayilan):
    try:
        return json.loads(s) if s else varsayilan
    except ValueError:
        return varsayilan


def durum_uret(db):
    """kpi.debak.com /api/state biçiminde tüm veri (arayüz bunu okur)."""
    from models import PkSurec, PkKpi, PkOlcum, PkNcr, PkNcrAtlama
    a = ayar(db)
    def iso(d):
        return d.isoformat() if d else ""
    surecler = [_json(p.ham_json, None) or {"id": p.id, "code": p.id, "name": p.ad, "title": p.baslik, "family": p.aile}
                for p in db.query(PkSurec).order_by(PkSurec.aile, PkSurec.id).all()]
    kpiler = [_json(k.ham_json, None) or {"id": k.id, "processId": k.surec_id, "sira": k.sira, "procedure": k.prosedur, "name": k.ad, "reviewPeriod": k.periyot,
                                           "owner": k.sorumlu or "", "targetLabel": k.hedef_metin, "unit": k.birim or "", "direction": k.yon, "monitoring": k.izleme,
                                           "target": k.hedef, "amberThreshold": k.sari_esik, "bandLow": k.bant_alt, "bandHigh": k.bant_ust, "ratioScale": k.oran_olcek,
                                           "history": k.gecmis, "year": k.yil, "active": k.aktif}
              for k in db.query(PkKpi).order_by(PkKpi.surec_id, PkKpi.id).all()]
    olcumler = [_json(o.ham_json, None) or {"id": o.id, "kpiId": o.kpi_id, "period": o.donem, "actual": o.gerceklesen, "text": o.metin or "",
                                             "enteredBy": o.giren or "", "enteredAt": o.girilme.isoformat() + "Z" if o.girilme else ""}
                for o in db.query(PkOlcum).order_by(PkOlcum.kpi_id, PkOlcum.donem).all()]
    ncrler = [_json(n.ham_json, None) or {"id": n.id, "number": n.numara, "type": n.tur, "severity": n.onem, "title": n.baslik, "description": n.aciklama or "",
                                           "kpiId": n.kpi_id, "period": n.donem, "process": n.surec_id, "iatfClause": n.iatf_madde, "detectedAt": iso(n.tespit_tarihi),
                                           "detectedBy": n.tespit_eden or "", "owner": n.sorumlu or "", "dueDate": iso(n.termin), "stage": n.asama, "team": n.ekip or "",
                                           "containment": n.koruma or "", "containmentDate": iso(n.koruma_tarihi), "rootCause": n.kok_neden or "", "fiveWhys": n.bes_neden,
                                           "errorProofing": n.hata_onleme or "", "qmsChangeNeeded": bool(n.kys_degisiklik), "qmsChangeNote": n.kys_degisiklik_notu or "",
                                           "riskUpdate": n.risk_guncelleme or "", "effectivenessEvidence": n.etkinlik_kaniti or "", "effectivenessDate": iso(n.etkinlik_tarihi),
                                           "closedAt": iso(n.kapanis_tarihi), "closedBy": n.kapatan or "", "actions": n.aksiyonlar, "events": n.olaylar}
              for n in db.query(PkNcr).all()]
    atla = [{"kpiId": x.kpi_id, "period": x.donem} for x in db.query(PkNcrAtlama).order_by(PkNcrAtlama.id).all()]
    return {"version": a.get("version") or 8, "seedRev": a.get("seed_rev") or "", "seedLabel": a.get("kaynak_etiket") or "", "plantName": a.get("tesis") or "Debak",
            "year": int(a.get("yil") or date.today().year), "form": a.get("form") or "Y03 F06", "people": a.get("kisiler") or [],
            "processes": surecler, "kpis": kpiler, "measurements": olcumler, "ncrs": ncrler, "ncrSkips": atla}


def durum_yaz(db, s, tam=True):
    """Arayüzden gelen tüm durumu yazar (içe aktarma / sıfırlama). tam=True: durumda olmayan KPI / ölçüm / NCR silinir."""
    from models import PkSurec, PkKpi, PkOlcum, PkNcr, PkNcrAtlama
    R = {"surec": 0, "kpi": 0, "olcum": 0, "ncr": 0, "atlama": 0}
    for p in s.get("processes", []):
        surec_yaz(db, p); R["surec"] += 1
    for k in s.get("kpis", []):
        kpi_yaz(db, k); R["kpi"] += 1
    for m in s.get("measurements", []):
        olcum_yaz(db, m); R["olcum"] += 1
    db.flush()
    for n in s.get("ncrs", []):
        ncr_yaz(db, n); R["ncr"] += 1
    if tam:
        for model, anahtar in ((PkKpi, "kpis"), (PkOlcum, "measurements")):
            ids = {x["id"] for x in s.get(anahtar, [])}
            for e in db.query(model).all():
                if e.id not in ids:
                    db.delete(e)
        ids = {x["id"] for x in s.get("ncrs", [])}
        for e in db.query(PkNcr).all():
            if e.id not in ids:
                ncr_sil(db, e.id)
        db.query(PkNcrAtlama).delete()
    db.flush()
    for a in s.get("ncrSkips", []):
        if not db.query(PkNcrAtlama).filter_by(kpi_id=a["kpiId"], donem=a["period"]).first():
            db.add(PkNcrAtlama(kpi_id=a["kpiId"], donem=a["period"], aciklama="kpi.debak.com")); R["atlama"] += 1
    ayar_yaz(db, yil=s.get("year") or date.today().year, tesis=s.get("plantName") or "Debak", form=s.get("form") or "Y03 F06",
             kisiler=s.get("people") or [], kaynak_etiket=s.get("seedLabel"), version=s.get("version"), seed_rev=s.get("seedRev"))
    return R


def aktar(db, kaynak=None):
    """kpi.debak.com'dan (ya da aynı biçimde dosyadan) aktarım. Mevcut DYS kayıtları silinmez; aynı kimlikler güncellenir."""
    s = veri_al(kaynak)
    R = durum_yaz(db, s, tam=False)
    ayar_yaz(db, son_aktarim=datetime.now().isoformat(timespec="seconds"), son_aktarim_ozet=R)
    db.commit()
    return R
