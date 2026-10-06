"""
DYS — Doküman Yönetim Sistemi
================================
IATF 16949 · ISO 14001 · ISO 45001 · ISO 27001 uyumlu
Flask tabanlı web uygulaması

Çalıştırmak için:
    python app.py
"""

import os
import re
import json
import secrets
import hashlib
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, send_from_directory, jsonify, make_response, send_file,
    after_this_request, g, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import func, or_

from config import Config
from models import (
    SessionLocal, init_db, Base, engine,
    User, Process, Document, DocumentRevision,
    DocumentApproval, DocumentDistribution, AuditLog,
    PPAPSubmission, PPAPElement, PPAPApproval, PPAP_ELEMENTLERI,
    PPAP_SUNUM_SEVIYELERI, PPAP_DURUMLARI, PPAP_ELEMENT_DURUMLARI, PPAP_ONAY_ADIMLARI,
    DOKUMAN_TIPLERI, DOKUMAN_SEVIYELERI, GUVENLIK_SINIFLARI, DOKUMAN_DURUMLARI,
    KULLANICI_ROLLERI, TIP_KISALTMALARI,
    DOKUMAN_SABLONLARI, STANDARTLAR, STANDART_MADDELERI,
    CorrectiveAction, InternalAudit, AuditFinding, TrainingRecord,
    RiskRegisterEntry, DistributionGroup, DistributionGroupMember,
    CAPA_KAYNAKLARI, CAPA_DURUMLARI, AUDIT_DURUMLARI, AUDIT_TIPLERI, BULGU_TIPLERI,
    EGITIM_TIPLERI, RISK_KATEGORILERI, RISK_DURUMLARI, DEGISIKLIK_KATEGORILERI,
    RecordDisposal, IMHA_DURUMLARI, DOF_YONTEMLERI,
    QRQCItem, QRQC_KATEGORILERI, QRQC_SEVIYELERI, QRQC_DURUMLARI, QRQC_VARDIYALAR,
)
from helpers import (
    allowed_file, file_ext, is_unsafe_inline, save_document_file, save_generic_file,
    file_sha256, resolve_file_path, document_dir_path, create_approval_steps, document_storage_dir,
    PREVIEW_MIME_MAP, PREVIEWABLE_EXTS, next_sequence_no, next_document_number,
)
from sqlalchemy.exc import IntegrityError
from translations import _, translate_enum, slug, get_locale
import notifications
import search_index
import authz
import onizleme_pdf  # 2026 — Office dosyalarının PDF önizlemesi
import dokuman_atif  # 2026 — doküman atıf analizi (nerede kullanılıyor / kırık atıflar)


def _resolve_dokuman_no(db, raw_no, surec_id, dokuman_tipi):
    """Eksik/önek numarayı (ör. D02-PR-) bir sonraki benzersiz değere tamamlar."""
    no = (raw_no or "").strip()
    tip_kisa = TIP_KISALTMALARI.get(dokuman_tipi or "")
    surec = db.get(Process, surec_id) if surec_id else None

    needs_seq = (not no) or no.endswith("-")
    # Şirket biçimi (DYS_NUMARA_FORMATI=debak): "D04 P" gibi numarasız önek de tamamlanır
    if not needs_seq and Config.NUMARA_FORMATI == "debak" and re.fullmatch(r"[DMY]\d\d(\.\d)?\s?[A-Z]", no):
        needs_seq = True
    if not needs_seq and tip_kisa and surec:
        # Önekle biten ama sayısal sıra yoksa (D02-PR) da tamamla
        prefix = f"{surec.kod}-{tip_kisa}-"
        if no == prefix.rstrip("-") or no == prefix:
            needs_seq = True
        else:
            try:
                int(no.rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                if no.startswith(f"{surec.kod}-{tip_kisa}"):
                    needs_seq = True

    if needs_seq and surec and tip_kisa:
        return next_document_number(db, surec, tip_kisa)
    return no

def _parse_icerik(form):
    """Formdan gelen icerik_* alanlarını JSON string'e çevirir."""
    icerik = {}
    for key in form:
        if key.startswith("icerik_"):
            bolum = key[len("icerik_"):]
            deger = form.get(key, "").strip()
            if deger:
                icerik[bolum] = deger
    return json.dumps(icerik, ensure_ascii=False) if icerik else None


def _to_int(value, default=None):
    """Güvenli int dönüşümü (hatalı girişte default döner)."""
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (ValueError, TypeError):
        return default


def _to_date(value, default=None):
    """Güvenli ISO tarih dönüşümü."""
    try:
        if not value:
            return default
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return default


def _sifre_gecerli_mi(sifre):
    """Minimum uzunluk + harf & rakam içerir mi kontrolü. (gecerli, mesaj) döner."""
    if len(sifre) < Config.MIN_PASSWORD_LENGTH:
        return False, f"Şifre en az {Config.MIN_PASSWORD_LENGTH} karakter olmalıdır."
    if not any(c.isalpha() for c in sifre) or not any(c.isdigit() for c in sifre):
        return False, "Şifre hem harf hem rakam içermelidir."
    return True, ""


# ── Loglama ────────────────────────────────────────────────────────
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_log_handler = RotatingFileHandler(
    os.path.join(_LOG_DIR, "dys.log"), maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(message)s"
))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
logger = logging.getLogger("dys")


# ── Flask App ──────────────────────────────────────────────────────
app = Flask(__name__)

# Secret key: ortam değişkeninden; yoksa rastgele üret + uyar.
if Config.SECRET_KEY:
    app.secret_key = Config.SECRET_KEY
else:
    app.secret_key = secrets.token_hex(32)
    logger.warning(
        "DYS_SECRET_KEY tanımlı degil; gecici rastgele anahtar uretildi. "
        "Uretimde DYS_SECRET_KEY ortam degiskenini ayarlayin (aksi halde her "
        "yeniden baslatmada oturumlar gecersiz olur)."
    )

app.config.update(
    MAX_CONTENT_LENGTH=Config.MAX_CONTENT_LENGTH,
    UPLOAD_FOLDER=Config.UPLOAD_FOLDER,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=Config.FORCE_HTTPS,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=Config.SESSION_LIFETIME_HOURS),
)

for _folder in (Config.UPLOAD_FOLDER, Config.get_temp_folder()):
    try:
        os.makedirs(_folder, exist_ok=True)
    except OSError:
        pass


# ── CSRF Koruması ──────────────────────────────────────────────────
from flask_wtf.csrf import CSRFProtect, CSRFError
csrf = CSRFProtect(app)


# ── Oran Sınırlama (kaba kuvvet saldırısına karşı) ──────────────────
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri=Config.RATE_LIMIT_STORAGE,
)


# ── Reverse Proxy Prefix Middleware ────────────────────────────────
# IIS ARR arkasinda /dys prefix ile calisirken URL'leri dogru olusturur.
_DYS_PREFIX = Config.PREFIX


class PrefixMiddleware:
    """IIS Application veya Waitress için URL öneki (/dys).

    PATH_INFO önekini soyar; SCRIPT_NAME ayarlar (url_for doğru üretir).
    """

    def __init__(self, wsgi_app, prefix=""):
        self.wsgi_app = wsgi_app
        self.prefix = (prefix or "").rstrip("/")

    def __call__(self, environ, start_response):
        if self.prefix:
            path = environ.get("PATH_INFO", "") or ""
            if path == self.prefix or path.startswith(self.prefix + "/"):
                environ["SCRIPT_NAME"] = (environ.get("SCRIPT_NAME") or "") + self.prefix
                environ["PATH_INFO"] = path[len(self.prefix):] or "/"
        return self.wsgi_app(environ, start_response)


if _DYS_PREFIX:
    app.wsgi_app = PrefixMiddleware(app.wsgi_app, prefix=_DYS_PREFIX)


_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = Config.UPLOAD_FOLDER


# ── Güvenlik Başlıkları ────────────────────────────────────────────
@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # PDF.js/SheetJS/Mammoth CDN'leri ve inline stil/scriptlere izin veren CSP
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' data: https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
        "img-src 'self' data: blob:; "
        "worker-src 'self' blob:; "
        "object-src 'self' blob:; "
        "frame-src 'self' blob:; "
        "connect-src 'self' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net"
    )
    return response


# ── Hata Yönetimi ──────────────────────────────────────────────────
@app.errorhandler(CSRFError)
def _handle_csrf(e):
    flash("Güvenlik doğrulaması başarısız (CSRF). Lütfen sayfayı yenileyip tekrar deneyin.", "error")
    return redirect(request.referrer or url_for("dashboard")), 400


@app.errorhandler(403)
def _handle_403(e):
    return render_template("error.html", code=403,
                           mesaj="Bu sayfaya erişim yetkiniz yok."), 403


@app.errorhandler(404)
def _handle_404(e):
    return render_template("error.html", code=404,
                           mesaj="Aradığınız sayfa bulunamadı."), 404


@app.errorhandler(413)
def _handle_413(e):
    return render_template("error.html", code=413,
                           mesaj="Yüklenen dosya izin verilen boyutu aşıyor."), 413


@app.errorhandler(429)
def _handle_429(e):
    return render_template("error.html", code=429,
                           mesaj="Çok fazla istek gönderdiniz. Lütfen kısa bir süre sonra tekrar deneyin."), 429


@app.errorhandler(500)
def _handle_500(e):
    logger.exception("Sunucu hatası (500): %s", request.path)
    return render_template("error.html", code=500,
                           mesaj="Beklenmeyen bir sunucu hatası oluştu."), 500


# ── Helper: DB Session ─────────────────────────────────────────────
def get_db():
    return SessionLocal()


# ── Helper: Login Required ─────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("rol") not in ("Admin", "Doküman Kontrol"):
            flash("Bu işlem için yetkiniz bulunmamaktadır.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


def admin_only(f):
    """Yalnızca Admin rolüne izin verir (kullanıcı silme, erişim kayıtları)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("rol") != "Admin":
            flash("Bu bölüme yalnızca yöneticiler erişebilir.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


# ── Faz 0: Her istekte oturum/rol doğrulama ────────────────────────
# Kimliği doğrulanmış her istekte kullanıcı DB'den yeniden doğrulanır; pasife
# alınan/silinen kullanıcının oturumu düşer ve rol değişimi anında yansır.
_PUBLIC_ENDPOINTS = {
    "login", "logout", "static", "set_locale", "healthz", "health_check",
    "documents._platform_ping",
}

# Salt-okunur rol için izin verilen mutasyon (POST) uçları.
_READ_ONLY_ALLOWED_ENDPOINTS = {"logout", "set_locale", "api_mark_read", "mark_distribution_read"}


@app.before_request
def _assign_correlation_id():
    """Her isteğe izlenebilirlik için bir correlation ID atar."""
    g.request_id = request.headers.get("X-Request-ID") or secrets.token_hex(8)


@app.after_request
def _observability_headers(response):
    """Correlation ID'yi yanıta ekler ve isteği yapılandırılmış biçimde loglar."""
    rid = getattr(g, "request_id", "-")
    response.headers["X-Request-ID"] = rid
    try:
        logger.info(
            "req id=%s method=%s path=%s status=%s user=%s",
            rid, request.method, request.path, response.status_code,
            session.get("user_id", "-"),
        )
    except Exception:  # loglama isteği asla bozmamalı
        pass
    return response


@app.before_request
def _revalidate_session():
    g.current_user = None
    endpoint = request.endpoint
    if endpoint in _PUBLIC_ENDPOINTS:
        return
    if "user_id" not in session:
        return  # login_required ilgili uçta yönlendirir

    db = get_db()
    try:
        user = db.get(User, session["user_id"])
        if user is None or not user.aktif:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "error": "unauthorized"}), 401
            flash("Oturumunuz sonlandırıldı. Lütfen tekrar giriş yapın.", "error")
            return redirect(url_for("login"))

        # Redis oturum iptal listesi (rol/aktif değişiminde admin tarafından işaretlenir)
        from redis_client import is_session_revoked
        if is_session_revoked(user.id):
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "error": "unauthorized"}), 401
            flash("Oturumunuz sonlandırıldı. Lütfen tekrar giriş yapın.", "error")
            return redirect(url_for("login"))

        # Rol/departman değişikliklerini oturuma yansıt.
        session["rol"] = user.rol
        session["departman"] = user.departman
        g.current_user = user

        # Salt-okunur rol: güvenli olmayan HTTP metodlarını engelle.
        if authz.is_read_only(user.rol) and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if endpoint not in _READ_ONLY_ALLOWED_ENDPOINTS:
                if request.path.startswith("/api/"):
                    return jsonify({"success": False, "error": "forbidden"}), 403
                flash("Sadece görüntüleme yetkiniz var; bu işlem engellendi.", "error")
                return redirect(request.referrer or url_for("dashboard"))
    finally:
        db.close()


# ── Helper: Audit Log (append-only, hash-zincirli) ─────────────────
def _audit_record_hash(onceki_hash, kullanici_id, islem_tipi, document_id, detay, tarih):
    """Tahrifat tespiti için kayıt özeti üretir (önceki hash'e zincirlenir)."""
    ham = f"{onceki_hash or ''}|{kullanici_id}|{islem_tipi}|{document_id}|{detay or ''}|{tarih.isoformat()}"
    return hashlib.sha256(ham.encode("utf-8")).hexdigest()


def log_action(db, islem_tipi, document_id=None, detay=None):
    tarih = datetime.now()
    kullanici_id = session.get("user_id")
    # Zincirin son halkasını al ve yeni kaydı ona bağla.
    son = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
    onceki_hash = son.kayit_hash if son else None
    log = AuditLog(
        kullanici_id=kullanici_id,
        islem_tipi=islem_tipi,
        document_id=document_id,
        detay=detay,
        ip_adresi=request.remote_addr,
        tarih=tarih,
        onceki_hash=onceki_hash,
        kayit_hash=_audit_record_hash(onceki_hash, kullanici_id, islem_tipi,
                                      document_id, detay, tarih),
    )
    db.add(log)
    db.commit()


# ── Doküman bütünlük doğrulaması (Faz 3) ───────────────────────────
def verify_document_integrity(db, doc):
    """Onaylı dokümanın dosyası, onaylanan revizyonun hash'i ile uyuşuyor mu?

    Döner: True (uyumlu / doğrulanamıyor-atlanır) veya False (bütünlük ihlali).
    İhlal durumunda değiştirilemez audit'e güvenlik olayı yazılır.
    """
    if not doc or doc.durum != "Onaylı":
        return True
    rev = db.query(DocumentRevision).filter_by(
        document_id=doc.id, revizyon_no=doc.revizyon_no
    ).order_by(DocumentRevision.id.desc()).first()
    if not rev or not rev.icerik_hash:
        return True  # kanıt hash'i yok → doğrulanamaz, atlanır
    mevcut = file_sha256(resolve_file_path(doc.dosya_yolu, doc.dosya_adi))
    if mevcut and mevcut != rev.icerik_hash:
        log_action(db, "Bütünlük İhlali", document_id=doc.id,
                   detay=f"Dosya hash uyumsuz: {doc.dokuman_no} "
                         f"(beklenen {rev.icerik_hash[:12]}…, bulunan {mevcut[:12]}…)")
        return False
    return True


def _can_fetch_master_file():
    return session.get("rol") in ("Admin", "Doküman Kontrol")


def _cleanup_temp_file(path):
    @after_this_request
    def _remove(response):
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError as error:
            logger.exception("Filigranlı geçici dosya silinemedi: %s", error)
        return response
    return _remove


def _send_issued_copy(doc, file_path, *, purpose, as_attachment=True, inline=False):
    """Orijinali paylaşımda bırakır; dağıtım kopyasına filigran basar.

    Çıktı (cikti) her zaman PDF döner. Diğer amaçlarda native filigran yoksa None.
    """
    import watermark as wm

    spec = wm.build_spec_for_document(
        doc,
        user_name=session.get("ad_soyad") or session.get("eposta") or "",
        purpose=purpose,
    )
    ext = os.path.splitext(doc.dosya_adi or file_path or "")[1].lower()
    temp_dir = Config.get_temp_folder()
    os.makedirs(temp_dir, exist_ok=True)

    if purpose == "cikti":
        pdf_path = wm.render_print_pdf(
            file_path,
            spec,
            temp_dir,
            title=getattr(doc, "baslik", "") or "",
            source_name=getattr(doc, "dosya_adi", "") or os.path.basename(file_path or ""),
        )
        if not pdf_path:
            return None
        _cleanup_temp_file(pdf_path)
        return send_file(
            pdf_path,
            as_attachment=as_attachment and not inline,
            download_name=wm.issued_copy_filename(doc, ext="pdf"),
            mimetype="application/pdf",
        )

    watermarked = wm.apply_issued_copy(file_path, spec, temp_dir, ext=ext)
    if not watermarked:
        return None
    _cleanup_temp_file(watermarked)
    download_name = wm.issued_copy_filename(doc, ext=ext.lstrip("."))
    mime = PREVIEW_MIME_MAP.get(ext, "application/octet-stream")
    return send_file(
        watermarked,
        as_attachment=as_attachment and not inline,
        download_name=download_name,
        mimetype=mime,
    )


# ── Bildirim Yardımcıları ──────────────────────────────────────────
def _notify_pending(db, doc):
    """Onaya gönderilen doküman için ilgili onaylayıcılara e-posta gönderir."""
    try:
        ids = {doc.kontrol_eden_id, doc.onaylayan_id}
        ids.discard(None)
        approvers = db.query(User).filter(User.id.in_(ids)).all() if ids else []
        notifications.notify_submitted_for_approval(doc, approvers)
    except Exception:
        logger.exception("Onay bildirimi gönderilemedi (doc_id=%s)", getattr(doc, "id", None))


def send_daily_digest():
    """Gecikmiş gözden geçirmeler + 3+ gün bekleyen onaylar için özet e-postası."""
    db = get_db()
    try:
        bugun = date.today()
        geciken_gozden = db.query(Document).filter(
            Document.sonraki_gozden_gecirme.isnot(None),
            Document.sonraki_gozden_gecirme < bugun,
            Document.durum == "Onaylı",
        ).all()
        esik = datetime.now() - timedelta(days=3)
        bekleyen_onaylar = db.query(DocumentApproval).filter(
            DocumentApproval.durum == "Bekliyor",
            DocumentApproval.tarih < esik,
        ).all()
        geciken_dof = [k for k in db.query(CorrectiveAction).all() if k.gecikti_mi]

        if not (geciken_gozden or bekleyen_onaylar or geciken_dof):
            logger.info("Günlük özet: bildirilecek gecikme yok.")
            return

        satirlar = ["DYS Günlük Özet", "=" * 30, ""]
        satirlar.append(f"Gecikmiş gözden geçirme: {len(geciken_gozden)}")
        for d in geciken_gozden[:20]:
            satirlar.append(f"  - {d.dokuman_no}: {d.sonraki_gozden_gecirme}")
        satirlar.append("")
        satirlar.append(f"3+ gün bekleyen onay: {len(bekleyen_onaylar)}")
        satirlar.append("")
        satirlar.append(f"Gecikmiş DÖF: {len(geciken_dof)}")
        for k in geciken_dof[:20]:
            satirlar.append(f"  - {k.dof_no}: {k.baslik}")
        govde = "\n".join(satirlar)

        alicilar = [u.eposta for u in db.query(User).filter_by(aktif=True).filter(
            User.rol.in_(["Admin", "Doküman Kontrol"])
        ).all() if u.eposta]
        notifications.send_mail(alicilar, "[DYS] Günlük Özet", govde)
        logger.info("Günlük özet gönderildi (%d alıcı).", len(alicilar))
    except Exception:
        logger.exception("Günlük özet oluşturulamadı.")
    finally:
        db.close()


def send_capa_reminders():
    """Açık DÖF'ler için gecikme / yaklaşan kapanış hatırlatma e-postaları."""
    db = get_db()
    try:
        bugun = date.today()
        gun = max(0, int(Config.CAPA_REMINDER_DAYS or 7))
        yaklasan_esik = bugun + timedelta(days=gun)
        aciklar = db.query(CorrectiveAction).filter(
            CorrectiveAction.durum != "Kapatıldı",
            CorrectiveAction.planlanan_tarih.isnot(None),
        ).all()
        gonderilen = 0
        for capa in aciklar:
            # İlişkileri yükle
            _ = capa.sorumlu, capa.acan
            if capa.gecikti_mi:
                if notifications.notify_capa_reminder(capa, "geciken"):
                    gonderilen += 1
            elif bugun <= capa.planlanan_tarih <= yaklasan_esik:
                if notifications.notify_capa_reminder(capa, "yaklasan"):
                    gonderilen += 1
        logger.info("DÖF hatırlatma: %d e-posta gönderildi.", gonderilen)
        return gonderilen
    except Exception:
        logger.exception("DÖF hatırlatmaları gönderilemedi.")
        return 0
    finally:
        db.close()


def _capa_export_rows(db, durum=None, kaynak=None):
    """DÖF listesi Excel/PDF satırları."""
    query = db.query(CorrectiveAction)
    if durum:
        query = query.filter(CorrectiveAction.durum == durum)
    if kaynak:
        query = query.filter(CorrectiveAction.kaynak_tipi == kaynak)
    kayitlar = query.order_by(CorrectiveAction.olusturma_tarihi.desc()).all()
    kolonlar = [
        "DÖF No", "Başlık", "Kaynak", "Durum", "Sorumlu", "Yöntem",
        "Tespit", "Planlanan", "Kapanış", "Gecikti",
    ]
    satirlar = []
    for k in kayitlar:
        satirlar.append([
            k.dof_no,
            k.baslik,
            k.kaynak_tipi or "",
            k.durum or "",
            k.sorumlu.ad_soyad if k.sorumlu else "",
            k.kok_neden_yontemi or "",
            k.tespit_tarihi.strftime("%d.%m.%Y") if k.tespit_tarihi else "",
            k.planlanan_tarih.strftime("%d.%m.%Y") if k.planlanan_tarih else "",
            k.kapanma_tarihi.strftime("%d.%m.%Y") if k.kapanma_tarihi else "",
            "Evet" if k.gecikti_mi else "Hayır",
        ])
    return "DÖF Düzeltici Faaliyet Raporu", kolonlar, satirlar


# ── Context Processor (tüm template'lere pending_approvals gönder) ─
@app.context_processor
def inject_pending():
    if "user_id" in session:
        db = get_db()
        try:
            count = db.query(DocumentApproval).filter_by(durum="Bekliyor").count()
            return {"pending_approvals": count}
        finally:
            db.close()
    return {"pending_approvals": 0}


@app.template_filter("fromjson")
def _fromjson(value):
    """JSON string'i template içinde dict'e çevirir."""
    if not value:
        return {}
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return {}


@app.template_filter("procedure_refs")
def _procedure_refs(value):
    """Prosedür kayıt / ilgili doküman alanını listeye çevirir."""
    from procedure_docx import parse_procedure_refs
    return parse_procedure_refs(value)


# ── 11 süreçlik yapı (2026): dokümanlar arası gezinme ─────────────────────
_DOC_MAP = {"t": 0.0, "map": {}}
_KOD_RE = re.compile(r"(?<![\w.])([DMY]\d\d(?:\.\d)?\s(?:GT|[PATFK])\d{2})(?!\d)")


