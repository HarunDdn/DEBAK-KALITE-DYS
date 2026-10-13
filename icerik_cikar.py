"""
DYS — Doküman dosyasından OKUNUR İÇERİK çıkarma (2026): Word (.docx / .doc), Excel (.xlsx / .xlsm / .xls) ve PDF dosyalarının içeriğini HTML olarak üretir;
doküman sayfasındaki "İçerik" sekmesinde gösterilir, böylece dosya (paylaşım) erişilemese bile talimat / form / politika / görev tanımı DYS içinde okunur.
HTML'i bu modül kendisi üretir; dosyadaki hiçbir metin ham HTML olarak aktarılmaz (hepsi kaçışlanır) → ek temizleyiciye gerek yok.
  cikar(yol)               → (html | None, not)      not: kesildi / görsel sayısı vb. bilgi
  doldur(db, doc, ...)     → Document.govde_html alanını yazar (yükleme / revizyon sonrası güvenle çağrılabilir; hata verirse sessizce geçer)
Sınırlar: doküman başına ≤ 900 KB HTML, görsel ≤ 150 KB (toplam ≤ 450 KB), Excel sayfa başına ≤ 250 satır × 24 sütun, ≤ 8 sayfa, ≤ 40 MB dosya (okuma süresi).
.doc / .xls dosyaları Word / Excel (COM) ya da xlrd ile okunur; Office yoksa .doc atlanır (not alanına yazılır).
"""
import base64
import html
import io
import logging
import os
import re
import tempfile
from datetime import date, datetime

log = logging.getLogger("dys")
MAX_HTML = 900_000
MAX_GORSEL = 150_000
MAX_GORSEL_TOPLAM = 450_000
MAX_SATIR, MAX_SUTUN, MAX_SAYFA = 250, 24, 8
MAX_DOSYA_MB = 60


def _e(s):
    return html.escape(str(s), quote=True)


# ─────────────────────────── Word ───────────────────────────
def _docx_html(dosya):
    from docx import Document as _D
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    d = _D(dosya)
    toplam_gorsel = [0, 0]                                         # bayt, adet
    notlar = []

    def gorsel(run):
        try:
            for blip in run._r.xpath(".//a:blip"):
                rid = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
                if not rid:
                    continue
                part = d.part.related_parts.get(rid)
                if part is None or not (part.content_type or "").startswith("image/"):
                    continue
                if part.content_type in ("image/x-emf", "image/x-wmf", "image/emf", "image/wmf"):
                    notlar.append("EMF/WMF görsel")
                    continue
                b = part.blob
                if len(b) > MAX_GORSEL or toplam_gorsel[0] + len(b) > MAX_GORSEL_TOPLAM:
                    notlar.append("büyük görsel atlandı")
                    continue
                toplam_gorsel[0] += len(b); toplam_gorsel[1] += 1
                return f'<img src="data:{part.content_type};base64,{base64.b64encode(b).decode()}" alt="" style="max-width:100%;height:auto;">'
        except Exception:  # noqa: BLE001
            pass
        return ""

    def para(p, etiket=None):
        parcalar = []
        for r in p.runs:
            g = gorsel(r)
            t = r.text
            if t:
                x = _e(t).replace("\n", "<br>").replace("\t", "&emsp;")
                if r.bold:
                    x = f"<b>{x}</b>"
                if r.italic:
                    x = f"<i>{x}</i>"
                if r.underline:
                    x = f"<u>{x}</u>"
                parcalar.append(x)
            if g:
                parcalar.append(g)
        ic = "".join(parcalar).strip()
        if not ic:
            return ""
        stil = (p.style.name or "") if p.style is not None else ""
        if etiket:
            return f"<{etiket}>{ic}</{etiket}>"
        m = re.match(r"(?:Heading|Başlık)\s*(\d)", stil, re.I)
        if m:
            n = min(max(int(m.group(1)), 1), 4) + 1
            return f"<h{n}>{ic}</h{n}>"
        if stil.lower() in ("title", "başlık", "konu başlığı"):
            return f"<h2>{ic}</h2>"
        if "list" in stil.lower() or "liste" in stil.lower() or p._p.pPr is not None and p._p.pPr.numPr is not None:
            return f"<p class='lst'>• {ic}</p>"
        return f"<p>{ic}</p>"

    def tablo(t):
        satirlar = []
        for tr in t.rows[:400]:
            hucre, onceki = [], None
            for tc in tr.cells:
                if onceki is not None and tc._tc is onceki:         # birleşik hücre tekrarı
                    continue
                onceki = tc._tc
                ic = "".join(para(p, "div") for p in tc.paragraphs) or "&nbsp;"
                for it in tc.tables:
                    ic += tablo(it)
                hucre.append(f"<td>{ic}</td>")
            satirlar.append("<tr>" + "".join(hucre) + "</tr>")
        return "<table class='ic-t'>" + "".join(satirlar) + "</table>"

    out = []
    govde = d.element.body
    for ch in govde.iterchildren():
        tag = ch.tag.rsplit("}", 1)[-1]
        if tag == "p":
            h = para(Paragraph(ch, d))
            if h:
                out.append(h)
        elif tag == "tbl":
            out.append(tablo(Table(ch, d)))
        if sum(len(x) for x in out) > MAX_HTML:
            notlar.append("içerik uzun olduğu için kesildi")
            break
    # üst bilgi (başlık tablosu) — ilk bölümün header'ı
    try:
        ust = []
        for s in d.sections[:1]:
            for t in s.header.tables:
                ust.append(tablo(t))
            for p in s.header.paragraphs:
                h = para(p, "div")
                if h:
                    ust.append(h)
        if ust:
            out.insert(0, "<div class='ic-ust'>" + "".join(ust) + "</div>")
    except Exception:  # noqa: BLE001
        pass
    return "".join(out), notlar


