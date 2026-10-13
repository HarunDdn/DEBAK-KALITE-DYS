"""
DYS — Merkezi Yapılandırma
==========================
Tüm ayarlar ortam değişkenlerinden okunur; güvenli varsayılanlarla gelir.
Üretimde en azından DYS_SECRET_KEY ve DYS_FORCE_HTTPS ayarlanmalıdır.
SMTP bildirimleri için DYS_SMTP_* değişkenleri doldurulmalıdır.
"""

import os

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# .env dosyasını süreç ortamına yükle (yoksa sessizce geçer)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_BASE_DIR, ".env"), override=False)
except ImportError:
    pass


_unc_mapped = False


def connect_upload_share():
    """IIS oturum 0'da UNC (AL DOSYALAR) — WNetAddConnection2 (parola özel karakterleri)."""
    global _unc_mapped
    if _unc_mapped or os.name != "nt":
        return
    root = (os.environ.get("DYS_UPLOAD_FOLDER") or "").strip()
    if not root.startswith("\\\\"):
        return
    parts = root.lstrip("\\").split("\\")
    if len(parts) < 2:
        return
    share = "\\\\" + parts[0] + "\\" + parts[1]
    user = (os.environ.get("DYS_FILESHARE_USER") or "").strip() or None
    password = os.environ.get("DYS_FILESHARE_PASSWORD") or ""
    try:
        import ctypes
        from ctypes import wintypes

        class NETRESOURCE(ctypes.Structure):
            _fields_ = [
                ("dwScope", wintypes.DWORD),
                ("dwType", wintypes.DWORD),
                ("dwDisplayType", wintypes.DWORD),
                ("dwUsage", wintypes.DWORD),
                ("lpLocalName", wintypes.LPWSTR),
                ("lpRemoteName", wintypes.LPWSTR),
                ("lpComment", wintypes.LPWSTR),
                ("lpProvider", wintypes.LPWSTR),
            ]

        mpr = ctypes.WinDLL("mpr")
        cancel = mpr.WNetCancelConnection2W
        cancel.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL]
        cancel.restype = wintypes.DWORD
        add = mpr.WNetAddConnection2W
        add.argtypes = [
            ctypes.POINTER(NETRESOURCE),
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
        ]
        add.restype = wintypes.DWORD

        RESOURCETYPE_DISK = 1
        CONNECT_TEMPORARY = 4
        CONFLICT_CODES = {85, 1202, 1219}  # already assigned / remembered / cred conflict

        nr = NETRESOURCE()
        nr.dwType = RESOURCETYPE_DISK
        nr.lpRemoteName = share
        pwd = password if user else None
        rc = add(ctypes.byref(nr), pwd, user, CONNECT_TEMPORARY)
        if rc in CONFLICT_CODES:
            cancel(share, 0, True)
            rc = add(ctypes.byref(nr), pwd, user, CONNECT_TEMPORARY)
        who = user or "(process)"
        print(f"DYS UNC map {share} user={who} code={rc}", flush=True)
        if rc == 0:
            _unc_mapped = True
    except Exception as exc:
        print(f"DYS UNC map failed: {exc}", flush=True)


connect_upload_share()


