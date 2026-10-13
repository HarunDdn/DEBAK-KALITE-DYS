"""
DYS — Kontrollü / kontrolsüz kopya filigranı
============================================
ISO 9001 / IATF 16949 doküman kontrolü:

- Paylaşımdaki **orijinal** dosya değişmez.
- İndirme, önizleme ve çıktı kopyalarına filigran + tanıtım şeridi uygulanır.
- Onaylı → KONTROLLÜ KOPYA
- Taslak / incelemede → KONTROLSÜZ KOPYA
- İptal / eskimiş → GEÇERSİZ
- Gizli → ek "GİZLİ" işareti
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
import xml.sax.saxutils as xml_escape
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("dys.watermark")

SUPPORTED_EXTS = {"pdf", "docx", "xlsx"}

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\arialuni.ttf",
    r"C:\Windows\Fonts\ARIALUNI.TTF",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
    r"C:\Windows\Fonts\tahoma.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)

_FITZ_FONT = None


@dataclass
class WatermarkSpec:
    """Bir dağıtım kopyasının görünür işaretleri."""

    label: str  # KONTROLLÜ KOPYA / KONTROLSÜZ KOPYA / GEÇERSİZ
    kind: str  # controlled | uncontrolled | void
    extras: list[str] = field(default_factory=list)
    footer_left: str = ""
    footer_center: str = ""
    footer_right: str = ""
    header: str = "DYS kontrollü kopya"
    color_rgb: tuple[float, float, float] = (0.72, 0.11, 0.11)
    opacity: float = 0.16

    @property
    def diagonal_text(self) -> str:
        parts = [self.label] + list(self.extras)
        return "  ·  ".join(p for p in parts if p)

    @property
    def short_text(self) -> str:
        return self.label


def classify_copy_kind(durum: str | None, guvenlik_sinifi: str | None = None) -> tuple[str, str, list[str]]:
    """(label, kind, extras) — doküman durumuna göre kopya sınıfı."""
    extras: list[str] = []
    d = _fold_tr(durum)
    g = _fold_tr(guvenlik_sinifi)
    if g in ("gizli", "cok gizli", "çok gizli"):
        extras.append("GİZLİ — KOPYALANAMAZ")
    if d in ("iptal", "eskimis"):
        return "GEÇERSİZ", "void", extras
    if d in ("onayli",):
        return "KONTROLLÜ KOPYA", "controlled", extras
    return "KONTROLSÜZ KOPYA", "uncontrolled", extras


def _fold_tr(s: str | None) -> str:
    t = (s or "").strip()
    for a, b in (
        ("ı", "i"), ("İ", "i"), ("I", "i"),
        ("ş", "s"), ("Ş", "s"),
        ("ğ", "g"), ("Ğ", "g"),
        ("ü", "u"), ("Ü", "u"),
        ("ö", "o"), ("Ö", "o"),
        ("ç", "c"), ("Ç", "c"),
    ):
        t = t.replace(a, b)
    return t.casefold()


def build_spec_for_document(doc, *, user_name: str = "", purpose: str = "indirme") -> WatermarkSpec:
    """Document modelinden filigran spesifikasyonu üretir."""
    label, kind, extras = classify_copy_kind(
        getattr(doc, "durum", None),
        getattr(doc, "guvenlik_sinifi", None),
    )
    no = getattr(doc, "dokuman_no", "") or ""
    rev = getattr(doc, "revizyon_no", 0)
    when = datetime.now().strftime("%d.%m.%Y %H:%M")
    who = (user_name or "DYS").strip() or "DYS"

    footer_left = f"{no}  Rev.{rev}".replace("\u2010", "-").replace("\u2011", "-").replace("\u2013", "-")
    footer_center = label
    footer_right = f"{who}  {when}".replace("\u2010", "-")

    color = (0.72, 0.11, 0.11)
    opacity = 0.16
    if kind == "void":
        color = (0.35, 0.35, 0.35)
        opacity = 0.22
    elif kind == "uncontrolled":
        color = (0.55, 0.25, 0.05)
        opacity = 0.18

    return WatermarkSpec(
        label=label,
        kind=kind,
        extras=extras,
        footer_left=footer_left,
        footer_center=footer_center,
        footer_right=footer_right,
        color_rgb=color,
        opacity=opacity,
    )


def issued_copy_filename(doc, ext: str | None = None) -> str:
    """Dağıtım kopyası dosya adı (orijinal adı ezmez)."""
    from werkzeug.utils import secure_filename

    src = getattr(doc, "dosya_adi", "") or "dokuman"
    base, src_ext = os.path.splitext(src)
    use_ext = (ext or src_ext or "").lstrip(".")
    no = secure_filename(getattr(doc, "dokuman_no", "") or base) or "dokuman"
    rev = getattr(doc, "revizyon_no", 0)
    label, kind, _ = classify_copy_kind(getattr(doc, "durum", None), getattr(doc, "guvenlik_sinifi", None))
    tag = {
        "controlled": "KONTROLLU_KOPYA",
        "uncontrolled": "KONTROLSUZ_KOPYA",
        "void": "GECERSIZ",
    }.get(kind, "KOPYA")
    return f"{no}_Rev{rev}_{tag}.{use_ext}" if use_ext else f"{no}_Rev{rev}_{tag}"


def _tr_fontfile() -> str | None:
    for p in _FONT_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _fitz_tr_font():
    """Unicode TrueType (ğüşıöç). Helvetica kullanılmaz."""
    global _FITZ_FONT
    if _FITZ_FONT is not None:
        return _FITZ_FONT
    import fitz

    path = _tr_fontfile()
    if path:
        _FITZ_FONT = fitz.Font(fontfile=path)
        return _FITZ_FONT
    try:
        _FITZ_FONT = fitz.Font("cjk")
    except Exception:
        _FITZ_FONT = fitz.Font("helv")
    return _FITZ_FONT


def _ensure_outdir(output_dir: str) -> str:
    output_dir = output_dir or os.path.abspath("temp")
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


def apply_issued_copy(file_path: str, spec: WatermarkSpec, output_dir: str, ext: str | None = None) -> str | None:
    """Orijinali kopyalayıp filigran uygular. Desteklenmiyorsa None."""
    if not file_path or not os.path.isfile(file_path):
        return None
    output_dir = _ensure_outdir(output_dir)
    ext = (ext or os.path.splitext(file_path)[1] or "").lower().lstrip(".")
    filename = os.path.basename(file_path)
    output_path = os.path.join(output_dir, f"wm_{uuid.uuid4().hex}_{filename}")
    try:
        if ext == "pdf":
            return _watermark_pdf(file_path, spec, output_path)
        if ext == "docx":
            return _watermark_docx(file_path, spec, output_path)
        if ext == "xlsx":
            return _watermark_xlsx(file_path, spec, output_path)
        return None
    except Exception:
        logger.exception("Filigran uygulanamadı: %s", file_path)
        try:
            if os.path.isfile(output_path):
                os.remove(output_path)
        except OSError:
            pass
        return None


def render_print_pdf(file_path: str, spec: WatermarkSpec, output_dir: str, *, title: str = "", source_name: str = "") -> str | None:
    """Çıktı düğmesi: her zaman filigranlı PDF üretir (xls/doc dahil)."""
    if not file_path or not os.path.isfile(file_path):
        return _stamp_pdf(spec, output_dir, title=title, source_name=source_name)
    output_dir = _ensure_outdir(output_dir)
    ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    pdf_out = os.path.join(output_dir, f"print_{uuid.uuid4().hex}.pdf")

    if ext == "pdf":
        try:
            return _watermark_pdf(file_path, spec, pdf_out)
        except Exception:
            logger.exception("PDF filigranı başarısız, kapak üretiliyor")
            return _stamp_pdf(spec, output_dir, title=title, source_name=source_name)

    # Office dosyası: önce önizlemenin önbelleğindeki PDF'i kullan (bir kez üretilir, tekrar kullanılır;
    # önbellek dosyası SİLİNMEZ). Yoksa anlık dönüştür (üretilen geçici PDF silinir).
    onbellekten = False
    converted = None
    try:
        import onizleme_pdf
        if onizleme_pdf.uzanti(file_path) in onizleme_pdf.OFFICE_UZANTILARI:
            cache = onizleme_pdf.pdf_onizleme(file_path)
            if cache and os.path.isfile(cache):
                converted, onbellekten = cache, True
    except Exception:  # noqa: BLE001
        logger.exception("Önizleme önbelleğinden PDF alınamadı: %s", file_path)
    if not converted:
        converted = _office_to_pdf(file_path, output_dir, ext)
    if converted:
        try:
            stamped = _watermark_pdf(converted, spec, pdf_out)
            return stamped
        except Exception:
            logger.exception("Dönüştürülen PDF filigranlanamadı")
        finally:
            if not onbellekten:   # önbellek dosyasına dokunma
                try:
                    os.remove(converted)
                except OSError:
                    pass

    native = apply_issued_copy(file_path, spec, output_dir, ext=ext)
    if native and native.lower().endswith(".pdf"):
        return native
    if native:
        try:
            os.remove(native)
        except OSError:
            pass
    return _stamp_pdf(spec, output_dir, title=title, source_name=source_name)


def _office_to_pdf(file_path: str, output_dir: str, ext: str) -> str | None:
    """Office → PDF: önce Office COM (Windows+Office), olmazsa LibreOffice headless. Yoksa None."""
    ext = (ext or "").lower().lstrip(".")
    if ext not in {"xls", "xlsx", "xlsm", "doc", "docx", "rtf", "odt", "ods", "ppt", "pptx"}:
        return None
    local = os.path.join(output_dir, f"src_{uuid.uuid4().hex}.{ext}")
    pdf_path = os.path.join(output_dir, f"conv_{uuid.uuid4().hex}.pdf")
    try:
        shutil.copy2(file_path, local)
    except OSError:
        logger.exception("Çıktı için yerel kopya alınamadı")
        return None
    try:
        if ext in {"xls", "xlsx", "xlsm"}:
            ok = _excel_to_pdf(local, pdf_path)
        elif ext in {"doc", "docx"}:
            ok = _word_to_pdf(local, pdf_path)
        else:
            ok = False
        if ok and os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 0:
            return pdf_path
        # Office (COM) yoksa / üretemediyse LibreOffice headless (sunucuda Office gerekmez)
        try:
            import onizleme_pdf
            lo = onizleme_pdf._soffice_to_pdf(file_path, output_dir)
            if lo and os.path.isfile(lo) and os.path.getsize(lo) > 0:
                return lo
        except Exception:  # noqa: BLE001
            logger.exception("LibreOffice ile çıktı PDF üretilemedi: %s", file_path)
        return None
    finally:
        try:
            os.remove(local)
        except OSError:
            pass


def _excel_to_pdf(src: str, pdf_path: str) -> bool:
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return False
    pythoncom.CoInitialize()
    xl = None
    try:
        xl = win32com.client.DispatchEx("Excel.Application")
        xl.Visible = False
        xl.DisplayAlerts = False
        wb = xl.Workbooks.Open(os.path.abspath(src), ReadOnly=True, UpdateLinks=0, IgnoreReadOnlyRecommended=True)
        wb.ExportAsFixedFormat(0, os.path.abspath(pdf_path))
        wb.Close(False)
        return True
    except Exception:
        logger.exception("Excel PDF dönüşümü başarısız")
        return False
    finally:
        if xl is not None:
            try:
                xl.Quit()
            except Exception:
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _word_to_pdf(src: str, pdf_path: str) -> bool:
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return False
    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(os.path.abspath(src), ReadOnly=True)
        doc.ExportAsFixedFormat(os.path.abspath(pdf_path), 17)  # wdExportFormatPDF
        doc.Close(False)
        return True
    except Exception:
        logger.exception("Word PDF dönüşümü başarısız")
        return False
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _stamp_pdf(spec: WatermarkSpec, output_dir: str, *, title: str = "", source_name: str = "") -> str:
    """Dosya dönüştürülemezse kontrollü kopya kapak PDF'i."""
    import fitz

    output_dir = _ensure_outdir(output_dir)
    output_path = os.path.join(output_dir, f"stamp_{uuid.uuid4().hex}.pdf")
    font = _fitz_tr_font()
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    r, g, b = spec.color_rgb
    tw = fitz.TextWriter(page.rect, color=(r, g, b))
    title_w = font.text_length(spec.label, fontsize=22)
    tw.append(fitz.Point((595 - title_w) / 2, 120), spec.label, font=font, fontsize=22)
    tw.write_text(page)

    body = (
        f"{spec.footer_left}\n"
        f"{title or ''}\n"
        f"Kaynak dosya: {source_name or '-'}\n"
        f"{spec.header}\n"
        f"{spec.footer_right}\n\n"
        "Bu sayfa DYS kontrollü kopya kaydıdır."
    )
    tw2 = fitz.TextWriter(page.rect, color=(0.15, 0.15, 0.15))
    y = 180
    for line in body.split("\n"):
        tw2.append(fitz.Point(60, y), line, font=font, fontsize=11)
        y += 18
    tw2.write_text(page)
    _draw_copy_marks(page, spec)
    doc.save(output_path)
    doc.close()
    return output_path


