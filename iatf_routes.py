"""
DYS — IATF Modül Route'ları (SPC & CSR)
=========================================
IATF 16949 § 9.1.1.1 (SPC) ve Müşteriye Özel Gereklilikler (CSR) yönetimi.
"""

from sqlalchemy import case as _sa_case
import json
from datetime import date, datetime
from flask import render_template, request, redirect, url_for, flash, abort, session, jsonify, send_file

from app_runtime import host as _host
_h = _host()
app = _h.app
login_required = _h.login_required
get_db = _h.get_db
log_action = _h.log_action
admin_required = _h.admin_required

from models import (
    User, Document, SPCRecord, CustomerSpecificRequirement,
    CSR_DURUMLARI, STANDARTLAR, APQPProject, ControlPlan,
    CalibrationEquipment, NonconformingProduct, Supplier,
    KALIBRASYON_DURUMLARI as KALIBRASYON_DURUMLARI_LISTE,
    TEDARIKCI_KATEGORILERI, TEDARIKCI_DURUMLARI
)
from spc_chart import cp_cpk_hesapla, generate_valid_spc_data
from excel_import import ImportSemasi, SutunTanimi, sablon_uret, dogrula, aktar

def _float(v):
    if not v:
        return None
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None

def _date(v):
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  SPC — İstatistiksel Proses Kontrol
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/iatf/spc", methods=["GET", "POST"])
@login_required
def iatf_spc():
    db = get_db()
    try:
        if request.method == "POST":
            parca_no = (request.form.get("parca_no") or "").strip()
            karakteristik = (request.form.get("karakteristik") or "").strip()
            tarih = _date(request.form.get("olcum_tarihi")) or date.today()
            usl = _float(request.form.get("usl"))
            lsl = _float(request.form.get("lsl"))
            olcum_turu = request.form.get("olcum_turu") or "auto"
            yorum = (request.form.get("yorum") or "").strip() or None

            if not parca_no or not karakteristik or usl is None or lsl is None:
                flash("Parça No, karakteristik, LSL ve USL girilmesi zorunludur.", "error")
                return redirect(url_for("iatf_spc"))

            if lsl >= usl:
                flash("LSL değeri USL değerinden küçük olmalıdır.", "error")
                return redirect(url_for("iatf_spc"))

            # Ölçüm verilerini elde et
            olcumler = []
            if olcum_turu == "auto":
                olcumler = generate_valid_spc_data(lsl, usl)
            else:
                raw_data = request.form.get("olcumler_raw") or ""
                # Boşluk, virgül veya yeni satıra göre ayır
                raw_tokens = raw_data.replace(",", " ").replace("\n", " ").replace("\r", " ").split()
                for token in raw_tokens:
                    val = _float(token)
                    if val is not None:
                        olcumler.append(val)

                if len(olcumler) < 30:
                    flash(f"Manuel ölçüm girildiğinde en az 30 değer olmalıdır (Alınan: {len(olcumler)}).", "error")
                    return redirect(url_for("iatf_spc"))

            # İstatistiksel analiz yap
            analysis = cp_cpk_hesapla(olcumler, usl, lsl)
            if not analysis:
                flash("İstatistiksel analiz yapılamadı. Girdiğiniz verileri kontrol edin.", "error")
                return redirect(url_for("iatf_spc"))

            # Veritabanına kaydet
            record = SPCRecord(
                parca_no=parca_no,
                karakteristik=karakteristik,
                olcum_tarihi=tarih,
                deger=analysis["ortalama"], # Eski deger kolonu ortalama olarak saklanabilir
                usl=usl,
                lsl=lsl,
                ortalama=analysis["ortalama"],
                std_sapma=analysis["std_sapma"],
                cp=analysis["cp"],
                cpk=analysis["cpk"],
                yorum=yorum,
                olcumler=json.dumps(olcumler)
            )
            db.add(record)
            db.commit()
            log_action(db, "Oluşturma", detay=f"SPC Ölçüm Kaydı: {parca_no} - {karakteristik} (Cpk: {analysis['cpk']})")
            flash(f"SPC analizi başarıyla kaydedildi! Cpk: {analysis['cpk']}", "success")
            return redirect(url_for("iatf_spc"))

        # GET metodu: tüm kayıtları getir
        kayitlar = db.query(SPCRecord).order_by(SPCRecord.olusturma_tarihi.desc()).all()
        
        # JSON string'i listeye çöz
        for r in kayitlar:
            if r.olcumler:
                try:
                    r.olcum_listesi = json.loads(r.olcumler)
                except Exception:
                    r.olcum_listesi = []
            else:
                r.olcum_listesi = []

        return render_template("iatf_spc.html", kayitlar=kayitlar)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════
