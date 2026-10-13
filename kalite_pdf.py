"""
DYS — Kalite raporu A4 PDF (ReportLab): Tedarikçi Uygunsuzluk Raporu (SCAR) / Müşteri Şikayeti 8D Raporu (2026)
Başlıklar Türkçe + İngilizce; fotoğraflar (rapora eklenecek işaretli görseller) ızgarada; imza alanları; "Sayfa x / y".
Tedarikçiye / müşteriye e-posta ile gönderilmek üzere indirilir.
"""
import io
import os
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, KeepTogether

import exports
import kalite_spec as KS

LACI = colors.HexColor("#1f3864"); ACIK = colors.HexColor("#eef2f8"); CIZGI = colors.HexColor("#9aa7bd"); GRI = colors.HexColor("#5a6474")
LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "branding", "debak-logo.png")
GEN = 180 * mm   # kullanılabilir genişlik (A4 210 − 2 × 15)


def _s():
    exports._register_fonts()
    R, B = exports._FONT_REG, exports._FONT_BOLD
    return {"deger": ParagraphStyle("d", fontName=R, fontSize=8.6, leading=10.6),
            "etiket": ParagraphStyle("e", fontName=B, fontSize=7.4, leading=9, textColor=colors.HexColor("#1f2937")),
            "bolum": ParagraphStyle("b", fontName=B, fontSize=9.4, leading=11.5, textColor=colors.white),
            "baslik": ParagraphStyle("t", fontName=B, fontSize=13, leading=15.5, alignment=1, textColor=LACI),
            "alt": ParagraphStyle("a", fontName=R, fontSize=9.4, leading=11.5, alignment=1, textColor=GRI),
            "kucuk": ParagraphStyle("k", fontName=R, fontSize=7.2, leading=8.8, textColor=GRI),
            "th": ParagraphStyle("th", fontName=B, fontSize=7.2, leading=8.8),
            "td": ParagraphStyle("td", fontName=R, fontSize=7.8, leading=9.6), "R": R, "B": B}


def _p(metin, st):
    return Paragraph(escape("" if metin is None else str(metin)).replace("\n", "<br/>"), st)


def _etiket(tr, en, st):
    return Paragraph(f"{escape(tr)}<br/><font size='6.6' color='#5a6474'><i>{escape(en)}</i></font>", st["etiket"])


def _metin(k, tk, a):
    anahtar, tr, en, tip, sec, _ = a
    v = getattr(k, anahtar, None) if anahtar in KS.KOLON[tk] else k.veri.get(anahtar)
    if tip == "tedarikci":
        return k.tedarikci_ad or "—"
    if tip == "kullanici":
        return k.sorumlu.ad_soyad if (anahtar == "sorumlu_id" and k.sorumlu) else "—"
    if tip == "durum":
        from kalite_routes import DURUM_EN
        return f"{v} / {DURUM_EN.get(v, v)}" if v else "—"
    if tip == "coklu":
        return ", ".join(f"{x} / {KS.secenek_en(tk, anahtar, x) or x}" if KS.secenek_en(tk, anahtar, x) not in (None, x) else x for x in (v or [])) or "—"
    if tip == "secim" and v:
        en_v = KS.secenek_en(tk, anahtar, v)
        return f"{v} / {en_v}" if en_v and en_v != v else v
    if tip == "tarih" and v:
        try:
            return (v if hasattr(v, "strftime") else datetime.strptime(str(v)[:10], "%Y-%m-%d")).strftime("%d.%m.%Y")
        except ValueError:
            return str(v)
    if tip == "sayi" and v is not None:
        try:
            f = float(v); return f"{f:,.0f}".replace(",", ".") if f == int(f) else f"{f:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except (TypeError, ValueError):
            return str(v)
    return "—" if v in (None, "", []) else str(v)