def add_watermark(file_path, text, ext, output_dir):
    """Eski API — yalnızca etiket metni ile filigran (geriye dönük)."""
    spec = WatermarkSpec(
        label=text or "KONTROLLÜ KOPYA",
        kind="controlled",
        footer_center=text or "",
        footer_left="",
        footer_right=datetime.now().strftime("%d.%m.%Y %H:%M"),
    )
    return apply_issued_copy(file_path, spec, output_dir, ext=ext)


def _draw_copy_marks(page, spec: WatermarkSpec) -> None:
    """İnce üst/alt şerit + çapraz filigran (Unicode yazı tipi)."""
    import fitz

    font = _fitz_tr_font()
    r, g, b = spec.color_rgb
    rect = page.rect
    margin = 10
    fs = 5.0

    # Çapraz etiket — küçük, sayfa içeriğini ezmesin
    origin = fitz.Point(rect.width / 2, rect.height / 2)
    rot = fitz.Matrix(1, 1).prerotate(45)
    tw_wm = fitz.TextWriter(page.rect, color=(r, g, b))
    label = spec.diagonal_text
    wm_size = max(14, min(rect.width, rect.height) / 22)
    tw_wm.append(origin, label, font=font, fontsize=wm_size)
    tw_wm.write_text(page, morph=(origin, rot), overlay=True, opacity=max(0.10, spec.opacity * 0.7))

    # İnce alt çizgi + 3 sütun (orijinal dipnotun üstünü kapatmasın)
    y = rect.height - 6.5
    page.draw_line(
        fitz.Point(margin, rect.height - 9.5),
        fitz.Point(rect.width - margin, rect.height - 9.5),
        color=(r, g, b),
        width=0.2,
        overlay=True,
    )
    tw = fitz.TextWriter(page.rect, color=(0.25, 0.12, 0.12))
    left = spec.footer_left or ""
    center = spec.footer_center or spec.label
    right = spec.footer_right or ""
    tw.append(fitz.Point(margin, y), left, font=font, fontsize=fs)
    cw = font.text_length(center, fontsize=fs)
    tw.append(fitz.Point((rect.width - cw) / 2, y), center, font=font, fontsize=fs)
    rw = font.text_length(right, fontsize=fs)
    tw.append(fitz.Point(rect.width - margin - rw, y), right, font=font, fontsize=fs)
    tw.write_text(page, overlay=True)

    if spec.header:
        tw_h = fitz.TextWriter(page.rect, color=(0.45, 0.22, 0.22))
        hw = font.text_length(spec.header, fontsize=fs)
        tw_h.append(fitz.Point((rect.width - hw) / 2, 8), spec.header, font=font, fontsize=fs)
        tw_h.write_text(page, overlay=True)


