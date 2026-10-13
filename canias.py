"""
DYS — CANIAS ERP okuması (2026)
Onaylı müşteri / tedarikçi (IASCUSTOMER) listesini DYS içindeki seçim alanlarına doldurur.
  - Yalnız OKUMA: SELECT dışında sorgu çalıştırılmaz.
  - Bağlantı Config.CANIAS_URL (DYS_CANIAS_*); kapalıysa her şey boş döner, DYS etkilenmez.
  - Firmalar kısa süreli bellek önbelleğinde tutulur (IASCUSTOMER büyük; her sayfada sorgulanmaz).
  - Bağlantı/zaman aşımı hatasında boş liste döner ve loglanır; sayfa açılmaya devam eder.
"""
import logging
import threading
import time

from sqlalchemy import create_engine, text

from config import Config

logger = logging.getLogger("dys")

_ENGINE = None
_ENGINE_LOCK = threading.Lock()
_CACHE = {"zaman": 0.0, "firmalar": None}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 600  # sn (10 dk)

# Onaylı firmalar (müşteri + tedarikçi). ISUNAPPROVED: 0=Onaylı, 2=Müşteri Onaylı (1=Onaysız hariç).
_SORGU = text(
    """
    SELECT CUSTOMER AS kod, NAME1 AS ad, NAME2 AS ad2, CITY AS sehir, COUNTRY AS ulke,
           TAXDEPT AS vergi_dairesi, TAXNUM AS vergi_no, TELNUM AS telefon, CURRENCY AS para_birimi,
           ISCUSTORVEND AS tur_kodu,
           CASE WHEN ISUNAPPROVED = 0 THEN N'Onaylı'
                WHEN ISUNAPPROVED = 2 THEN N'Müşteri Onaylı' ELSE N'' END AS onay,
           CASE WHEN ISUNAPPROVEDDBK = 0 THEN N'1. Seviye'
                WHEN ISUNAPPROVEDDBK = 1 THEN N'2. Seviye'
                WHEN ISUNAPPROVEDDBK = 2 THEN N'3. Seviye' ELSE N'' END AS seviye
    FROM IASCUSTOMER
    WHERE CLIENT = '00' AND COMPANY = '01' AND BUSAREA = '*'
          AND ISDELETE = 0 AND ISCUSTORVEND >= 0 AND ISUNAPPROVED <> 1
    ORDER BY NAME1
    """
)


def aktif():
    return bool(getattr(Config, "CANIAS_ENABLED", False))


def _engine():
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                _ENGINE = create_engine(
                    Config.CANIAS_URL, pool_pre_ping=True, pool_recycle=1800,
                    connect_args={"timeout": 10}, future=True,
                )
    return _ENGINE


def _yukle():
    """IASCUSTOMER'dan onaylı firmaları çeker (ham liste)."""
    with _engine().connect() as c:
        c = c.execution_options(stream_results=True)
        rows = c.execute(_SORGU).mappings().all()
    out = []
    for r in rows:
        ad = (r["ad"] or "").strip()
        ad2 = (r["ad2"] or "").strip()
        out.append({
            "kod": (r["kod"] or "").strip(),
            "ad": ad,
            "ad2": ad2,
            "tam_ad": (ad + (" " + ad2 if ad2 else "")).strip(),
            "sehir": (r["sehir"] or "").strip(),
            "ulke": (r["ulke"] or "").strip(),
            "vergi_dairesi": (r["vergi_dairesi"] or "").strip(),
            "vergi_no": (r["vergi_no"] or "").strip(),
            "telefon": (r["telefon"] or "").strip(),
            "para_birimi": (r["para_birimi"] or "").strip(),
            "onay": (r["onay"] or "").strip(),
            "seviye": (r["seviye"] or "").strip(),
        })
    return out


def firmalar(zorla=False):
    """Onaylı firmaların listesi (önbellekli). Hata/kapalı → []."""
    if not aktif():
        return []
    simdi = time.time()
    with _CACHE_LOCK:
        if not zorla and _CACHE["firmalar"] is not None and (simdi - _CACHE["zaman"]) < _CACHE_TTL:
            return _CACHE["firmalar"]
    try:
        data = _yukle()
    except Exception:  # noqa: BLE001 — CANIAS erişilemezse DYS çalışmaya devam eder
        logger.exception("CANIAS firmalar okunamadı")
        with _CACHE_LOCK:
            # Elde eski veri varsa onu koru; yoksa boş
            return _CACHE["firmalar"] or []
    with _CACHE_LOCK:
        _CACHE["firmalar"] = data
        _CACHE["zaman"] = simdi
    return data


def ara(q="", limit=50):
    """kod / ad / ad2 / şehir içinde arar. q boşsa ilk `limit` firma."""
    q = (q or "").strip().lower()
    tumu = firmalar()
    if not q:
        return tumu[:limit]
    sonuc = []
    for f in tumu:
        if (q in f["kod"].lower() or q in f["ad"].lower() or q in f["ad2"].lower()
                or q in f["sehir"].lower()):
            sonuc.append(f)
            if len(sonuc) >= limit:
                break
    return sonuc