def _bolum_tablosu(k, tk, bolum, no, st):
    tr, en, alanlar = bolum
    parcalar = [Table([[Paragraph(f"{no}. {escape(tr)}  <font size='8'>/ {escape(en)}</font>", st["bolum"])]], colWidths=[GEN],
                      style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), LACI), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))]
    satirlar, stil, yarim = [], [], []
    ek_tablolar = []

    def yarim_bosalt():
        if yarim:
            s = yarim + ([Paragraph("", st["etiket"]), Paragraph("", st["deger"])] if len(yarim) == 2 else [])
            satirlar.append(s); yarim.clear()
    for a in alanlar:
        if a[3] == "tablo":
            ek_tablolar.append(a); continue
        genis = a[5] >= 2 or a[3] in ("uzun", "coklu")
        hucre = [_etiket(a[1], a[2], st), _p(_metin(k, tk, a), st["deger"])]
        if genis:
            yarim_bosalt(); satirlar.append(hucre + ["", ""]); stil.append(("SPAN", (1, len(satirlar) - 1), (3, len(satirlar) - 1)))
        else:
            yarim.extend(hucre)
            if len(yarim) == 4:
                yarim_bosalt()
    yarim_bosalt()
    if satirlar:
        t = Table(satirlar, colWidths=[30 * mm, 60 * mm, 30 * mm, 60 * mm])
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, CIZGI), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("BACKGROUND", (0, 0), (0, -1), ACIK), ("BACKGROUND", (2, 0), (2, -1), ACIK),
                               ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)] + stil))
        for i, s in enumerate(satirlar):   # geniş satırın 3. sütunu açık zemin olmasın
            if s[2] == "":
                t.setStyle(TableStyle([("BACKGROUND", (2, i), (2, i), colors.white)]))
        parcalar.append(t)
    for a in ek_tablolar:
        sut = a[4]; veri = k.veri.get(a[0]) or []
        def _bas(c):
            tr, _, en = c.partition(" / ")
            return Paragraph(escape(tr) + (f"<br/><font size='6.4' color='#5a6474'><i>{escape(en)}</i></font>" if en else ""), st["th"])
        rows = [[_etiket(a[1], a[2], st)] + [""] * (len(sut) - 1), [_bas(c) for c in sut]]
        for r in veri or [{}]:
            rows.append([_p(r.get(c, "") or ("—" if not veri else ""), st["td"]) for c in sut])
        t = Table(rows, colWidths=[GEN / len(sut)] * len(sut), repeatRows=2)
        t.setStyle(TableStyle([("SPAN", (0, 0), (-1, 0)), ("GRID", (0, 1), (-1, -1), 0.4, CIZGI), ("BOX", (0, 0), (-1, -1), 0.4, CIZGI),
                               ("BACKGROUND", (0, 0), (-1, 1), ACIK), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        parcalar.append(t)
    return parcalar


def _goruntu(yol, gen, yuk):
    from PIL import Image as PImage
    with PImage.open(yol) as im:
        im = im.convert("RGB"); im.thumbnail((1400, 1400))
        w, h = im.size; buf = io.BytesIO(); im.save(buf, "JPEG", quality=85); buf.seek(0)
    oran = min(gen / w, yuk / h)
    return Image(buf, width=w * oran, height=h * oran)


def _gorsel_bolumu(gorseller, no, st):
    g = [x for x in gorseller if x.pdfte is not False and os.path.isfile(getattr(x, "_yerel", None) or x.dosya_yolu)]
    if not g:
        return []
    out = [Table([[Paragraph(f"{no}. Görseller  <font size='8'>/ Images</font>", st["bolum"])]], colWidths=[GEN],
                 style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), LACI), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))]
    hucreler = []
    for i, x in enumerate(g, 1):
        renk = "#b91c1c" if (x.etiket or "").startswith("Hatalı") else ("#15803d" if (x.etiket or "").startswith("Referans") else "#1f2937")
        alt = Paragraph(f"<font color='{renk}'><b>{i}. {escape(x.etiket or '')}</b> / <i>{escape(dict(KS.ETIKET).get(x.etiket or '', ''))}</i></font>"
                        + (f"<br/>{escape(x.aciklama)}" if x.aciklama else ""), st["td"])
        try:
            hucreler.append([_goruntu(getattr(x, "_yerel", None) or x.dosya_yolu, 86 * mm, 64 * mm), alt])
        except Exception:  # noqa: BLE001 — okunamayan görsel rapora girmez
            continue
    satir = []
    for j in range(0, len(hucreler), 2):
        ikili = hucreler[j:j + 2]
        satir.append([Table([[h[0]], [h[1]]], colWidths=[88 * mm], style=TableStyle([("ALIGN", (0, 0), (-1, 0), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "TOP")])) for h in ikili]
                     + ([""] if len(ikili) == 1 else []))
    if satir:
        t = Table(satir, colWidths=[GEN / 2, GEN / 2])
        t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, CIZGI), ("INNERGRID", (0, 0), (-1, -1), 0.4, CIZGI), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        out.append(t)
    return out