# ── PDF ─────────────────────────────────────────────────────────────────────
def _watermark_pdf(file_path: str, spec: WatermarkSpec, output_path: str) -> str:
    import fitz

    doc = fitz.open(file_path)
    try:
        for page in doc:
            _draw_copy_marks(page, spec)
        doc.save(output_path, deflate=True, garbage=3)
    finally:
        doc.close()
    return output_path


# ── DOCX ────────────────────────────────────────────────────────────────────
def _watermark_docx(file_path: str, spec: WatermarkSpec, output_path: str) -> str:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import parse_xml
    from docx.shared import Pt, RGBColor

    document = docx.Document(file_path)
    label = xml_escape.escape(spec.diagonal_text)
    r, g, b = spec.color_rgb
    fill = f"{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"

    watermark_xml = f'''
    <w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
         xmlns:v="urn:schemas-microsoft-com:vml"
         xmlns:o="urn:schemas-microsoft-com:office:office">
      <w:pPr><w:pStyle w:val="Header"/></w:pPr>
      <w:r>
        <w:rPr><w:noProof/></w:rPr>
        <w:pict>
          <v:shapetype id="_x0000_t136" coordsize="21600,21600" o:spt="136"
            adj="10800" path="m@7,l@8,m@5,21600l@6,21600e">
            <v:formulas>
              <v:f eqn="sum #0 0 10800"/>
              <v:f eqn="prod #0 2 1"/>
              <v:f eqn="sum 21600 0 @1"/>
              <v:f eqn="sum 0 0 @2"/>
              <v:f eqn="sum 21600 0 @3"/>
              <v:f eqn="if @0 @3 0"/>
              <v:f eqn="if @0 21600 @1"/>
              <v:f eqn="if @0 0 @2"/>
              <v:f eqn="if @0 @4 21600"/>
              <v:f eqn="mid @5 @6"/>
              <v:f eqn="mid @8 @5"/>
              <v:f eqn="mid @7 @8"/>
              <v:f eqn="mid @6 @7"/>
              <v:f eqn="sum @6 0 @5"/>
            </v:formulas>
            <v:path textpathok="t" o:connecttype="custom"
              o:connectlocs="@9,0;@10,10800;@11,21600;@12,10800" o:connectangles="270,180,90,0"/>
            <v:textpath on="t" fitshape="t"/>
            <v:handles>
              <v:h position="#0,bottomRight" polar="10800,10800"/>
            </v:handles>
          </v:shapetype>
          <v:shape id="DYSWaterMark" o:spid="_x0000_s2049" type="#_x0000_t136"
            style="position:absolute;margin-left:0;margin-top:0;width:500pt;height:120pt;rotation:315;z-index:-251658752;mso-position-horizontal:center;mso-position-horizontal-relative:margin;mso-position-vertical:center;mso-position-vertical-relative:margin"
            o:allowincell="f" fillcolor="#{fill}" stroked="f">
            <v:fill opacity=".35"/>
            <v:textpath style="font-family:&quot;Arial&quot;;font-size:1pt" on="t" string="{label}"/>
          </v:shape>
        </w:pict>
      </w:r>
    </w:p>
    '''.strip()

    rgb = RGBColor(int(r * 255), int(g * 255), int(b * 255))
    for section in document.sections:
        header = section.header
        header.is_linked_to_previous = False
        try:
            header._element.append(parse_xml(watermark_xml))
        except Exception:
            logger.exception("DOCX filigran XML eklenemedi")
        hp = header.add_paragraph()
        run = hp.add_run(spec.header)
        run.font.size = Pt(8)
        run.font.color.rgb = rgb
        hp.alignment = WD_ALIGN_PARAGRAPH.CENTER

        footer = section.footer
        footer.is_linked_to_previous = False
        fp = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        fp.clear()
        run = fp.add_run(f"{spec.footer_left}  |  {spec.footer_center}  |  {spec.footer_right}")
        run.font.size = Pt(6)
        run.font.bold = True
        run.font.color.rgb = rgb
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER

    document.save(output_path)
    return output_path


# ── XLSX (yazdırma üst/alt bilgi — Excel çıktısında görünür) ────────────────
def _watermark_xlsx(file_path: str, spec: WatermarkSpec, output_path: str) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(file_path)
    r, g, b = spec.color_rgb
    hex_color = f"{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"

    for ws in wb.worksheets:
        ws.oddHeader.center.text = f'&K{hex_color}&"Arial,Bold"&8{spec.short_text}'
        ws.evenHeader.center.text = ws.oddHeader.center.text
        ws.oddFooter.left.text = f"&6{spec.footer_left}"
        ws.oddFooter.center.text = f"&6{spec.footer_center}"
        ws.oddFooter.right.text = f"&6{spec.footer_right}"
        ws.evenFooter.left.text = f"&6{spec.footer_left}"
        ws.evenFooter.center.text = f"&6{spec.footer_center}"
        ws.evenFooter.right.text = f"&6{spec.footer_right}"
    wb.save(output_path)
    return output_path
