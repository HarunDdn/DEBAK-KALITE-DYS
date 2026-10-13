"""
DYS — Kullanıcıları Microsoft 365 posta kutusu listesinden ekler / eşleştirir (2026)
Kaynak: Exchange yönetim merkezinden dışa aktarılan Mailboxes.csv (DisplayName, PrimarySmtpAddress, FirstName, LastName, Department, Title …).
  · Yalnız kişisel posta kutuları alınır (UserMailbox, adı ve soyadı dolu); genel / ortak kutular (info, muhasebe, order …) ve
    DIS_KISILER listesindekiler alınmaz.
  · Eşleştirme: önce e-posta, yoksa ad soyad (Türkçe büyük / küçük harf ve noktalama farkı gözetilmez).
  · Mevcut kullanıcıda yalnız BOŞ olan e-posta, departman ve unvan doldurulur; rol, şifre ve aktiflik değişmez.
  · Yeni kullanıcı rol "Kullanıcı", aktif; şifresi rastgele ve bilinmez (kimse giremez) — yönetici Kullanıcılar ekranından şifre belirler.
  · --unvan organizasyon_unvanlari.json: organizasyon şemasındaki görev unvanı kullanıcının unvanı olur (şema güncel kaynak; üzerine yazar).
    Şemada olup posta kutusu olmayan kişiler için kullanıcı AÇILMAZ (e-posta zorunlu) — listede raporlanır.
  · İç denetçi kayıtlarında (DenetciYetkinlik) DYS kullanıcısı seçilmemiş olanlar ad soyaddan eşleştirilir (kısmi ad: "Nilsena Koç" ⊂ "Nilsena Koç Memişoğlu").
Kullanım: python kullanici_aktar.py --csv "D:\\indirilenler\\Mailboxes.csv" [--unvan data\\organizasyon_unvanlari.json] [--uygula]
"""
import argparse
import csv
import re
import secrets
import unicodedata

from werkzeug.security import generate_password_hash

GENEL = {"admin", "admin1", "bilgi", "bilgiislem", "debak", "etik", "info", "laboratuvar", "muhasebe", "mutabakat", "order",
         "personel", "planlama", "uretim", "sikayet"}
DIS_KISILER = {"ozkan.gulsoy@debak.com.tr"}   # dış danışman (Marmara Üniversitesi) — DYS kullanıcısı değil