#  CSR — Müşteriye Özel Gereklilikler
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/iatf/csr", methods=["GET", "POST"])
@login_required
def iatf_csr():
    db = get_db()
    try:
        if request.method == "POST":
            if session.get("rol") not in ("Admin", "Doküman Kontrol"):
                abort(403)
            
            csr = CustomerSpecificRequirement(
                musteri=(request.form.get("musteri") or "").strip() or "OEM",
                madde_no=(request.form.get("madde_no") or "").strip() or None,
                aciklama=(request.form.get("aciklama") or "").strip() or None,
                ilgili_document_id=_float(request.form.get("ilgili_document_id")), # _to_int helper yerine
                durum=request.form.get("durum") or "Açık"
            )
            db.add(csr)
            db.commit()
            log_action(db, "Oluşturma", detay=f"CSR Eklendi: {csr.musteri} {csr.madde_no or ''}")
            flash("Müşteriye özel gereklilik başarıyla eklendi.", "success")
            return redirect(url_for("iatf_csr"))

        kayitlar = db.query(CustomerSpecificRequirement).order_by(CustomerSpecificRequirement.olusturma_tarihi.desc()).all()
        docs = db.query(Document).order_by(Document.dokuman_no).all()
        return render_template(
            "iatf_csr.html",
            kayitlar=kayitlar,
            documents=docs,
            durumlar=CSR_DURUMLARI
        )
    finally:
        db.close()


def _can_edit():
    return session.get("rol") in ("Admin", "Doküman Kontrol")

def _int(v):
    if not v:
        return None
    try:
        return int(float(str(v)))
    except (TypeError, ValueError):
        return None


@app.route("/iatf/apqp", methods=["GET", "POST"])
@login_required
def iatf_apqp():
    db = get_db()
    try:
        if request.method == "POST":
            if not _can_edit():
                abort(403)
                
            proje_no = (request.form.get("proje_no") or "").strip()
            ad = (request.form.get("ad") or "").strip()
            if not proje_no or not ad:
                flash("Proje No ve Ad alanları zorunludur.", "error")
                return redirect(url_for("iatf_apqp"))
                
            project = APQPProject(
                proje_no=proje_no,
                ad=ad,
                musteri=(request.form.get("musteri") or "").strip() or None,
                parca_no=(request.form.get("parca_no") or "").strip() or None,
                faz=_int(request.form.get("faz")) or 1,
                durum=request.form.get("durum") or "Planlandı",
                sorumlu_id=_int(request.form.get("sorumlu_id")),
                baslangic_tarihi=_date(request.form.get("baslangic_tarihi")),
                hedef_tarih=_date(request.form.get("hedef_tarih")),
                aciklama=(request.form.get("aciklama") or "").strip() or None
            )
            db.add(project)
            db.commit()
            log_action(db, "Oluşturma", detay=f"APQP Projesi eklendi: {project.proje_no}")
            flash("APQP Projesi başarıyla oluşturuldu.", "success")
            return redirect(url_for("iatf_apqp"))
            
        kayitlar = db.query(APQPProject).order_by(APQPProject.id.desc()).all()
        users = db.query(User).filter_by(aktif=True).order_by(User.ad_soyad).all()
        return render_template("iatf_apqp.html", kayitlar=kayitlar, users=users, can_edit=_can_edit())
    finally:
        db.close()