class _Numarali(rl_canvas.Canvas):
    """Sayfa x / y ve alt bilgi."""
    def __init__(self, *a, **kw):
        self._altbilgi = kw.pop("altbilgi", ""); self._font = kw.pop("font", "Helvetica")
        super().__init__(*a, **kw); self._sayfalar = []

    def showPage(self):
        self._sayfalar.append(dict(self.__dict__)); self._startPage()

    def save(self):
        n = len(self._sayfalar)
        for s in self._sayfalar:
            self.__dict__.update(s)
            self.setFont(self._font, 7); self.setFillColor(GRI)
            self.drawString(15 * mm, 9 * mm, self._altbilgi)
            self.drawRightString(195 * mm, 9 * mm, f"Sayfa / Page {self._pageNumber} / {n}")
            self.setStrokeColor(CIZGI); self.setLineWidth(0.4); self.line(15 * mm, 12 * mm, 195 * mm, 12 * mm)
            super().showPage()
        super().save()


def rapor(k, tk, gorseller, hazirlayan=""):
    st = _s(); spec = KS.SPEC[tk]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=12 * mm, bottomMargin=16 * mm,
                            title=f"{k.no} {spec['baslik'][1]}", author="Debak A.Ş.", subject=spec["baslik"][0])
    logo = Image(LOGO, width=30 * mm, height=12 * mm, kind="proportional") if os.path.isfile(LOGO) else Paragraph("<b>DEBAK</b>", st["baslik"])
    sag = Table([[_etiket("Doküman", "Document", st), _p(spec["kod"], st["deger"])],
                 [_etiket("Rapor No", "Report No", st), Paragraph(f"<b>{escape(k.no)}</b>", st["deger"])],
                 [_etiket("Tarih", "Date", st), _p(k.tarih.strftime("%d.%m.%Y") if k.tarih else "", st["deger"])]],
                colWidths=[20 * mm, 24 * mm], style=TableStyle([("GRID", (0, 0), (-1, -1), 0.4, CIZGI), ("BACKGROUND", (0, 0), (0, -1), ACIK),
                                                                 ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                                                                 ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
    ust = Table([[logo, [Paragraph(escape(spec["baslik"][0]), st["baslik"]), Paragraph(escape(spec["baslik"][1]), st["alt"])], sag]],
                colWidths=[36 * mm, 96 * mm, 48 * mm])
    ust.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (2, 0), (2, 0), 2), ("RIGHTPADDING", (2, 0), (2, 0), 2), ("BOX", (0, 0), (-1, -1), 0.6, LACI), ("LINEAFTER", (0, 0), (1, 0), 0.4, CIZGI)]))
    akis = [ust, Spacer(1, 4 * mm)]
    gorsel_sonra = 2 if tk == "MS" else 1   # görseller problem tanımından sonra (8D: D2 · SCAR: Uygunsuzluk)
    no = 1
    for i, b in enumerate(spec["bolumler"]):
        parca = _bolum_tablosu(k, tk, b, no, st); no += 1
        akis.append(KeepTogether(parca[:2]) if len(parca) > 1 else parca[0]); akis.extend(parca[2:]); akis.append(Spacer(1, 3 * mm))
        if i == gorsel_sonra:
            gb = _gorsel_bolumu(gorseller, no, st)
            if gb:
                no += 1; akis.append(gb[0]); akis.extend(gb[1:]); akis.append(Spacer(1, 3 * mm))
    imza_bas = [("Hazırlayan", "Prepared by"), ("Onaylayan (Kalite)", "Approved by (Quality)")] + ([("Tedarikçi Onayı", "Supplier Acknowledgement")] if tk == "TU" else [("Müşteri Onayı", "Customer Acceptance")])
    imza = Table([[_etiket(a, b, st) for a, b in imza_bas], [_p(hazirlayan, st["deger"]), _p("", st["deger"]), _p("", st["deger"])],
                  [_p("Ad Soyad / İmza / Tarih — Name / Signature / Date", st["kucuk"])] * 3], colWidths=[GEN / 3] * 3, rowHeights=[None, 14 * mm, None])
    imza.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, CIZGI), ("BACKGROUND", (0, 0), (-1, 0), ACIK), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    akis += [KeepTogether([imza])]
    alt = f"Debak A.Ş. · {spec['kod']} · {k.no} · DYS'den oluşturuldu / generated by DMS {datetime.now():%d.%m.%Y %H:%M} · Gizli / Confidential"
    doc.build(akis, canvasmaker=lambda *a, **kw: _Numarali(*a, altbilgi=alt, font=st["R"], **kw))
    buf.seek(0)
    return buf