def anahtar(ad):
    """'Abdullah UĞURLU' ≈ 'abdullah uğurlu' ≈ 'ABDULLAH UGURLU'"""
    s = (ad or "").replace("İ", "i").replace("I", "ı").lower()
    s = unicodedata.normalize("NFKD", s.replace("ı", "i"))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def kisiler(yol):
    out = []
    with open(yol, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            ep = (r.get("PrimarySmtpAddress") or "").strip().lower()
            if not ep or r.get("RecipientTypeDetails") != "UserMailbox" or ep.split("@")[0] in GENEL or ep in DIS_KISILER:
                continue
            ad, soyad = (r.get("FirstName") or "").strip(), (r.get("LastName") or "").strip()
            if not ad or not soyad or soyad == "null":
                continue
            temiz = lambda v: "" if (v or "").strip() in ("", "null") else v.strip()
            out.append({"eposta": ep, "ad_soyad": temiz(r.get("DisplayName")) or f"{ad} {soyad}", "departman": temiz(r.get("Department")),
                        "unvan": temiz(r.get("Title")), "ad": ad, "soyad": soyad})
    return out


def _bul(ad, sozluk):
    """tam anahtar; yoksa kelime kümesi biri diğerini kapsıyorsa (en az 2 kelime) — 'Ege Yalman İnceoğlu' ≈ 'Ege İnceoğlu'"""
    k = anahtar(ad)
    if k in sozluk:
        return sozluk[k]
    a = set(k.split())
    aday = [v for kk, v in sozluk.items() if len(a & set(kk.split())) >= 2 and (a <= set(kk.split()) or set(kk.split()) <= a)]
    return aday[0] if len(aday) == 1 else None


def unvanlari_uygula(db, unvanlar):
    from models import User
    R = {"unvan": [], "kullanicisi_yok": []}
    by_ad = {anahtar(u.ad_soyad): u for u in db.query(User).all()}
    for ad, unvan in unvanlar.items():
        u = _bul(ad, by_ad)
        if not u:
            R["kullanicisi_yok"].append(f"{ad} ({unvan})"); continue
        if u.unvan != unvan[:50]:
            R["unvan"].append(f"{u.ad_soyad}: {u.unvan} → {unvan}"); u.unvan = unvan[:50]
    return R


def aktar(db, liste):
    from models import User, DenetciYetkinlik
    R = {"yeni": [], "guncellenen": [], "ayni": [], "denetci": []}
    kullanicilar = db.query(User).all()
    by_ep = {(u.eposta or "").lower(): u for u in kullanicilar if u.eposta}
    by_ad = {}
    for u in kullanicilar:
        by_ad.setdefault(anahtar(u.ad_soyad), u)
    for k in liste:
        u = by_ep.get(k["eposta"]) or by_ad.get(anahtar(k["ad_soyad"])) or by_ad.get(anahtar(f"{k['ad']} {k['soyad']}"))
        if u is None:
            u = User(ad_soyad=k["ad_soyad"][:100], eposta=k["eposta"], departman=k["departman"][:100] or "—", unvan=k["unvan"][:100] or "—",
                     rol="Kullanıcı", aktif=True, sifre_hash=generate_password_hash(secrets.token_urlsafe(32)))
            db.add(u); R["yeni"].append(k["ad_soyad"]); by_ep[k["eposta"]] = u; by_ad[anahtar(k["ad_soyad"])] = u
            continue
        degisti = []
        if not u.eposta or u.eposta.lower() == k["eposta"]:
            if (u.eposta or "").lower() != k["eposta"]:
                u.eposta = k["eposta"]; degisti.append("e-posta")
        for alan in ("departman", "unvan"):
            if not (getattr(u, alan) or "").strip() or getattr(u, alan) == "—":
                if k[alan]:
                    setattr(u, alan, k[alan][:100]); degisti.append(alan)
        (R["guncellenen"] if degisti else R["ayni"]).append(f"{u.ad_soyad}" + (f" ({', '.join(degisti)})" if degisti else ""))
    db.flush()
    kullanicilar = db.query(User).filter(User.aktif.__eq__(True)).all()
    by_ad = {anahtar(u.ad_soyad): u for u in kullanicilar}
    for d in db.query(DenetciYetkinlik).filter(DenetciYetkinlik.user_id.is_(None)).all():
        u = _bul(d.ad_soyad, by_ad)
        if u:
            d.user_id = u.id; R["denetci"].append(f"{d.ad_soyad} → {u.ad_soyad}")
    return R


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--unvan", default=None, help="organizasyon_unvanlari.json")
    ap.add_argument("--uygula", action="store_true")
    a = ap.parse_args()
    from models import init_db, SessionLocal
    init_db()
    db = SessionLocal()
    try:
        liste = kisiler(a.csv)
        R = aktar(db, liste)
        if a.unvan:
            import json
            R.update(unvanlari_uygula(db, json.load(open(a.unvan, encoding="utf-8"))["unvanlar"]))
        if a.uygula:
            db.commit()
        else:
            db.rollback()
        print(f"Kişisel posta kutusu: {len(liste)} · yeni kullanıcı {len(R['yeni'])} · güncellenen {len(R['guncellenen'])} · değişmeyen {len(R['ayni'])}"
              f" · denetçi eşleşen {len(R['denetci'])}" + ("" if a.uygula else "  (DENEME TURU — yazılmadı; uygulamak için --uygula)"))
        for k in ("yeni", "guncellenen", "denetci", "unvan", "kullanicisi_yok"):
            for x in R[k]:
                print(f"  {k}: {x}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