@app.route("/iatf/control-plans", methods=["GET", "POST"])
@login_required
def iatf_control_plans():
    db = get_db()
    try:
        if request.method == "POST":
            if not _can_edit():
                abort(403)
                
            cp_no = (request.form.get("cp_no") or "").strip()
            if not cp_no:
                flash("Kontrol Planı No zorunludur.", "error")
                return redirect(url_for("iatf_control_plans"))
                
            plan = ControlPlan(
                cp_no=cp_no,
                tip=request.form.get("tip") or "Üretim",
                parca_no=(request.form.get("parca_no") or "").strip() or None,
                parca_adi=(request.form.get("parca_adi") or "").strip() or None,
                proses=(request.form.get("proses") or "").strip() or None,
                revizyon=(request.form.get("revizyon") or "00").strip(),
                ilgili_document_id=_int(request.form.get("ilgili_document_id")) or None,
                fmea_id=_int(request.form.get("fmea_id")) or None
            )
            db.add(plan)
            db.commit()
            log_action(db, "Oluşturma", detay=f"Kontrol Planı eklendi: {plan.cp_no}")
            flash("Kontrol Planı başarıyla oluşturuldu.", "success")
            return redirect(url_for("iatf_control_plans"))
            
        kayitlar = db.query(ControlPlan).order_by(ControlPlan.id.desc()).all()
        docs = db.query(Document).order_by(Document.dokuman_no).all()
        from models import FMEA
        fmeas = db.query(FMEA).order_by(FMEA.fmea_no).all()
        return render_template("iatf_control_plans.html", kayitlar=kayitlar, documents=docs, fmeas=fmeas, can_edit=_can_edit())
    finally:
        db.close()


