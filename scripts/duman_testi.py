"""Canlıya alma sonrası DUMAN TESTİ: giriş yapar, ana ekranları açar, 5xx / hata sayfası arar. Veri DEĞİŞTİRMEZ (yalnız GET).
Kullanım (şifre komut satırına yazılmaz, ortam değişkeninden okunur):
  set DYS_TEST_URL=http://192.168.0.245:8081
  set DYS_TEST_EPOSTA=...        set DYS_TEST_SIFRE=...
  python scripts\\duman_testi.py
Yetkili (Admin) ve istenirse ikinci bir kullanıcı (DYS_TEST_EPOSTA2 / DYS_TEST_SIFRE2, ör. Sadece Görüntüleme) ile çalıştırın; salt-okur kullanıcıda 'Yetkiniz yok' yönlendirmesi beklenir."""
import os
import re
import sys

import requests

URL = os.environ.get("DYS_TEST_URL", "http://127.0.0.1:5055").rstrip("/")
SAYFALAR = [
    "/", "/islerim", "/processes", "/documents", "/forms", "/approvals",
    "/strateji", "/audits/automation", "/audits", "/capa", "/performans", "/toplantilar", "/ims/reviews",
    "/kalite/tedarikci", "/kalite/musteri", "/kalite/yeniden-islem", "/kalite/yeniden-islem/rapor", "/teknik-resim",
    "/ppap", "/urun-proses", "/urun-proses/kutuphane", "/iatf/apqp", "/iatf/fmea", "/iatf/control-plans", "/iatf/calibration",
    "/dogrulama", "/dogrulama?tur=Terazi", "/dogrulama?tur=Termokupl", "/dogrulama/rapor", "/iatf/spc", "/iatf/csr",
    "/bakim", "/bakim/ekipman", "/qrqc", "/risks", "/training", "/competency", "/reports", "/uretim/enjeksiyon-parametreleri",
]


def giris(eposta, sifre):
    s = requests.Session()
    r = s.get(URL + "/login", timeout=30)
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    r = s.post(URL + "/login", data={"eposta": eposta, "sifre": sifre, "csrf_token": m.group(1) if m else ""}, timeout=30, allow_redirects=True)
    if "/login" in r.url:
        raise SystemExit("Giriş başarısız (e-posta / şifre / kilitlenme?)")
    return s


def calistir(eposta, sifre, etiket):
    s = giris(eposta, sifre)
    hata = 0
    for u in SAYFALAR:
        try:
            r = s.get(URL + u, timeout=60)
        except requests.RequestException as e:
            print(f"  HATA {u}: {e}"); hata += 1; continue
        kotu = r.status_code >= 500 or "Beklenmeyen bir sunucu hatası" in r.text or "<title>500" in r.text
        if kotu:
            print(f"  HATA {u}: {r.status_code}"); hata += 1
        elif r.status_code == 404:
            print(f"  yok  {u}: 404 (modül kapalı olabilir)")
    print(f"{etiket}: {len(SAYFALAR)} sayfa denendi · hata {hata}")
    return hata


if __name__ == "__main__":
    e, p = os.environ.get("DYS_TEST_EPOSTA"), os.environ.get("DYS_TEST_SIFRE")
    if not (e and p):
        raise SystemExit("DYS_TEST_EPOSTA ve DYS_TEST_SIFRE ortam değişkenlerini tanımlayın.")
    toplam = calistir(e, p, "Kullanıcı 1")
    if os.environ.get("DYS_TEST_EPOSTA2") and os.environ.get("DYS_TEST_SIFRE2"):
        toplam += calistir(os.environ["DYS_TEST_EPOSTA2"], os.environ["DYS_TEST_SIFRE2"], "Kullanıcı 2")
    print("\nSONUÇ:", "TEMİZ" if toplam == 0 else f"{toplam} hata")
    sys.exit(1 if toplam else 0)
