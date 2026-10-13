"""
DYS — Office dosyalarının PDF önizlemesi (2026)
Word / Excel / PowerPoint (doc, docx, xls, xlsx, xlsm, rtf, odt, ods, ppt, pptx) önizlemede antet ve sayfa düzeniyle,
yazdırıldığı gibi PDF olarak gösterilir.
  - Önbellek anahtarı dosya İÇERİĞİNİN SHA-256 özetidir: dosya taşınsa / kopyalansa da aynı PDF kullanılır; dosya
    değişirse (yeni revizyon) özet değişir, yeni PDF üretilir.
  - Arama sırası: DYS_ONIZLEME_KLASORU (yazılabilir önbellek; varsayılan UPLOAD_FOLDER/onizleme_pdf) + DYS_ONIZLEME_EK_KLASORLER
    (salt okunur, ';' ile ayrılmış — ör. önceden üretilmiş 'DYS Aktarım/Önizleme PDF' paketi).
  - Bulunamazsa sunucuda Office varsa (watermark._office_to_pdf, COM) anında üretilir ve önbelleğe yazılır.
    Office yoksa None döner; ekran eski (tablo / metin) önizlemeye düşer.
"""
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import uuid

logger = logging.getLogger("dys")

OFFICE_UZANTILARI = {"doc", "docx", "xls", "xlsx", "xlsm", "rtf", "odt", "ods", "ppt", "pptx"}
_KILIT = threading.Lock()          # Office COM / LibreOffice aynı anda tek dönüştürme
_OZET = {}                         # (yol, mtime, boyut) → sha256
_BASARISIZ = {}                    # sha256 → deneme sayısı (bozuk dosyada her açılışta Office açılmasın)
_SOFFICE = None                    # soffice.exe yolu (bir kez bulunur); False = bulunamadı


def _soffice_yolu():
    """LibreOffice/soffice çalıştırılabilirini bulur (sunucuda Office olmadan PDF üretimi için)."""
    global _SOFFICE
    if _SOFFICE is not None:
        return _SOFFICE or None
    adaylar = []
    ozel = (os.environ.get("DYS_SOFFICE") or os.environ.get("SOFFICE_PATH") or "").strip()
    if ozel:
        adaylar.append(ozel)
    adaylar += [
        shutil.which("soffice"),
        shutil.which("soffice.com"),
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/usr/bin/libreoffice",
    ]
    for p in adaylar:
        if p and os.path.isfile(p):
            _SOFFICE = p
            return p
    _SOFFICE = False
    return None


def _soffice_to_pdf(src, out_dir):
    """LibreOffice headless ile PDF üretir; çıktı PDF yolu ya da None.

    IIS servis hesabı altında yazılabilir bir kullanıcı profili (UserInstallation) şarttır;
    aksi halde soffice sessizce çıkar. Her çağrı kendi geçici profilini kullanır.
    """
    exe = _soffice_yolu()
    if not exe:
        return None
    pdf = os.path.join(out_dir, os.path.splitext(os.path.basename(src))[0] + ".pdf")
    # Soğuk başlatma (kurulum / ilk açılış sonrası ilk çağrı) bazen boş döner; bir kez yeniden dene.
    for deneme in (1, 2):
        profil = tempfile.mkdtemp(prefix="dys_lo_profil_")
        try:
            cmd = [
                exe, "--headless", "--nologo", "--nofirststartwizard", "--norestore",
                f"-env:UserInstallation=file:///{profil.replace(os.sep, '/')}",
                "--convert-to", "pdf", "--outdir", out_dir, os.path.abspath(src),
            ]
            try:
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=180, check=False)
            except (subprocess.TimeoutExpired, OSError):
                logger.exception("LibreOffice PDF dönüşümü başarısız / zaman aşımı: %s", src)
                return None
            if os.path.isfile(pdf) and os.path.getsize(pdf) > 0:
                return pdf
        finally:
            shutil.rmtree(profil, ignore_errors=True)
        if deneme == 1:
            logger.warning("LibreOffice ilk denemede PDF üretmedi, yeniden deneniyor: %s", src)
    return None


def _klasorler():
    from config import Config
    ana = getattr(Config, "ONIZLEME_KLASORU", None) or os.path.join(Config.UPLOAD_FOLDER, "onizleme_pdf")
    ek = [k.strip() for k in (getattr(Config, "ONIZLEME_EK_KLASORLER", "") or "").split(";") if k.strip()]
    return ana, ek


def ozet(yol):
    st = os.stat(yol)
    k = (os.path.abspath(yol), st.st_mtime, st.st_size)
    if k not in _OZET:
        h = hashlib.sha256()
        with open(yol, "rb") as f:
            for parca in iter(lambda: f.read(1 << 20), b""):
                h.update(parca)
        _OZET[k] = h.hexdigest()
    return _OZET[k]


def uzanti(ad):
    return (os.path.splitext(ad or "")[1] or "").lower().lstrip(".")