@app.route("/iatf/calibration", methods=["GET", "POST"])
@login_required
def iatf_calibration():
    db = get_db()
    try:
        if request.method == "POST":
            if not _can_edit():
                abort(403)
                
            ekipman_no = (request.form.get("ekipman_no") or "").strip()
            ad = (request.form.get("ad") or "").strip()
            if not ekipman_no or not ad:
                flash("Ekipman No ve Ad alanları zorunludur.", "error")
                return redirect(url_for("iatf_calibration"))
                
            eq = CalibrationEquipment(
                ekipman_no=ekipman_no,
                ad=ad,
                tip=(request.form.get("tip") or "").strip() or None,
                konum=(request.form.get("konum") or "").strip() or None,
                kalibrasyon_araligi_ay=_int(request.form.get("kalibrasyon_araligi_ay")) or 12,
                son_kalibrasyon=_date(request.form.get("son_kalibrasyon")),
                sonraki_kalibrasyon=_date(request.form.get("sonraki_kalibrasyon")),
                izin_verilen_hata=(request.form.get("izin_verilen_hata") or "").strip() or None,
                kalibrasyon_sapma=(request.form.get("kalibrasyon_sapma") or "").strip() or None,
                msa_sonucu=(request.form.get("msa_sonucu") or "").strip() or None,
                durum=request.form.get("durum") or "Geçerli",
                sorumlu_id=_int(request.form.get("sorumlu_id"))
            )
            db.add(eq)
            db.commit()
            log_action(db, "Oluşturma", detay=f"Kalibrasyon Cihazı eklendi: {eq.ekipman_no}")
            flash("Kalibrasyon / MSA Ekipmanı başarıyla kaydedildi.", "success")
            return redirect(url_for("iatf_calibration"))
            
        tum = db.query(CalibrationEquipment).order_by(_sa_case((CalibrationEquipment.sonraki_kalibrasyon.is_(None), 1), else_=0), CalibrationEquipment.sonraki_kalibrasyon.asc()).all()
        bugun = date.today()
        geciken = [k for k in tum if k.sonraki_kalibrasyon and k.sonraki_kalibrasyon < bugun and k.durum not in ("Pasif", "Kullanım Dışı")]
        # 2026 — arama / filtre (D04 F06 listesi yüzlerce cihaz içerir)
        q = (request.args.get("q") or "").strip().lower()
        f_durum, f_yer, f_tur = request.args.get("durum") or "", request.args.get("yer") or "", request.args.get("tur") or ""
        yaklasan = request.args.get("yaklasan") == "1"
        kayitlar = [k for k in tum
                    if (not q or q in " ".join(str(x or "") for x in (k.ekipman_no, k.cihaz_kodu, k.ad, k.tip, k.seri_no, k.sertifika_no, k.konum)).lower())
                    and (not f_durum or k.durum == f_durum) and (not f_yer or (k.konum or "") == f_yer)
                    and (not f_tur or (k.cihaz_turu or "") == f_tur)
                    and (not yaklasan or (k.kalan_gun is not None and k.kalan_gun <= 30))]
        users = db.query(User).filter_by(aktif=True).order_by(User.ad_soyad).all()
        return render_template("iatf_calibration.html", kayitlar=kayitlar, toplam=len(tum), geciken=len(geciken), users=users,
                               can_edit=_can_edit(), q=request.args.get("q") or "", f_durum=f_durum, f_yer=f_yer, f_tur=f_tur,
                               yaklasan=yaklasan, yerler=sorted({k.konum for k in tum if k.konum}),
                               turler=sorted({k.cihaz_turu for k in tum if k.cihaz_turu}),
                               yaklasan_sayi=len([k for k in tum if k.kalan_gun is not None and 0 <= k.kalan_gun <= 30]),
                               durumlar=KALIBRASYON_DURUMLARI_LISTE)
    finally:
        db.close()


@app.route("/iatf/nonconforming", methods=["GET", "POST"])
@login_required
def iatf_nonconforming():
    db = get_db()
    try:
        if request.method == "POST":
            if not _can_edit():
                abort(403)
                
            nc_no = (request.form.get("nc_no") or "").strip()
            if not nc_no:
                flash("Uygunsuzluk No zorunludur.", "error")
                return redirect(url_for("iatf_nonconforming"))
                
            nc = NonconformingProduct(
                nc_no=nc_no,
                parca_no=(request.form.get("parca_no") or "").strip() or None,
                parca_adi=(request.form.get("parca_adi") or "").strip() or None,
                miktar=_float(request.form.get("miktar")),
                tespit_yeri=(request.form.get("tespit_yeri") or "").strip() or None,
                parti_izlenebilirlik=(request.form.get("parti_izlenebilirlik") or "").strip() or None,
                tanim=(request.form.get("tanim") or "").strip() or None,
                karar=request.form.get("karar") or "Beklemede",
                musteriye_bildirim=request.form.get("musteriye_bildirim") == "1",
                durum=request.form.get("durum") or "Açık",
                sorumlu_id=_int(request.form.get("sorumlu_id")),
                capa_id=_int(request.form.get("capa_id")) or None,
                tespit_tarihi=_date(request.form.get("tespit_tarihi")) or date.today()
            )
            db.add(nc)
            db.commit()
            log_action(db, "Oluşturma", detay=f"Uygun Olmayan Ürün eklendi: {nc.nc_no}")
            flash("Uygun olmayan ürün / karantina kaydı başarıyla oluşturuldu.", "success")
            return redirect(url_for("iatf_nonconforming"))
            
        kayitlar = db.query(NonconformingProduct).order_by(NonconformingProduct.id.desc()).all()
        users = db.query(User).filter_by(aktif=True).order_by(User.ad_soyad).all()
        from models import CorrectiveAction
        capas = db.query(CorrectiveAction).order_by(CorrectiveAction.id.desc()).all()
        return render_template("iatf_nonconforming.html", kayitlar=kayitlar, users=users, capas=capas, can_edit=_can_edit())
    finally:
        db.close()