def _doc_map():
    """dokuman_no → (id, başlık, tip, durum); 60 sn önbellekli."""
    import time
    if time.time() - _DOC_MAP["t"] > 60:
        db = get_db()
        try:
            _DOC_MAP["map"] = {no: (i, b, t, d) for i, no, b, t, d in
                               db.query(Document.id, Document.dokuman_no, Document.baslik, Document.dokuman_tipi, Document.durum).all()}
        finally:
            db.close()
        _DOC_MAP["t"] = time.time()
    return _DOC_MAP["map"]


@app.template_filter("kodlari_bagla")
def _kodlari_bagla(text):
    """Metindeki doküman kodlarını (D04.5 T07 gibi) DYS doküman bağlantısına çevirir."""
    from markupsafe import Markup, escape
    if not text:
        return ""
    m = _doc_map()
    root = request.script_root

    s = str(escape(text))

    def rep(mo):
        k = mo.group(1)
        # "D06 P01 … (Rev. 7)" → eski yapıdaki doküman atfı: aynı numaralı YENİ dokümana değil, '(ESKİ)' kaydına bağla
        sonrasi = s[mo.end():mo.end() + 110].split("\n", 1)[0]
        nxt = _KOD_RE.search(sonrasi)
        if "(Rev." in (sonrasi[:nxt.start()] if nxt else sonrasi):
            d = m.get(f"{k} (ESKİ)")
            return f'<a href="{root}/documents/{d[0]}" title="Eski yapı: {escape(d[1])}" class="doc-code-link eski">{k}</a>' if d else k
        d = m.get(k)
        return f'<a href="{root}/documents/{d[0]}" title="{escape(d[1])}" class="doc-code-link">{k}</a>' if d else k
    return Markup(_KOD_RE.sub(rep, s))


def _esli_dokuman(doc, harf):
    """Prosedür ↔ iş akışı eşi: D04 P02 → D04 A02."""
    m = re.fullmatch(r"([DMY]\d\d(?:\.\d)?) [PA](\d{2})", doc.dokuman_no or "")
    if not m:
        return None
    d = _doc_map().get(f"{m.group(1)} {harf}{m.group(2)}")
    return {"id": d[0], "no": f"{m.group(1)} {harf}{m.group(2)}", "baslik": d[1]} if d else None


_FORM_KATALOG = {"yuklendi": False, "data": {}}


def _form_katalog():
    if not _FORM_KATALOG["yuklendi"]:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "form_katalogu.json")
        try:
            import json as _json
            _FORM_KATALOG["data"] = _json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
        except (OSError, ValueError):
            _FORM_KATALOG["data"] = {}
        _FORM_KATALOG["yuklendi"] = True
    return _FORM_KATALOG["data"]


def _form_islem(doc):
    """Form dokümanının DYS'de nerede doldurulacağı: dinamik form tanımı ya da ilgili modül ekranı."""
    if not doc or doc.dokuman_tipi != "Form":
        return None
    k = _form_katalog().get(doc.dokuman_no)
    if k and k.get("sekil") in ("Şablon", "DYS dışı"):
        # Şablon: DYS'de güncel şablon indirilir / yazdırılır, doldurulmuş hali DYS'de tutulmaz (ör. D01 iş sözleşmesi — KVKK)
        # DYS dışı: kaydı başka sistemde tutulur (ör. puantaj → bordro programı)
        return {"tur": "sablon" if k["sekil"] == "Şablon" else "disarida", "url": "", "ad": k.get("modul") or k["sekil"], "not": k.get("not") or ""}
    if k and k.get("sekil") == "DYS modülü" and (k.get("ekran") or "").startswith("/"):
        # kaydı bir DYS modülünde tutulan form (ör. D05 F04 → Çevre Boyut & Etki) — dinamik form taslağı olsa da modül öncelikli
        return {"tur": "modul", "url": f"{request.script_root}{k['ekran']}", "ad": k.get("modul")}
    from models import FormTanim
    db = get_db()
    try:
        t = db.query(FormTanim).filter(FormTanim.form_kodu == doc.dokuman_no).first()
        if t:
            return {"tur": "dinamik", "url": f"{request.script_root}/forms/{t.id}/new", "detay": f"{request.script_root}/forms/{t.id}", "ad": "Dinamik Form"}
    finally:
        db.close()
    k = _form_katalog().get(doc.dokuman_no)
    if k and k.get("ekran") and k["ekran"].startswith("/"):
        return {"tur": "modul", "url": f"{request.script_root}{k['ekran']}", "ad": k.get("modul")}
    return None


app.jinja_env.globals["esli_dokuman"] = _esli_dokuman
app.jinja_env.globals["form_islem"] = _form_islem


@app.template_filter("doc_dir")
def _doc_dir(doc):
    """Dokümanın bulunduğu klasör yolunu şablonda gösterir."""
    return document_dir_path(getattr(doc, "dosya_yolu", None), getattr(doc, "dosya_adi", None))


# i18n yardımcılarını şablonlara aç
app.jinja_env.globals["_"] = _
app.jinja_env.globals["get_locale"] = get_locale
app.jinja_env.filters["enum"] = translate_enum
app.jinja_env.filters["slug"] = slug


@app.context_processor
def inject_globals():
    """Şablon tanımlarını ve rol bilgisini tüm template'lere aktarır."""
    return {
        "DOKUMAN_SABLONLARI": DOKUMAN_SABLONLARI,
        "is_admin": session.get("rol") == "Admin",
        "is_editor": session.get("rol") in ("Admin", "Doküman Kontrol"),
        "current_locale": get_locale(),
    }


@app.route("/set-locale/<locale>")
def set_locale(locale):
    """Arayüz dilini değiştirir (tr/en)."""
    if locale in Config.SUPPORTED_LOCALES:
        session["locale"] = locale
    return redirect(request.referrer or url_for("dashboard"))


# ── Health Check Endpoint (Hub durum kontrolü için) ────────────────
@app.route("/health")
def health_check():
    """Hub landing page servis durum kontrolü için."""
    return jsonify({"status": "ok", "service": "dys"})


# ═══════════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ═══════════════════════════════════════════════════════════════════
@app.route("/login", methods=["GET", "POST"])
@limiter.limit(Config.LOGIN_RATELIMIT, methods=["POST"])
def login():
    if request.method == "POST":
        eposta = request.form.get("eposta", "").strip()
        sifre = request.form.get("sifre", "")
        db = get_db()
        try:
            user = db.query(User).filter_by(eposta=eposta, aktif=True).first()

            # Hesap geçici olarak kilitli mi?
            if user and user.kilit_tarihi and user.kilit_tarihi > datetime.now():
                kalan = int((user.kilit_tarihi - datetime.now()).total_seconds() // 60) + 1
                flash(f"Çok fazla hatalı deneme. Hesabınız {kalan} dakika süreyle kilitlendi.", "error")
                logger.warning("Kilitli hesaba giris denemesi: %s", eposta)
                return render_template("login.html")

            if user and check_password_hash(user.sifre_hash, sifre):
                user.basarisiz_giris_sayisi = 0
                user.kilit_tarihi = None
                db.commit()
                session.permanent = True
                session["user_id"] = user.id
                session["ad_soyad"] = user.ad_soyad
                session["rol"] = user.rol
                session["departman"] = user.departman
                from redis_client import clear_session_revoke
                clear_session_revoke(user.id)
                log_action(db, "Giriş", detay=f"{user.ad_soyad} giriş yaptı")
                return redirect(url_for("dashboard"))
            else:
                if user:
                    user.basarisiz_giris_sayisi = (user.basarisiz_giris_sayisi or 0) + 1
                    if user.basarisiz_giris_sayisi >= Config.MAX_LOGIN_ATTEMPTS:
                        user.kilit_tarihi = datetime.now() + timedelta(minutes=Config.LOGIN_LOCK_MINUTES)
                        user.basarisiz_giris_sayisi = 0
                        flash(f"Çok fazla hatalı deneme. Hesabınız {Config.LOGIN_LOCK_MINUTES} dakika kilitlendi.", "error")
                    else:
                        kalan = Config.MAX_LOGIN_ATTEMPTS - user.basarisiz_giris_sayisi
                        flash(f"E-posta veya şifre hatalı. ({kalan} deneme hakkınız kaldı)", "error")
                    db.commit()
                    logger.warning("Basarisiz giris: %s", eposta)
                else:
                    flash("E-posta veya şifre hatalı.", "error")
        finally:
            db.close()
    return render_template("login.html")


@app.route("/logout")
def logout():
    db = get_db()
    try:
        log_action(db, "Çıkış", detay=f"{session.get('ad_soyad', '')} çıkış yaptı")
    finally:
        db.close()
    session.clear()
    return redirect("/")


# ═══════════════════════════════════════════════════════════════════
#  DASHBOARD
# ═══════════════════════════════════════════════════════════════════
@app.route("/healthz")
def healthz():
    """DB bağlantısını doğrulayan hazır olma (readiness) ucu."""
    from sqlalchemy import text
    db = get_db()
    try:
        db.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "db": "up", "request_id": getattr(g, "request_id", "-")})
    except Exception as exc:  # pragma: no cover - hata yolu
        logger.error("healthz DB kontrolü başarısız: %s", exc)
        return jsonify({"status": "error", "db": "down"}), 503
    finally:
        db.close()


@app.route("/search")
@login_required
def global_search():
    """Modüller arası birleşik arama (yetki filtreli)."""
    from models import (
        Supplier, NonconformingProduct, InformationAsset, OHSHazard,
        EnvironmentalAspect,
    )
    q = (request.args.get("q") or request.args.get("search") or "").strip()
    gruplar = []
    if not q:
        return render_template("search_results.html", q=q, gruplar=gruplar, toplam=0)

    like = f"%{q}%"
    db = get_db()
    try:
        # Dokümanlar (erişim politikası uygulanır)
        docs = db.query(Document).filter(
            (Document.baslik.ilike(like)) | (Document.dokuman_no.ilike(like))
        ).limit(50).all()
        docs = authz.accessible_documents(db, session.get("user_id"), session.get("rol"), docs)
        if docs:
            gruplar.append({"baslik": "Dokümanlar", "ikon": "📄", "kayitlar": [
                {"ad": f"{d.dokuman_no} — {d.baslik}", "url": f"/documents/{d.id}"}
                for d in docs[:15]]})

        # DÖF
        capalar = db.query(CorrectiveAction).filter(
            (CorrectiveAction.dof_no.ilike(like)) | (CorrectiveAction.baslik.ilike(like))
        ).limit(15).all()
        if capalar:
            gruplar.append({"baslik": "DÖF", "ikon": "🛠️", "kayitlar": [
                {"ad": f"{c.dof_no} — {c.baslik}", "url": f"/capa/{c.id}"} for c in capalar]})

        # İç tetkik
        audits = db.query(InternalAudit).filter(
            (InternalAudit.tetkik_no.ilike(like)) | (InternalAudit.baslik.ilike(like))
        ).limit(15).all()
        if audits:
            gruplar.append({"baslik": "İç Tetkik", "ikon": "🕵️", "kayitlar": [
                {"ad": f"{a.tetkik_no} — {a.baslik}", "url": f"/audits/{a.id}"} for a in audits]})

        # Risk kaydı
        riskler = db.query(RiskRegisterEntry).filter(
            (RiskRegisterEntry.risk_no.ilike(like)) | (RiskRegisterEntry.tanim.ilike(like))
        ).limit(15).all()
        if riskler:
            gruplar.append({"baslik": "Risk Kaydı", "ikon": "⚠️", "kayitlar": [
                {"ad": f"{r.risk_no} — {(r.tanim or '')[:60]}", "url": "/risks"} for r in riskler]})

        # Tedarikçiler
        sups = db.query(Supplier).filter(
            (Supplier.ad.ilike(like)) | (Supplier.kod.ilike(like))
        ).limit(15).all()
        if sups:
            gruplar.append({"baslik": "Tedarikçiler", "ikon": "🚚", "kayitlar": [
                {"ad": f"{s.kod} — {s.ad}", "url": "/iatf/suppliers"} for s in sups]})

        # Uygun olmayan ürün
        ncs = db.query(NonconformingProduct).filter(
            (NonconformingProduct.nc_no.ilike(like)) | (NonconformingProduct.parca_no.ilike(like))
        ).limit(15).all()
        if ncs:
            gruplar.append({"baslik": "Uygun Olmayan Ürün", "ikon": "🚧", "kayitlar": [
                {"ad": f"{n.nc_no} — {n.parca_no or ''}", "url": "/iatf/nonconforming"} for n in ncs]})

        # Bilgi varlıkları
        assets = db.query(InformationAsset).filter(
            (InformationAsset.ad.ilike(like)) | (InformationAsset.varlik_no.ilike(like))
        ).limit(15).all()
        if assets:
            gruplar.append({"baslik": "Bilgi Varlıkları", "ikon": "💾", "kayitlar": [
                {"ad": f"{a.varlik_no or ''} — {a.ad}", "url": "/isms/assets"} for a in assets]})

        # İSG tehlikeleri
        hazards = db.query(OHSHazard).filter(
            (OHSHazard.tehlike.ilike(like)) | (OHSHazard.faaliyet.ilike(like))
        ).limit(15).all()
        if hazards:
            gruplar.append({"baslik": "İSG Tehlikeleri", "ikon": "🩹", "kayitlar": [
                {"ad": f"{h.faaliyet} — {h.tehlike}", "url": "/ohs/hazards"} for h in hazards]})

        # Çevre boyutları
        aspects = db.query(EnvironmentalAspect).filter(
            (EnvironmentalAspect.boyut.ilike(like)) | (EnvironmentalAspect.faaliyet.ilike(like))
        ).limit(15).all()
        if aspects:
            gruplar.append({"baslik": "Çevre Boyutları", "ikon": "🌱", "kayitlar": [
                {"ad": f"{a.faaliyet} — {a.boyut}", "url": "/env/aspects"} for a in aspects]})

        toplam = sum(len(g["kayitlar"]) for g in gruplar)
        return render_template("search_results.html", q=q, gruplar=gruplar, toplam=toplam)
    finally:
        db.close()


@app.route("/")
@login_required
def dashboard():
    db = get_db()
    try:
        documents = db.query(Document).all()
        processes = db.query(Process).all()

        # Stats
        bugun = date.today()
        stats = {
            "toplam_dokuman": len(documents),
            "onayli": sum(1 for d in documents if d.durum == "Onaylı"),
            "bekleyen_onay": db.query(DocumentApproval).filter_by(durum="Bekliyor").count(),
            "gozden_gecirme": sum(
                1 for d in documents
                if d.sonraki_gozden_gecirme and d.sonraki_gozden_gecirme <= bugun + timedelta(days=30)
            ),
            "gecikmis": sum(
                1 for d in documents
                if d.durum == "Onaylı" and d.sonraki_gozden_gecirme
                and d.sonraki_gozden_gecirme < bugun
            ),
            "surec_sayisi": len(processes),
            "acik_dof": sum(1 for k in db.query(CorrectiveAction).all() if k.durum != "Kapatıldı"),
            "acik_risk": db.query(RiskRegisterEntry).filter(RiskRegisterEntry.durum != "Kapatıldı").count(),
            "planli_tetkik": db.query(InternalAudit).filter(
                InternalAudit.durum.in_(("Planlandı", "Devam Ediyor"))
            ).count(),
        }

        # Status data for chart
        status_data = {}
        for d in documents:
            status_data[d.durum] = status_data.get(d.durum, 0) + 1

        # Process data for chart
        process_data = {}
        for p in processes:
            process_data[p.kod] = len(p.documents)

        # Standard data for chart
        standard_data = {"IATF 16949": 0, "ISO 14001": 0, "ISO 45001": 0, "ISO 27001": 0}
        for d in documents:
            if d.ilgili_standartlar:
                for std_key in standard_data:
                    if std_key in d.ilgili_standartlar or std_key.split()[-1] in d.ilgili_standartlar:
                        standard_data[std_key] += 1

        # Recent documents
        recent_documents = sorted(documents, key=lambda x: x.olusturma_tarihi or datetime.min, reverse=True)[:5]

        # Pending approvals
        pending_approval_docs = db.query(DocumentApproval).filter_by(durum="Bekliyor").all()

        # Rol/göreve göre kişiselleştirilmiş görevler ("Bana atanan")
        uid = session.get("user_id")
        benim = {
            "onaylarim": db.query(DocumentApproval).filter_by(
                durum="Bekliyor", kullanici_id=uid).count(),
            "acik_dof": db.query(CorrectiveAction).filter(
                CorrectiveAction.sorumlu_id == uid,
                CorrectiveAction.durum != "Kapatıldı").count(),
            "okunmamis": db.query(DocumentDistribution).filter_by(
                kullanici_id=uid, okundu_mu=False).count(),
        }
        try:  # 2026 — tüm bekleyen işlerin toplamı (İşlerim)
            import dms_routes
            benim["toplam"] = dms_routes.is_sayisi(db, uid, session.get("rol"))
        except Exception:
            logger.exception("İşlerim sayacı hesaplanamadı")
            benim["toplam"] = None

        # Upcoming reviews (next 30 days)
        upcoming_reviews = []
        for d in documents:
            if d.sonraki_gozden_gecirme and d.durum == "Onaylı":
                remaining = (d.sonraki_gozden_gecirme - date.today()).days
                if remaining <= 60:
                    upcoming_reviews.append((d, remaining))
        upcoming_reviews.sort(key=lambda x: x[1])

        return render_template("dashboard.html",
            stats=stats,
            status_data=status_data,
            process_data=process_data,
            standard_data=standard_data,
            recent_documents=recent_documents,
            pending_approval_docs=pending_approval_docs,
            upcoming_reviews=upcoming_reviews,
            benim=benim,
        )
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  PROCESSES
# ═══════════════════════════════════════════════════════════════════
_SUREC_TIPLERI = ("Prosedür", "İş Akışı", "Talimat", "Form")  # süreç haritasında ayrı sayılan doküman tipleri
_SUREC_YASAM_DISI = ("Eskimiş", "İptal")


def _surec_sayilari(db, surec_idleri):
    """{surec_id: {tip: n, 'Diğer': n, 'toplam': n}} — yürürlükteki (eskimiş/iptal dışı) dokümanlar."""
    from sqlalchemy import func
    sonuc = {i: {**{t: 0 for t in _SUREC_TIPLERI}, "Diğer": 0, "toplam": 0} for i in surec_idleri}
    if not surec_idleri:
        return sonuc
    q = (db.query(Document.surec_id, Document.dokuman_tipi, func.count(Document.id))
         .filter(Document.surec_id.in_(list(surec_idleri)), ~Document.durum.in_(_SUREC_YASAM_DISI))
         .group_by(Document.surec_id, Document.dokuman_tipi))
    for sid, tip, n in q.all():
        d = sonuc[sid]
        d[tip if tip in _SUREC_TIPLERI else "Diğer"] += n
        d["toplam"] += n
    return sonuc


@app.route("/processes")
@login_required
def processes():
    """Süreç haritası: yalnız aktif ana süreçler + aktif alt süreçleri (eski/pasif süreçler görünmez)."""
    db = get_db()
    try:
        tum = [p for p in db.query(Process).order_by(Process.kod).all() if p.aktif_mi]
        ust = [p for p in tum if p.ust_surec_id is None]
        alt_map = {}
        for p in tum:
            if p.ust_surec_id is not None:
                alt_map.setdefault(p.ust_surec_id, []).append(p)
        sayilar = _surec_sayilari(db, [p.id for p in tum])
        harita = {}
        for p in ust:
            alt = sorted(alt_map.get(p.id, []), key=lambda x: x.kod)
            toplam = {k: sayilar[p.id][k] + sum(sayilar[a.id][k] for a in alt) for k in sayilar[p.id]}
            harita[p.kod] = {"p": p, "alt": alt, "say": toplam, "alt_say": {a.id: sayilar[a.id] for a in alt}}
        return render_template("processes.html", harita=harita, tipler=_SUREC_TIPLERI)
    finally:
        db.close()


@app.route("/processes/<int:process_id>")
@login_required
def process_detail(process_id):
    db = get_db()
    try:
        proc = db.query(Process).get(process_id)
        if not proc:
            flash("Süreç bulunamadı.", "error")
            return redirect(url_for("processes"))

        # Dokümanlar sayfasındaki ile aynı filtreleme sistemi (tip, durum, güvenlik, arama).
        tip = request.args.get("tip")
        durum = request.args.get("durum")
        guvenlik = request.args.get("guvenlik")
        search = request.args.get("search", "").strip()

        # Ana süreçte alt süreçlerin dokümanları da listelenir (11 süreçlik yapı)
        alt_surecler = sorted([a for a in (proc.alt_surecler or []) if a.aktif_mi or Config.ESKI_SURECLERI_GOSTER or request.args.get("eski") == "1"], key=lambda p: p.kod)
        alt = request.args.get("alt")
        if alt:
            surec_ids = [int(alt)] if alt.isdigit() else [proc.id]
        else:
            surec_ids = [proc.id] + [a.id for a in alt_surecler]
        query = db.query(Document).filter(Document.surec_id.in_(surec_ids))
        if tip:
            query = query.filter(Document.dokuman_tipi == tip)
        if durum:
            query = query.filter(Document.durum == durum)
        elif not request.args.get("eskiler"):
            query = query.filter(~Document.durum.in_(("Eskimiş", "İptal")))
        if guvenlik:
            query = query.filter(Document.guvenlik_sinifi == guvenlik)
        if search:
            query = query.filter(
                (Document.baslik.ilike(f"%{search}%")) |
                (Document.dokuman_no.ilike(f"%{search}%"))
            )
        filtered_docs = query.order_by(Document.dokuman_no).all()

        all_processes = [p for p in db.query(Process).order_by(Process.kod).all() if p.aktif_mi or p.id == proc.id]
        tum_docs = db.query(Document).filter(Document.surec_id.in_([proc.id] + [a.id for a in alt_surecler])).all()
        # Bağlı DYS modülleri (data/modul_dokumanlari.json): dokümanı bu sürece / alt süreçlerine ait olan modüller
        kodlar = [proc.kod] + [a.kod for a in alt_surecler]
        moduller = []
        try:
            import modul_dokuman, modul_routes
            modul_dokuman._yukle()
            for m in modul_dokuman._ON["moduller"]:
                n = sum(1 for d in m.get("dokumanlar", []) if any((d.get("no") or "").startswith(k + " ") for k in kodlar))
                if n and modul_routes.modul_acik(m.get("id")):
                    moduller.append({"id": m["id"], "baslik": m.get("baslik") or m.get("eski_baslik"), "yol": m.get("yol"),
                                     "standart": m.get("standart"), "madde": m.get("madde"), "n": n})
        except Exception:
            logger.exception("Süreç modül bağlantıları okunamadı (process_id=%s)", process_id)
        sayilar = _surec_sayilari(db, [proc.id] + [a.id for a in alt_surecler])
        toplam_say = {k: sum(v[k] for v in sayilar.values()) for k in sayilar[proc.id]}
        # Tipe göre gruplu liste (sabit sıra; tanımsız tipler sonda)
        TIP_SIRA = ["Politika", "El Kitabı", "Prosedür", "İş Akışı", "Talimat", "Form", "Görev Tanımı"]
        gruplar = {}
        for d in filtered_docs:
            gruplar.setdefault(d.dokuman_tipi or "Diğer", []).append(d)
        gruplar = sorted(gruplar.items(), key=lambda kv: (TIP_SIRA.index(kv[0]) if kv[0] in TIP_SIRA else 99, kv[0]))
        return render_template(
            "process_detail.html",
            process=proc, filtered_docs=filtered_docs, processes=all_processes,
            alt_surecler=alt_surecler, tum_docs=tum_docs, secili_alt=alt,
            moduller=moduller, toplam_say=toplam_say, alt_say=sayilar, gruplar=gruplar, tipler=_SUREC_TIPLERI,
        )
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  DOCUMENTS
# ═══════════════════════════════════════════════════════════════════
@app.route("/documents")
@login_required
def documents():
    db = get_db()
    try:
        query = db.query(Document)

        # Filters
        surec = request.args.get("surec")
        tip = request.args.get("tip")
        durum = request.args.get("durum")
        guvenlik = request.args.get("guvenlik")
        search = request.args.get("search", "").strip()

        if surec:
            query = query.filter(Document.surec_id == int(surec))
        if tip:
            query = query.filter(Document.dokuman_tipi == tip)
        if durum:
            query = query.filter(Document.durum == durum)
        if guvenlik:
            query = query.filter(Document.guvenlik_sinifi == guvenlik)
        if search:
            # Başlık/no LIKE aramasına ek olarak dosya içeriği (FTS5) araması.
            fts_ids = search_index.search_ids(search)
            kosul = (Document.baslik.ilike(f"%{search}%")) | \
                    (Document.dokuman_no.ilike(f"%{search}%")) | \
                    (Document.dosya_yolu.ilike(f"%{search}%"))
            if fts_ids:
                kosul = kosul | Document.id.in_(fts_ids)
            query = query.filter(kosul)

        docs = query.order_by(Document.dokuman_no).all()
        # Merkezî erişim politikası: gizli dokümanlar yetkisiz kullanıcılardan gizlenir.
        docs = authz.accessible_documents(db, session.get("user_id"), session.get("rol"), docs)
        procs = [p for p in db.query(Process).order_by(Process.kod).all() if p.aktif_mi or str(p.id) == (surec or "")]

        return render_template("documents.html", documents=docs, processes=procs)
    finally:
        db.close()


