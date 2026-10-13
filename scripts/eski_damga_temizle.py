"""Doküman içeriğinde (documents.govde_html) eski sistemden kalan KOD / REV / TARİH DAMGALARINI siler (ör. 'D4.2 T03(2) 10.12.2021', 'D06 F24 Rev1 25.06.2025').
Damga = bloğun (paragraf / hücre / div) TAMAMI yalnızca eski bir doküman kodu + isteğe bağlı rev + tarih olan kısa satır. Cümle içindeki atıflara dokunulmaz (sayılır, raporlanır).
Eski kod = kod_eslestirme.csv'deki eski_kod değerleri ya da eski numaralama biçimi (tek haneli süreç: D2 T45, D3 F6 53, D4.2 T03, Y02.2 T01 …), dokümanın kendi YENİ kodu değil.
KULLANIM: python scripts/eski_damga_temizle.py            → kuru çalışma: ne silinecek, örnekler
          python scripts/eski_damga_temizle.py --uygula   → uygular (önce veritabanı yedeği)
          --csv <yol>                                       → kod_eslestirme.csv yolu (öntanımlı: DYS_GELISTIRME üstündeki YENİ YAPI\\DYS Aktarım)"""
import csv
import html
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import SessionLocal, Document  # noqa: E402

UYGULA = "--uygula" in sys.argv
KENDI_ESKI = {}
KOK = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))             # ...\DYS_GELISTIRME
CSV = sys.argv[sys.argv.index("--csv") + 1] if "--csv" in sys.argv else os.path.join(os.path.dirname(KOK), "YENİ YAPI", "DYS Aktarım", "kod_eslestirme.csv")

TAG = re.compile(r"<[^>]+>")
BLOK = re.compile(r"<(p|div|td)(\b[^>]*)>(.*?)</\1>", re.S)
KOD = re.compile(r"\b([A-ZÇĞİÖŞÜ])\s?(\d{1,2})(\.\d+)?\s?([TFPAKG]{1,2})\s?(\d{1,3})")
ESKI_BICIM = re.compile(r"\b[A-ZÇĞİÖŞÜ]\s?\d(\.\d+)?\s?[TFPAKG]{1,2}\s?\d{1,3}")                 # tek haneli süreç no (D2, D3, D4.2)
ESKI_SUREC = re.compile(r"\b(Y0[23](\.\d)?|D11|D07|D08|D09|D10|M05)\s?[TFPAKG]{1,2}\s?\d{1,3}")  # kaldırılmış süreçler
ARTIK = re.compile(r"(?i)\(\d+\)|\brev\.?\s?\d*|\bdok\.?\s?no\b|[_:,;\-–/()\s]|\d{1,2}[./]\d{1,2}[./]\d{2,4}")


def norm(t):
    t = t.upper().replace("İ", "I")
    m = KOD.search(t)
    if not m:
        return None
    return f"{m.group(1)}{int(m.group(2)):02d}{m.group(3) or ''} {m.group(4)}{int(m.group(5)):02d}"


def yukle_eski():
    eski, yeni = set(), set()
    global KENDI_ESKI
    KENDI_ESKI = {}
    try:
        with open(CSV, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f, delimiter=";"):
                e, y = norm(r.get("eski_kod") or ""), norm(r.get("yeni_kod") or "")
                if e:
                    eski.add(e)
                if y:
                    yeni.add(y)
                if e and y and e != y:
                    KENDI_ESKI.setdefault(y, set()).add(e)
    except OSError:
        print("UYARI: kod_eslestirme.csv bulunamadı:", CSV)
    return eski - yeni, yeni


def damga_mi(metin, eski, kendi, hucre=False):
    t = re.sub(r"\s+", " ", html.unescape(metin).replace("\xa0", " ")).strip()
    if not 6 <= len(t) <= 80:
        return False
    kodlar = list(KOD.finditer(t))
    if not kodlar:
        return False
    kalan = KOD.sub("", t)
    if ARTIK.sub("", kalan).strip():                                    # kod / rev / tarih dışında anlamlı metin var → damga değil
        return False
    for k in kodlar:
        n = norm(k.group(0))
        if n == kendi:
            return False                                                # dokümanın güncel kodu → dokunma
    if re.search(r"(?i)rev\.?\s?\d|\d{1,2}[./]\d{1,2}[./]\d{2,4}", t):                # kod + rev / tarih = revizyon damgası; kendi kodu değilse eskidir
        return True
    if hucre:                                                           # tablo hücresindeki yalın kod = liste içeriği (ör. kayıt listesi) → yalnız dokümanın KENDİ eski kodu silinir
        return all(norm(k.group(0)) in KENDI_ESKI.get(kendi, ()) for k in kodlar)
    return any((norm(k.group(0)) in eski) or ESKI_BICIM.search(k.group(0)) or ESKI_SUREC.search(k.group(0)) for k in kodlar)


def temizle(h, eski, kendi):
    """(yeni_html, silinen_metinler[])"""
    silinen = []
    # yalın kod hücresi çoksa doküman bir kod LİSTESİdir (ör. kayıt listesi) → hücre kuralı uygulanmaz
    liste_mi = sum(1 for m in BLOK.finditer(h) if m.group(1) == "td" and KOD.fullmatch(re.sub(r"\s+", " ", html.unescape(TAG.sub("", m.group(3))).replace(" ", " ")).strip() or "x")) > 6

    def f(m):
        ic = TAG.sub("", m.group(3))
        if "<table" in m.group(3) or "<img" in m.group(3):
            return m.group(0)
        if m.group(1) == "td" and liste_mi and not re.search(r"(?i)rev\.?\s?\d|\d{1,2}[./]\d{1,2}[./]\d{2,4}", ic):
            return m.group(0)
        if damga_mi(ic, eski, kendi, hucre=(m.group(1) == "td")):
            silinen.append(re.sub(r"\s+", " ", html.unescape(ic)).strip())
            return "" if m.group(1) != "td" else f"<td{m.group(2)}>&nbsp;</td>"
        return m.group(0)
    return BLOK.sub(f, h), silinen


def main():
    eski, yeni = yukle_eski()
    print(f"eski kod sayısı (eşleştirme): {len(eski)}")
    db = SessionLocal()
    try:
        say, doc_say, ornek = 0, 0, {}
        for d in db.query(Document).filter(Document.govde_html.isnot(None)).all():
            kendi = norm(d.dokuman_no or "")
            h2, sil = temizle(d.govde_html, eski, kendi)
            if sil:
                say += len(sil); doc_say += 1
                for s_ in sil:
                    ornek.setdefault(s_, d.dokuman_no)
                if UYGULA:
                    d.govde_html = h2
        print(f"silinecek damga: {say} · etkilenen doküman: {doc_say}")
        for s_, no in list(ornek.items())[:400]:
            print(f"  {no:12s} {s_[:80]}")
        if UYGULA:
            db.commit()
            print("UYGULANDI.")
        else:
            print("\nKURU ÇALIŞMA — hiçbir şey değişmedi. Uygulamak için --uygula (önce yedek!).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