def _bool(env_name, default=False):
    val = os.environ.get(env_name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on", "evet")


def _int(env_name, default):
    try:
        return int(os.environ.get(env_name, default))
    except (ValueError, TypeError):
        return default


def _build_database_url():
    """DYS_DATABASE_URL veya DYS_MSSQL_* parçalarından bağlantı dizisi üretir."""
    explicit = (os.environ.get("DYS_DATABASE_URL") or "").strip()
    if explicit:
        return explicit

    server = (os.environ.get("DYS_MSSQL_SERVER") or "").strip()
    if not server:
        return "sqlite:///dys.db"

    from sqlalchemy.engine import URL

    database = os.environ.get("DYS_MSSQL_DATABASE", "DBKDYS")
    driver = os.environ.get("DYS_MSSQL_DRIVER", "ODBC Driver 17 for SQL Server")
    trusted = _bool("DYS_MSSQL_TRUSTED", False)
    query = {
        "driver": driver,
        "TrustServerCertificate": "yes",
    }
    if trusted:
        query["Trusted_Connection"] = "yes"
        return URL.create(
            "mssql+pyodbc",
            host=server,  # örn. DEBAKNETSIS\DB20 — backslash korunur
            database=database,
            query=query,
        ).render_as_string(hide_password=False)

    user = os.environ.get("DYS_MSSQL_USER", "sa")
    password = os.environ.get("DYS_MSSQL_PASSWORD", "")
    return URL.create(
        "mssql+pyodbc",
        username=user,
        password=password,
        host=server,
        database=database,
        query=query,
    ).render_as_string(hide_password=False)


def _build_canias_url():
    """CANIAS ERP (IAS) veritabanı bağlantısı — DYS_CANIAS_* ile. Yoksa '' (özellik kapalı).

    Sunucu/kullanıcı verilmezse DYS_MSSQL_* değerlerine düşer (aynı SQL Server örneği).
    Veritabanı adı (DYS_CANIAS_DATABASE) zorunludur; IASCUSTOMER sorgusu bu veritabanında çalışır.
    """
    database = (os.environ.get("DYS_CANIAS_DATABASE") or "").strip()
    if not database:
        return ""
    from sqlalchemy.engine import URL
    server = (os.environ.get("DYS_CANIAS_SERVER") or os.environ.get("DYS_MSSQL_SERVER") or "").strip()
    if not server:
        return ""
    driver = os.environ.get("DYS_CANIAS_DRIVER", os.environ.get("DYS_MSSQL_DRIVER", "ODBC Driver 17 for SQL Server"))
    query = {"driver": driver, "TrustServerCertificate": "yes"}
    if _bool("DYS_CANIAS_TRUSTED", False):
        query["Trusted_Connection"] = "yes"
        return URL.create("mssql+pyodbc", host=server, database=database, query=query).render_as_string(hide_password=False)
    user = (os.environ.get("DYS_CANIAS_USER") or os.environ.get("DYS_MSSQL_USER") or "").strip()
    password = os.environ.get("DYS_CANIAS_PASSWORD") or os.environ.get("DYS_MSSQL_PASSWORD") or ""
    return URL.create("mssql+pyodbc", username=user, password=password, host=server,
                      database=database, query=query).render_as_string(hide_password=False)


class Config:
    # ── Temel ────────────────────────────────────────────────────────
    ENV = os.environ.get("DYS_ENV", "development").strip().lower()
    IS_PRODUCTION = ENV in ("production", "prod")

    SECRET_KEY = os.environ.get("DYS_SECRET_KEY")  # None ise app rastgele üretir
    DATABASE_URL = _build_database_url()

    # ── CANIAS ERP (opsiyonel; müşteri/tedarikçi seçimini IASCUSTOMER'dan doldurur) ──
    CANIAS_URL = _build_canias_url()
    CANIAS_ENABLED = bool(CANIAS_URL)

    # ── Redis (opsiyonel; boş = bellek / iptal listesi kapalı) ────────
    REDIS_URL = os.environ.get("DYS_REDIS_URL", "").strip()
    RATE_LIMIT_STORAGE = REDIS_URL or "memory://"
    SESSION_REVOKE_ENABLED = bool(REDIS_URL)

    # Demo verisi ve varsayılan hesap: yalnızca üretim dışında otomatik yüklenir.
    AUTO_SEED = _bool("DYS_AUTO_SEED", not IS_PRODUCTION)
    SHOW_STARTUP_CREDENTIALS = _bool("DYS_SHOW_STARTUP_CREDENTIALS", not IS_PRODUCTION)

    # Görevler ayrılığı (SoD): hazırlayan kontrol/onay yapamaz ve bir kullanıcı aynı
    # onay çevriminde birden fazla adımı işleyemez. Küçük ekipler için kapatılabilir.
    ENFORCE_SEGREGATION_OF_DUTIES = _bool("DYS_ENFORCE_SOD", True)

    UPLOAD_FOLDER = os.environ.get("DYS_UPLOAD_FOLDER", os.path.join(_BASE_DIR, "uploads"))
    # Filigran / geçici dosyalar (boşsa UPLOAD_FOLDER). UNC yavaşsa yerel temp kullanın.
    TEMP_FOLDER = os.environ.get("DYS_TEMP_FOLDER", "").strip() or None
    # 2026 — Office önizleme PDF önbelleği (yazılabilir) ve önceden üretilmiş PDF paketleri (salt okunur, ';' ayrılmış)
    ONIZLEME_KLASORU = os.environ.get("DYS_ONIZLEME_KLASORU", "").strip() or None
    ONIZLEME_EK_KLASORLER = os.environ.get("DYS_ONIZLEME_EK_KLASORLER", "").strip()
    MAX_CONTENT_LENGTH = _int("DYS_MAX_UPLOAD_MB", 50) * 1024 * 1024

    PREFIX = os.environ.get("DYS_PREFIX", "")

    # ── 11 süreçlik yapı (2026) ──────────────────────────────────────
    # eski  : D04-PR-001 (varsayılan, mevcut davranış)
    # debak : D04 P02 / D04.5 F04 (şirket numara biçimi: Süreç[.Alt] + Harf + NN)
    NUMARA_FORMATI = (os.environ.get("DYS_NUMARA_FORMATI", "eski") or "eski").strip().lower()
    # Dinamik form verilerinin otomatik yazılacağı klasör (Excel / Power BI bağlantısı için). Boş = kapalı.
    FORM_VERI_KLASORU = (os.environ.get("DYS_FORM_VERI_KLASORU") or "").strip() or None
    # "YENİ YAPI" doküman klasörünün bu ortamdaki yeri. Kayıtlı yollar başka sürücü/sunucudaki
    # ...\YENİ YAPI\... klasörünü gösteriyorsa bu köke eşlenir. Boş = eşleme yok.
    YENI_YAPI_KLASORU = (os.environ.get("DYS_YENI_YAPI_KLASORU") or "").strip() or None
    # Kullanıcılara gösterilen dizin kökü (ör. \\SUNUCU\Paylasim\YENİ YAPI). Boş = YENI_YAPI_KLASORU.
    YENI_YAPI_GORUNEN_KLASOR = (os.environ.get("DYS_YENI_YAPI_GORUNEN_KLASOR") or "").strip() or None
    # Haritada eski (pasif) süreçleri de göster (geçiş dönemi)
    ESKI_SURECLERI_GOSTER = _bool("DYS_ESKI_SURECLERI_GOSTER", False)

    @classmethod
    def get_temp_folder(cls):
        """Yazılabilir geçici klasör (filigran / çıktı). IIS yolu yoksa proje/temp."""
        candidates = []
        if cls.TEMP_FOLDER:
            candidates.append(cls.TEMP_FOLDER)
        candidates.append(os.path.join(_BASE_DIR, "temp"))
        candidates.append(os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or ".", "dys-temp"))
        for folder in candidates:
            try:
                os.makedirs(folder, exist_ok=True)
                probe = os.path.join(folder, ".dys_write_test")
                with open(probe, "wb") as fh:
                    fh.write(b"ok")
                os.remove(probe)
                return folder
            except OSError:
                continue
        import tempfile
        return tempfile.gettempdir()

    # ── Oturum / HTTPS ───────────────────────────────────────────────
    FORCE_HTTPS = _bool("DYS_FORCE_HTTPS", False)
    SESSION_LIFETIME_HOURS = _int("DYS_SESSION_HOURS", 8)

    # ── Giriş güvenliği ──────────────────────────────────────────────
    MAX_LOGIN_ATTEMPTS = _int("DYS_MAX_LOGIN_ATTEMPTS", 5)
    LOGIN_LOCK_MINUTES = _int("DYS_LOGIN_LOCK_MINUTES", 15)
    MIN_PASSWORD_LENGTH = _int("DYS_MIN_PASSWORD_LENGTH", 8)
    LOGIN_RATELIMIT = os.environ.get("DYS_LOGIN_RATELIMIT", "10 per minute")

    # ── Dosya yükleme beyaz listesi ─────────────────────────────────
    ALLOWED_UPLOAD_EXTENSIONS = {
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
        "png", "jpg", "jpeg", "gif", "webp", "bmp",
        "txt", "csv", "rtf", "odt", "ods",
    }
    # Tarayıcıda satır içi (inline) gösterilmesi tehlikeli olan tipler → her zaman indirilir
    UNSAFE_INLINE_EXTENSIONS = {"html", "htm", "svg", "xml", "js"}

    # ── E-posta / SMTP ───────────────────────────────────────────────
    SMTP_HOST = os.environ.get("DYS_SMTP_HOST", "")
    SMTP_PORT = _int("DYS_SMTP_PORT", 587)
    SMTP_USER = os.environ.get("DYS_SMTP_USER", "")
    SMTP_PASS = os.environ.get("DYS_SMTP_PASS", "")
    SMTP_FROM = os.environ.get("DYS_SMTP_FROM", "dys@debak.local")
    SMTP_USE_TLS = _bool("DYS_SMTP_USE_TLS", True)
    NOTIFICATIONS_ENABLED = _bool("DYS_NOTIFICATIONS_ENABLED", True)

    # Uygulamanın dış erişim adresi (e-postalardaki bağlantılar için)
    APP_BASE_URL = os.environ.get("DYS_APP_BASE_URL", "http://127.0.0.1:5000")

    # ── Yerelleştirme ────────────────────────────────────────────────
    DEFAULT_LOCALE = os.environ.get("DYS_DEFAULT_LOCALE", "tr")
    SUPPORTED_LOCALES = ("tr", "en")

    # ── Zamanlanmış görevler ─────────────────────────────────────────
    SCHEDULER_ENABLED = _bool("DYS_SCHEDULER_ENABLED", False)
    DAILY_DIGEST_HOUR = _int("DYS_DAILY_DIGEST_HOUR", 8)
    # DÖF hatırlatma: planlanan kapanışa kaç gün kala uyarı (0 = yalnızca gecikenler)
    CAPA_REMINDER_DAYS = _int("DYS_CAPA_REMINDER_DAYS", 7)
    CAPA_REMINDER_HOUR = _int("DYS_CAPA_REMINDER_HOUR", 9)