@app.route("/iatf/suppliers", methods=["GET", "POST"])
@login_required
def iatf_suppliers():
    db = get_db()
    try:
        if request.method == "POST":
            if not _can_edit():
                abort(403)
                
            kod = (request.form.get("kod") or "").strip()
            ad = (request.form.get("ad") or "").strip()
            if not kod or not ad:
                flash("Tedarikçi Kodu ve Adı alanları zorunludur.", "error")
                return redirect(url_for("iatf_suppliers"))
                
            supplier = Supplier(
                kod=kod,
                ad=ad,
                kategori=request.form.get("kategori") or "Direkt",
                risk_sinifi=request.form.get("risk_sinifi") or "Düşük",
                onay_durumu=request.form.get("onay_durumu") or "Aday",
                iletisim=(request.form.get("iletisim") or "").strip() or None,
                iatf_sertifikali=request.form.get("iatf_sertifikali") == "1",
                sertifika_bitis=_date(request.form.get("sertifika_bitis")),
                aciklama=(request.form.get("aciklama") or "").strip() or None
            )
            db.add(supplier)
            db.commit()
            log_action(db, "Oluşturma", detay=f"Tedarikçi eklendi: {supplier.ad}")
            flash("Tedarikçi kartı başarıyla eklendi.", "success")
            return redirect(url_for("iatf_suppliers"))
            
        kayitlar = db.query(Supplier).order_by(Supplier.ad.asc()).all()
        return render_template("iatf_suppliers.html", kayitlar=kayitlar, can_edit=_can_edit())
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════
#  Tedarikçiler — Excel Şablonu / Toplu İçe Aktarım
#  (Genel amaçlı motor: excel_import.py — bkz. modül docstring'i)
# ═══════════════════════════════════════════════════════════════════════════
def _tedarikci_kod_benzersiz_mi(db, deger):
    """'kod' sütunu için referans kontrolü: bu kod veritabanında zaten var mı?"""
    if db is None:
        return None
    if db.query(Supplier).filter(Supplier.kod == deger).first():
        return f"'{deger}' kodlu tedarikçi veritabanında zaten kayıtlı."
    return None


