"""
DYS — FMEA çalışma sayfası çekirdeği (GSI-RD-370 Rev. C02): D-FMEA / P-FMEA / FMEA-MSR.
Saf fonksiyonlar (Flask / veritabanı yok): AP kararı, puan normalizasyonu, uyarılar, özetler.
AP kararı yalnız matristen gelir (data/fmea_ap_matrisleri.json — golden test vektörlerinden üretildi); RPN karar için kullanılmaz.
Puanlar yönlendirme amaçlıdır; nihai karar FMEA ekibine aittir.
"""
import json
import os

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_C = {}

TIPLER = ("D", "P", "M")
TIP_AD = {"D": "D-FMEA", "P": "P-FMEA", "M": "FMEA-MSR"}
S_PUANLAR = (1, 3, 6, 7, 8, 9, 10)
O_PUANLAR = (1, 3, 5, 7, 10)
D_PUANLAR = (1, 4, 6, 8, 10)
FM_PUANLAR = (1, 2, 3, 4, 6, 8, 10)
DURUMLAR = {"D": ("Taahhüt edildi", "Devam ediyor", "Tamamlandı"), "P": ("Taahhüt edildi", "Devam ediyor", "Uygulandı"),
            "M": ("Taahhüt edildi", "Devam ediyor", "Tamamlandı")}
STANDARTLAR = {"GSI": "GSI-RD-370 (Valeo)", "VDA": "AIAG-VDA (standart)"}
AP_AD = {"H": "Yüksek", "M": "Orta", "L": "Düşük", "": ""}


def _matris():
    if "m" not in _C:
        _C["m"] = json.load(open(os.path.join(_DATA, "fmea_ap_matrisleri.json"), encoding="utf-8"))
    return _C["m"]


def kriterler():
    """Puan dayanakları (VALEO Restricted — dosya yoksa boş döner; uygulama puan dayanağı olmadan çalışır)."""
    if "k" not in _C:
        p = os.path.join(_DATA, "fmea_kriterleri.json")
        _C["k"] = json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else {"kriterler": {}, "notlar": {}, "ipuclari": {}, "tipler": {}}
    return _C["k"]


def _n(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def s_grup(s):
    return 3 if 2 <= s <= 5 else s


def _dp_grup(tip, s, o, d):
    """D/P ara puanlarını matris puanına çevirir (S 2-5→3; O 2-3→3, 4-5→5, 6-7→7, 8-10→10; D 2-4→4, 5-6→6, 7-8→8, 9-10→10)."""
    s = 3 if 2 <= s <= 5 else s
    o = 1 if o <= 1 else 3 if o <= 3 else 5 if o <= 5 else 7 if o <= 7 else 10
    d = 1 if d <= 1 else 4 if d <= 4 else 6 if d <= 6 else 8 if d <= 8 else 10
    return s, o, d


def ap(tip, s, o, d):
    """'H' | 'M' | 'L' | '' (S, O, D'den biri eksikse ''). tip: 'D' | 'P' | 'M' (MSR: o = F, d = M)."""
    s, o, d = _n(s), _n(o), _n(d)
    if not (s and o and d):
        return ""
    if s == 1:
        return "L"
    m = _matris()
    if tip == "M":
        return m["MSR"].get(f"{s_grup(s)}|{o}|{d}", "")
    s2, o2, d2 = _dp_grup(tip, s, o, d)
    return m["DP"].get(f"{s2}|{o2}|{d2}", "")


def normalize(satir, tip):
    """Eski / ara puanları izin verilen puana çevirir (S 2,4,5→3; O 2→3, 4→5, 6→7, 8,9→10; D 2,3→4, 5→6, 7→8, 9→10). AP sonucunu değiştirmez."""
    out = dict(satir)
    S = {2: 3, 4: 3, 5: 3}
    O = {2: 3, 4: 5, 6: 7, 8: 10, 9: 10}
    D = {2: 4, 3: 4, 5: 6, 7: 8, 9: 10}
    for k, tab in (("S", S), ("O", O if tip != "M" else {}), ("D", D if tip != "M" else {}), ("O2", O if tip != "M" else {}), ("D2", D if tip != "M" else {})):
        v = _n(out.get(k))
        if v in tab:
            out[k] = tab[v]
    return out


def puan_listesi(std="GSI"):
    """Standarda göre seçilebilen puanlar: GSI-RD-370 kısıtlı kümeler; AIAG-VDA 1-10 tam ölçek."""
    if std == "VDA":
        t = tuple(range(1, 11))
        return {"S": t, "O": t, "D": t}
    return {"S": S_PUANLAR, "O": O_PUANLAR, "D": D_PUANLAR}


def _ap_std(tip, s, o, d, std):
    if std != "VDA":
        return ap(tip, s, o, d)
    s, o, d = _n(s), _n(o), _n(d)
    if not (1 <= s <= 10 and 1 <= o <= 10 and 1 <= d <= 10):
        return ""
    from fmea_ap_matrix import hesapla_ap  # AIAG-VDA 2019 PFMEA Action Priority tablosu
    return hesapla_ap(s, o, d)


def hesapla(satir, tip, std="GSI"):
    """Satır için {ap, ap2, rpn, uyari: [(seviye, metin)]} — saklanmaz, her seferinde üretilir. std: 'GSI' (Valeo GSI-RD-370) | 'VDA' (AIAG-VDA)."""
    S, O, D = _n(satir.get("S")), _n(satir.get("O")), _n(satir.get("D"))
    O2, D2 = _n(satir.get("O2")), _n(satir.get("D2"))
    a = _ap_std(tip, S, O, D, std)
    a2 = _ap_std(tip, S, O2 or O, D2 or D, std) if (O2 or D2) else ""
    rpn = S * O * D if (S and O and D) else None
    u = []
    if S == 1:
        u.append(("bilgi", "S=1: etki yok, risk yok."))
    elif O == 1 and tip != "M" and std != "VDA":
        u.append(("bilgi", "O=1 ise tespit gereksiz / isteğe bağlıdır (D=1)."))
    act = (satir.get("act") or "").strip()
    if a == "H" and not act:
        u.append(("uyari", "Kırmızı alan: optimizasyon aksiyonu zorunlu."))
    if a == "M" and not act:
        u.append(("bilgi", "Sarı alan: aksiyon önerilir; yoksa gerekçe yazın."))
    return {"ap": a, "ap2": a2, "rpn": rpn, "uyari": u}


def ozet(satirlar, tip, std="GSI"):
    """İlk / güncel AP sayaçları: {'ilk': {H, M, L}, 'guncel': {H, M, L}}."""
    ilk = {"H": 0, "M": 0, "L": 0}
    gun = {"H": 0, "M": 0, "L": 0}
    for r in satirlar:
        h = hesapla(r, tip, std)
        if h["ap"]:
            ilk[h["ap"]] += 1
        f = h["ap2"] or h["ap"]
        if f:
            gun[f] += 1
    return {"ilk": ilk, "guncel": gun}


def dogrula_puan(tip, alan, deger, std="GSI"):
    """Seçilebilen puan mı? Boş serbest."""
    if deger in (None, "", 0, "0"):
        return True
    v = _n(deger)
    if std == "VDA":
        return 1 <= v <= 10
    if alan == "S":
        return v in S_PUANLAR
    if tip == "M":
        return v in FM_PUANLAR
    return v in (O_PUANLAR if alan in ("O", "O2") else D_PUANLAR)
