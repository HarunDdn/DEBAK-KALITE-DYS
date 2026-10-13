"""
DYS — Dosya deposu (2026): sertifika, teknik resim, doğrulama / kalite görselleri gibi yüklenen dosyaları VERİTABANINA da alır (dosya_depo tablosu),
böylece dosya paylaşımı / yükleme klasörü olmadan da DYS çalışır. İçerik SHA-256'ya göre tek kez saklanır (aynı fotoğraf birçok kartta tekrarlansa da bir kez).
  ekle(db, yol=None, veri=None, ad=None)       → depo_id (aynı içerik varsa mevcut kayıt); hata verirse None döner
  yol_coz(db, depo_id, ad, disk_yolu=None)     → kullanılabilir YEREL yol: disk dosyası varsa o; yoksa depodan geçici önbelleğe yazılır
  veri_al(db, depo_id)                         → bytes | None
Mevcut kod akışları (send_file, PIL, reportlab) yol bekler; bu yüzden yol_coz önbellek kopyası üretir (DYS_TEMP_FOLDER / sistem temp altında 'dys_depo').
"""
import hashlib
import logging
import mimetypes
import os
import tempfile
from datetime import datetime

log = logging.getLogger("dys")
MAX_MB = 25


def _onbellek():
    try:
        from config import Config
        kok = getattr(Config, "TEMP_FOLDER", None) or tempfile.gettempdir()
    except Exception:  # noqa: BLE001
        kok = tempfile.gettempdir()
    d = os.path.join(kok, "dys_depo")
    os.makedirs(d, exist_ok=True)
    return d


def ekle(db, yol=None, veri=None, ad=None):
    """Dosyayı depoya alır; depo_id döner. db.flush() yapar, commit çağıranındır."""
    from models import DosyaDepo
    try:
        if veri is None:
            if not yol or not os.path.isfile(yol):
                return None
            if os.path.getsize(yol) > MAX_MB * 1024 * 1024:
                log.warning("dosya_depo: %s çok büyük, depoya alınmadı", yol)
                return None
            with open(yol, "rb") as f:
                veri = f.read()
        if len(veri) > MAX_MB * 1024 * 1024:
            return None
        h = hashlib.sha256(veri).hexdigest()
        k = db.query(DosyaDepo).filter_by(sha256=h).first()
        if k is None:
            ad = (ad or (os.path.basename(yol) if yol else "dosya"))[:200]
            k = DosyaDepo(sha256=h, ad=ad, mime=(mimetypes.guess_type(ad)[0] or "application/octet-stream")[:100], boyut=len(veri), icerik=veri, olusturma=datetime.now())
            db.add(k)
            db.flush()
        return k.id
    except Exception as ex:  # noqa: BLE001
        log.warning("dosya_depo.ekle: %s", ex)
        return None


def veri_al(db, depo_id):
    from models import DosyaDepo
    if not depo_id:
        return None
    k = db.get(DosyaDepo, depo_id)
    return bytes(k.icerik) if k is not None and k.icerik is not None else None


def yol_coz(db, depo_id, ad=None, disk_yolu=None):
    """Disk dosyası varsa onu; yoksa depodan önbelleğe yazıp o yolu döndürür. Hiçbiri yoksa None."""
    if disk_yolu and os.path.isfile(disk_yolu):
        return disk_yolu
    if not depo_id:
        return None
    from models import DosyaDepo
    k = db.get(DosyaDepo, depo_id)
    if k is None or k.icerik is None:
        return None
    guvenli = "".join(c for c in (ad or k.ad or "dosya") if c.isalnum() or c in "._- ()").strip() or "dosya"
    hedef = os.path.join(_onbellek(), f"{k.sha256[:16]}_{guvenli}")
    if not os.path.isfile(hedef) or os.path.getsize(hedef) != k.boyut:
        with open(hedef, "wb") as f:
            f.write(bytes(k.icerik))
    return hedef
