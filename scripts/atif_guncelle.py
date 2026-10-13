"""Doküman içeriğinde (documents.govde_html) cümle içi ESKİ doküman kodu atıflarını YENİ koda çevirir (kod_eslestirme.csv: eski_kod → yeni_kod).
Güvenlik kuralları:
  • yalnız metin düğümlerinde değiştirir (etiket / öznitelik / base64 görsel verisine dokunmaz);
  • eski kod başka bir dokümanın GÜNCEL kodu ise (çakışma, ör. M03 ↔ M04 yer değiştirmesi) otomatik ÇEVRİLMEZ — raporlanır;
  • yeni kodu olmayan (silinmiş) eski kodlar çevrilmez — raporlanır; dokümanın kendi eski koduna kendi yeni kodu yazılır (zaten damga olarak silinmiştir).
KULLANIM: python scripts/atif_guncelle.py            → kuru çalışma (sayım, örnekler, belirsiz kodlar)
          python scripts/atif_guncelle.py --uygula   → uygular (önce veritabanı yedeği)
          --csv <yol>                                  → kod_eslestirme.csv (öntanımlı: YENİ YAPI\\DYS Aktarım)
          --rapor <dosya.tsv>                          → doküman / eski / yeni / adet dökümü"""
import csv
import html
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import SessionLocal, Document  # noqa: E402

UYGULA = "--uygula" in sys.argv
# eski kodların İÇERİĞİN KENDİSİ olduğu envanter / liste dokümanları (ör. eski Ana Doküman Listesi) — çevrilmez
HARIC = {"D04.6 F01"}
KOK = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSV = sys.argv[sys.argv.index("--csv") + 1] if "--csv" in sys.argv else os.path.join(os.path.dirname(KOK), "YENİ YAPI", "DYS Aktarım", "kod_eslestirme.csv")
RAPOR = sys.argv[sys.argv.index("--rapor") + 1] if "--rapor" in sys.argv else None

KOD = re.compile(r"(?<![\w.])([A-ZÇĞİÖŞÜ]\d{2}(?:\.\d)?)([  ]?)([TFPAKG]{1,2}\d{2})(?![\w])")
PARCA = re.compile(r"(<[^>]*>)|([^<]+)", re.S)


def norm(k):
    return re.sub(r"[\s ]+", " ", k.strip().upper().replace("İ", "I"))


ESKI_AD = {}          # eski kod → eski doküman adı (csv)
CAKISMA = {}          # çakışan kod → yeni kod (eski karşılığı)


def kelimeler(t):
    t = re.sub(r"[^\w\s]", " ", (t or "").replace("İ", "i").replace("I", "ı").lower())
    return {w[:5] for w in t.split() if len(w) >= 4}


def harita(csv_yol):
    eski_yeni, yeni_set, belirsiz = {}, set(), {}
    with open(csv_yol, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter=";"):
            e, y = norm(r.get("eski_kod") or ""), (r.get("yeni_kod") or "").strip()
            if y:
                yeni_set.add(norm(y))
            if e:
                ESKI_AD[e] = r.get("ad") or ""
            if e and y and norm(y) != e:
                eski_yeni[e] = y
            elif e and not y:
                belirsiz[e] = "silinmiş (yeni kod yok)"
    for e in list(eski_yeni):
        if e in yeni_set:                                               # eski kod aynı zamanda başka bir dokümanın güncel kodu
            belirsiz[e] = f"çakışma: {e} güncel kod da (eski → {eski_yeni[e]})"
            CAKISMA[e] = eski_yeni[e]
            del eski_yeni[e]
    return eski_yeni, belirsiz


def baglam_coz(k, sonraki, baslik):
    """Çakışan kod için: kodu izleyen ad eski dokümana mı (→ yeni koda çevir), güncel dokümana mı (→ dokunma) benziyor? 'eski' | 'guncel' | None"""
    w = kelimeler(sonraki[:90])
    if len(w) < 2:
        return None
    e = len(w & kelimeler(ESKI_AD.get(k, "")))
    g = len(w & kelimeler(baslik.get(k, "")))
    if e >= 2 and e >= g + 2:
        return "eski"
    if g >= 2 and g >= e + 2:
        return "guncel"
    return None


def cevir(h, eski_yeni, belirsiz, kendi, ist, baslik=None):
    baslik = baslik or {}

    def metin(m):
        def kod(mm):
            k = norm(mm.group(1) + " " + mm.group(3))
            if k == kendi:
                return mm.group(0)
            if k in eski_yeni:
                ist["cevrilen"][(k, eski_yeni[k])] += 1
                return eski_yeni[k]
            if k in CAKISMA:
                karar = baglam_coz(k, mm.string[mm.end():], baslik)
                if karar == "eski":
                    ist["cevrilen"][(k, CAKISMA[k])] += 1
                    ist["baglam"] += 1
                    return CAKISMA[k]
                if karar == "guncel":
                    ist["guncel"] += 1
                    return mm.group(0)
            if k in belirsiz:
                ist["belirsiz"][(k, belirsiz[k])] += 1
            return mm.group(0)
        return KOD.sub(kod, m.group(2)) if m.group(2) is not None else m.group(0)
    return PARCA.sub(lambda m: m.group(1) if m.group(1) else metin(m), h)


def main():
    eski_yeni, belirsiz = harita(CSV)
    print(f"çevrilebilir eski kod: {len(eski_yeni)} · belirsiz (çakışma / silinmiş): {len(belirsiz)}")
    db = SessionLocal()
    try:
        toplam = Counter(); belirsiz_say = Counter(); doc_say = 0; satirlar = []; baglam_say = guncel_say = 0
        baslik = {norm(no): b for no, b in db.query(Document.dokuman_no, Document.baslik).all() if no}
        for d in db.query(Document).filter(Document.govde_html.isnot(None)).all():
            if (d.dokuman_no or "").upper() in HARIC:
                continue
            ist = {"cevrilen": Counter(), "belirsiz": Counter(), "baglam": 0, "guncel": 0}
            h2 = cevir(d.govde_html, eski_yeni, belirsiz, norm(d.dokuman_no or ""), ist, baslik)
            baglam_say += ist["baglam"]; guncel_say += ist["guncel"]
            if ist["cevrilen"]:
                doc_say += 1
                toplam.update(ist["cevrilen"])
                for (e, y), n in ist["cevrilen"].items():
                    satirlar.append((d.dokuman_no, e, y, n))
                if UYGULA:
                    d.govde_html = h2
            belirsiz_say.update(ist["belirsiz"])
        print(f"çevrilecek atıf: {sum(toplam.values())} · etkilenen doküman: {doc_say} · farklı eski kod: {len(toplam)}")
        for (e, y), n in toplam.most_common(15):
            print(f"   {e:11s} → {y:11s}  {n} yerde")
        print(f"bağlamdan çözülen çakışma: {baglam_say} eski → yeni çevrildi · {guncel_say} güncel kod olarak bırakıldı")
        print(f"ÇEVRİLMEYEN belirsiz atıf: {sum(belirsiz_say.values())} ({len(belirsiz_say)} kod)")
        for (e, neden), n in belirsiz_say.most_common(12):
            print(f"   {e:11s} {n:4d}  {neden}")
        if RAPOR:
            with open(RAPOR, "w", encoding="utf-8-sig") as f:
                f.write("dokuman\teski\tyeni\tadet\n" + "\n".join("\t".join(map(str, s)) for s in satirlar))
        if UYGULA:
            db.commit(); print("UYGULANDI.")
        else:
            print("\nKURU ÇALIŞMA — hiçbir şey değişmedi. Uygulamak için --uygula (önce yedek!).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
