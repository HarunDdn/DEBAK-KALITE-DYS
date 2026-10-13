"""
SQLite → MS SQL Server tam veri aktarımı
=========================================
SQLite veritabanındaki TÜM model tablolarını (dosya_depo içindeki dosyalar dahil)
MS SQL Server'a kopyalar.

Güvenlik:
  * Varsayılan KURU ÇALIŞMA: yalnız ön kontrol yapar, hiçbir şey yazmaz.
  * --uygula olmadan hedefe yazılmaz.
  * Hedefte veri varsa --temizle --onay <VERITABANI_ADI> olmadan durur.
  * Şema SİLİNMEZ (drop yok): eksik tablo/kolon eklenir, satırlar silinip yeniden yazılır.
  * Tek transaction: bir hata olursa hedef eski hâline döner.
  * Kaynak SQLite salt okunur açılır.

Kullanım (debakdys klasöründe, sanal ortam aktif):
    # 1) Ön kontrol (yazmaz)
    python migrate_sqlite_to_mssql.py --sqlite ..\\_deneme\\dys_deneme.db
    # 2) Boş hedefe aktar
    python migrate_sqlite_to_mssql.py --sqlite ..\\_deneme\\dys_deneme.db --uygula
    # 3) Dolu hedefi temizleyip aktar (önce TAM YEDEK alın)
    python migrate_sqlite_to_mssql.py --sqlite dys.db --uygula --temizle --onay DBKDYS

Hedef: --hedef verilmezse .env (DYS_DATABASE_URL veya DYS_MSSQL_*) kullanılır.
    --hedef "mssql+pyodbc://@(localdb)\\MSSQLLocalDB/DYS_TEST?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Bu tablolar SQLite'a özel (FTS5 arama indeksi); MSSQL'e taşınmaz.
SQLITE_OZEL_ONEKLER = ("document_fts", "sqlite_")
BATCH_SATIR = 1000
BATCH_BAYT = 16 * 1024 * 1024  # dosya_depo gibi büyük kolonlar için
MSSQL_MIN_TARIH = datetime(1753, 1, 1)


def _parse_args():
    p = argparse.ArgumentParser(description="SQLite → MSSQL tam veri aktarımı")
    p.add_argument("--sqlite", default=os.path.join(_BASE_DIR, "dys.db"), help="Kaynak SQLite dosyası")
    p.add_argument("--hedef", help="Hedef SQLAlchemy URL (verilmezse .env)")
    p.add_argument("--uygula", action="store_true", help="Aktarımı gerçekten yap (yoksa yalnız ön kontrol)")
    p.add_argument("--temizle", action="store_true", help="Hedefteki mevcut satırları sil")
    p.add_argument("--onay", help="--temizle için hedef veritabanı adı (yanlış hedefe karşı)")
    p.add_argument("--zorla", action="store_true",
                   help="Ön kontroldeki engelleyici bulgulara rağmen devam et (bkz. çıktı)")
    return p.parse_args()


ARGS = _parse_args()
if ARGS.hedef:
    os.environ["DYS_DATABASE_URL"] = ARGS.hedef

# models modül düzeyinde engine kurar; hedef URL'den önce import edilmemeli.
sys.path.insert(0, _BASE_DIR)
from sqlalchemy import Boolean, Date, DateTime, LargeBinary, String, Text, create_engine, text  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402

import models  # noqa: E402
from models import Base  # noqa: E402


# Yeni oluşturulan tablolarda Türkçe/özel karakter kaybı olmasın (canlı şema ile aynı: NVARCHAR)
@compiles(String, "mssql")
def _nvarchar(t, compiler, **kw):
    return f"NVARCHAR({t.length})" if t.length else "NVARCHAR(max)"


@compiles(Text, "mssql")
def _nvarchar_max(t, compiler, **kw):
    return "NVARCHAR(max)"


@compiles(LargeBinary, "mssql")
def _varbinary_max(t, compiler, **kw):
    return "VARBINARY(max)"


# ── Tip dönüşümleri ─────────────────────────────────────────────────────────
def _kind(col):
    t = col.type
    for _ in range(5):
        if isinstance(t, (DateTime, Date, Boolean)):
            return type(t)
        impl = getattr(t, "impl", None)
        if impl is None or impl is t:
            break
        t = impl
    return None


class Gecersiz(Exception):
    pass


def _parse_dt(val):
    if val is None or isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day)
    s = str(val).strip()
    if not s or s.lower() in ("none", "null"):
        return None
    if s.endswith("Z"):
        s = s[:-1]
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        raise Gecersiz(s)


def _donustur(val, kind):
    if val is None:
        return None
    if kind is DateTime:
        return _parse_dt(val)
    if kind is Date:
        dt = _parse_dt(val)
        return dt.date() if dt else None
    if kind is Boolean:
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on", "evet")
        return bool(val)
    return val


# ── Yardımcılar ────────────────────────────────────────────────────────────
def _sqlite_engine(path):
    p = os.path.abspath(path).replace(os.sep, "/")
    return create_engine(f"sqlite:///file:{p}?mode=ro&uri=true", future=True)


def _q(name):
    return "[" + name.replace("]", "]]") + "]"


def _hedef_kolonlar(dc):
    """{tablo: {kolon: (tip, max_uzunluk, kod_sayfası)}}"""
    rows = dc.execute(text(
        "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, "
        "CAST(COLLATIONPROPERTY(COLLATION_NAME, 'CodePage') AS INT) "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = SCHEMA_NAME()"
    )).fetchall()
    out = {}
    for t, c, typ, ln, cp in rows:
        out.setdefault(t, {})[c] = (typ.lower(), ln, cp)
    return out


def _identity_tablolar(dc):
    return {r[0] for r in dc.execute(text(
        "SELECT OBJECT_NAME(object_id) FROM sys.identity_columns "
        "WHERE OBJECTPROPERTY(object_id, 'IsUserTable') = 1"
    ))}


# ── Ön kontrol ─────────────────────────────────────────────────────────────
def on_kontrol(sc, tablolar, hedef_kol):
    """Engelleyici/uyarı bulguları, kaynak satır sayıları ve yetim FK'lı tablolar."""
    engel, uyari, sayilar, yetim_tablolar = [], [], {}, set()
    src_tablolar = {r[0] for r in sc.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}

    for ad in sorted(src_tablolar - set(Base.metadata.tables)):
        if not ad.startswith(SQLITE_OZEL_ONEKLER):
            uyari.append(f"{ad}: modelde yok, AKTARILMAYACAK")

    for t in tablolar:
        if t.name not in src_tablolar:
            uyari.append(f"{t.name}: kaynakta yok, boş kalacak")
            continue
        src_kol = [r[1] for r in sc.execute(text(f'PRAGMA table_info("{t.name}")'))]
        fazla = set(src_kol) - set(t.c.keys())
        if fazla:
            uyari.append(f"{t.name}: modelde olmayan kolonlar aktarılmayacak: {sorted(fazla)}")
        n = sc.execute(text(f'SELECT COUNT(*) FROM "{t.name}"')).scalar()
        sayilar[t.name] = n
        if not n:
            continue
        hk = hedef_kol.get(t.name, {})
        for c in t.columns:
            if c.name not in src_kol:
                if not c.nullable and c.default is None and c.server_default is None and not c.primary_key:
                    engel.append(f"{t.name}.{c.name}: kaynakta yok ama hedefte NOT NULL")
                continue
            kind = _kind(c)
            if kind in (DateTime, Date):
                hatali, eski = 0, 0
                for (v,) in sc.execute(text(f'SELECT "{c.name}" FROM "{t.name}" WHERE "{c.name}" IS NOT NULL')):
                    try:
                        d = _parse_dt(v)
                    except Gecersiz:
                        hatali += 1
                        continue
                    if d is not None and d < MSSQL_MIN_TARIH and kind is DateTime:
                        eski += 1
                if hatali:
                    engel.append(f"{t.name}.{c.name}: {hatali} satırda okunamayan tarih")
                if eski:
                    engel.append(f"{t.name}.{c.name}: {eski} satırda 1753 öncesi tarih (MSSQL DATETIME almaz)")
                continue
            # Metin: hedef kolon uzunluğu ve kod sayfası
            typ, ln, cp = hk.get(c.name, (None, None, None))
            limit = ln if ln and ln > 0 else getattr(c.type, "length", None) if typ is None else None
            if limit:
                m = sc.execute(text(f'SELECT MAX(LENGTH("{c.name}")) FROM "{t.name}"')).scalar()
                if m and m > limit:
                    engel.append(f"{t.name}.{c.name}: veri {m} karakter, hedef sınır {limit}")
            if typ in ("varchar", "char", "text") and cp:
                kayip = 0
                for (v,) in sc.execute(text(f'SELECT "{c.name}" FROM "{t.name}" WHERE "{c.name}" IS NOT NULL')):
                    if isinstance(v, str):
                        try:
                            v.encode(f"cp{cp}")
                        except (UnicodeEncodeError, LookupError):
                            kayip += 1
                if kayip:
                    engel.append(f"{t.name}.{c.name}: hedef {typ} (kod sayfası {cp}); "
                                 f"{kayip} değerde Türkçe harf bozulur (ş→s, ı→i, İ→I, ğ→g) → kolonu NVARCHAR yapın")

    yetim = sc.execute(text("PRAGMA foreign_key_check")).fetchall()
    if yetim:
        ozet = {}
        for tablo, _rowid, ebeveyn, _fk in yetim:
            ozet[(tablo, ebeveyn)] = ozet.get((tablo, ebeveyn), 0) + 1
            yetim_tablolar.add(tablo)
        for (tablo, ebeveyn), k in sorted(ozet.items()):
            uyari.append(f"{tablo}: {k} satır silinmiş {ebeveyn} kaydına bağlı (yetim FK); "
                         "aynen aktarılır, bu tablonun FK'ları eski satırlar doğrulanmadan açılır")
    return engel, uyari, sayilar, yetim_tablolar