def _tedarikci_import_semasi() -> ImportSemasi:
    """Tedarikçi (Supplier) modeli için Excel içe aktarım şeması.

    Şablon üretimi VE doğrulama bu tek şemadan beslenir; alanlar iki ayrı
    yerde tekrar tanımlanmaz.
    """
    return ImportSemasi(
        ad="Tedarikçiler",
        sutunlar=[
            SutunTanimi(
                baslik="Tedarikçi Kodu", alan_adi="kod", tip="metin",
                zorunlu=True, maks_uzunluk=30, benzersiz=True,
                referans_kontrol=_tedarikci_kod_benzersiz_mi,
            ),
            SutunTanimi(
                baslik="Firma Adı / Unvan", alan_adi="ad", tip="metin",
                zorunlu=True, maks_uzunluk=200,
            ),
            SutunTanimi(
                baslik="Kategori", alan_adi="kategori", tip="enum",
                zorunlu=True, enum_degerleri=TEDARIKCI_KATEGORILERI,
            ),
            SutunTanimi(
                baslik="Onay Durumu", alan_adi="onay_durumu", tip="enum",
                zorunlu=True, enum_degerleri=TEDARIKCI_DURUMLARI,
            ),
            SutunTanimi(
                baslik="Risk Sınıfı", alan_adi="risk_sinifi", tip="enum",
                zorunlu=False, enum_degerleri=("Düşük", "Orta", "Yüksek"),
            ),
            SutunTanimi(
                baslik="İletişim Bilgileri", alan_adi="iletisim", tip="metin",
                zorunlu=False, maks_uzunluk=200,
            ),
            SutunTanimi(
                baslik="IATF 16949 Sertifikalı mı? (Evet/Hayır)",
                alan_adi="iatf_sertifikali", tip="boolean",
                zorunlu=False, varsayilan=False,
            ),
            SutunTanimi(
                baslik="Sertifika Bitiş Tarihi (YYYY-AA-GG)",
                alan_adi="sertifika_bitis", tip="tarih", zorunlu=False,
            ),
            SutunTanimi(
                baslik="Açıklama / Değerlendirme Notu", alan_adi="aciklama",
                tip="metin", zorunlu=False,
            ),
        ],
        ornek_satirlar=[
            {
                "kod": "TED-101", "ad": "Akme Plastik Kalıp A.Ş.",
                "kategori": "Hammadde", "onay_durumu": "Onaylı",
                "risk_sinifi": "Düşük",
                "iletisim": "info@akmeplastik.com, +90 224 555 00 00",
                "iatf_sertifikali": "Evet", "sertifika_bitis": date(2027, 6, 30),
                "aciklama": "Enjeksiyon kalıp ve plastik parça üretimi.",
            },
            {
                "kod": "TED-102", "ad": "Bora Kaplama Sanayi Ltd. Şti.",
                "kategori": "Kalıp/Aparat", "onay_durumu": "Şartlı",
                "risk_sinifi": "Orta",
                "iletisim": "satis@borakaplama.com, +90 224 555 11 11",
                "iatf_sertifikali": "Hayır", "sertifika_bitis": None,
                "aciklama": "Yüzey kaplama ve ısıl işlem hizmeti.",
            },
        ],
    )


@app.route("/suppliers/import-template")
@login_required
def suppliers_import_template():
    """Tedarikçi toplu içe aktarım için boş Excel şablonunu indirir."""
    sema = _tedarikci_import_semasi()
    akis = sablon_uret(sema)
    return send_file(
        akis,
        as_attachment=True,
        download_name="tedarikci_ice_aktarim_sablonu.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/suppliers/import", methods=["GET", "POST"])
@login_required
@admin_required
def suppliers_import():
    """Doldurulmuş Excel şablonunu doğrular ve TEK transaction'da içe aktarır."""
    if request.method != "POST":
        return render_template("suppliers_import.html", hatalar=[])

    db = get_db()
    try:
        dosya = request.files.get("file")
        if not dosya or not dosya.filename:
            flash("Lütfen içe aktarılacak bir Excel dosyası seçin.", "error")
            return redirect(url_for("suppliers_import"))

        if not dosya.filename.lower().endswith(".xlsx"):
            flash("Yalnızca .xlsx uzantılı Excel dosyaları kabul edilir.", "error")
            return redirect(url_for("suppliers_import"))

        sema = _tedarikci_import_semasi()
        hatalar, temiz_satirlar = dogrula(dosya, sema, db=db)

        if hatalar:
            return render_template("suppliers_import.html", hatalar=hatalar)

        if not temiz_satirlar:
            flash("Dosyada içe aktarılacak veri bulunamadı. Lütfen 'Veri' sayfasını doldurun.", "error")
            return redirect(url_for("suppliers_import"))

        eklenen = aktar(db, Supplier, temiz_satirlar)
        log_action(db, "Oluşturma", detay=f"Tedarikçi Excel toplu içe aktarım: {eklenen} kayıt eklendi.")
        flash(f"{eklenen} tedarikçi başarıyla içe aktarıldı.", "success")
        return redirect(url_for("iatf_suppliers"))
    except Exception as exc:
        db.rollback()
        flash(f"İçe aktarım başarısız oldu, hiçbir kayıt eklenmedi: {exc}", "error")
        return redirect(url_for("suppliers_import"))
    finally:
        db.close()