# ─────────────────────────── Excel ───────────────────────────
def _sayi(v):
    if isinstance(v, bool):
        return "Evet" if v else "Hayır"
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e12:
            return str(int(v))
        return ("%.6g" % v) if abs(v) < 1e6 else str(round(v, 4))
    if isinstance(v, datetime):
        return v.strftime("%d.%m.%Y %H:%M") if (v.hour or v.minute) else v.strftime("%d.%m.%Y")
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    return str(v)


def _tablo_html(satirlar, birlesik=None):
    """satirlar: [[değer,...],...] (boş satır / sütunlar atılmış). Hücre değerleri kaçışlanır."""
    if not satirlar:
        return ""
    h = ["<table class='ic-t ic-x'>"]
    for i, s in enumerate(satirlar):
        h.append("<tr>" + "".join(f"<td>{_e(v).replace(chr(10), '<br>') if v not in (None, '') else '&nbsp;'}</td>" for v in s) + "</tr>")
    h.append("</table>")
    return "".join(h)


def _kirp(matris):
    """Boş satırları / baştaki-sondaki boş sütunları at, sınırlara kes. → (satırlar, kesildi mi)"""
    kesildi = False
    m = [list(r) for r in matris if any(c not in (None, "") for c in r)]
    if not m:
        return [], False
    if len(m) > MAX_SATIR:
        m = m[:MAX_SATIR]; kesildi = True
    ncol = max(len(r) for r in m)
    for r in m:
        r.extend([None] * (ncol - len(r)))
    kullan = [j for j in range(ncol) if any(r[j] not in (None, "") for r in m)]
    if len(kullan) > MAX_SUTUN:
        kullan = kullan[:MAX_SUTUN]; kesildi = True
    return [[(_sayi(r[j]) if r[j] not in (None, "") else "") for j in kullan] for r in m], kesildi