# ── Kopyalama ──────────────────────────────────────────────────────────────
def _kopyala(sc, dc, t, identity):
    src_kol = {r[1] for r in sc.execute(text(f'PRAGMA table_info("{t.name}")'))}
    cols = [c for c in t.columns if c.name in src_kol]
    kinds = [_kind(c) for c in cols]
    ins = text(f"INSERT INTO {_q(t.name)} ({', '.join(_q(c.name) for c in cols)}) "
               f"VALUES ({', '.join(':p%d' % i for i in range(len(cols)))})")
    sel = text(f'SELECT {", ".join(chr(34) + c.name + chr(34) for c in cols)} FROM "{t.name}"')

    if identity:
        dc.execute(text(f"SET IDENTITY_INSERT {_q(t.name)} ON"))
    n, batch, bayt = 0, [], 0
    res = sc.execution_options(stream_results=True).execute(sel)
    while True:
        rows = res.fetchmany(BATCH_SATIR)
        if not rows:
            break
        for r in rows:
            d = {}
            for i, (v, k) in enumerate(zip(r, kinds)):
                d[f"p{i}"] = _donustur(v, k)
                if isinstance(v, (bytes, str)):
                    bayt += len(v)
            batch.append(d)
            if len(batch) >= BATCH_SATIR or bayt >= BATCH_BAYT:
                dc.execute(ins, batch)
                n += len(batch)
                batch, bayt = [], 0
    if batch:
        dc.execute(ins, batch)
        n += len(batch)
    if identity:
        dc.execute(text(f"SET IDENTITY_INSERT {_q(t.name)} OFF"))
        if n:
            dc.execute(text(f"DBCC CHECKIDENT ('{t.name}', RESEED) WITH NO_INFOMSGS"))
    return n