@app.route("/documents/<int:doc_id>")
@login_required
def document_detail(doc_id):
    db = get_db()
    try:
        doc = db.get(Document, doc_id)
        if not doc:
            flash("Doküman bulunamadı.", "error")
            return redirect(url_for("documents"))

        if not authz.can_access_document(db, session.get("user_id"), session.get("rol"), doc):
            log_action(db, "Görüntüleme", document_id=doc.id,
                       detay=f"Yetkisiz erişim reddedildi: {doc.dokuman_no}")
            abort(403)

        users = db.query(User).filter_by(aktif=True).all()
        dist_gruplar = db.query(DistributionGroup).order_by(DistributionGroup.ad).all()
        departmanlar = sorted({u.departman for u in users if u.departman})
        log_action(db, "Görüntüleme", document_id=doc.id, detay=f"Doküman görüntülendi: {doc.dokuman_no}")

        file_path_resolved = resolve_file_path(doc.dosya_yolu, doc.dosya_adi) if doc.dosya_yolu or doc.dosya_adi else None
        file_found = bool(file_path_resolved and os.path.isfile(file_path_resolved))

        import watermark as wm
        copy_spec = wm.build_spec_for_document(
            doc,
            user_name=session.get("ad_soyad") or "",
            purpose="onizleme",
        )
        # 2026 — Office dosyası: önbellekte PDF varsa ya da sunucu dönüştürebiliyorsa PDF görüntüleyici kullanılır
        ext_ = onizleme_pdf.uzanti(doc.dosya_adi)
        pdf_mod = bool(file_found and ext_ in onizleme_pdf.OFFICE_UZANTILARI and request.args.get("gorunum") != "ham"
                       and (onizleme_pdf.onbellekte(file_path_resolved) or onizleme_pdf.donusturucu_var()))
        # 2026 — atıf analizi: bu dokümana atıf yapanlar (revizyon etki analizi) ve bu dokümanın atıfları
        try:
            atif_yapanlar, atiflari = dokuman_atif.atif_yapanlar(db, doc.dokuman_no), dokuman_atif.atiflar(db, doc)
        except Exception:
            logger.exception("Atıf analizi yapılamadı (doc_id=%s)", doc.id)
            atif_yapanlar, atiflari = [], []
        return render_template(
            "document_detail.html",
            doc=doc,
            users=users,
            file_found=file_found,
            pdf_mod=pdf_mod,
            ham_gorunum_var=ext_ in ("xls", "xlsx", "csv", "docx"),
            dist_gruplar=dist_gruplar,
            departmanlar=departmanlar,
            copy_spec=copy_spec,
            can_fetch_master=_can_fetch_master_file(),
            atif_yapanlar=atif_yapanlar,
            atiflari=atiflari,
        )
    finally:
        db.close()


@app.route("/api/documents/next-number")
@login_required
def api_documents_next_number():
    """Süreç + tip için bir sonraki doküman numarasını döner (ör. D02-PR-003)."""
    surec_id = _to_int(request.args.get("surec_id"))
    tip = (request.args.get("tip") or "").strip()
    tip_kisa = TIP_KISALTMALARI.get(tip)
    if not surec_id or not tip_kisa:
        return jsonify({"error": "surec_id ve tip gerekli"}), 400
    db = get_db()
    try:
        surec = db.get(Process, surec_id)
        if not surec:
            return jsonify({"error": "Süreç bulunamadı"}), 404
        return jsonify({"dokuman_no": next_document_number(db, surec, tip_kisa)})
    finally:
        db.close()


@app.route("/api/documents/lookup")
@login_required
def api_documents_lookup():
    """Prosedür 6.0/7.0 seçicisi için aramalı doküman listesi."""
    q = (request.args.get("q") or "").strip()
    limit = min(_to_int(request.args.get("limit"), 25) or 25, 50)
    db = get_db()
    try:
        query = db.query(Document).order_by(Document.dokuman_no)
        if q:
            like = f"%{q}%"
            query = query.filter(or_(
                Document.dokuman_no.ilike(like),
                Document.baslik.ilike(like),
                Document.dokuman_tipi.ilike(like),
            ))
        docs = query.limit(80).all()
        docs = authz.accessible_documents(
            db, session.get("user_id"), session.get("rol"), docs
        )[:limit]
        return jsonify({
            "results": [
                {
                    "id": d.id,
                    "dokuman_no": d.dokuman_no,
                    "baslik": d.baslik,
                    "dokuman_tipi": d.dokuman_tipi,
                    "durum": d.durum,
                    "label": f"{d.dokuman_no} — {d.baslik}",
                }
                for d in docs
            ]
        })
    finally:
        db.close()


@app.route("/documents/new", methods=["GET", "POST"])
@login_required
def document_new():
    db = get_db()
    try:
        if request.method == "POST":
            file = request.files.get("dosya")
            if file and file.filename and not allowed_file(file.filename):
                flash(f"İzin verilmeyen dosya tipi: .{file_ext(file.filename)}", "error")
                procs = db.query(Process).order_by(Process.kod).all()
                users = db.query(User).filter_by(aktif=True).all()
                return render_template("document_form.html", doc=None, processes=procs, users=users)

            surec_id = _to_int(request.form.get("surec_id"))
            dokuman_tipi = request.form["dokuman_tipi"]
            dokuman_no = _resolve_dokuman_no(
                db, request.form.get("dokuman_no"), surec_id, dokuman_tipi
            )
            if not dokuman_no:
                flash("Doküman numarası oluşturulamadı. Süreç ve tip seçimini kontrol edin.", "error")
                procs = db.query(Process).order_by(Process.kod).all()
                users = db.query(User).filter_by(aktif=True).all()
                return render_template("document_form.html", doc=None, processes=procs, users=users)

            if db.query(Document).filter_by(dokuman_no=dokuman_no).first():
                flash(f"Bu doküman numarası zaten kullanılıyor: {dokuman_no}. Lütfen farklı bir numara girin.", "error")
                procs = db.query(Process).order_by(Process.kod).all()
                users = db.query(User).filter_by(aktif=True).all()
                return render_template("document_form.html", doc=None, processes=procs, users=users)

            doc = Document(
                dokuman_no=dokuman_no,
                baslik=request.form["baslik"],
                surec_id=surec_id,
                dokuman_tipi=dokuman_tipi,
                dokuman_seviyesi=_to_int(request.form.get("dokuman_seviyesi"), 2),
                guvenlik_sinifi=request.form.get("guvenlik_sinifi", "Genel"),
                ilgili_standartlar=request.form.get("ilgili_standartlar", ""),
                durum=request.form.get("durum", "Taslak"),
                hazirlayan_id=_to_int(request.form.get("hazirlayan_id"), session.get("user_id")),
                kontrol_eden_id=_to_int(request.form.get("kontrol_eden_id")),
                onaylayan_id=_to_int(request.form.get("onaylayan_id")),
                yururluk_tarihi=_to_date(request.form.get("yururluk_tarihi")),
                sonraki_gozden_gecirme=_to_date(request.form.get("sonraki_gozden_gecirme")),
                saklama_suresi_ay=_to_int(request.form.get("saklama_suresi_ay"), 36),
                revizyon_no=0,
                icerik_json=_parse_icerik(request.form),
                olusturma_tarihi=datetime.now(),
                guncelleme_tarihi=datetime.now(),
            )
            db.add(doc)
            try:
                db.flush()  # doc.id versiyonlu depolama yolu için gerekli
            except IntegrityError:
                db.rollback()
                flash(f"Bu doküman numarası zaten kullanılıyor: {dokuman_no}. Lütfen farklı bir numara girin.", "error")
                procs = db.query(Process).order_by(Process.kod).all()
                users = db.query(User).filter_by(aktif=True).all()
                return render_template("document_form.html", doc=None, processes=procs, users=users)

            dosya_adi = None
            dosya_yolu = None
            if file and file.filename:
                dosya_adi, dosya_yolu = save_document_file(file, doc.id, 0)
                doc.dosya_adi = dosya_adi
                doc.dosya_yolu = dosya_yolu
            elif doc.dokuman_tipi == "Prosedür" and doc.icerik_json:
                # Referans antetli prosedür DOCX'ini otomatik üret
                from procedure_docx import save_procedure_docx
                storage = document_storage_dir(doc.id, 0)
                os.makedirs(storage, exist_ok=True)
                safe_name = f"{secure_filename(doc.dokuman_no) or 'prosedur'}.docx"
                target = os.path.join(storage, safe_name)
                doc.surec = db.get(Process, doc.surec_id) if doc.surec_id else None
                doc.hazirlayan = db.get(User, doc.hazirlayan_id) if doc.hazirlayan_id else None
                doc.kontrol_eden = db.get(User, doc.kontrol_eden_id) if doc.kontrol_eden_id else None
                doc.onaylayan = db.get(User, doc.onaylayan_id) if doc.onaylayan_id else None
                try:
                    save_procedure_docx(doc, target)
                    dosya_adi = safe_name
                    dosya_yolu = target
                    doc.dosya_adi = dosya_adi
                    doc.dosya_yolu = dosya_yolu
                except Exception as exc:
                    app.logger.exception("Prosedür DOCX üretilemedi: %s", exc)
                    flash("Doküman kaydedildi; Word anteti oluşturulamadı. İndirme ile yeniden deneyin.", "warning")

            # Create initial revision
            rev = DocumentRevision(
                document_id=doc.id,
                revizyon_no=0,
                degisiklik_aciklamasi=request.form.get("degisiklik_aciklamasi", "İlk yayın"),
                dosya_adi=dosya_adi,
                dosya_yolu=dosya_yolu,
                dosya_boyutu=(os.path.getsize(dosya_yolu) / 1024) if dosya_yolu and os.path.exists(dosya_yolu) else None,
                hazirlayan_id=session.get("user_id"),
                tarih=datetime.now(),
                onay_durumu="Bekliyor" if doc.durum == "İncelemede" else "Onaylandı",
            )
            db.add(rev)

            # If submitted for approval, create approval steps
            if doc.durum == "İncelemede":
                create_approval_steps(db, doc)

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                flash(f"Bu doküman numarası zaten kullanılıyor: {dokuman_no}. Lütfen farklı bir numara girin.", "error")
                procs = db.query(Process).order_by(Process.kod).all()
                users = db.query(User).filter_by(aktif=True).all()
                return render_template("document_form.html", doc=None, processes=procs, users=users)

            log_action(db, "Oluşturma", document_id=doc.id, detay=f"Yeni doküman: {doc.dokuman_no} - {doc.baslik}")
            search_index.index_document(
                doc.id, doc.dokuman_no, doc.baslik,
                resolve_file_path(doc.dosya_yolu, doc.dosya_adi) if doc.dosya_yolu else None,
            )
            if doc.durum == "İncelemede":
                _notify_pending(db, doc)
            flash(f"Doküman başarıyla oluşturuldu: {doc.dokuman_no}", "success")
            return redirect(url_for("document_detail", doc_id=doc.id))

        # GET: Show form
        procs = db.query(Process).order_by(Process.kod).all()
        users = db.query(User).filter_by(aktif=True).all()
        return render_template("document_form.html", doc=None, processes=procs, users=users)
    finally:
        db.close()