def onbellekte(yol):
    """Önbellekteki PDF yolu ya da None (dönüştürme yapmaz)."""
    try:
        o = ozet(yol)
    except OSError:
        return None
    ana, ek = _klasorler()
    for k in [ana] + ek:
        p = os.path.join(k, o[:2], o + ".pdf")
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    return None


def _ppt_to_pdf(src, pdf):
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return False
    pythoncom.CoInitialize()
    app = None
    try:
        app = win32com.client.DispatchEx("PowerPoint.Application")
        pres = app.Presentations.Open(os.path.abspath(src), ReadOnly=True, WithWindow=False)
        pres.SaveAs(os.path.abspath(pdf), 32)  # ppSaveAsPDF
        pres.Close()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("PowerPoint PDF dönüşümü başarısız")
        return False
    finally:
        if app is not None:
            try:
                app.Quit()
            except Exception:  # noqa: BLE001
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


def _word_farkli_kaydet(yol, klasor):
    """Word 'PDF olarak kaydet' (SaveAs2, wdFormatPDF=17): ExportAsFixedFormat'ta çöken belgelerde çalışır."""
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return None
    pythoncom.CoInitialize()
    app = None
    kopya = os.path.join(klasor, "kaynak" + os.path.splitext(yol)[1])
    pdf = os.path.join(klasor, "kaydet.pdf")
    try:
        shutil.copy2(yol, kopya)
        app = win32com.client.DispatchEx("Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0
        d = app.Documents.Open(os.path.abspath(kopya), ReadOnly=True)
        d.SaveAs2(os.path.abspath(pdf), 17)
        d.Close(False)
        return pdf if os.path.isfile(pdf) and os.path.getsize(pdf) > 0 else None
    except Exception:  # noqa: BLE001
        logger.exception("Word 'PDF olarak kaydet' başarısız: %s", yol)
        return None
    finally:
        if app is not None:
            try:
                app.Quit()
            except Exception:  # noqa: BLE001
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


def uret(yol, hedef_klasor=None):
    """PDF üretir ve önbelleğe yazar; üretilemezse None. hedef_klasor verilirse oraya yazar (toplu üretim)."""
    ext = uzanti(yol)
    if ext not in OFFICE_UZANTILARI:
        return None
    o = ozet(yol)
    if _BASARISIZ.get(o, 0) >= 2:
        return None
    ana, _ = _klasorler()
    hedef = os.path.join(hedef_klasor or ana, o[:2], o + ".pdf")
    if os.path.isfile(hedef):
        return hedef
    os.makedirs(os.path.dirname(hedef), exist_ok=True)
    gecici = tempfile.mkdtemp(prefix="dys_onz_")
    try:
        with _KILIT:
            if ext in ("ppt", "pptx"):
                kopya = os.path.join(gecici, f"src.{ext}"); shutil.copy2(yol, kopya)
                pdf = os.path.join(gecici, "out.pdf")
                pdf = pdf if _ppt_to_pdf(kopya, pdf) else None
            else:
                from watermark import _office_to_pdf
                pdf = _office_to_pdf(yol, gecici, {"rtf": "doc", "odt": "doc", "ods": "xls"}.get(ext, ext))
                if not pdf and ext in ("doc", "docx", "rtf", "odt"):
                    pdf = _word_farkli_kaydet(yol, gecici)   # dışa aktarmada çöken belgeler için yedek yöntem
            # Office (COM) yoksa / üretemediyse LibreOffice headless ile dene (sunucuda Office gerekmez)
            if not pdf or not os.path.isfile(pdf) or os.path.getsize(pdf) == 0:
                pdf = _soffice_to_pdf(yol, gecici)
        if not pdf or not os.path.isfile(pdf) or os.path.getsize(pdf) == 0:
            _BASARISIZ[o] = _BASARISIZ.get(o, 0) + 1
            return None
        ara = hedef + f".{uuid.uuid4().hex}.tmp"
        shutil.move(pdf, ara)
        os.replace(ara, hedef)
        return hedef
    except Exception:  # noqa: BLE001
        logger.exception("Önizleme PDF üretilemedi: %s", yol)
        _BASARISIZ[o] = _BASARISIZ.get(o, 0) + 1
        return None
    finally:
        shutil.rmtree(gecici, ignore_errors=True)


def donusturucu_var():
    """Sunucuda PDF dönüştürmesi denenebilir mi: Office COM (pywin32) ya da LibreOffice (soffice)."""
    if _soffice_yolu():
        return True
    try:
        import win32com.client  # noqa: F401
        return True
    except ImportError:
        return False


def pdf_onizleme(yol, uretilsin=True):
    """Önizleme için PDF yolu: önbellek → (izinliyse) anında üretim → None."""
    if not yol or not os.path.isfile(yol) or uzanti(yol) not in OFFICE_UZANTILARI:
        return None
    p = onbellekte(yol)
    if p or not uretilsin or not donusturucu_var():
        return p
    return uret(yol)