def _dogrula(sc, dc, tablolar, sayilar):
    hatalar = []
    for t in tablolar:
        if t.name not in sayilar:
            continue
        hn = dc.execute(text(f"SELECT COUNT(*) FROM {_q(t.name)}")).scalar()
        if hn != sayilar[t.name]:
            hatalar.append(f"{t.name}: kaynak {sayilar[t.name]}, hedef {hn} satır")
        for c in t.columns:
            if isinstance(c.type, LargeBinary):
                kb = sc.execute(text(f'SELECT COALESCE(SUM(LENGTH("{c.name}")),0) FROM "{t.name}"')).scalar()
                hb = dc.execute(text(f"SELECT COALESCE(SUM(CAST(DATALENGTH({_q(c.name)}) AS BIGINT)),0) "
                                     f"FROM {_q(t.name)}")).scalar()
                if kb != hb:
                    hatalar.append(f"{t.name}.{c.name}: kaynak {kb} bayt, hedef {hb} bayt")
    return hatalar


# ── Ana akış ───────────────────────────────────────────────────────────────
def main():
    if not os.path.isfile(ARGS.sqlite):
        sys.exit(f"[HATA] SQLite bulunamadı: {ARGS.sqlite}")
    if not models._is_mssql:
        sys.exit(f"[HATA] Hedef MSSQL değil: {models.engine.url.render_as_string(hide_password=True)}\n"
                 "        --hedef verin veya .env içinde DYS_MSSQL_* ayarlayın.")

    src = _sqlite_engine(ARGS.sqlite)
    dst = create_engine(models.engine.url, pool_pre_ping=True, future=True, fast_executemany=True)

    print(f"[..] Kaynak SQLite : {os.path.abspath(ARGS.sqlite)}")
    print(f"[..] Hedef MSSQL   : {dst.url.render_as_string(hide_password=True)}")
    with dst.connect() as c:
        db_adi, sunucu = c.execute(text("SELECT DB_NAME(), @@SERVERNAME")).fetchone()
    print(f"[OK] Hedef bağlantı: veritabanı={db_adi} sunucu={sunucu}")

    tablolar = list(Base.metadata.sorted_tables)

    # Hedefte mevcut veri
    with dst.connect() as dc:
        mevcut = {}
        for ad in set(_hedef_kolonlar(dc)) & set(Base.metadata.tables):
            k = dc.execute(text(f"SELECT COUNT(*) FROM {_q(ad)}")).scalar()
            if k:
                mevcut[ad] = k
    if mevcut:
        print(f"[!!] Hedefte {len(mevcut)} tabloda toplam {sum(mevcut.values())} satır VAR.")
        if ARGS.uygula and not ARGS.temizle:
            sys.exit("[DUR] Hedef boş değil. Silip yeniden yazmak için: --temizle --onay " + db_adi)
        if ARGS.temizle and (ARGS.onay or "") != db_adi:
            sys.exit(f"[DUR] --temizle için --onay {db_adi} gerekli (verilen: {ARGS.onay!r}).")

    # Şema: yalnız uygula modunda (eksik tablo/kolonlar eklenir, hiçbir şey silinmez)
    if ARGS.uygula:
        print("[..] Hedef şema hazırlanıyor (init_db: eksik tablo/kolon eklenir)...")
        models.init_db()

    with src.connect() as sc, dst.connect() as dc:
        hedef_kol = _hedef_kolonlar(dc)
        engel, uyari, sayilar, yetim_tablolar = on_kontrol(sc, tablolar, hedef_kol)

    print(f"[..] Kaynak: {len(sayilar)} tablo, {sum(sayilar.values())} satır")
    for u in uyari:
        print(f"  [uyarı] {u}")
    for e in engel:
        print(f"  [ENGEL] {e}")
    if not ARGS.uygula:
        print("[OK] Ön kontrol bitti (KURU ÇALIŞMA, hedefe yazılmadı). Aktarmak için --uygula ekleyin.")
        return
    if engel and not ARGS.zorla:
        sys.exit("[DUR] Engelleyici bulgular var. Düzeltin ya da bilerek --zorla ile devam edin.")

    eksik = [t.name for t in tablolar if t.name not in hedef_kol]
    if eksik:
        sys.exit(f"[HATA] Şema oluşturulamadı, hedefte eksik tablolar: {eksik}")

    bas = time.time()
    with src.connect() as sc, dst.begin() as dc:
        # Herhangi bir hata transaction'ın tamamını geri alsın (yarım aktarım kalmasın)
        dc.execute(text("SET XACT_ABORT ON"))
        identity = _identity_tablolar(dc)
        print("[..] FK kısıtları geçici olarak kapatılıyor...")
        for t in tablolar:
            dc.execute(text(f"ALTER TABLE {_q(t.name)} NOCHECK CONSTRAINT ALL"))
        if mevcut:
            print("[..] Hedefteki mevcut satırlar siliniyor...")
            for t in reversed(tablolar):
                dc.execute(text(f"DELETE FROM {_q(t.name)}"))

        toplam = 0
        for t in tablolar:
            if t.name not in sayilar:
                continue
            n = _kopyala(sc, dc, t, t.name in identity)
            toplam += n
            print(f"  + {t.name}: {n}")

        print("[..] FK kısıtları yeniden açılıyor ve doğrulanıyor...")
        kontrolsuz = sorted(yetim_tablolar)
        for t in tablolar:
            # Yetim satırlı tablolarda eski satırlar doğrulanmaz; yeni kayıtlar yine denetlenir
            mod = "" if t.name in yetim_tablolar else "WITH CHECK "
            dc.execute(text(f"ALTER TABLE {_q(t.name)} {mod}CHECK CONSTRAINT ALL"))

        hatalar = _dogrula(sc, dc, tablolar, sayilar)
        if hatalar:
            for h in hatalar:
                print(f"  [FARK] {h}")
            raise SystemExit("[HATA] Doğrulama başarısız — tüm değişiklikler geri alındı.")

    sure = time.time() - bas
    print(f"[OK] Aktarım tamam: {toplam} satır, {sure:.0f} sn. Satır sayıları ve dosya boyutları doğrulandı.")
    if kontrolsuz:
        print(f"[i] Yetim kayıtlar nedeniyle FK'ları eski satırlar doğrulanmadan açılan tablolar: {kontrolsuz}")
    print("[i] Arama indeksi SQLite'a özeldir; MSSQL'de uygulama kendi aramasını kullanır.")


if __name__ == "__main__":
    main()