@app.route("/documents/<int:doc_id>/procedure.docx")
@login_required
def document_procedure_docx(doc_id):
    """Prosedür antetli DOCX'i yeniden üretip indirir."""
    from procedure_docx import build_procedure_docx, meta_from_document

    db = get_db()
    try:
        doc = db.get(Document, doc_id)
        if not doc:
            flash("Doküman bulunamadı.", "error")
            return redirect(url_for("documents"))
        if not authz.can_access_document(db, session.get("user_id"), session.get("rol"), doc):
            abort(403)
        if doc.dokuman_tipi != "Prosedür":
            flash("Bu indirme yalnızca Prosedür tipi için geçerlidir.", "error")
            return redirect(url_for("document_detail", doc_id=doc_id))

        buf = build_procedure_docx(meta_from_document(doc), doc.icerik_json)
        log_action(db, "İndirme", document_id=doc.id, detay="Prosedür DOCX (antetli şablon)")
        return send_file(
            buf,
            as_attachment=True,
            download_name=f"{secure_filename(doc.dokuman_no) or 'prosedur'}.docx",
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    finally:
        db.close()


@app.route("/documents/<int:doc_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def document_edit(doc_id):
    db = get_db()
    try:
        doc = db.get(Document, doc_id)
        if not doc:
            flash("Doküman bulunamadı.", "error")
            return redirect(url_for("documents"))

        # Onaylı içerik değiştirilemez: yalnızca revizyon/iptal/eskime workflow'u.
        if doc.durum == "Onaylı":
            flash("Onaylı doküman doğrudan düzenlenemez. Lütfen 'Revize Et', "
                  "'İptal Et' veya 'Eskit' işlemlerini kullanın.", "error")
            return redirect(url_for("document_detail", doc_id=doc.id))

        if request.method == "POST":
            file = request.files.get("dosya")
            if file and file.filename and not allowed_file(file.filename):
                flash(f"İzin verilmeyen dosya tipi: .{file_ext(file.filename)}", "error")
                return redirect(url_for("document_edit", doc_id=doc.id))

            doc.baslik = request.form["baslik"]
            doc.surec_id = _to_int(request.form.get("surec_id"), doc.surec_id)
            doc.dokuman_tipi = request.form["dokuman_tipi"]
            doc.dokuman_seviyesi = _to_int(request.form.get("dokuman_seviyesi"), 2)
            doc.guvenlik_sinifi = request.form.get("guvenlik_sinifi", "Genel")
            doc.ilgili_standartlar = request.form.get("ilgili_standartlar", "")
            doc.hazirlayan_id = _to_int(request.form.get("hazirlayan_id"), doc.hazirlayan_id)
            doc.kontrol_eden_id = _to_int(request.form.get("kontrol_eden_id"))
            doc.onaylayan_id = _to_int(request.form.get("onaylayan_id"))
            doc.saklama_suresi_ay = _to_int(request.form.get("saklama_suresi_ay"), 36)

            yeni_icerik = _parse_icerik(request.form)
            if yeni_icerik is not None:
                doc.icerik_json = yeni_icerik

            doc.yururluk_tarihi = _to_date(request.form.get("yururluk_tarihi"))
            doc.sonraki_gozden_gecirme = _to_date(request.form.get("sonraki_gozden_gecirme"))

            # Handle file upload (mevcut revizyon klasörüne)
            if file and file.filename:
                dosya_adi, dosya_yolu = save_document_file(file, doc.id, doc.revizyon_no)
                doc.dosya_adi = dosya_adi
                doc.dosya_yolu = dosya_yolu
            elif doc.dokuman_tipi == "Prosedür" and doc.icerik_json:
                from procedure_docx import save_procedure_docx
                storage = document_storage_dir(doc.id, doc.revizyon_no)
                os.makedirs(storage, exist_ok=True)
                safe_name = f"{secure_filename(doc.dokuman_no) or 'prosedur'}.docx"
                target = os.path.join(storage, safe_name)
                doc.surec = db.get(Process, doc.surec_id) if doc.surec_id else None
                doc.hazirlayan = db.get(User, doc.hazirlayan_id) if doc.hazirlayan_id else None
                doc.kontrol_eden = db.get(User, doc.kontrol_eden_id) if doc.kontrol_eden_id else None
                doc.onaylayan = db.get(User, doc.onaylayan_id) if doc.onaylayan_id else None
                try:
                    save_procedure_docx(doc, target)
                    doc.dosya_adi = safe_name
                    doc.dosya_yolu = target
                except Exception as exc:
                    app.logger.exception("Prosedür DOCX üretilemedi: %s", exc)
                    flash("Doküman kaydedildi; Word anteti oluşturulamadı. İndirme ile yeniden deneyin.", "warning")

            doc.guncelleme_tarihi = datetime.now()

            # Handle status
            new_status = request.form.get("durum", doc.durum)
            if new_status == "İncelemede" and doc.durum != "İncelemede":
                doc.durum = "İncelemede"
                create_approval_steps(db, doc)
                db.commit()
                _notify_pending(db, doc)
            else:
                doc.durum = new_status
                db.commit()

            log_action(db, "Düzenleme", document_id=doc.id, detay=f"Doküman güncellendi: {doc.dokuman_no}")
            search_index.index_document(
                doc.id, doc.dokuman_no, doc.baslik,
                resolve_file_path(doc.dosya_yolu, doc.dosya_adi) if doc.dosya_yolu else None,
            )
            flash(f"Doküman güncellendi: {doc.dokuman_no}", "success")
            return redirect(url_for("document_detail", doc_id=doc.id))

        procs = db.query(Process).order_by(Process.kod).all()
        users = db.query(User).filter_by(aktif=True).all()
        return render_template("document_form.html", doc=doc, processes=procs, users=users)
    finally:
        db.close()


@app.route("/documents/<int:doc_id>/revise", methods=["GET", "POST"])
@login_required
@admin_required
def document_revise(doc_id):
    db = get_db()
    try:
        doc = db.get(Document, doc_id)
        if not doc:
            flash("Doküman bulunamadı.", "error")
            return redirect(url_for("documents"))

        # Revizyon yalnızca YAYINDA (Onaylı) olan bir doküman için yapılabilir.
        # Taslak/İncelemede/Reddedilmiş dokümanlar "Düzenle" ile güncellenir; revizyon atlamaz.
        if doc.durum != "Onaylı":
            flash("Yalnızca onaylı (yayında) dokümanlar revize edilebilir. "
                  "Taslak dokümanlar için 'Düzenle' seçeneğini kullanın.", "error")
            return redirect(url_for("document_detail", doc_id=doc.id))

        if request.method == "POST":
            file = request.files.get("dosya")
            if file and file.filename and not allowed_file(file.filename):
                flash(f"İzin verilmeyen dosya tipi: .{file_ext(file.filename)}", "error")
                return redirect(url_for("document_revise", doc_id=doc.id))

            doc.revizyon_no += 1
            doc.durum = "İncelemede"
            doc.guncelleme_tarihi = datetime.now()

            degisiklik = request.form.get("degisiklik_aciklamasi", "")

            # Handle file (yeni revizyon klasörüne kaydet)
            dosya_adi = doc.dosya_adi
            dosya_yolu = doc.dosya_yolu
            if file and file.filename:
                dosya_adi, dosya_yolu = save_document_file(file, doc.id, doc.revizyon_no)
                doc.dosya_adi = dosya_adi
                doc.dosya_yolu = dosya_yolu

            # Create revision record (onaylanana kadar Bekliyor; ret halinde geri alınır)
            rev = DocumentRevision(
                document_id=doc.id,
                revizyon_no=doc.revizyon_no,
                degisiklik_aciklamasi=degisiklik,
                dosya_adi=dosya_adi,
                dosya_yolu=dosya_yolu,
                dosya_boyutu=(os.path.getsize(dosya_yolu) / 1024) if dosya_yolu and os.path.exists(dosya_yolu) else None,
                hazirlayan_id=session.get("user_id"),
                tarih=datetime.now(),
                onay_durumu="Bekliyor",
                degisiklik_kategorisi=request.form.get("degisiklik_kategorisi") or None,
                etki_degerlendirmesi=request.form.get("etki_degerlendirmesi", "").strip() or None,
            )
            db.add(rev)

            create_approval_steps(db, doc)

            db.commit()
            log_action(db, "Düzenleme", document_id=doc.id,
                       detay=f"Doküman revize edildi: {doc.dokuman_no} Rev.{doc.revizyon_no}")
            search_index.index_document(
                doc.id, doc.dokuman_no, doc.baslik,
                resolve_file_path(doc.dosya_yolu, doc.dosya_adi) if doc.dosya_yolu else None,
            )
            _notify_pending(db, doc)
            flash(f"Doküman revize edildi: Rev. {doc.revizyon_no}", "success")
            return redirect(url_for("document_detail", doc_id=doc.id))

        return render_template("document_revise.html", doc=doc)
    finally:
        db.close()


@app.route("/documents/<int:doc_id>/download")
@login_required
def document_download(doc_id):
    db = get_db()
    try:
        doc = db.get(Document, doc_id)
        if not doc:
            flash("Dosya bulunamadı.", "error")
            return redirect(url_for("documents"))

        if not authz.can_access_document(db, session.get("user_id"), session.get("rol"), doc):
            abort(403)

        if not doc.dosya_yolu and not doc.dosya_adi:
            flash("Dosya bulunamadı.", "error")
            return redirect(url_for("documents"))

        # Bütünlük doğrulaması: onaylı dokümanın dosyası değiştirilmiş mi?
        if not verify_document_integrity(db, doc):
            flash("Bütünlük ihlali tespit edildi: dosya, onaylanan sürümle uyuşmuyor. "
                  "İndirme engellendi ve olay kaydedildi.", "error")
            return redirect(url_for("document_detail", doc_id=doc.id))

        file_path = resolve_file_path(doc.dosya_yolu, doc.dosya_adi)
        if not file_path or not os.path.isfile(file_path):
            flash("Dosya sunucuda bulunamadı.", "error")
            return redirect(url_for("document_detail", doc_id=doc.id))

        want_master = request.args.get("master") in ("1", "true", "yes")
        if want_master and _can_fetch_master_file():
            log_action(db, "İndirme", document_id=doc.id,
                       detay=f"Orijinal (filigransız) indirildi: {doc.dokuman_no}")
            return send_from_directory(
                os.path.dirname(file_path),
                os.path.basename(file_path),
                as_attachment=True,
                download_name=doc.dosya_adi,
            )

        log_action(db, "İndirme", document_id=doc.id,
                   detay=f"Filigranlı kopya indirildi: {doc.dokuman_no} ({doc.dosya_adi})")
        issued = _send_issued_copy(doc, file_path, purpose="indirme", as_attachment=True)
        if issued:
            return issued

        flash(
            "Bu dosya formatına otomatik filigran eklenemez. "
            "Yazdırırken kontrollü kopya işaretini kullanın.",
            "warning",
        )
        return send_from_directory(
            os.path.dirname(file_path),
            os.path.basename(file_path),
            as_attachment=True,
            download_name=doc.dosya_adi,
        )
    finally:
        db.close()


@app.route("/documents/<int:doc_id>/print")
@login_required
def document_print(doc_id):
    """Kontrollü/kontrolsüz kopya çıktısı — filigranlı dağıtım dosyası."""
    db = get_db()
    try:
        doc = db.get(Document, doc_id)
        if not doc:
            flash("Dosya bulunamadı.", "error")
            return redirect(url_for("documents"))
        if not authz.can_access_document(db, session.get("user_id"), session.get("rol"), doc):
            abort(403)
        if not doc.dosya_yolu and not doc.dosya_adi:
            flash("Dosya bulunamadı.", "error")
            return redirect(url_for("document_detail", doc_id=doc.id))
        if not verify_document_integrity(db, doc):
            logger.warning("Çıktı bütünlük uyarısı (devam ediliyor) doc_id=%s", doc.id)

        file_path = resolve_file_path(doc.dosya_yolu, doc.dosya_adi)
        if not file_path or not os.path.isfile(file_path):
            # Dosya yoksa da kapak PDF ver (düğme boş dönmesin)
            file_path = ""

        import watermark as wm
        spec = wm.build_spec_for_document(
            doc, user_name=session.get("ad_soyad") or "", purpose="cikti"
        )
        log_action(
            db, "Çıktı", document_id=doc.id,
            detay=f"{spec.label}: {doc.dokuman_no} Rev.{doc.revizyon_no} ({doc.dosya_adi})",
        )
        issued = _send_issued_copy(doc, file_path, purpose="cikti", as_attachment=True)
        if issued:
            return issued
        flash("Kontrollü kopya üretilemedi. Lütfen tekrar deneyin.", "error")
        return redirect(url_for("document_detail", doc_id=doc.id))
    finally:
        db.close()


@app.route("/documents/<int:doc_id>/preview")
@login_required
def document_preview(doc_id):
    """Dosyayı tarayıcıda önizleme için inline olarak serve eder."""
    db = get_db()
    try:
        doc = db.get(Document, doc_id)
        if not doc or not doc.dosya_adi:
            return "Dosya bulunamadı", 404

        if not authz.can_access_document(db, session.get("user_id"), session.get("rol"), doc):
            abort(403)

        if not verify_document_integrity(db, doc):
            return "Bütünlük ihlali: dosya onaylanan sürümle uyuşmuyor.", 409

        log_action(db, "Görüntüleme", document_id=doc.id, detay=f"Dosya önizlendi: {doc.dosya_adi}")

        file_path = resolve_file_path(doc.dosya_yolu, doc.dosya_adi)

        if not file_path or not os.path.isfile(file_path):
            logger.warning(
                "Onizleme dosyasi yok doc_id=%s upload=%s path=%s",
                doc.id, Config.UPLOAD_FOLDER, file_path,
            )
            return "Dosya sunucuda bulunamadı", 404

        ext = os.path.splitext(doc.dosya_adi)[1].lower()

        # 2026 — Office dosyaları antet ve sayfa düzeniyle PDF olarak önizlenir (onizleme_pdf: önbellek → anında dönüştürme)
        if request.args.get("format") == "pdf" and ext.lstrip(".") in onizleme_pdf.OFFICE_UZANTILARI:
            pdf = onizleme_pdf.pdf_onizleme(file_path)
            if not pdf:
                return "PDF önizleme üretilemedi", 404
            import watermark as wm
            spec = wm.build_spec_for_document(doc, user_name=session.get("ad_soyad") or session.get("eposta") or "", purpose="onizleme")
            gonder = pdf
            try:
                isaretli = wm.apply_issued_copy(pdf, spec, Config.get_temp_folder(), ext=".pdf")
                if isaretli:
                    _cleanup_temp_file(isaretli)
                    gonder = isaretli
            except Exception:  # noqa: BLE001 — filigran basılamazsa PDF.js üst katmanındaki filigran yine görünür
                logger.exception("Önizleme PDF filigranı basılamadı: %s", doc.dokuman_no)
            resp = send_file(gonder, mimetype="application/pdf", as_attachment=False,
                             download_name=os.path.splitext(doc.dosya_adi)[0] + ".pdf")
            resp.headers["Content-Disposition"] = "inline"
            resp.headers["Cache-Control"] = "private, no-store"
            resp.headers["X-Content-Type-Options"] = "nosniff"
            return resp

        if is_unsafe_inline(doc.dosya_adi):
            return send_file(
                file_path, as_attachment=True, download_name=doc.dosya_adi,
                mimetype="application/octet-stream",
            )

        want_master = request.args.get("master") in ("1", "true", "yes")
        if ext == ".pdf" and not (want_master and _can_fetch_master_file()):
            issued = _send_issued_copy(doc, file_path, purpose="onizleme", as_attachment=False, inline=True)
            if issued:
                issued.headers["Content-Disposition"] = "inline"
                issued.headers["Cache-Control"] = "private, no-store"
                issued.headers["X-Content-Type-Options"] = "nosniff"
                return issued

        content_type = PREVIEW_MIME_MAP.get(ext, "application/octet-stream")

        # Dosyayı oku
        file_size = os.path.getsize(file_path)
        with open(file_path, "rb") as f:
            data = f.read()

        from flask import Response
        response = Response(data, status=200, mimetype=content_type)
        response.headers["Content-Length"] = str(file_size)
        response.headers["Content-Disposition"] = "inline"
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Cache-Control"] = "private, max-age=300"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  DOKÜMAN YAŞAM DÖNGÜSÜ (Faz 3): İptal / Eskime / Saklama / İmha
# ═══════════════════════════════════════════════════════════════════
def _notify_document_recall(db, doc, baslik):
    """İptal/eskime durumunda dağıtım listesindekilere bilgi verir."""
    try:
        alicilar = []
        for d in db.query(DocumentDistribution).filter_by(document_id=doc.id).all():
            u = db.get(User, d.kullanici_id)
            if u and u.eposta:
                alicilar.append(u.eposta)
        if alicilar:
            notifications.send_mail(
                alicilar, f"[DYS] {baslik}: {doc.dokuman_no}",
                f"{doc.dokuman_no} — {doc.baslik} dokümanı '{baslik}' olarak işaretlendi. "
                f"Lütfen kontrolsüz kopyaları kullanımdan kaldırın."
            )
    except Exception:
        logger.exception("Geri çağırma bildirimi gönderilemedi (doc_id=%s)", doc.id)


@app.route("/documents/<int:doc_id>/cancel", methods=["POST"])
@login_required
@admin_required
def document_cancel(doc_id):
    db = get_db()
    try:
        doc = db.get(Document, doc_id)
        if not doc:
            abort(404)
        neden = (request.form.get("iptal_nedeni") or "").strip()
        if not neden:
            flash("İptal için gerekçe zorunludur.", "error")
            return redirect(url_for("document_detail", doc_id=doc_id))
        doc.durum = "İptal"
        doc.iptal_nedeni = neden
        doc.iptal_tarihi = date.today()
        doc.yerine_gecen_id = _to_int(request.form.get("yerine_gecen_id"))
        db.commit()
        log_action(db, "Düzenleme", document_id=doc.id,
                   detay=f"Doküman iptal edildi: {doc.dokuman_no} — {neden}")
        _notify_document_recall(db, doc, "İptal")
        flash(f"'{doc.dokuman_no}' iptal edildi ve dağıtım listesi bilgilendirildi.", "success")
        return redirect(url_for("document_detail", doc_id=doc_id))
    finally:
        db.close()


@app.route("/documents/<int:doc_id>/obsolete", methods=["POST"])
@login_required
@admin_required
def document_obsolete(doc_id):
    db = get_db()
    try:
        doc = db.get(Document, doc_id)
        if not doc:
            abort(404)
        neden = (request.form.get("iptal_nedeni") or "").strip()
        doc.durum = "Eskimiş"
        doc.iptal_nedeni = neden or "Süresi doldu / yeni sürümle değiştirildi"
        doc.iptal_tarihi = date.today()
        doc.yerine_gecen_id = _to_int(request.form.get("yerine_gecen_id"))
        db.commit()
        log_action(db, "Düzenleme", document_id=doc.id,
                   detay=f"Doküman eskitildi: {doc.dokuman_no}")
        _notify_document_recall(db, doc, "Eskimiş")
        flash(f"'{doc.dokuman_no}' eskimiş olarak işaretlendi.", "success")
        return redirect(url_for("document_detail", doc_id=doc_id))
    finally:
        db.close()


@app.route("/documents/<int:doc_id>/legal-hold", methods=["POST"])
@login_required
@admin_required
def document_legal_hold(doc_id):
    db = get_db()
    try:
        doc = db.get(Document, doc_id)
        if not doc:
            abort(404)
        doc.legal_hold = not bool(doc.legal_hold)
        db.commit()
        durum = "açıldı" if doc.legal_hold else "kaldırıldı"
        log_action(db, "Düzenleme", document_id=doc.id,
                   detay=f"Yasal saklama (legal hold) {durum}: {doc.dokuman_no}")
        flash(f"Yasal saklama {durum}.", "success")
        return redirect(url_for("document_detail", doc_id=doc_id))
    finally:
        db.close()


@app.route("/disposals")
@login_required
@admin_only
def disposals():
    db = get_db()
    try:
        kayitlar = db.query(RecordDisposal).order_by(RecordDisposal.talep_tarihi.desc()).all()
        return render_template("disposals.html", kayitlar=kayitlar)
    finally:
        db.close()


@app.route("/documents/<int:doc_id>/disposal/request", methods=["POST"])
@login_required
@admin_required
def disposal_request(doc_id):
    db = get_db()
    try:
        doc = db.get(Document, doc_id)
        if not doc:
            abort(404)
        if doc.legal_hold:
            flash("Yasal saklama (legal hold) aktif; imha talebi oluşturulamaz.", "error")
            return redirect(url_for("document_detail", doc_id=doc_id))
        acik = db.query(RecordDisposal).filter(
            RecordDisposal.document_id == doc_id,
            RecordDisposal.durum.in_(["Talep Edildi", "Onaylandı"]),
        ).first()
        if acik:
            flash("Bu doküman için zaten açık bir imha talebi var.", "warning")
            return redirect(url_for("document_detail", doc_id=doc_id))
        db.add(RecordDisposal(
            document_id=doc_id,
            talep_eden_id=session.get("user_id"),
            gerekce=request.form.get("gerekce") or None,
            yasal_dayanak=request.form.get("yasal_dayanak") or None,
            durum="Talep Edildi",
        ))
        db.commit()
        log_action(db, "Düzenleme", document_id=doc_id,
                   detay=f"İmha talebi oluşturuldu: {doc.dokuman_no}")
        flash("İmha talebi oluşturuldu. İki farklı yöneticinin onayı gereklidir.", "success")
        return redirect(url_for("document_detail", doc_id=doc_id))
    finally:
        db.close()


@app.route("/disposals/<int:disposal_id>/approve", methods=["POST"])
@login_required
@admin_only
def disposal_approve(disposal_id):
    db = get_db()
    try:
        rec = db.get(RecordDisposal, disposal_id)
        if not rec:
            abort(404)
        if rec.durum != "Talep Edildi":
            flash("Bu talep onaya uygun durumda değil.", "error")
            return redirect(url_for("disposals"))
        uid = session.get("user_id")
        # Çift onay: talep eden kendi talebini onaylayamaz; iki onay farklı kişi olmalı.
        if uid == rec.talep_eden_id:
            flash("Talebi oluşturan kişi imhayı onaylayamaz (görevler ayrılığı).", "error")
            return redirect(url_for("disposals"))
        if rec.onay1_id is None:
            rec.onay1_id = uid
            rec.onay1_tarihi = datetime.now()
            flash("Birinci onay verildi. İkinci (farklı) yönetici onayı bekleniyor.", "success")
        elif rec.onay2_id is None:
            if uid == rec.onay1_id:
                flash("İkinci onay birinci onaydan farklı bir yönetici tarafından verilmelidir.", "error")
                return redirect(url_for("disposals"))
            rec.onay2_id = uid
            rec.onay2_tarihi = datetime.now()
            rec.durum = "Onaylandı"
            flash("İkinci onay verildi. İmha yürütülebilir.", "success")
        db.commit()
        log_action(db, "Onay", document_id=rec.document_id,
                   detay=f"İmha onayı verildi (#{disposal_id})")
        return redirect(url_for("disposals"))
    finally:
        db.close()


@app.route("/disposals/<int:disposal_id>/execute", methods=["POST"])
@login_required
@admin_only
def disposal_execute(disposal_id):
    db = get_db()
    try:
        rec = db.get(RecordDisposal, disposal_id)
        if not rec:
            abort(404)
        if rec.durum != "Onaylandı":
            flash("İmha için çift onay tamamlanmalıdır.", "error")
            return redirect(url_for("disposals"))
        doc = db.get(Document, rec.document_id)
        if doc and doc.legal_hold:
            flash("Yasal saklama aktif; imha yürütülemez.", "error")
            return redirect(url_for("disposals"))
        rec.durum = "İmha Edildi"
        rec.imha_tarihi = datetime.now()
        if doc:
            doc.durum = "Eskimiş"
            doc.iptal_nedeni = (doc.iptal_nedeni or "") + " | Kayıt imha edildi (saklama süresi doldu)."
        db.commit()
        log_action(db, "Silme", document_id=rec.document_id,
                   detay=f"Kayıt imha edildi (#{disposal_id})")
        flash("İmha kaydı tamamlandı.", "success")
        return redirect(url_for("disposals"))
    finally:
        db.close()


@app.route("/admin/audit-integrity")
@login_required
@admin_only
def audit_integrity():
    """Değiştirilemez audit hash zincirini baştan sona doğrular."""
    db = get_db()
    try:
        loglar = db.query(AuditLog).order_by(AuditLog.id.asc()).all()
        onceki = None
        bozuk = []
        for lg in loglar:
            beklenen = _audit_record_hash(onceki, lg.kullanici_id, lg.islem_tipi,
                                          lg.document_id, lg.detay, lg.tarih)
            # Eski (hash'siz) kayıtları atla; yalnızca hash'li kayıtları doğrula.
            if lg.kayit_hash:
                if lg.onceki_hash != onceki or lg.kayit_hash != beklenen:
                    bozuk.append(lg.id)
                onceki = lg.kayit_hash
        return render_template("audit_integrity.html",
                               toplam=len(loglar), bozuk=bozuk)
    finally:
        db.close()


@app.route("/documents/<int:doc_id>/submit-approval", methods=["POST"])
@login_required
@admin_required
def submit_approval(doc_id):
    db = get_db()
    try:
        doc = db.get(Document, doc_id)
        if not doc:
            flash("Doküman bulunamadı.", "error")
            return redirect(url_for("documents"))

        doc.durum = "İncelemede"
        create_approval_steps(db, doc)

        db.commit()
        log_action(db, "Düzenleme", document_id=doc.id, detay="Doküman onaya gönderildi")
        _notify_pending(db, doc)
        flash("Doküman onaya gönderildi.", "success")
        return redirect(url_for("document_detail", doc_id=doc.id))
    finally:
        db.close()


@app.route("/documents/<int:doc_id>/distribute", methods=["POST"])
@login_required
@admin_required
def document_distribute(doc_id):
    db = get_db()
    try:
        # Hedef kullanıcı kümesi: tekil kullanıcı + departman + dağıtım grubu
        hedef_ids = set()
        tekil = _to_int(request.form.get("kullanici_id"))
        if tekil:
            hedef_ids.add(tekil)

        departman = request.form.get("departman")
        if departman:
            for u in db.query(User).filter_by(departman=departman, aktif=True).all():
                hedef_ids.add(u.id)

        group_id = _to_int(request.form.get("group_id"))
        if group_id:
            grp = db.query(DistributionGroup).get(group_id)
            if grp:
                for m in grp.members:
                    hedef_ids.add(m.kullanici_id)

        if not hedef_ids:
            flash("Dağıtım için kullanıcı, departman veya grup seçilmedi.", "error")
            return redirect(url_for("document_detail", doc_id=doc_id))

        # Zaten dağıtılmış kullanıcıları atla
        mevcut = {
            d.kullanici_id for d in db.query(DocumentDistribution).filter_by(document_id=doc_id).all()
        }
        yeni_ids = [uid for uid in hedef_ids if uid not in mevcut]

        doc = db.query(Document).get(doc_id)
        for uid in yeni_ids:
            db.add(DocumentDistribution(document_id=doc_id, kullanici_id=uid, dagitim_tarihi=datetime.now()))
        db.commit()

        for uid in yeni_ids:
            try:
                notifications.notify_distributed(doc, db.query(User).get(uid))
            except Exception:
                logger.exception("Dağıtım bildirimi gönderilemedi (doc_id=%s, user=%s)", doc_id, uid)

        log_action(db, "Düzenleme", document_id=doc_id,
                   detay=f"Doküman {len(yeni_ids)} kullanıcıya dağıtıldı")
        flash(f"Doküman {len(yeni_ids)} kişiye dağıtıldı.", "success")
        return redirect(url_for("document_detail", doc_id=doc_id))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  APPROVALS
# ═══════════════════════════════════════════════════════════════════
@app.route("/approvals")
@login_required
def approvals():
    db = get_db()
    try:
        pending = db.query(DocumentApproval).filter_by(durum="Bekliyor").order_by(DocumentApproval.tarih.desc()).all()
        history = db.query(DocumentApproval).filter(
            DocumentApproval.durum.in_(["Onaylandı", "Reddedildi"])
        ).order_by(DocumentApproval.tarih.desc()).limit(50).all()

        return render_template("approvals.html", pending=pending, history=history)
    finally:
        db.close()


@app.route("/approvals/<int:approval_id>/approve", methods=["POST"])
@login_required
def approve(approval_id):
    db = get_db()
    try:
        approval = db.get(DocumentApproval, approval_id)
        hata = authz.approval_action_error(db, session.get("user_id"), session.get("rol"), approval)
        if hata:
            flash(hata, "error")
            return redirect(url_for("approvals"))

        approval.durum = "Onaylandı"
        approval.kullanici_id = session.get("user_id")
        approval.tarih = datetime.now()

        # Aktif onay çevrimindeki tüm adımlar onaylandı mı?
        doc = approval.document
        cevrim_adimlari = db.query(DocumentApproval).filter_by(
            document_id=doc.id, workflow_no=approval.workflow_no
        ).all()

        if cevrim_adimlari and all(a.durum == "Onaylandı" for a in cevrim_adimlari):
            doc.durum = "Onaylı"
            doc.yururluk_tarihi = date.today()
            if not doc.sonraki_gozden_gecirme:
                doc.sonraki_gozden_gecirme = date.today() + timedelta(days=365)

            # Mevcut revizyon kaydını onaylı olarak işaretle (ret'te geri alınmaması için).
            aktif_rev = db.query(DocumentRevision).filter_by(
                document_id=doc.id, revizyon_no=doc.revizyon_no
            ).order_by(DocumentRevision.id.desc()).first()
            if aktif_rev:
                aktif_rev.onay_durumu = "Onaylandı"
                # Bütünlük kanıtı: onaylanan dosyanın SHA-256 özetini kaydet.
                aktif_rev.icerik_hash = file_sha256(
                    resolve_file_path(doc.dosya_yolu, doc.dosya_adi)
                )

        # 2026 — okuma kaydı revizyon bazındadır: yeni revizyon yürürlüğe girince önceki dağıtımlar yeniden 'okunmadı' olur
        yeniden_okunacak = []
        if cevrim_adimlari and all(a.durum == "Onaylandı" for a in cevrim_adimlari) and (doc.revizyon_no or 0) > 0:
            for d_ in db.query(DocumentDistribution).filter_by(document_id=doc.id).all():
                d_.okundu_mu = False; d_.okunma_tarihi = None; d_.dagitim_tarihi = datetime.now()
                yeniden_okunacak.append(d_.kullanici_id)

        db.commit()
        tam_onayli = doc.durum == "Onaylı"
        hazirlayan = doc.hazirlayan
        log_action(db, "Onay", document_id=doc.id,
                   detay=f"Onaylandı: {approval.onay_adimi} adımı - {doc.dokuman_no}")
        if tam_onayli:
            try:
                notifications.notify_approved(doc, hazirlayan)
            except Exception:
                logger.exception("Onay bildirimi gönderilemedi (doc_id=%s)", doc.id)
        if yeniden_okunacak:
            log_action(db, "Düzenleme", document_id=doc.id,
                       detay=f"Rev. {doc.revizyon_no} yürürlüğe girdi: {len(yeniden_okunacak)} dağıtım alıcısından yeniden okuma istendi")
            for uid_ in yeniden_okunacak:
                try:
                    notifications.notify_distributed(doc, db.query(User).get(uid_))
                except Exception:
                    logger.exception("Yeniden dağıtım bildirimi gönderilemedi (doc_id=%s, user=%s)", doc.id, uid_)
        flash(f"{approval.onay_adimi} adımı onaylandı.", "success")
        return redirect(url_for("approvals"))
    finally:
        db.close()


@app.route("/approvals/<int:approval_id>/reject", methods=["POST"])
@login_required
def reject(approval_id):
    db = get_db()
    try:
        approval = db.get(DocumentApproval, approval_id)
        hata = authz.approval_action_error(db, session.get("user_id"), session.get("rol"), approval)
        if hata:
            flash(hata, "error")
            return redirect(url_for("approvals"))

        approval.durum = "Reddedildi"
        approval.kullanici_id = session.get("user_id")
        approval.aciklama = request.form.get("aciklama", "")
        approval.tarih = datetime.now()

        doc = approval.document

        # Aynı dokümanda bekleyen diğer onay adımlarını iptal et ki akış tekrar
        # baştan başlasın ve bekleyen onay sayısı doğru olsun.
        bekleyenler = db.query(DocumentApproval).filter_by(
            document_id=doc.id, durum="Bekliyor"
        ).all()
        for b in bekleyenler:
            db.delete(b)

        # Bekleyen bir REVİZYON talebi mi reddedildi? (revizyon > 0 ve o revizyon henüz onaylanmamış)
        bekleyen_rev = None
        if doc.revizyon_no > 0:
            bekleyen_rev = db.query(DocumentRevision).filter_by(
                document_id=doc.id, revizyon_no=doc.revizyon_no
            ).order_by(DocumentRevision.id.desc()).first()

        if bekleyen_rev and bekleyen_rev.onay_durumu == "Bekliyor":
            # Reddedilen revizyon talebini geri al: revizyon ilerlemez.
            talep_sahibi = bekleyen_rev.hazirlayan_id

            # Bir önceki (yürürlükteki) revizyona geri dön; dosya bilgisini de geri yükle.
            onceki_rev = db.query(DocumentRevision).filter_by(
                document_id=doc.id, revizyon_no=doc.revizyon_no - 1
            ).order_by(DocumentRevision.id.desc()).first()

            db.delete(bekleyen_rev)
            doc.revizyon_no -= 1
            if onceki_rev:
                doc.dosya_adi = onceki_rev.dosya_adi
                doc.dosya_yolu = onceki_rev.dosya_yolu

            # Doküman, revizyon talebini oluşturan kullanıcıya (Taslak olarak) geri döner.
            if talep_sahibi:
                doc.hazirlayan_id = talep_sahibi
            doc.durum = "Taslak"
            doc.guncelleme_tarihi = datetime.now()

            db.commit()
            log_action(db, "Red", document_id=doc.id,
                       detay=f"Revizyon talebi reddedildi ve geri alındı: {doc.dokuman_no} "
                             f"(Rev. {doc.revizyon_no}'da kaldı) - {approval.aciklama}")
            try:
                notifications.notify_rejected(doc, doc.hazirlayan, approval.aciklama)
            except Exception:
                logger.exception("Ret bildirimi gönderilemedi (doc_id=%s)", doc.id)
            flash(f"'{doc.dokuman_no}' revizyon talebi reddedildi. Revizyon ilerlemedi "
                  f"(Rev. {doc.revizyon_no}) ve doküman talebi oluşturana geri gönderildi.", "warning")
            return redirect(url_for("approvals"))

        # Revizyon değilse (ilk yayın / taslak onayı): revizyon değişmeden hazırlayana döner.
        doc.durum = "Taslak"
        db.commit()
        log_action(db, "Red", document_id=doc.id,
                   detay=f"Reddedildi: {approval.onay_adimi} adımı - {doc.dokuman_no} - {approval.aciklama}")
        try:
            notifications.notify_rejected(doc, doc.hazirlayan, approval.aciklama)
        except Exception:
            logger.exception("Ret bildirimi gönderilemedi (doc_id=%s)", doc.id)
        flash(f"'{doc.dokuman_no}' reddedildi ve hazırlayana geri gönderildi (Taslak).", "warning")
        return redirect(url_for("approvals"))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  DISTRIBUTIONS
# ═══════════════════════════════════════════════════════════════════
@app.route("/distributions")
@login_required
def distributions():
    db = get_db()
    try:
        dists = db.query(DocumentDistribution).filter_by(
            kullanici_id=session.get("user_id")
        ).order_by(DocumentDistribution.dagitim_tarihi.desc()).all()
        return render_template("distributions.html", distributions=dists)
    finally:
        db.close()


@app.route("/distributions/<int:dist_id>/mark-read", methods=["POST"])
@login_required
def mark_distribution_read(dist_id):
    db = get_db()
    try:
        dist = db.query(DocumentDistribution).get(dist_id)
        if dist and dist.kullanici_id == session.get("user_id"):
            dist.okundu_mu = True
            dist.okunma_tarihi = datetime.now()
            db.commit()
            log_action(db, "Görüntüleme", document_id=dist.document_id,
                       detay=f"Okudum, anladım: {dist.document.dokuman_no} Rev. {dist.document.revizyon_no}")
            flash("Doküman okundu olarak işaretlendi.", "success")
        sonraki = request.form.get("next") or ""
        if sonraki.startswith("/") and not sonraki.startswith("//"):  # yalnız site içi dönüş
            return redirect(request.script_root + sonraki)
        return redirect(url_for("distributions"))
    finally:
        db.close()


@app.route("/api/distribution/<int:dist_id>/read", methods=["POST"])
@login_required
def api_mark_read(dist_id):
    db = get_db()
    try:
        dist = db.query(DocumentDistribution).get(dist_id)
        if not dist:
            return jsonify({"success": False}), 404
        # IDOR: yalnızca dağıtımın sahibi kendi kaydını okundu işaretleyebilir.
        if dist.kullanici_id != session.get("user_id"):
            return jsonify({"success": False, "error": "forbidden"}), 403
        dist.okundu_mu = True
        dist.okunma_tarihi = datetime.now()
        db.commit()
        return jsonify({"success": True})
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  REPORTS
# ═══════════════════════════════════════════════════════════════════
@app.route("/reports")
@login_required
def reports():
    db = get_db()
    try:
        documents = db.query(Document).all()
        processes = db.query(Process).order_by(Process.kod).all()

        stats = {
            "toplam": len(documents),
            "onayli": sum(1 for d in documents if d.durum == "Onaylı"),
            "taslak": sum(1 for d in documents if d.durum == "Taslak"),
            "eskimis": sum(1 for d in documents if d.durum in ("Eskimiş", "İptal")),
        }

        return render_template("reports.html", stats=stats, processes=processes)
    finally:
        db.close()


@app.route("/reports/compliance")
@login_required
def report_compliance():
    """Standart Uyumluluk Matrisi — standart maddeleri ve doküman kapsamı."""
    db = get_db()
    try:
        documents = db.query(Document).all()
        processes = db.query(Process).order_by(Process.kod).all()

        # Her standart maddesi için, o maddeye atıfta bulunan doküman sayısı.
        matris = {}
        for standart, maddeler in STANDART_MADDELERI.items():
            satirlar = []
            for madde_no, madde_ad in maddeler:
                ilgili_dokumanlar = [
                    d for d in documents
                    if d.ilgili_standartlar and standart in d.ilgili_standartlar
                    and (f"{standart} - {madde_no}" in d.ilgili_standartlar
                         or f"{standart}-{madde_no}" in d.ilgili_standartlar
                         or f" {madde_no}" in d.ilgili_standartlar
                         or f",{madde_no}" in d.ilgili_standartlar)
                ]
                satirlar.append({
                    "no": madde_no,
                    "ad": madde_ad,
                    "adet": len(ilgili_dokumanlar),
                    "kapsandi": len(ilgili_dokumanlar) > 0,
                })
            kapsanan = sum(1 for s in satirlar if s["kapsandi"])
            matris[standart] = {
                "satirlar": satirlar,
                "kapsanan": kapsanan,
                "toplam": len(satirlar),
                "oran": round(kapsanan / len(satirlar) * 100) if satirlar else 0,
            }

        return render_template("report_compliance.html", matris=matris, processes=processes)
    finally:
        db.close()


@app.route("/reports/inventory")
@login_required
def report_inventory():
    """Doküman Envanteri — tüm dokümanların filtrelenebilir listesi."""
    db = get_db()
    try:
        query = db.query(Document)
        surec = request.args.get("surec")
        tip = request.args.get("tip")
        durum = request.args.get("durum")
        if surec:
            query = query.filter(Document.surec_id == int(surec))
        if tip:
            query = query.filter(Document.dokuman_tipi == tip)
        if durum:
            query = query.filter(Document.durum == durum)

        documents = query.order_by(Document.dokuman_no).all()
        processes = db.query(Process).order_by(Process.kod).all()

        tip_dagilimi = {}
        for d in db.query(Document).all():
            tip_dagilimi[d.dokuman_tipi] = tip_dagilimi.get(d.dokuman_tipi, 0) + 1

        return render_template(
            "report_inventory.html",
            documents=documents, processes=processes,
            tip_dagilimi=tip_dagilimi, tipler=DOKUMAN_TIPLERI,
        )
    finally:
        db.close()


@app.route("/reports/review-calendar")
@login_required
def report_review_calendar():
    """Gözden Geçirme Takvimi — yaklaşan ve gecikmiş gözden geçirmeler."""
    db = get_db()
    try:
        documents = db.query(Document).filter(
            Document.sonraki_gozden_gecirme.isnot(None)
        ).order_by(Document.sonraki_gozden_gecirme).all()

        bugun = date.today()
        gruplar = {"gecikmis": [], "bu_ay": [], "yaklasan": [], "ileride": []}
        for d in documents:
            kalan = (d.sonraki_gozden_gecirme - bugun).days
            kayit = {"doc": d, "kalan": kalan}
            if kalan < 0:
                gruplar["gecikmis"].append(kayit)
            elif kalan <= 30:
                gruplar["bu_ay"].append(kayit)
            elif kalan <= 90:
                gruplar["yaklasan"].append(kayit)
            else:
                gruplar["ileride"].append(kayit)

        return render_template("report_review_calendar.html", gruplar=gruplar, bugun=bugun)
    finally:
        db.close()


@app.route("/reports/obsolete")
@login_required
def report_obsolete():
    """Eskimiş / İptal Dokümanlar raporu."""
    db = get_db()
    try:
        documents = db.query(Document).filter(
            Document.durum.in_(["Eskimiş", "İptal"])
        ).order_by(Document.guncelleme_tarihi.desc()).all()
        processes = db.query(Process).order_by(Process.kod).all()
        return render_template("report_obsolete.html", documents=documents, processes=processes)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  DIŞA AKTARMA (PDF / Excel)
# ═══════════════════════════════════════════════════════════════════
def _send_export(buf, filename):
    from flask import send_file as _sf
    return _sf(buf, as_attachment=True, download_name=filename)


def _filtered_documents(db):
    """Aktif filtrelerle doküman listesi (documents/report_inventory ile aynı mantık)."""
    query = db.query(Document)
    surec = request.args.get("surec")
    tip = request.args.get("tip")
    durum = request.args.get("durum")
    guvenlik = request.args.get("guvenlik")
    search = request.args.get("search", "").strip()
    if surec:
        query = query.filter(Document.surec_id == _to_int(surec))
    if tip:
        query = query.filter(Document.dokuman_tipi == tip)
    if durum:
        query = query.filter(Document.durum == durum)
    if guvenlik:
        query = query.filter(Document.guvenlik_sinifi == guvenlik)
    if search:
        query = query.filter(
            (Document.baslik.ilike(f"%{search}%"))
            | (Document.dokuman_no.ilike(f"%{search}%"))
            | (Document.dosya_yolu.ilike(f"%{search}%"))
        )
    return query.order_by(Document.dokuman_no).all()


def _inventory_rows(db):
    docs = _filtered_documents(db)
    kolonlar = ["Doküman No", "Başlık", "Dizin", "Süreç", "Tip", "Seviye", "Rev.", "Durum", "Güvenlik", "Yürürlük", "Sonraki GG"]
    satirlar = []
    for d in docs:
        satirlar.append([
            d.dokuman_no, d.baslik, document_dir_path(d.dosya_yolu, d.dosya_adi),
            d.surec.kod if d.surec else "", d.dokuman_tipi,
            d.dokuman_seviyesi, d.revizyon_no, d.durum, d.guvenlik_sinifi,
            d.yururluk_tarihi.strftime("%d.%m.%Y") if d.yururluk_tarihi else "",
            d.sonraki_gozden_gecirme.strftime("%d.%m.%Y") if d.sonraki_gozden_gecirme else "",
        ])
    return "Doküman Envanteri", kolonlar, satirlar


def _compliance_rows(db):
    documents = db.query(Document).all()
    kolonlar = ["Standart", "Madde", "Açıklama", "İlgili Doküman", "Kapsandı"]
    satirlar = []
    for standart, maddeler in STANDART_MADDELERI.items():
        for madde_no, madde_ad in maddeler:
            ilgili = [
                d for d in documents
                if d.ilgili_standartlar and standart in d.ilgili_standartlar
                and (f"{standart} - {madde_no}" in d.ilgili_standartlar
                     or f"{standart}-{madde_no}" in d.ilgili_standartlar
                     or f" {madde_no}" in d.ilgili_standartlar
                     or f",{madde_no}" in d.ilgili_standartlar)
            ]
            satirlar.append([standart, madde_no, madde_ad, len(ilgili), "Evet" if ilgili else "Hayır"])
    return "Standart Uyumluluk Matrisi", kolonlar, satirlar


def _review_rows(db):
    docs = db.query(Document).filter(Document.sonraki_gozden_gecirme.isnot(None)).order_by(
        Document.sonraki_gozden_gecirme).all()
    bugun = date.today()
    kolonlar = ["Doküman No", "Başlık", "Sonraki GG", "Kalan Gün", "Durum"]
    satirlar = []
    for d in docs:
        kalan = (d.sonraki_gozden_gecirme - bugun).days
        satirlar.append([d.dokuman_no, d.baslik, d.sonraki_gozden_gecirme.strftime("%d.%m.%Y"),
                         kalan, d.durum])
    return "Gözden Geçirme Takvimi", kolonlar, satirlar


def _obsolete_rows(db):
    docs = db.query(Document).filter(Document.durum.in_(["Eskimiş", "İptal"])).order_by(
        Document.guncelleme_tarihi.desc()).all()
    kolonlar = ["Doküman No", "Başlık", "Süreç", "Tip", "Rev.", "Durum", "Güncelleme"]
    satirlar = []
    for d in docs:
        satirlar.append([d.dokuman_no, d.baslik, d.surec.kod if d.surec else "", d.dokuman_tipi,
                         d.revizyon_no, d.durum,
                         d.guncelleme_tarihi.strftime("%d.%m.%Y") if d.guncelleme_tarihi else ""])
    return "Eskimiş / İptal Dokümanlar", kolonlar, satirlar


_EXPORT_BUILDERS = {
    "inventory": _inventory_rows,
    "compliance": _compliance_rows,
    "review-calendar": _review_rows,
    "obsolete": _obsolete_rows,
}


@app.route("/reports/<rapor>/export.<fmt>")
@login_required
def report_export(rapor, fmt):
    builder = _EXPORT_BUILDERS.get(rapor)
    if not builder or fmt not in ("pdf", "xlsx"):
        flash("Geçersiz dışa aktarma isteği.", "error")
        return redirect(url_for("reports"))
    db = get_db()
    try:
        from exports import excel_from_rows, pdf_from_rows
        baslik, kolonlar, satirlar = builder(db)
        log_action(db, "İndirme", detay=f"Rapor dışa aktarıldı: {rapor}.{fmt}")
        if fmt == "xlsx":
            buf = excel_from_rows(baslik, kolonlar, satirlar)
            return _send_export(buf, f"{rapor}.xlsx")
        buf = pdf_from_rows(baslik, kolonlar, satirlar)
        return _send_export(buf, f"{rapor}.pdf")
    finally:
        db.close()


@app.route("/reports/audit-package")
@login_required
@admin_required
def report_audit_package():
    """Denetim paketi: gereklilik / kanıt / DÖF / bulgu özeti (SHA-256 imzalı Excel)."""
    from io import BytesIO
    from openpyxl import Workbook

    standart = request.args.get("standart") or ""
    db = get_db()
    try:
        from models import (
            Requirement, RequirementStatus, Evidence, AuditFinding, StandardEdition,
        )

        wb = Workbook()
        ws = wb.active
        ws.title = "Gereklilikler"
        ws.append(["Standart", "Sürüm", "Madde", "Başlık", "Durum", "Sorumlu", "Son Doğrulama"])
        q = db.query(Requirement).join(StandardEdition)
        if standart:
            q = q.filter(StandardEdition.standart == standart)
        for req in q.order_by(StandardEdition.standart, Requirement.madde_no).all():
            st = req.status
            ed = req.edition
            ws.append([
                ed.standart if ed else "",
                ed.surum if ed else "",
                req.madde_no,
                req.baslik or "",
                st.uygulama_durumu if st else "",
                st.sorumlu.ad_soyad if st and st.sorumlu else "",
                str(st.son_dogrulama_tarihi) if st and st.son_dogrulama_tarihi else "",
            ])

        ws2 = wb.create_sheet("Kanitlar")
        ws2.append(["Gereklilik ID", "Başlık", "Kanıt Tipi", "Doküman ID", "Tarih"])
        for ev in db.query(Evidence).order_by(Evidence.id.desc()).limit(500).all():
            ws2.append([
                ev.requirement_id or "",
                ev.baslik or "",
                ev.kanit_tipi or "",
                ev.ilgili_document_id or "",
                str(ev.olusturma_tarihi) if getattr(ev, "olusturma_tarihi", None) else "",
            ])

        ws3 = wb.create_sheet("DOF")
        ws3.append(["DÖF No", "Başlık", "Durum", "Yöntem", "Planlanan"])
        for c in db.query(CorrectiveAction).order_by(CorrectiveAction.olusturma_tarihi.desc()).limit(500).all():
            ws3.append([
                c.dof_no, c.baslik, c.durum, c.kok_neden_yontemi or "",
                str(c.planlanan_tarih) if c.planlanan_tarih else "",
            ])

        ws4 = wb.create_sheet("Bulgular")
        ws4.append(["Tetkik", "Tip", "Açıklama", "İlgili Madde", "DÖF ID"])
        for f in db.query(AuditFinding).order_by(AuditFinding.id.desc()).limit(500).all():
            ws4.append([
                f.audit.tetkik_no if f.audit else "",
                f.bulgu_tipi or "",
                (f.aciklama or "")[:200],
                f.ilgili_madde or "",
                f.capa_id or "",
            ])

        buf = BytesIO()
        wb.save(buf)
        data = buf.getvalue()
        paket_hash = hashlib.sha256(data).hexdigest()
        log_action(
            db, "İndirme",
            detay=f"Denetim paketi üretildi standart={standart or 'ALL'} sha256={paket_hash}",
        )

        resp = make_response(data)
        resp.headers["Content-Type"] = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        resp.headers["Content-Disposition"] = 'attachment; filename="denetim-paketi.xlsx"'
        resp.headers["X-Content-SHA256"] = paket_hash
        return resp
    finally:
        db.close()


@app.route("/documents/export.xlsx")
@login_required
def documents_export():
    db = get_db()
    try:
        from exports import excel_from_rows
        baslik, kolonlar, satirlar = _inventory_rows(db)
        log_action(db, "İndirme", detay="Doküman listesi Excel'e aktarıldı")
        buf = excel_from_rows(baslik, kolonlar, satirlar)
        return _send_export(buf, "dokuman-listesi.xlsx")
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  AUDIT LOG
# ═══════════════════════════════════════════════════════════════════
@app.route("/audit-log")
@login_required
@admin_only
def audit_log():
    db = get_db()
    try:
        query = db.query(AuditLog)

        islem = request.args.get("islem")
        kullanici = request.args.get("kullanici")

        if islem:
            query = query.filter(AuditLog.islem_tipi == islem)
        if kullanici:
            query = query.filter(AuditLog.kullanici_id == int(kullanici))

        logs = query.order_by(AuditLog.tarih.desc()).limit(200).all()
        users = db.query(User).order_by(User.ad_soyad).all()

        return render_template("audit_log.html", logs=logs, users=users)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  ADMIN — USER MANAGEMENT
# ═══════════════════════════════════════════════════════════════════
@app.route("/admin/users")
@login_required
@admin_required
def admin_users():
    db = get_db()
    try:
        users = db.query(User).order_by(User.ad_soyad).all()
        return render_template("admin_users.html", users=users)
    finally:
        db.close()


@app.route("/admin/users/new", methods=["POST"])
@login_required
@admin_required
def admin_user_new():
    db = get_db()
    try:
        gecerli, mesaj = _sifre_gecerli_mi(request.form.get("sifre", ""))
        if not gecerli:
            flash(mesaj, "error")
            return redirect(url_for("admin_users"))
        user = User(
            ad_soyad=request.form["ad_soyad"],
            eposta=request.form["eposta"],
            sifre_hash=generate_password_hash(request.form["sifre"]),
            departman=request.form["departman"],
            unvan=request.form["unvan"],
            rol=request.form.get("rol", "Kullanıcı"),
            aktif=True,
            olusturma_tarihi=datetime.now(),
        )
        db.add(user)
        db.commit()
        log_action(db, "Oluşturma", detay=f"Yeni kullanıcı: {user.ad_soyad}")
        flash(f"Kullanıcı oluşturuldu: {user.ad_soyad}", "success")
        return redirect(url_for("admin_users"))
    finally:
        db.close()


@app.route("/admin/users/<int:user_id>/edit", methods=["POST"])
@login_required
@admin_required
def admin_user_edit(user_id):
    db = get_db()
    try:
        user = db.get(User, user_id)
        if not user:
            flash("Kullanıcı bulunamadı.", "error")
            return redirect(url_for("admin_users"))

        eski_rol = user.rol
        user.ad_soyad = request.form["ad_soyad"]
        user.eposta = request.form["eposta"]
        user.departman = request.form["departman"]
        user.unvan = request.form["unvan"]
        user.rol = request.form.get("rol", user.rol)

        sifre = request.form.get("sifre", "").strip()
        if sifre:
            gecerli, mesaj = _sifre_gecerli_mi(sifre)
            if not gecerli:
                flash(mesaj, "error")
                return redirect(url_for("admin_users"))
            user.sifre_hash = generate_password_hash(sifre)

        db.commit()
        if user.rol != eski_rol:
            from redis_client import revoke_session
            revoke_session(user.id)
        log_action(db, "Düzenleme", detay=f"Kullanıcı güncellendi: {user.ad_soyad}")
        flash(f"Kullanıcı güncellendi: {user.ad_soyad}", "success")
        return redirect(url_for("admin_users"))
    finally:
        db.close()


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@login_required
@admin_only
def admin_user_delete(user_id):
    db = get_db()
    try:
        user = db.get(User, user_id)
        if not user:
            flash("Kullanıcı bulunamadı.", "error")
            return redirect(url_for("admin_users"))

        if user.id == session.get("user_id"):
            flash("Kendi hesabınızı silemezsiniz.", "error")
            return redirect(url_for("admin_users"))

        ad = user.ad_soyad

        # İlişkili kayıtlardaki referansları güvenli şekilde temizle.
        db.query(Document).filter_by(hazirlayan_id=user.id).update({"hazirlayan_id": None})
        db.query(Document).filter_by(kontrol_eden_id=user.id).update({"kontrol_eden_id": None})
        db.query(Document).filter_by(onaylayan_id=user.id).update({"onaylayan_id": None})
        db.query(DocumentRevision).filter_by(hazirlayan_id=user.id).update({"hazirlayan_id": None})
        db.query(DocumentApproval).filter_by(kullanici_id=user.id).update({"kullanici_id": None})
        db.query(AuditLog).filter_by(kullanici_id=user.id).update({"kullanici_id": None})
        # Dağıtım kaydı kullanıcıya zorunlu bağlı olduğundan silinir.
        db.query(DocumentDistribution).filter_by(kullanici_id=user.id).delete()

        db.delete(user)
        db.commit()
        log_action(db, "Silme", detay=f"Kullanıcı kalıcı olarak silindi: {ad}")
        flash(f"Kullanıcı silindi: {ad}", "success")
        return redirect(url_for("admin_users"))
    finally:
        db.close()


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@admin_required
def admin_user_toggle(user_id):
    db = get_db()
    try:
        user = db.get(User, user_id)
        if user:
            user.aktif = not user.aktif
            db.commit()
            from redis_client import revoke_session
            revoke_session(user.id)
            status = "aktif" if user.aktif else "pasif"
            log_action(db, "Düzenleme", detay=f"Kullanıcı {status} yapıldı: {user.ad_soyad}")
            flash(f"{user.ad_soyad} {status} yapıldı.", "success")
        return redirect(url_for("admin_users"))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  DÖF / DÜZELTİCİ FAALİYET (CAPA)
# ═══════════════════════════════════════════════════════════════════
@app.route("/capa")
@login_required
def capa_dashboard():
    """DÖF paneli: KPI, yaklaşan/geciken listeler, rapor bağlantıları."""
    db = get_db()
    try:
        tum = db.query(CorrectiveAction).order_by(CorrectiveAction.olusturma_tarihi.desc()).all()
        bugun = date.today()
        gun = max(0, int(Config.CAPA_REMINDER_DAYS or 7))
        yaklasan_esik = bugun + timedelta(days=gun)

        aciklar = [k for k in tum if k.durum != "Kapatıldı"]
        geciken = [k for k in aciklar if k.gecikti_mi]
        yaklasan = [
            k for k in aciklar
            if k.planlanan_tarih and bugun <= k.planlanan_tarih <= yaklasan_esik and not k.gecikti_mi
        ]
        kapatilan = [k for k in tum if k.durum == "Kapatıldı"]

        durum_dagilim = {d: 0 for d in CAPA_DURUMLARI}
        kaynak_dagilim = {k: 0 for k in CAPA_KAYNAKLARI}
        for c in tum:
            durum_dagilim[c.durum] = durum_dagilim.get(c.durum, 0) + 1
            kaynak_dagilim[c.kaynak_tipi] = kaynak_dagilim.get(c.kaynak_tipi, 0) + 1

        stats = {
            "toplam": len(tum),
            "acik": len(aciklar),
            "geciken": len(geciken),
            "yaklasan": len(yaklasan),
            "kapatilan": len(kapatilan),
            "hatirlatma_gun": gun,
        }
        return render_template(
            "capa_dashboard.html",
            stats=stats,
            geciken=geciken[:10],
            yaklasan=yaklasan[:10],
            son_kayitlar=tum[:8],
            durum_labels=list(durum_dagilim.keys()),
            durum_values=list(durum_dagilim.values()),
            kaynak_labels=list(kaynak_dagilim.keys()),
            kaynak_values=list(kaynak_dagilim.values()),
            smtp_ok=notifications.is_configured(),
        )
    finally:
        db.close()


@app.route("/capa/list")
@login_required
def capa_list():
    db = get_db()
    try:
        query = db.query(CorrectiveAction)
        durum = request.args.get("durum")
        kaynak = request.args.get("kaynak")
        if durum:
            query = query.filter(CorrectiveAction.durum == durum)
        if kaynak:
            query = query.filter(CorrectiveAction.kaynak_tipi == kaynak)
        kayitlar = query.order_by(CorrectiveAction.olusturma_tarihi.desc()).all()
        acik = sum(1 for k in db.query(CorrectiveAction).all() if k.durum != "Kapatıldı")
        geciken = sum(1 for k in db.query(CorrectiveAction).all() if k.gecikti_mi)
        return render_template("capa_list.html", kayitlar=kayitlar, acik=acik, geciken=geciken,
                               kaynaklar=CAPA_KAYNAKLARI, durumlar=CAPA_DURUMLARI)
    finally:
        db.close()


@app.route("/capa/export.<fmt>")
@login_required
def capa_export(fmt):
    if fmt not in ("xlsx", "pdf"):
        abort(404)
    db = get_db()
    try:
        from exports import excel_from_rows, pdf_from_rows
        durum = request.args.get("durum") or None
        kaynak = request.args.get("kaynak") or None
        baslik, kolonlar, satirlar = _capa_export_rows(db, durum=durum, kaynak=kaynak)
        if fmt == "xlsx":
            buf = excel_from_rows(baslik, kolonlar, satirlar)
            mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            buf = pdf_from_rows(baslik, kolonlar, satirlar, yatay=True)
            mime = "application/pdf"
        return send_file(buf, as_attachment=True, download_name=f"dof-raporu.{fmt}",
                         mimetype=mime)
    finally:
        db.close()


@app.route("/capa/<int:capa_id>/export.pdf")
@login_required
def capa_export_one(capa_id):
    db = get_db()
    try:
        capa = db.get(CorrectiveAction, capa_id)
        if not capa:
            flash("DÖF bulunamadı.", "error")
            return redirect(url_for("capa_dashboard"))
        from exports import capa_detail_pdf
        from capa_kok_neden import format_kok_neden_blocks
        kok_bloklar = format_kok_neden_blocks(capa.kok_neden, capa.kok_neden_yontemi)
        buf = capa_detail_pdf(capa, kok_bloklar=kok_bloklar)
        return send_file(
            buf, as_attachment=True,
            download_name=f"{secure_filename(capa.dof_no) or 'dof'}.pdf",
            mimetype="application/pdf",
        )
    finally:
        db.close()


@app.route("/capa/send-reminders", methods=["POST"])
@login_required
@admin_required
def capa_send_reminders():
    n = send_capa_reminders()
    if notifications.is_configured():
        flash(f"DÖF hatırlatmaları tetiklendi ({n} e-posta).", "success")
    else:
        flash("SMTP yapılandırılmamış; hatırlatmalar loglandı ancak e-posta gönderilmedi.", "warning")
    return redirect(url_for("capa_dashboard"))


@app.route("/capa/new", methods=["GET", "POST"])
@login_required
@admin_required
def capa_new():
    db = get_db()
    try:
        if request.method == "POST":
            yontem = request.form.get("kok_neden_yontemi", "").strip() or None
            from capa_kok_neden import build_kok_neden_from_form
            capa = CorrectiveAction(
                dof_no=next_sequence_no(db, CorrectiveAction, "dof_no", "DÖF"),
                baslik=request.form["baslik"].strip(),
                kaynak_tipi=request.form.get("kaynak_tipi", "Uygunsuzluk"),
                ilgili_document_id=_to_int(request.form.get("ilgili_document_id")),
                ilgili_surec_id=_to_int(request.form.get("ilgili_surec_id")),
                tespit_tarihi=_to_date(request.form.get("tespit_tarihi"), date.today()),
                acan_id=session.get("user_id"),
                sorumlu_id=_to_int(request.form.get("sorumlu_id")),
                tanim=request.form.get("tanim", "").strip() or None,
                kok_neden=build_kok_neden_from_form(request.form, yontem),
                kok_neden_yontemi=yontem,
                duzeltici_faaliyet=request.form.get("duzeltici_faaliyet", "").strip() or None,
                onleyici_faaliyet=request.form.get("onleyici_faaliyet", "").strip() or None,
                planlanan_tarih=_to_date(request.form.get("planlanan_tarih")),
                durum=request.form.get("durum", "Açık"),
                etkinlik_bekleme_gunu=_to_int(request.form.get("etkinlik_bekleme_gunu")) or 30,
            )
            if yontem == "8D":
                capa.d1_team = request.form.get("d1_team", "").strip() or None
                capa.d2_problem = request.form.get("d2_problem", "").strip() or None
                capa.d3_containment = request.form.get("d3_containment", "").strip() or None
                capa.d4_root_cause = request.form.get("d4_root_cause", "").strip() or None
                capa.d5_corrective = request.form.get("d5_corrective", "").strip() or None
                capa.d6_implement = request.form.get("d6_implement", "").strip() or None
                capa.d7_prevent = request.form.get("d7_prevent", "").strip() or None
                capa.d8_congratulate = request.form.get("d8_congratulate", "").strip() or None
            # Faz B: Containment, benzer analiz, FMEA/CP bağlantısı
            capa.containment_aksiyonu = request.form.get("containment_aksiyonu", "").strip() or None
            capa.benzer_analiz = request.form.get("benzer_analiz", "").strip() or None
            capa.fmea_guncellendi = bool(request.form.get("fmea_guncellendi"))
            capa.cp_guncellendi = bool(request.form.get("cp_guncellendi"))
            # Faz B: Müşteri şikayeti / saha iade / garanti ek alanları
            capa.sikayet_musteri = request.form.get("sikayet_musteri", "").strip() or None
            capa.sikayet_parca_no = request.form.get("sikayet_parca_no", "").strip() or None
            capa.sikayet_miktar = _to_int(request.form.get("sikayet_miktar"))
            capa.sikayet_aciliyet = request.form.get("sikayet_aciliyet", "").strip() or None
            capa.garanti_talebi_mi = bool(request.form.get("garanti_talebi_mi"))
            capa.saha_iade_mi = bool(request.form.get("saha_iade_mi"))
            db.add(capa)
            db.commit()
            log_action(db, "Oluşturma", detay=f"DÖF oluşturuldu: {capa.dof_no}")
            try:
                if capa.sorumlu_id:
                    capa.sorumlu = db.get(User, capa.sorumlu_id)
                    notifications.notify_capa_assigned(capa, capa.sorumlu)
            except Exception:
                logger.exception("DÖF atama bildirimi gönderilemedi")
            flash(f"DÖF oluşturuldu: {capa.dof_no}", "success")
            return redirect(url_for("capa_detail", capa_id=capa.id))

        from capa_kok_neden import kok_neden_form_context
        users = db.query(User).filter_by(aktif=True).all()
        procs = db.query(Process).order_by(Process.kod).all()
        docs = db.query(Document).order_by(Document.dokuman_no).all()
        prefill_doc = _to_int(request.args.get("document_id"))
        return render_template("capa_form.html", capa=None, users=users, processes=procs,
                               documents=docs, kaynaklar=CAPA_KAYNAKLARI, durumlar=CAPA_DURUMLARI,
                               yontemler=DOF_YONTEMLERI, prefill_doc=prefill_doc,
                               kok_analiz=kok_neden_form_context(None))
    finally:
        db.close()


@app.route("/capa/<int:capa_id>")
@login_required
def capa_detail(capa_id):
    db = get_db()
    try:
        capa = db.query(CorrectiveAction).get(capa_id)
        if not capa:
            flash("DÖF bulunamadı.", "error")
            return redirect(url_for("capa_dashboard"))
        from capa_kok_neden import format_kok_neden_blocks
        kok_bloklar = format_kok_neden_blocks(capa.kok_neden, capa.kok_neden_yontemi)
        return render_template("capa_detail.html", capa=capa, kok_bloklar=kok_bloklar)
    finally:
        db.close()


@app.route("/capa/<int:capa_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def capa_edit(capa_id):
    db = get_db()
    try:
        capa = db.query(CorrectiveAction).get(capa_id)
        if not capa:
            flash("DÖF bulunamadı.", "error")
            return redirect(url_for("capa_dashboard"))

        if request.method == "POST":
            from capa_kok_neden import build_kok_neden_from_form
            capa.baslik = request.form["baslik"].strip()
            capa.kaynak_tipi = request.form.get("kaynak_tipi", capa.kaynak_tipi)
            capa.ilgili_document_id = _to_int(request.form.get("ilgili_document_id"))
            capa.ilgili_surec_id = _to_int(request.form.get("ilgili_surec_id"))
            capa.tespit_tarihi = _to_date(request.form.get("tespit_tarihi"), capa.tespit_tarihi)
            capa.sorumlu_id = _to_int(request.form.get("sorumlu_id"))
            capa.tanim = request.form.get("tanim", "").strip() or None
            capa.kok_neden_yontemi = request.form.get("kok_neden_yontemi", "").strip() or None
            capa.kok_neden = build_kok_neden_from_form(request.form, capa.kok_neden_yontemi)
            capa.duzeltici_faaliyet = request.form.get("duzeltici_faaliyet", "").strip() or None
            capa.onleyici_faaliyet = request.form.get("onleyici_faaliyet", "").strip() or None
            capa.planlanan_tarih = _to_date(request.form.get("planlanan_tarih"))
            capa.etkinlik_kontrolu = request.form.get("etkinlik_kontrolu", "").strip() or None
            capa.durum = request.form.get("durum", capa.durum)
            bekleme = _to_int(request.form.get("etkinlik_bekleme_gunu"))
            if bekleme is not None:
                capa.etkinlik_bekleme_gunu = bekleme
            if capa.kok_neden_yontemi == "8D":
                capa.d1_team = request.form.get("d1_team", "").strip() or None
                capa.d2_problem = request.form.get("d2_problem", "").strip() or None
                capa.d3_containment = request.form.get("d3_containment", "").strip() or None
                capa.d4_root_cause = request.form.get("d4_root_cause", "").strip() or None
                capa.d5_corrective = request.form.get("d5_corrective", "").strip() or None
                capa.d6_implement = request.form.get("d6_implement", "").strip() or None
                capa.d7_prevent = request.form.get("d7_prevent", "").strip() or None
                capa.d8_congratulate = request.form.get("d8_congratulate", "").strip() or None
            else:
                # Yöntem değiştiyse eski 8D alanlarını temizleme — veri kaybını önlemek için bırak
                pass
            # Faz B: Containment, benzer analiz, FMEA/CP bağlantısı
            capa.containment_aksiyonu = request.form.get("containment_aksiyonu", "").strip() or None
            capa.benzer_analiz = request.form.get("benzer_analiz", "").strip() or None
            capa.fmea_guncellendi = bool(request.form.get("fmea_guncellendi"))
            capa.cp_guncellendi = bool(request.form.get("cp_guncellendi"))
            # Faz B: Müşteri şikayeti / saha iade / garanti ek alanları
            capa.sikayet_musteri = request.form.get("sikayet_musteri", "").strip() or None
            capa.sikayet_parca_no = request.form.get("sikayet_parca_no", "").strip() or None
            capa.sikayet_miktar = _to_int(request.form.get("sikayet_miktar"))
            capa.sikayet_aciliyet = request.form.get("sikayet_aciliyet", "").strip() or None
            capa.garanti_talebi_mi = bool(request.form.get("garanti_talebi_mi"))
            capa.saha_iade_mi = bool(request.form.get("saha_iade_mi"))
            db.commit()
            log_action(db, "Düzenleme", detay=f"DÖF güncellendi: {capa.dof_no}")
            flash(f"DÖF güncellendi: {capa.dof_no}", "success")
            return redirect(url_for("capa_detail", capa_id=capa.id))

        from capa_kok_neden import kok_neden_form_context
        users = db.query(User).filter_by(aktif=True).all()
        procs = db.query(Process).order_by(Process.kod).all()
        docs = db.query(Document).order_by(Document.dokuman_no).all()
        return render_template("capa_form.html", capa=capa, users=users, processes=procs,
                               documents=docs, kaynaklar=CAPA_KAYNAKLARI, durumlar=CAPA_DURUMLARI,
                               yontemler=DOF_YONTEMLERI, prefill_doc=None,
                               kok_analiz=kok_neden_form_context(capa.kok_neden, capa.kok_neden_yontemi))
    finally:
        db.close()


@app.route("/capa/<int:capa_id>/close", methods=["POST"])
@login_required
@admin_required
def capa_close(capa_id):
    db = get_db()
    try:
        capa = db.query(CorrectiveAction).get(capa_id)
        if capa:
            # Etkinlik doğrulaması olmadan doğrudan kapatmayı engelle.
            etkinlik = request.form.get("etkinlik_kontrolu", "").strip() or capa.etkinlik_kontrolu
            if not etkinlik:
                flash("DÖF kapatmak için etkinlik doğrulaması girilmelidir.", "error")
                return redirect(url_for("capa_detail", capa_id=capa_id))
            # IATF 16949 § 10.2.3: FMEA ve Kontrol Planı güncelleme uyarısı
            if not capa.fmea_guncellendi:
                flash("⚠️ PFMEA henüz güncellenmedi. DÖF kapatılıyor ancak FMEA güncellemesi önerilir.", "warning")
            if not capa.cp_guncellendi:
                flash("⚠️ Kontrol Planı henüz güncellenmedi. DÖF kapatılıyor ancak CP güncellemesi önerilir.", "warning")
            capa.durum = "Kapatıldı"
            capa.kapanma_tarihi = date.today()
            capa.etkinlik_kontrolu = etkinlik
            db.commit()
            log_action(db, "Düzenleme", detay=f"DÖF kapatıldı: {capa.dof_no}")
            flash(f"DÖF kapatıldı: {capa.dof_no}", "success")
        return redirect(url_for("capa_detail", capa_id=capa_id))
    finally:
        db.close()


@app.route("/capa/<int:capa_id>/reopen", methods=["POST"])
@login_required
@admin_required
def capa_reopen(capa_id):
    """Kapatılmış bir DÖF'ü tekrar açar (etkinlik doğrulanamadığında)."""
    db = get_db()
    try:
        capa = db.query(CorrectiveAction).get(capa_id)
        if capa and capa.durum == "Kapatıldı":
            capa.durum = "Devam Ediyor"
            capa.kapanma_tarihi = None
            capa.yeniden_acildi = True
            gerekce = request.form.get("gerekce", "").strip()
            if gerekce:
                capa.etkinlik_kontrolu = (
                    (capa.etkinlik_kontrolu or "") + f"\n[Yeniden açıldı] {gerekce}").strip()
            db.commit()
            log_action(db, "Düzenleme", detay=f"DÖF yeniden açıldı: {capa.dof_no}")
            flash(f"DÖF yeniden açıldı: {capa.dof_no}", "success")
        return redirect(url_for("capa_detail", capa_id=capa_id))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  QRQC (HIZLI YANIT KALİTE KONTROLÜ)
# ═══════════════════════════════════════════════════════════════════

@app.route("/qrqc")
@login_required
def qrqc_dashboard():
    """QRQC Panosu: KPI, kategori/seviye dağılımları ve acil müdahale bekleyenler."""
    db = get_db()
    try:
        tum = db.query(QRQCItem).order_by(QRQCItem.olusturma_tarihi.desc()).all()
        aciklar = [q for q in tum if q.durum != "Kapatıldı"]
        mudahele_asimi = [q for q in aciklar if q.mudahele_suresi_astimi]
        kapatilan = [q for q in tum if q.durum == "Kapatıldı"]
        dof_tetiklenen = [q for q in tum if q.dof_id or q.durum == "DÖF Tetiklendi"]

        kategori_dagilim = {k: 0 for k in QRQC_KATEGORILERI}
        seviye_dagilim = {s: 0 for s in QRQC_SEVIYELERI}
        for q in tum:
            if q.kategori in kategori_dagilim:
                kategori_dagilim[q.kategori] += 1
            if q.seviye in seviye_dagilim:
                seviye_dagilim[q.seviye] += 1

        stats = {
            "toplam": len(tum),
            "acik": len(aciklar),
            "mudahele_asimi": len(mudahele_asimi),
            "kapatilan": len(kapatilan),
            "dof_tetiklenen": len(dof_tetiklenen),
        }

        return render_template(
            "qrqc_dashboard.html",
            stats=stats,
            acik_list=aciklar[:10],
            kategori_labels=list(kategori_dagilim.keys()),
            kategori_values=list(kategori_dagilim.values()),
            seviye_labels=list(seviye_dagilim.keys()),
            seviye_values=list(seviye_dagilim.values()),
        )
    finally:
        db.close()


@app.route("/qrqc/list")
@login_required
def qrqc_list():
    """Tüm QRQC olay kayıtları listesi ve filtreleme."""
    db = get_db()
    try:
        query = db.query(QRQCItem)
        durum = request.args.get("durum")
        kategori = request.args.get("kategori")
        seviye = request.args.get("seviye")
        vardiya = request.args.get("vardiya")

        if durum:
            query = query.filter(QRQCItem.durum == durum)
        if kategori:
            query = query.filter(QRQCItem.kategori == kategori)
        if seviye:
            query = query.filter(QRQCItem.seviye == seviye)
        if vardiya:
            query = query.filter(QRQCItem.vardiya == vardiya)

        kayitlar = query.order_by(QRQCItem.olusturma_tarihi.desc()).all()
        return render_template(
            "qrqc_list.html",
            kayitlar=kayitlar,
            durumlar=QRQC_DURUMLARI,
            kategoriler=QRQC_KATEGORILERI,
            seviyeler=QRQC_SEVIYELERI,
            vardiyalar=QRQC_VARDIYALAR,
            secili_durum=durum,
            secili_kategori=kategori,
            secili_seviye=seviye,
            secili_vardiya=vardiya,
        )
    finally:
        db.close()


@app.route("/qrqc/new", methods=["GET", "POST"])
@login_required
def qrqc_new():
    """Yeni QRQC kaydı oluşturma."""
    db = get_db()
    try:
        if request.method == "POST":
            qrqc_no = next_sequence_no(db, QRQCItem, "qrqc_no", "QRQC")
            qrqc = QRQCItem(
                qrqc_no=qrqc_no,
                baslik=request.form["baslik"].strip(),
                kategori=request.form.get("kategori", "Kalite"),
                seviye=request.form.get("seviye", "Hat / Saha"),
                hat_istasyon=request.form.get("hat_istasyon", "").strip() or None,
                vardiya=request.form.get("vardiya", "").strip() or None,
                surec_id=_to_int(request.form.get("surec_id")),
                sorumlu_id=_to_int(request.form.get("sorumlu_id")),
                bildiren_id=g.user.id if hasattr(g, "user") and g.user else None,
                problem_tanimi=request.form.get("problem_tanimi", "").strip() or None,
                etkilenen_miktar=_to_int(request.form.get("etkilenen_miktar")) or 0,
                gecici_onlem=request.form.get("gecici_onlem", "").strip() or None,
                kok_neden=request.form.get("kok_neden", "").strip() or None,
                kalici_aksiyon=request.form.get("kalici_aksiyon", "").strip() or None,
                durum=request.form.get("durum", "Açık"),
                hedef_kapanis_tarihi=_to_date(request.form.get("hedef_kapanis_tarihi")),
            )
            db.add(qrqc)
            db.commit()
            log_action(db, "Oluşturma", detay=f"QRQC oluşturuldu: {qrqc.qrqc_no}")
            flash(f"QRQC kaydı oluşturuldu: {qrqc.qrqc_no}", "success")
            return redirect(url_for("qrqc_detail", qrqc_id=qrqc.id))

        users = db.query(User).filter_by(aktif=True).all()
        procs = db.query(Process).order_by(Process.kod).all()
        return render_template(
            "qrqc_form.html",
            qrqc=None,
            users=users,
            surecler=procs,
            kategoriler=QRQC_KATEGORILERI,
            seviyeler=QRQC_SEVIYELERI,
            vardiyalar=QRQC_VARDIYALAR,
            durumlar=QRQC_DURUMLARI,
        )
    finally:
        db.close()


@app.route("/qrqc/<int:qrqc_id>")
@login_required
def qrqc_detail(qrqc_id):
    """QRQC detay kartı."""
    db = get_db()
    try:
        qrqc = db.query(QRQCItem).get(qrqc_id)
        if not qrqc:
            flash("QRQC kaydı bulunamadı.", "error")
            return redirect(url_for("qrqc_dashboard"))
        return render_template("qrqc_detail.html", qrqc=qrqc)
    finally:
        db.close()


@app.route("/qrqc/<int:qrqc_id>/edit", methods=["GET", "POST"])
@login_required
def qrqc_edit(qrqc_id):
    """QRQC kaydı güncelleme."""
    db = get_db()
    try:
        qrqc = db.query(QRQCItem).get(qrqc_id)
        if not qrqc:
            flash("QRQC kaydı bulunamadı.", "error")
            return redirect(url_for("qrqc_dashboard"))

        if request.method == "POST":
            qrqc.baslik = request.form["baslik"].strip()
            qrqc.kategori = request.form.get("kategori", qrqc.kategori)
            qrqc.seviye = request.form.get("seviye", qrqc.seviye)
            qrqc.hat_istasyon = request.form.get("hat_istasyon", "").strip() or None
            qrqc.vardiya = request.form.get("vardiya", "").strip() or None
            qrqc.surec_id = _to_int(request.form.get("surec_id"))
            qrqc.sorumlu_id = _to_int(request.form.get("sorumlu_id"))
            qrqc.problem_tanimi = request.form.get("problem_tanimi", "").strip() or None
            qrqc.etkilenen_miktar = _to_int(request.form.get("etkilenen_miktar")) or 0
            qrqc.gecici_onlem = request.form.get("gecici_onlem", "").strip() or None
            qrqc.kok_neden = request.form.get("kok_neden", "").strip() or None
            qrqc.kalici_aksiyon = request.form.get("kalici_aksiyon", "").strip() or None
            qrqc.durum = request.form.get("durum", qrqc.durum)
            qrqc.hedef_kapanis_tarihi = _to_date(request.form.get("hedef_kapanis_tarihi"))

            if qrqc.durum == "Kapatıldı" and not qrqc.kapanis_tarihi:
                qrqc.kapanis_tarihi = datetime.now()

            db.commit()
            log_action(db, "Düzenleme", detay=f"QRQC güncellendi: {qrqc.qrqc_no}")
            flash(f"QRQC kaydı güncellendi: {qrqc.qrqc_no}", "success")
            return redirect(url_for("qrqc_detail", qrqc_id=qrqc.id))

        users = db.query(User).filter_by(aktif=True).all()
        procs = db.query(Process).order_by(Process.kod).all()
        return render_template(
            "qrqc_form.html",
            qrqc=qrqc,
            users=users,
            surecler=procs,
            kategoriler=QRQC_KATEGORILERI,
            seviyeler=QRQC_SEVIYELERI,
            vardiyalar=QRQC_VARDIYALAR,
            durumlar=QRQC_DURUMLARI,
        )
    finally:
        db.close()


@app.route("/qrqc/<int:qrqc_id>/close", methods=["POST"])
@login_required
def qrqc_close(qrqc_id):
    """QRQC kaydını kapatır."""
    db = get_db()
    try:
        qrqc = db.query(QRQCItem).get(qrqc_id)
        if qrqc:
            qrqc.durum = "Kapatıldı"
            qrqc.kapanis_tarihi = datetime.now()
            db.commit()
            try:
                log_action(db, "Düzenleme", detay=f"QRQC kapatıldı: {qrqc.qrqc_no}")
            except Exception:
                pass
            flash(f"QRQC kaydı kapatıldı: {qrqc.qrqc_no}", "success")
        else:
            flash("QRQC kaydı bulunamadı.", "error")
        return redirect(url_for("qrqc_detail", qrqc_id=qrqc_id))
    except Exception as e:
        db.rollback()
        logger.exception("QRQC kapatma hatası: %s", e)
        flash("QRQC kaydı kapatılırken bir hata oluştu.", "error")
        return redirect(url_for("qrqc_detail", qrqc_id=qrqc_id))
    finally:
        db.close()


@app.route("/qrqc/<int:qrqc_id>/trigger-dof", methods=["POST"])
@login_required
def qrqc_trigger_dof(qrqc_id):
    """QRQC kaydından doğrudan DÖF (CorrectiveAction / CAPA) tetikler ve bağlar."""
    db = get_db()
    try:
        qrqc = db.query(QRQCItem).get(qrqc_id)
        if not qrqc:
            flash("QRQC kaydı bulunamadı.", "error")
            return redirect(url_for("qrqc_dashboard"))

        if qrqc.dof_id:
            flash(f"Bu QRQC zaten {qrqc.dof.dof_no} numaralı DÖF ile ilişkili.", "info")
            return redirect(url_for("capa_detail", capa_id=qrqc.dof_id))

        # Otomatik DÖF oluştur
        dof_no = next_sequence_no(db, CorrectiveAction, "dof_no", "DÖF")
        dof = CorrectiveAction(
            dof_no=dof_no,
            baslik=f"[QRQC-{qrqc.qrqc_no}] {qrqc.baslik}",
            kaynak_tipi="QRQC",
            ilgili_surec_id=qrqc.surec_id,
            tespit_tarihi=qrqc.tespit_tarihi.date() if qrqc.tespit_tarihi else date.today(),
            acan_id=g.user.id if hasattr(g, "user") and g.user else qrqc.bildiren_id,
            sorumlu_id=qrqc.sorumlu_id,
            tanim=(
                f"QRQC Kaynaklı Olay: {qrqc.qrqc_no}\n"
                f"Hat/İstasyon: {qrqc.hat_istasyon or '-'}\n"
                f"Vardiya: {qrqc.vardiya or '-'}\n"
                f"Etkilenen Miktar: {qrqc.etkilenen_miktar or 0} adet\n\n"
                f"Problem Açıklaması:\n{qrqc.problem_tanimi or ''}"
            ).strip(),
            containment_aksiyonu=qrqc.gecici_onlem,
            kok_neden=qrqc.kok_neden,
            duzeltici_faaliyet=qrqc.kalici_aksiyon,
            planlanan_tarih=qrqc.hedef_kapanis_tarihi or (date.today() + timedelta(days=14)),
            durum="Devam Ediyor",
        )
        db.add(dof)
        db.flush()  # dof.id almak için

        # QRQC ilişkisini ve durumunu güncelle
        qrqc.dof_id = dof.id
        qrqc.durum = "DÖF Tetiklendi"
        db.commit()

        log_action(db, "Oluşturma", detay=f"QRQC ({qrqc.qrqc_no}) üzerinden DÖF tetiklendi: {dof.dof_no}")
        flash(f"✅ {qrqc.qrqc_no} kaydından başarıyla yeni DÖF ({dof.dof_no}) tetiklendi!", "success")
        return redirect(url_for("capa_detail", capa_id=dof.id))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  İÇ TETKİK
# ═══════════════════════════════════════════════════════════════════

@app.route("/audits/new", methods=["GET", "POST"])
@login_required
@admin_required
def audit_new():
    db = get_db()
    try:
        if request.method == "POST":
            tip = request.form.get("denetim_tipi") or "Sistem Denetimi"
            if tip not in AUDIT_TIPLERI:
                tip = "Sistem Denetimi"
            surec_id = _to_int(request.form.get("denetlenen_surec_id"))
            urun = (request.form.get("urun_adi") or "").strip() or None
            if tip == "Proses Denetimi" and not surec_id:
                flash("Proses denetimi için denetlenen süreç seçilmelidir.", "error")
                users = db.query(User).filter_by(aktif=True).all()
                procs = db.query(Process).order_by(Process.kod).all()
                return render_template(
                    "audit_form.html", users=users, processes=procs,
                    durumlar=AUDIT_DURUMLARI, standartlar=STANDARTLAR, tipler=AUDIT_TIPLERI,
                    form=request.form,
                )
            if tip == "Ürün Denetimi" and not urun:
                flash("Ürün denetimi için ürün / parça adı girilmelidir.", "error")
                users = db.query(User).filter_by(aktif=True).all()
                procs = db.query(Process).order_by(Process.kod).all()
                return render_template(
                    "audit_form.html", users=users, processes=procs,
                    durumlar=AUDIT_DURUMLARI, standartlar=STANDARTLAR, tipler=AUDIT_TIPLERI,
                    form=request.form,
                )
            audit = InternalAudit(
                tetkik_no=next_sequence_no(db, InternalAudit, "tetkik_no", "IT"),
                baslik=request.form["baslik"].strip(),
                denetim_tipi=tip,
                planlanan_tarih=_to_date(request.form.get("planlanan_tarih")),
                gerceklesen_tarih=_to_date(request.form.get("gerceklesen_tarih")),
                tetkik_eden_id=_to_int(request.form.get("tetkik_eden_id")),
                denetlenen_surec_id=surec_id,
                ilgili_standart=request.form.get("ilgili_standart") or None,
                urun_adi=urun if tip == "Ürün Denetimi" else None,
                kapsam=request.form.get("kapsam", "").strip() or None,
                durum=request.form.get("durum", "Planlandı"),
                vardiya=request.form.get("vardiya") or None,
            )
            db.add(audit)
            db.flush()
            from audit_checklist_routes import save_answers_from_form
            n_cev = save_answers_from_form(db, audit.id, request.form)
            db.commit()
            log_action(db, "Oluşturma", detay=f"İç tetkik oluşturuldu: {audit.tetkik_no} ({tip}), {n_cev} cevap")
            if n_cev:
                flash(f"İç tetkik oluşturuldu: {audit.tetkik_no} — {tip}. {n_cev} cevap kaydedildi.", "success")
            else:
                flash(f"İç tetkik oluşturuldu: {audit.tetkik_no} — {tip}", "success")
            return redirect(url_for("audit_checklist_run", audit_id=audit.id))

        users = db.query(User).filter_by(aktif=True).all()
        procs = db.query(Process).order_by(Process.kod).all()
        return render_template(
            "audit_form.html", users=users, processes=procs,
            durumlar=AUDIT_DURUMLARI, standartlar=STANDARTLAR, tipler=AUDIT_TIPLERI,
            form=None,
        )
    finally:
        db.close()


@app.route("/audits/<int:audit_id>")
@login_required
def audit_detail(audit_id):
    db = get_db()
    try:
        audit = db.query(InternalAudit).get(audit_id)
        if not audit:
            flash("İç tetkik bulunamadı.", "error")
            return redirect(url_for("audit_list"))
        return render_template(
            "audit_detail.html", audit=audit, bulgu_tipleri=BULGU_TIPLERI,
            durumlar=AUDIT_DURUMLARI, tipler=AUDIT_TIPLERI,
        )
    finally:
        db.close()


@app.route("/audits/<int:audit_id>/update", methods=["POST"])
@login_required
@admin_required
def audit_update(audit_id):
    db = get_db()
    try:
        audit = db.query(InternalAudit).get(audit_id)
        if audit:
            tip = request.form.get("denetim_tipi") or audit.denetim_tipi
            if tip in AUDIT_TIPLERI:
                audit.denetim_tipi = tip
            audit.durum = request.form.get("durum", audit.durum)
            audit.gerceklesen_tarih = _to_date(request.form.get("gerceklesen_tarih"), audit.gerceklesen_tarih)
            audit.sonuc_ozeti = request.form.get("sonuc_ozeti", "").strip() or None
            if tip == "Ürün Denetimi":
                audit.urun_adi = (request.form.get("urun_adi") or "").strip() or audit.urun_adi
            # 2026 — iç denetim otomasyonu: bulgu eşitleme + tamamlanınca DÖF
            import denetim_otomasyon as _OT
            _OT.bulgulari_esitle(db, audit)
            dof = []
            if audit.durum == "Tamamlandı" and _OT.ayar(db, "denetim_dof_otomatik") == "1":
                db.flush()
                db.expire(audit)
                dof = _OT.dof_ac(db, audit, session.get("user_id"))
            db.commit()
            flash("İç tetkik güncellendi." + (f" Açılan DÖF: {', '.join(c.dof_no for c in dof)}" if dof else ""), "success")
        return redirect(url_for("audit_detail", audit_id=audit_id))
    finally:
        db.close()


@app.route("/audits/<int:audit_id>/finding", methods=["POST"])
@login_required
@admin_required
def audit_finding_add(audit_id):
    db = get_db()
    try:
        audit = db.query(InternalAudit).get(audit_id)
        if not audit:
            flash("İç tetkik bulunamadı.", "error")
            return redirect(url_for("audit_list"))
        finding = AuditFinding(
            audit_id=audit.id,
            bulgu_tipi=request.form.get("bulgu_tipi", "Gözlem"),
            aciklama=request.form["aciklama"].strip(),
            ilgili_madde=request.form.get("ilgili_madde", "").strip() or None,
        )
        db.add(finding)
        db.commit()
        flash("Bulgu eklendi.", "success")
        return redirect(url_for("audit_detail", audit_id=audit_id))
    finally:
        db.close()


@app.route("/audits/finding/<int:finding_id>/to-capa", methods=["POST"])
@login_required
@admin_required
def audit_finding_to_capa(finding_id):
    """Bulguyu bir DÖF'e dönüştürür (ön-doldurma)."""
    db = get_db()
    try:
        finding = db.query(AuditFinding).get(finding_id)
        if not finding:
            flash("Bulgu bulunamadı.", "error")
            return redirect(url_for("audit_list"))
        if finding.capa_id:
            flash("Bu bulgu zaten bir DÖF'e bağlı.", "warning")
            return redirect(url_for("capa_detail", capa_id=finding.capa_id))

        audit = finding.audit
        capa = CorrectiveAction(
            dof_no=next_sequence_no(db, CorrectiveAction, "dof_no", "DÖF"),
            baslik=f"{audit.tetkik_no} bulgusu: {finding.bulgu_tipi}",
            kaynak_tipi="İç Tetkik",
            ilgili_surec_id=audit.denetlenen_surec_id,
            tespit_tarihi=audit.gerceklesen_tarih or date.today(),
            acan_id=session.get("user_id"),
            tanim=finding.aciklama,
            durum="Açık",
        )
        db.add(capa)
        db.flush()
        finding.capa_id = capa.id
        db.commit()
        log_action(db, "Oluşturma", detay=f"Bulgudan DÖF oluşturuldu: {capa.dof_no}")
        flash(f"Bulgu DÖF'e dönüştürüldü: {capa.dof_no}", "success")
        return redirect(url_for("capa_detail", capa_id=capa.id))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  EĞİTİM KAYITLARI
# ═══════════════════════════════════════════════════════════════════
@app.route("/training")
@login_required
def training_list():
    db = get_db()
    try:
        query = db.query(TrainingRecord)
        kullanici = _to_int(request.args.get("kullanici"))
        tip = request.args.get("tip")
        if kullanici:
            query = query.filter(TrainingRecord.kullanici_id == kullanici)
        if tip:
            query = query.filter(TrainingRecord.egitim_tipi == tip)
        kayitlar = query.order_by(TrainingRecord.egitim_tarihi.desc().nullslast()).all()

        # 60 gün içinde süresi dolacak sertifikalar
        bugun = date.today()
        yaklasan = [
            k for k in db.query(TrainingRecord).all()
            if k.gecerlilik_tarihi and 0 <= (k.gecerlilik_tarihi - bugun).days <= 60
        ]
        suresi_gecen = [
            k for k in db.query(TrainingRecord).all()
            if k.gecerlilik_tarihi and (k.gecerlilik_tarihi - bugun).days < 0
        ]
        users = db.query(User).filter_by(aktif=True).order_by(User.ad_soyad).all()
        return render_template("training_list.html", kayitlar=kayitlar, users=users,
                               tipler=EGITIM_TIPLERI, yaklasan=yaklasan, suresi_gecen=suresi_gecen)
    finally:
        db.close()


@app.route("/training/new", methods=["POST"])
@login_required
@admin_required
def training_new():
    db = get_db()
    try:
        dosya_adi = None
        dosya_yolu = None
        file = request.files.get("dosya")
        if file and file.filename:
            if not allowed_file(file.filename):
                flash(f"İzin verilmeyen dosya tipi: .{file_ext(file.filename)}", "error")
                return redirect(url_for("training_list"))
            dosya_adi, dosya_yolu = save_generic_file(file, "training", request.form.get("kullanici_id", "0"))

        kayit = TrainingRecord(
            kullanici_id=_to_int(request.form["kullanici_id"]),
            egitim_adi=request.form["egitim_adi"].strip(),
            egitim_tipi=request.form.get("egitim_tipi", "Diğer"),
            egitim_tarihi=_to_date(request.form.get("egitim_tarihi")),
            gecerlilik_tarihi=_to_date(request.form.get("gecerlilik_tarihi")),
            veren_kurum=request.form.get("veren_kurum", "").strip() or None,
            aciklama=request.form.get("aciklama", "").strip() or None,
            dosya_adi=dosya_adi,
            dosya_yolu=dosya_yolu,
        )
        db.add(kayit)
        db.commit()
        log_action(db, "Oluşturma", detay=f"Eğitim kaydı: {kayit.egitim_adi}")
        flash("Eğitim kaydı eklendi.", "success")
        return redirect(url_for("training_list"))
    finally:
        db.close()


@app.route("/training/<int:kayit_id>/delete", methods=["POST"])
@login_required
@admin_required
def training_delete(kayit_id):
    db = get_db()
    try:
        kayit = db.query(TrainingRecord).get(kayit_id)
        if kayit:
            db.delete(kayit)
            db.commit()
            flash("Eğitim kaydı silindi.", "success")
        return redirect(url_for("training_list"))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  RİSK KAYDI
# ═══════════════════════════════════════════════════════════════════
@app.route("/risks")
@login_required
def risk_list():
    db = get_db()
    try:
        query = db.query(RiskRegisterEntry)
        kategori = request.args.get("kategori")
        durum = request.args.get("durum")
        if kategori:
            query = query.filter(RiskRegisterEntry.kategori == kategori)
        if durum:
            query = query.filter(RiskRegisterEntry.durum == durum)
        kayitlar = query.order_by(RiskRegisterEntry.olusturma_tarihi.desc()).all()
        kayitlar = sorted(kayitlar, key=lambda r: r.risk_skoru, reverse=True)

        # Isı haritası: olasılık(satır) × etki(sütun) → risk sayısı
        heatmap = {(o, e): 0 for o in range(1, 6) for e in range(1, 6)}
        for r in db.query(RiskRegisterEntry).all():
            if r.durum != "Kapatıldı":
                heatmap[(r.olasilik, r.etki)] = heatmap.get((r.olasilik, r.etki), 0) + 1

        return render_template("risk_list.html", kayitlar=kayitlar, heatmap=heatmap,
                               kategoriler=RISK_KATEGORILERI, durumlar=RISK_DURUMLARI)
    finally:
        db.close()


@app.route("/risks/new", methods=["GET", "POST"])
@login_required
@admin_required
def risk_new():
    db = get_db()
    try:
        if request.method == "POST":
            risk = RiskRegisterEntry(
                risk_no=next_sequence_no(db, RiskRegisterEntry, "risk_no", "RSK"),
                surec_id=_to_int(request.form.get("surec_id")),
                kategori=request.form.get("kategori", "Kalite"),
                tanim=request.form["tanim"].strip(),
                olasilik=max(1, min(5, _to_int(request.form.get("olasilik"), 1))),
                etki=max(1, min(5, _to_int(request.form.get("etki"), 1))),
                mevcut_kontroller=request.form.get("mevcut_kontroller", "").strip() or None,
                aksiyon_plani=request.form.get("aksiyon_plani", "").strip() or None,
                sorumlu_id=_to_int(request.form.get("sorumlu_id")),
                hedef_tarih=_to_date(request.form.get("hedef_tarih")),
                durum=request.form.get("durum", "Açık"),
                gozden_gecirme_tarihi=_to_date(request.form.get("gozden_gecirme_tarihi")),
            )
            db.add(risk)
            db.commit()
            log_action(db, "Oluşturma", detay=f"Risk kaydı: {risk.risk_no}")
            flash(f"Risk kaydı oluşturuldu: {risk.risk_no}", "success")
            return redirect(url_for("risk_list"))

        users = db.query(User).filter_by(aktif=True).all()
        procs = db.query(Process).order_by(Process.kod).all()
        return render_template("risk_form.html", risk=None, users=users, processes=procs,
                               kategoriler=RISK_KATEGORILERI, durumlar=RISK_DURUMLARI)
    finally:
        db.close()


@app.route("/risks/<int:risk_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def risk_edit(risk_id):
    db = get_db()
    try:
        risk = db.query(RiskRegisterEntry).get(risk_id)
        if not risk:
            flash("Risk kaydı bulunamadı.", "error")
            return redirect(url_for("risk_list"))

        if request.method == "POST":
            risk.surec_id = _to_int(request.form.get("surec_id"))
            risk.kategori = request.form.get("kategori", risk.kategori)
            risk.tanim = request.form["tanim"].strip()
            risk.olasilik = max(1, min(5, _to_int(request.form.get("olasilik"), risk.olasilik)))
            risk.etki = max(1, min(5, _to_int(request.form.get("etki"), risk.etki)))
            risk.mevcut_kontroller = request.form.get("mevcut_kontroller", "").strip() or None
            risk.aksiyon_plani = request.form.get("aksiyon_plani", "").strip() or None
            risk.sorumlu_id = _to_int(request.form.get("sorumlu_id"))
            risk.hedef_tarih = _to_date(request.form.get("hedef_tarih"))
            risk.durum = request.form.get("durum", risk.durum)
            risk.gozden_gecirme_tarihi = _to_date(request.form.get("gozden_gecirme_tarihi"))
            db.commit()
            log_action(db, "Düzenleme", detay=f"Risk kaydı güncellendi: {risk.risk_no}")
            flash(f"Risk kaydı güncellendi: {risk.risk_no}", "success")
            return redirect(url_for("risk_list"))

        users = db.query(User).filter_by(aktif=True).all()
        procs = db.query(Process).order_by(Process.kod).all()
        return render_template("risk_form.html", risk=risk, users=users, processes=procs,
                               kategoriler=RISK_KATEGORILERI, durumlar=RISK_DURUMLARI)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  DAĞITIM GRUPLARI
# ═══════════════════════════════════════════════════════════════════
@app.route("/admin/distribution-groups")
@login_required
@admin_required
def distribution_groups():
    db = get_db()
    try:
        gruplar = db.query(DistributionGroup).order_by(DistributionGroup.ad).all()
        users = db.query(User).filter_by(aktif=True).order_by(User.ad_soyad).all()
        return render_template("distribution_groups.html", gruplar=gruplar, users=users)
    finally:
        db.close()


@app.route("/admin/distribution-groups/new", methods=["POST"])
@login_required
@admin_required
def distribution_group_new():
    db = get_db()
    try:
        ad = request.form["ad"].strip()
        if ad:
            db.add(DistributionGroup(ad=ad, aciklama=request.form.get("aciklama", "").strip() or None))
            db.commit()
            flash(f"Dağıtım grubu oluşturuldu: {ad}", "success")
        return redirect(url_for("distribution_groups"))
    finally:
        db.close()


@app.route("/admin/distribution-groups/<int:group_id>/members", methods=["POST"])
@login_required
@admin_required
def distribution_group_members(group_id):
    db = get_db()
    try:
        grp = db.query(DistributionGroup).get(group_id)
        if not grp:
            flash("Grup bulunamadı.", "error")
            return redirect(url_for("distribution_groups"))
        secili = set(_to_int(x) for x in request.form.getlist("kullanici_ids"))
        secili.discard(None)
        for m in list(grp.members):
            db.delete(m)
        db.flush()
        for uid in secili:
            db.add(DistributionGroupMember(group_id=grp.id, kullanici_id=uid))
        db.commit()
        flash(f"'{grp.ad}' üyeleri güncellendi ({len(secili)} kişi).", "success")
        return redirect(url_for("distribution_groups"))
    finally:
        db.close()


@app.route("/admin/distribution-groups/<int:group_id>/delete", methods=["POST"])
@login_required
@admin_required
def distribution_group_delete(group_id):
    db = get_db()
    try:
        grp = db.query(DistributionGroup).get(group_id)
        if grp:
            db.delete(grp)
            db.commit()
            flash("Dağıtım grubu silindi.", "success")
        return redirect(url_for("distribution_groups"))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  TOPLU ONAY
# ═══════════════════════════════════════════════════════════════════
@app.route("/approvals/bulk-approve", methods=["POST"])
@login_required
def bulk_approve():
    db = get_db()
    try:
        ids = [_to_int(x) for x in request.form.getlist("approval_ids")]
        ids = [i for i in ids if i]
        uid = session.get("user_id")
        rol = session.get("rol")
        onaylanan = 0
        atlanan = 0
        etkilenen_doc = set()
        for aid in ids:
            approval = db.get(DocumentApproval, aid)
            # Görevler ayrılığı ve yetki her adım için tek tek doğrulanır.
            if authz.approval_action_error(db, uid, rol, approval):
                atlanan += 1
                continue
            approval.durum = "Onaylandı"
            approval.kullanici_id = uid
            approval.tarih = datetime.now()
            etkilenen_doc.add(approval.document_id)
            onaylanan += 1
        db.flush()

        # Her etkilenen doküman için aktif çevrimin tüm adımları onaylandıysa yayınla
        for doc_id in etkilenen_doc:
            doc = db.get(Document, doc_id)
            aktif_wf = db.query(func.max(DocumentApproval.workflow_no)).filter_by(
                document_id=doc_id).scalar()
            son_adimlar = db.query(DocumentApproval).filter_by(
                document_id=doc_id, workflow_no=aktif_wf).all()
            if son_adimlar and all(a.durum == "Onaylandı" for a in son_adimlar):
                doc.durum = "Onaylı"
                doc.yururluk_tarihi = date.today()
                if not doc.sonraki_gozden_gecirme:
                    doc.sonraki_gozden_gecirme = date.today() + timedelta(days=365)
                aktif_rev = db.query(DocumentRevision).filter_by(
                    document_id=doc.id, revizyon_no=doc.revizyon_no
                ).order_by(DocumentRevision.id.desc()).first()
                if aktif_rev:
                    aktif_rev.onay_durumu = "Onaylandı"
                    aktif_rev.icerik_hash = file_sha256(resolve_file_path(doc.dosya_yolu, doc.dosya_adi))
        db.commit()
        log_action(db, "Onay", detay=f"Toplu onay: {onaylanan} adım onaylandı, {atlanan} atlandı")
        if onaylanan:
            flash(f"{onaylanan} onay adımı onaylandı.", "success")
        if atlanan:
            flash(f"{atlanan} adım yetki/görevler ayrılığı nedeniyle atlandı.", "warning")
        return redirect(url_for("approvals"))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  PPAP ONAY AKIŞI
# ═══════════════════════════════════════════════════════════════════
@app.route("/ppap/<int:ppap_id>/submit-approval", methods=["POST"])
@login_required
@admin_required
def ppap_submit_approval(ppap_id):
    db = get_db()
    try:
        ppap = db.query(PPAPSubmission).get(ppap_id)
        if not ppap:
            flash("PPAP bulunamadı.", "error")
            return redirect(url_for("ppap_list"))
        # Bekleyen eski adımları temizle
        for a in db.query(PPAPApproval).filter_by(ppap_id=ppap_id, durum="Bekliyor").all():
            db.delete(a)
        for adim in PPAP_ONAY_ADIMLARI:
            db.add(PPAPApproval(ppap_id=ppap_id, onay_adimi=adim, durum="Bekliyor", tarih=datetime.now()))
        ppap.durum = "İncelemede"
        db.commit()
        log_action(db, "Düzenleme", detay=f"PPAP onaya gönderildi: {ppap.parca_no}")
        flash("PPAP onay akışına gönderildi.", "success")
        return redirect(url_for("ppap_detail", ppap_id=ppap_id))
    finally:
        db.close()


@app.route("/ppap/approval/<int:approval_id>/<string:karar>", methods=["POST"])
@login_required
@admin_required
def ppap_approval_decide(approval_id, karar):
    db = get_db()
    try:
        approval = db.query(PPAPApproval).get(approval_id)
        if not approval:
            flash("Onay kaydı bulunamadı.", "error")
            return redirect(url_for("ppap_list"))
        ppap = approval.ppap
        approval.kullanici_id = session.get("user_id")
        approval.tarih = datetime.now()
        approval.aciklama = request.form.get("aciklama", "").strip() or None

        if karar == "reject":
            approval.durum = "Reddedildi"
            for a in db.query(PPAPApproval).filter_by(ppap_id=ppap.id, durum="Bekliyor").all():
                db.delete(a)
            ppap.durum = "Reddedildi"
            flash("PPAP reddedildi.", "warning")
        else:
            approval.durum = "Onaylandı"
            kalan = db.query(PPAPApproval).filter_by(ppap_id=ppap.id, durum="Bekliyor").count()
            if kalan == 0:
                ppap.durum = "Onaylandı"
                ppap.onay_tarihi = date.today()
            flash(f"{approval.onay_adimi} adımı onaylandı.", "success")
        db.commit()
        log_action(db, "Onay", detay=f"PPAP {ppap.parca_no} {approval.onay_adimi}: {approval.durum}")
        return redirect(url_for("ppap_detail", ppap_id=ppap.id))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  REVISE TEMPLATE (simple)
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════════
# ═════════════════════════════════════════════════════════════════════
#  PPAP Yönetimi Route’ları
# ═════════════════════════════════════════════════════════════════════

@app.route("/ppap")
@login_required
def ppap_list():
    """PPAP sunumlarının listesi."""
    db = get_db()
    try:
        query = db.query(PPAPSubmission)

        # Filtreler
        durum = request.args.get("durum")
        if durum:
            query = query.filter(PPAPSubmission.durum == durum)

        search = request.args.get("search", "").strip()
        if search:
            query = query.filter(
                (PPAPSubmission.parca_no.ilike(f"%{search}%")) |
                (PPAPSubmission.parca_adi.ilike(f"%{search}%")) |
                (PPAPSubmission.musteri.ilike(f"%{search}%"))
            )

        ppaps = query.order_by(PPAPSubmission.olusturma_tarihi.desc()).all()
        return render_template("ppap_list.html", ppaps=ppaps)
    finally:
        db.close()


@app.route("/ppap/new", methods=["GET", "POST"])
@login_required
@admin_required
def ppap_new():
    """Yeni PPAP sunumu oluştur."""
    db = get_db()
    try:
        if request.method == "POST":
            # M02 sürecini bul
            m02 = db.query(Process).filter_by(kod="M02").first()

            ppap = PPAPSubmission(
                parca_no=request.form["parca_no"].strip(),
                parca_adi=request.form["parca_adi"].strip(),
                musteri=request.form["musteri"].strip(),
                sunum_seviyesi=int(request.form.get("sunum_seviyesi", 3)),
                revizyon_nedeni=request.form.get("revizyon_nedeni", "").strip() or None,
                sorumlu_id=int(request.form["sorumlu_id"]) if request.form.get("sorumlu_id") else None,
                surec_id=m02.id if m02 else None,
            )
            sunum_tarihi = request.form.get("sunum_tarihi")
            if sunum_tarihi:
                ppap.sunum_tarihi = datetime.strptime(sunum_tarihi, "%Y-%m-%d").date()

            db.add(ppap)
            db.flush()

            # 18 elementi oluştur
            for no, en, tr in PPAP_ELEMENTLERI:
                elem = PPAPElement(
                    ppap_id=ppap.id,
                    element_no=no,
                    element_adi_en=en,
                    element_adi_tr=tr,
                    durum="Hazırlanmadı",
                )
                db.add(elem)

            db.commit()
            log_action(db, "Oluşturma", detay=f"PPAP sunumu oluşturuldu: {ppap.parca_no} - {ppap.musteri}")
            flash(f"PPAP sunumu '{ppap.parca_no}' başarıyla oluşturuldu.", "success")
            return redirect(url_for("ppap_detail", ppap_id=ppap.id))

        users = db.query(User).filter_by(aktif=True).all()
        return render_template("ppap_form.html", users=users, seviyeler=PPAP_SUNUM_SEVIYELERI)
    finally:
        db.close()


@app.route("/ppap/<int:ppap_id>")
@login_required
def ppap_detail(ppap_id):
    """PPAP sunum detay sayfası — 18 element durumu."""
    db = get_db()
    try:
        ppap = db.query(PPAPSubmission).get(ppap_id)
        if not ppap:
            flash("PPAP sunumu bulunamadı.", "error")
            return redirect(url_for("ppap_list"))

        log_action(db, "Görüntüleme", detay=f"PPAP görüntülendi: {ppap.parca_no}")
        docs = db.query(Document).order_by(Document.dokuman_no).all()
        return render_template("ppap_detail.html", ppap=ppap, documents=docs)
    finally:
        db.close()


@app.route("/ppap/<int:ppap_id>/element/<int:elem_no>", methods=["POST"])
@login_required
@admin_required
def ppap_element_update(ppap_id, elem_no):
    """PPAP element durumunu ve dosyasını güncelle."""
    db = get_db()
    try:
        elem = db.query(PPAPElement).filter_by(
            ppap_id=ppap_id, element_no=elem_no
        ).first()
        if not elem:
            flash("Element bulunamadı.", "error")
            return redirect(url_for("ppap_detail", ppap_id=ppap_id))

        elem.durum = request.form.get("durum", elem.durum)
        elem.notlar = request.form.get("notlar", "").strip() or None
        elem.ilgili_document_id = _to_int(request.form.get("ilgili_document_id"))

        # Dosya yükleme
        dosya = request.files.get("dosya")
        if dosya and dosya.filename:
            if not allowed_file(dosya.filename):
                flash(f"İzin verilmeyen dosya tipi: .{file_ext(dosya.filename)}", "error")
                return redirect(url_for("ppap_detail", ppap_id=ppap_id))
            filename = secure_filename(dosya.filename)
            # PPAP klasörü
            ppap_dir = os.path.join(app.config["UPLOAD_FOLDER"], "ppap", str(ppap_id))
            os.makedirs(ppap_dir, exist_ok=True)
            save_path = os.path.join(ppap_dir, f"elem{elem_no}_{filename}")
            dosya.save(save_path)
            elem.dosya_adi = filename
            elem.dosya_yolu = save_path

        elem.guncelleme_tarihi = datetime.now()
        db.commit()

        ppap = db.query(PPAPSubmission).get(ppap_id)
        log_action(db, "Düzenleme", detay=f"PPAP {ppap.parca_no} Element {elem_no} güncellendi: {elem.durum}")
        flash(f"Element {elem_no} başarıyla güncellendi.", "success")
        return redirect(url_for("ppap_detail", ppap_id=ppap_id))
    finally:
        db.close()


@app.route("/ppap/<int:ppap_id>/status", methods=["POST"])
@login_required
@admin_required
def ppap_status_update(ppap_id):
    """PPAP meta alanlarını güncelle. Durum yalnızca onay workflow sonucu değişebilir."""
    db = get_db()
    try:
        ppap = db.query(PPAPSubmission).get(ppap_id)
        if not ppap:
            flash("PPAP sunumu bulunamadı.", "error")
            return redirect(url_for("ppap_list"))

        # Durum kilidi: Onaylandı/Reddedildi/İncelemede yalnızca onay akışıyla değişir.
        istenen = request.form.get("durum", ppap.durum)
        kilitli = {"İncelemede", "Onaylandı", "Reddedildi"}
        if istenen != ppap.durum:
            if ppap.durum in kilitli or istenen in kilitli:
                flash(
                    "PPAP durumu yalnızca onay akışı sonucu değişebilir "
                    "(Onaya Gönder / Onay / Ret). Meta alanlar güncellendi.",
                    "warning",
                )
            else:
                ppap.durum = istenen

        ppap.notlar = request.form.get("notlar", "").strip() or None

        sunum_tarihi = request.form.get("sunum_tarihi")
        if sunum_tarihi:
            ppap.sunum_tarihi = datetime.strptime(sunum_tarihi, "%Y-%m-%d").date()
        else:
            ppap.sunum_tarihi = None

        # onay_tarihi yalnızca workflow Onaylandı olduğunda yazılır; elle ayarlanamaz.
        ppap.guncelleme_tarihi = datetime.now()
        db.commit()

        log_action(db, "Düzenleme", detay=f"PPAP {ppap.parca_no} meta bilgileri güncellendi (durum={ppap.durum})")
        flash(f"PPAP kaydı güncellendi (durum: {ppap.durum}).", "success")
        return redirect(url_for("ppap_detail", ppap_id=ppap_id))
    finally:
        db.close()


@app.route("/ppap/<int:ppap_id>/element/<int:elem_no>/download")
@login_required
def ppap_element_download(ppap_id, elem_no):
    """PPAP element dosyasını indir."""
    db = get_db()
    try:
        elem = db.query(PPAPElement).filter_by(ppap_id=ppap_id, element_no=elem_no).first()
        if not elem or not elem.dosya_yolu or not os.path.exists(elem.dosya_yolu):
            flash("Dosya bulunamadı.", "error")
            return redirect(url_for("ppap_detail", ppap_id=ppap_id))

        ppap = db.query(PPAPSubmission).get(ppap_id)
        log_action(db, "İndirme", detay=f"PPAP {ppap.parca_no} Element {elem_no} dosyası indirildi: {elem.dosya_adi}")

        file_path = elem.dosya_yolu
        directory = os.path.dirname(file_path)
        filename = os.path.basename(file_path)

        class _PpapDoc:
            dokuman_no = f"PPAP-{ppap.parca_no}"
            revizyon_no = 0
            dokuman_tipi = "PPAP"
            durum = "Onaylı" if ppap.durum == "Onaylandı" else ("İptal" if ppap.durum == "Reddedildi" else "Taslak")
            guvenlik_sinifi = None
            dosya_adi = elem.dosya_adi

        issued = _send_issued_copy(_PpapDoc(), file_path, purpose="ppap", as_attachment=True)
        if issued:
            return issued
        return send_from_directory(directory, filename, as_attachment=True, download_name=elem.dosya_adi)
        return send_from_directory(directory, filename, as_attachment=True, download_name=elem.dosya_adi)
    finally:
        db.close()


@app.route("/ppap/<int:ppap_id>/element/<int:elem_no>/preview")
@login_required
def ppap_element_preview(ppap_id, elem_no):
    """PPAP element dosyasını önizle."""
    db = get_db()
    try:
        elem = db.query(PPAPElement).filter_by(ppap_id=ppap_id, element_no=elem_no).first()
        if not elem or not elem.dosya_yolu or not os.path.exists(elem.dosya_yolu):
            flash("Dosya bulunamadı.", "error")
            return redirect(url_for("ppap_detail", ppap_id=ppap_id))

        ppap = db.query(PPAPSubmission).get(ppap_id)
        log_action(db, "Görüntüleme", detay=f"PPAP {ppap.parca_no} Element {elem_no} dosyası önizlendi: {elem.dosya_adi}")

        # Base64 encode
        import base64
        ext = os.path.splitext(elem.dosya_adi)[1].lower().lstrip(".")
        if ext not in ['pdf', 'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'bmp', 'xlsx', 'xls', 'csv', 'txt', 'json', 'xml', 'docx']:
            file_data_b64 = None
        else:
            with open(elem.dosya_yolu, "rb") as f:
                file_data_b64 = base64.b64encode(f.read()).decode("utf-8")

        return render_template(
            "ppap_element_preview.html",
            ppap=ppap,
            elem=elem,
            file_data_b64=file_data_b64,
            ext=ext,
            download_url=url_for("ppap_element_download", ppap_id=ppap_id, elem_no=elem_no),
        )
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
#  Manuel Özet Tetikleyici + Zamanlanmış Görev
# ═══════════════════════════════════════════════════════════════════
@app.route("/admin/send-digest", methods=["POST"])
@login_required
@admin_only
def admin_send_digest():
    send_daily_digest()
    flash("Günlük özet gönderimi tetiklendi (SMTP yapılandırılmışsa e-posta gider).", "success")
    return redirect(url_for("reports"))


def _init_scheduler():
    """SCHEDULER_ENABLED ise günlük özet ve DÖF hatırlatma görevlerini planlar."""
    if not Config.SCHEDULER_ENABLED:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(send_daily_digest, "cron", hour=Config.DAILY_DIGEST_HOUR, minute=0,
                          id="daily_digest", replace_existing=True)
        scheduler.add_job(send_capa_reminders, "cron", hour=Config.CAPA_REMINDER_HOUR, minute=0,
                          id="capa_reminders", replace_existing=True)
        import denetim_otomasyon  # 2026: plandan otomatik tetkik + denetim hatırlatmaları
        scheduler.add_job(denetim_otomasyon.gunluk_gorev, "cron", hour=Config.CAPA_REMINDER_HOUR, minute=15,
                          id="denetim_otomasyon", replace_existing=True)
        scheduler.start()
        logger.info(
            "Zamanlanmış görevler başlatıldı (özet %02d:00, DÖF hatırlatma %02d:00).",
            Config.DAILY_DIGEST_HOUR, Config.CAPA_REMINDER_HOUR,
        )
    except Exception:
        logger.exception("Zamanlayıcı başlatılamadı.")


_init_scheduler()


# ═══════════════════════════════════════════════════════════════════
#  Uygulama Başlatma
# ═══════════════════════════════════════════════════════════════════
# ── Blueprint iskeleti (Faz 1) + mevcut route modülleri ─────────────────────
# Blueprint'ler kademeli migrasyon için; mevcut *_routes.py birincil kaynak kalır.
from blueprints.auth_bp import auth_bp  # noqa: E402
from blueprints.documents_bp import documents_bp  # noqa: E402
from blueprints.ims_bp import ims_bp  # noqa: E402
from blueprints.iatf_bp import iatf_bp  # noqa: E402
from blueprints.env_bp import env_bp  # noqa: E402
from blueprints.ohs_bp import ohs_bp  # noqa: E402
from blueprints.isms_bp import isms_bp  # noqa: E402
from blueprints.reports_bp import reports_bp  # noqa: E402
from blueprints.admin_bp import admin_bp  # noqa: E402

app.register_blueprint(auth_bp)
app.register_blueprint(documents_bp)
app.register_blueprint(ims_bp)
app.register_blueprint(iatf_bp)
app.register_blueprint(env_bp)
app.register_blueprint(ohs_bp)
app.register_blueprint(isms_bp)
app.register_blueprint(reports_bp)
app.register_blueprint(admin_bp)

import ims_routes  # noqa: E402,F401
import iatf_routes  # noqa: E402,F401
import env_routes  # noqa: E402,F401
import ohs_routes  # noqa: E402,F401
import isms_routes  # noqa: E402,F401
import competency_routes  # noqa: E402,F401
import audit_program_routes  # noqa: E402,F401
try:
    import kpi_routes  # noqa: E402,F401
except ImportError:
    pass
import audit_routes  # noqa: E402,F401
import audit_checklist_routes  # noqa: E402,F401
import fmea_routes  # noqa: E402,F401
import pokayoke_routes  # noqa: E402,F401
import warranty_routes  # noqa: E402,F401
import form_routes  # noqa: E402,F401  — Dinamik Formlar (2026)
import modul_routes  # noqa: E402,F401  — Modül Yönetimi (2026)
import denetim_routes  # noqa: E402,F401  — İç denetim otomasyonu / denetçi yetkinlik (2026)
import modul_dokuman  # noqa: E402,F401  — Modül ↔ doküman eşlemesi (2026)
import kalibrasyon_routes  # noqa: E402,F401  — Kalibrasyon cihaz kartı + sertifika (2026)
import enjeksiyon_routes  # noqa: E402,F401  — M03 F08 Ürün Enjeksiyon Parametreleri (2026)
import dms_routes  # noqa: E402,F401  — İşlerim (tek iş kutusu) + atıf bütünlüğü raporu (2026)
import strateji_routes  # noqa: E402,F401  — Stratejik Planlama modülü (Y01 F02 / F03) (2026)
import pk_routes  # noqa: E402,F401  — Performans Değerlendirme ve CAPA (kpi.debak.com'un DYS'deki hali) (2026)
import toplanti_routes  # noqa: E402,F401  — Toplantı Tutanakları (Y01.4 İletişim) (2026)
import kalite_routes  # noqa: E402,F401  — Kalite: Tedarikçi Uygunsuzlukları (D02 F11) + Müşteri Şikayetleri / 8D (Y01.7 F03) (2026)
import teknik_resim_routes  # noqa: E402,F401  2026 — teknik resim takip ve revizyon
import bakim_routes  # noqa: E402,F401  2026 — D03 Bakım Formu (tek form: makine / kalıp + bakım türü)
import fmea_sayfa_routes  # noqa: E402,F401  2026 — FMEA çalışma sayfaları (D / P / MSR, GSI-RD-370)
import fmea_modul_routes  # noqa: E402,F401  2026 — FMEA Modülü (referans uygulama birebir; kayıt DYS veritabanında)
import urun_proses_routes  # noqa: E402,F401  2026 — Ürün prosesi: FMEA + kontrol planı otomatik, yalnız ölçü girişi
import ppap_dosya_routes  # noqa: E402,F401  2026 — PPAP dosyası: ürün ağacı → otomatik iş akışı + FMEA + kontrol planı + revizyon
import dogrulama_routes  # noqa: E402,F401  2026 — D04 F03 Mastar & Cihaz Doğrulama
import yeniden_islem_routes  # noqa: E402,F401  2026 — Yeniden İşlem Takip (D06 F06)


if __name__ == "__main__":
    # Güvenlik ağı: domain route'ları (/ims vb.) hangi instance'ta kayıtlıysa onu sun.
    import sys
    import logging as _logging

    def _has_domain_routes(flask_app):
        try:
            rules = {r.rule for r in flask_app.url_map.iter_rules()}
        except Exception:
            return False
        return "/ims" in rules or "/competency" in rules

    _candidates = []
    for _mod in (sys.modules.get("__main__"), sys.modules.get("app")):
        if _mod is None:
            continue
        _fa = getattr(_mod, "app", None)
        if _fa is not None and _fa not in _candidates:
            _candidates.append(_fa)

    _serve = app
    for _fa in _candidates:
        if _has_domain_routes(_fa):
            _serve = _fa
            break
    else:
        if not _has_domain_routes(_serve):
            _logging.getLogger("dys").error(
                "Domain route'ları (/ims, /competency) kayıtlı değil; "
                "IMS/IATF/ENV/OHS/ISMS 404 verebilir. start_dys.py ile başlatın."
            )

    init_db()

    # Demo verisi yalnızca üretim dışında ve açıkça izin verildiğinde yüklenir.
    if Config.AUTO_SEED:
        db = get_db()
        try:
            if not db.query(User).first():
                db.close()
                from seed_data import seed_all
                seed_all()
            else:
                db.close()
        except Exception:
            db.close()
    else:
        logger.info("AUTO_SEED kapalı: otomatik demo verisi yüklenmedi.")

    try:
        search_index.backfill_if_empty()
    except Exception:
        logger.exception("FTS backfill başlatılamadı.")

    print("\n" + "=" * 60)
    print("  DYS — Doküman Yönetim Sistemi")
    print("  IATF 16949 · ISO 14001 · ISO 45001 · ISO 27001")
    print("=" * 60)
    print("  http://127.0.0.1:5000")
    # Varsayılan hesap bilgisi yalnızca üretim dışında gösterilir.
    if Config.SHOW_STARTUP_CREDENTIALS:
        print("  Giriş: admin@dys.com / admin123")
    print("=" * 60 + "\n")

    print("URL MAP POKAYOKE:", [r.rule for r in _serve.url_map.iter_rules() if "pokayoke" in r.rule])
    print("URL MAP WARRANTY:", [r.rule for r in _serve.url_map.iter_rules() if "warranty" in r.rule])
    _serve.run(debug=False, host="127.0.0.1", port=5000)