def _xlsx_html(dosya):
    from openpyxl import load_workbook
    wb = load_workbook(dosya, data_only=True, read_only=True)
    out, notlar, n = [], [], 0
    for ws in wb.worksheets:
        if getattr(ws, "sheet_state", "visible") != "visible":
            continue
        if n >= MAX_SAYFA:
            notlar.append("yalnız ilk %d sayfa gösterildi" % MAX_SAYFA); break
        matris = []
        for i, row in enumerate(ws.iter_rows(min_row=1, max_row=MAX_SATIR * 3, values_only=True)):
            matris.append(row[:MAX_SUTUN * 3])
        satir, kes = _kirp(matris)
        if not satir:
            continue
        n += 1
        out.append(f"<h3 class='ic-sh'>{_e(ws.title)}</h3>" + _tablo_html(satir) + ("<p class='ic-not'>… devamı dosyada</p>" if kes else ""))
        if sum(len(x) for x in out) > MAX_HTML:
            notlar.append("içerik uzun olduğu için kesildi"); break
    wb.close()
    return "".join(out), notlar


def _xls_html(dosya):
    import xlrd
    wb = xlrd.open_workbook(dosya, on_demand=True)
    out, notlar, n = [], [], 0
    for i in range(wb.nsheets):
        sh = wb.sheet_by_index(i)
        if sh.visibility != 0:
            wb.unload_sheet(i); continue
        if n >= MAX_SAYFA:
            notlar.append("yalnız ilk %d sayfa gösterildi" % MAX_SAYFA); break
        matris = []
        for r in range(min(sh.nrows, MAX_SATIR * 3)):
            satir = []
            for c in range(min(sh.ncols, MAX_SUTUN * 3)):
                t, v = sh.cell_type(r, c), sh.cell_value(r, c)
                if t == xlrd.XL_CELL_DATE:
                    try:
                        v = xlrd.xldate.xldate_as_datetime(v, wb.datemode)
                    except Exception:  # noqa: BLE001
                        pass
                elif t == xlrd.XL_CELL_ERROR or t == xlrd.XL_CELL_EMPTY:
                    v = None
                elif t == xlrd.XL_CELL_BOOLEAN:
                    v = bool(v)
                satir.append(v)
            matris.append(satir)
        satir, kes = _kirp(matris)
        wb.unload_sheet(i)
        if not satir:
            continue
        n += 1
        out.append(f"<h3 class='ic-sh'>{_e(sh.name)}</h3>" + _tablo_html(satir) + ("<p class='ic-not'>… devamı dosyada</p>" if kes else ""))
        if sum(len(x) for x in out) > MAX_HTML:
            notlar.append("içerik uzun olduğu için kesildi"); break
    wb.release_resources()
    return "".join(out), notlar


# ─────────────────────────── PDF ───────────────────────────
def _pdf_html(dosya):
    import fitz
    d = fitz.open(dosya)
    out, notlar = [], []
    for i, pg in enumerate(d):
        if i >= 40:
            notlar.append("yalnız ilk 40 sayfa"); break
        t = pg.get_text("text").strip()
        if t:
            out.append(f"<h3 class='ic-sh'>Sayfa {i + 1}</h3>" + "".join(f"<p>{_e(x)}</p>" for x in re.split(r"\n{2,}", t) if x.strip()))
        if sum(len(x) for x in out) > MAX_HTML:
            notlar.append("içerik uzun olduğu için kesildi"); break
    d.close()
    return "".join(out), notlar


# ─────────────────────────── .doc → .docx (Word COM) ───────────────────────────
def _doc_to_docx(yol):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    w = win32com.client.DispatchEx("Word.Application")
    w.Visible = False
    w.DisplayAlerts = 0
    try:
        hedef = os.path.join(tempfile.mkdtemp(prefix="dysdoc_"), "k.docx")
        doc = w.Documents.Open(os.path.abspath(yol), ReadOnly=True, AddToRecentFiles=False, ConfirmConversions=False)
        doc.SaveAs2(hedef, 16)                                      # wdFormatXMLDocument
        doc.Close(False)
        return hedef
    finally:
        w.Quit()


def _xls_to_xlsx(yol):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    x = win32com.client.DispatchEx("Excel.Application")
    x.Visible = False
    x.DisplayAlerts = False
    try:
        hedef = os.path.join(tempfile.mkdtemp(prefix="dysxls_"), "k.xlsx")
        wb = x.Workbooks.Open(os.path.abspath(yol), ReadOnly=True, UpdateLinks=0)
        wb.SaveAs(hedef, 51)
        wb.Close(False)
        return hedef
    finally:
        x.Quit()


def cikar(yol):
    """(html | None, not). html boşsa içerik okunamadı (None) — not nedeni söyler."""
    if not yol or not os.path.isfile(yol):
        return None, "dosya bulunamadı"
    if os.path.getsize(yol) > MAX_DOSYA_MB * 1024 * 1024:
        return None, f"dosya çok büyük (> {MAX_DOSYA_MB} MB)"
    uz = os.path.splitext(yol)[1].lower()
    try:
        if uz == ".docx":
            h, n = _docx_html(yol)
        elif uz == ".doc":
            gecici = _doc_to_docx(yol)
            try:
                h, n = _docx_html(gecici)
            finally:
                try:
                    os.remove(gecici)
                except OSError:
                    pass
        elif uz in (".xlsx", ".xlsm"):
            h, n = _xlsx_html(yol)
        elif uz == ".xls":
            try:
                h, n = _xls_html(yol)
            except Exception as ex1:  # noqa: BLE001 — xlrd okuyamazsa Excel ile çevirip oku
                log.info("xls xlrd başarısız (%s); Excel ile çevriliyor", ex1)
                gecici = _xls_to_xlsx(yol)
                try:
                    h, n = _xlsx_html(gecici)
                finally:
                    try:
                        os.remove(gecici)
                    except OSError:
                        pass
        elif uz == ".pdf":
            h, n = _pdf_html(yol)
        else:
            return None, f"desteklenmeyen tür ({uz})"
    except Exception as ex:  # noqa: BLE001
        log.warning("icerik_cikar %s: %s", yol, ex)
        return None, "okunamadı: " + str(ex)[:120]
    if not h.strip():
        return None, "içerik boş"
    return h, "; ".join(sorted(set(n)))


def _damga_temizle(h, kod):
    """Eski sistemden kalan kod / rev / tarih damgalarını siler (scripts/eski_damga_temizle.py kuralları). Hata verirse içeriği değiştirmeden döndürür."""
    try:
        import importlib.util
        yol = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "eski_damga_temizle.py")
        spec = importlib.util.spec_from_file_location("eski_damga_temizle", yol)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        eski, _ = m.yukle_eski()
        return m.temizle(h, eski, m.norm(kod or ""))[0]
    except Exception as ex:  # noqa: BLE001
        log.warning("damga temizleme atlandı: %s", ex)
        return h


def _atif_guncelle(db, h, kod):
    """Cümle içi eski doküman kodu atıflarını yeni koda çevirir (scripts/atif_guncelle.py kuralları; çakışmalarda bağlam). Hata verirse içeriği değiştirmez."""
    try:
        import importlib.util
        yol = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "atif_guncelle.py")
        spec = importlib.util.spec_from_file_location("atif_guncelle", yol)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        if (kod or "").upper() in m.HARIC:
            return h
        ey, bel = m.harita(m.CSV)
        from models import Document
        baslik = {m.norm(n): b for n, b in db.query(Document.dokuman_no, Document.baslik).all() if n}
        ist = {"cevrilen": __import__("collections").Counter(), "belirsiz": __import__("collections").Counter(), "baglam": 0, "guncel": 0}
        return m.cevir(h, ey, bel, m.norm(kod or ""), ist, baslik)
    except Exception as ex:  # noqa: BLE001
        log.warning("atıf güncelleme atlandı: %s", ex)
        return h


def doldur(db, doc, yol=None):
    """Doküman için govde_html alanını yazar. Hata vermez; True/False döner. (commit çağıranın işi)"""
    try:
        h, n = cikar(yol or doc.dosya_yolu)
        if h:
            h = _damga_temizle(h, doc.dokuman_no)
            h = _atif_guncelle(db, h, doc.dokuman_no)
            doc.govde_html = h
            doc.govde_not = (n or None) and n[:300]
            return True
        doc.govde_not = n[:300] if n else None
    except Exception as ex:  # noqa: BLE001
        log.warning("icerik_cikar.doldur: %s", ex)
    return False
