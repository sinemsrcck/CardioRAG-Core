"""
Hoca sunum veritabanı (kardiyo_sunum): İngilizce, salt okunur view'lar.

    python -m src.sunum_hoca --kur      # veritabanını (yoksa) oluşturur, view'ları kurar; tekrar çalıştırılabilir
    python -m src.sunum_hoca --durum    # kurulu view'lar ve satır sayıları

Kaynak veritabanı .env'deki MYSQL_DATABASE'tir (bağlantının veritabanı); sunum veritabanının
adı MYSQL_SUNUM_DATABASE (varsayılan kardiyo_sunum). Veritabanı adları SQL dosyalarına yazılmaz:
sql/sunum_hoca/*.sql içindeki {{KAYNAK_DB}} ve {{SUNUM_DB}} yer tutucuları kurulumda doldurulur.

Dosyalar ADA GÖRE sırayla çalışır (00_ortak.sql önce); klasöre sonradan eklenen dosyalar
(ör. Sinem'in 20_diseases.sql, 21_diagnostics.sql, 22_symptoms.sql) otomatik alınır.
Her ifade CREATE OR REPLACE VIEW / DROP VIEW IF EXISTS olduğu için kurulum tekrar çalıştırılabilir.

Ortak taban view (v_sunum_taban) KAYNAK veritabanında kurulur; sunum veritabanında yalnız hocaya
gösterilen view'lar kalır (eski iç view _findings_base kurulumda silinir). Kaynak veritabanındaki
tablolara ve diğer view'lara (v_pilot_bulgular vb.) dokunulmaz.

Gereken yetkiler (kurulumu yapan MySQL kullanıcısı):
  - CREATE           : sunum veritabanını ilk kez oluşturmak için (CREATE DATABASE)
  - CREATE VIEW, DROP: sunum veritabanında (CREATE OR REPLACE VIEW, eski _findings_base için DROP VIEW)
  - CREATE VIEW, DROP: kaynak veritabanında yalnız v_sunum_taban için (CREATE OR REPLACE VIEW)
  - SELECT, SHOW VIEW: kaynak veritabanında okunan tablo/view'larda (v_pilot_bulgular ve onun okuduğu tablolar)
Hocanın kullanıcısı için yalnız: GRANT SELECT ON kardiyo_sunum.* TO <HOCA_KULLANICI>;
View'lar SQL SECURITY DEFINER ile oluşur; mysqldump ile başka sunucuya taşınmamalı (DEFINER
satırı orada hata verir), her makinede bu komutla kurulmalı.

measurements view'ındaki group_N sütunları verideki en fazla grup sayısına göre üretilir;
yeni veride daha çok grup olursa kurulum yeniden çalıştırılmalıdır.
"""

import argparse
import os
import re
import sys
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent.parent / "sql" / "sunum_hoca"
DEFAULT_SUNUM_DB = "kardiyo_sunum"
IDENT = re.compile(r"^[A-Za-z0-9_]{1,64}$")
PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")
GROUP_PLACEHOLDER = "MEASUREMENT_GROUP_COLUMNS"
MIN_GROUPS = 2
# Hoca view'larında görünmemesi gereken teknik sütunlar (testler de bunu kullanır).
TECHNICAL_COLUMNS = {"bulgu_id", "id", "document_id", "satir_hash", "taslak_notu", "chunk_id", "makale_id",
                     "review_status", "cikarma_yontemi", "row_key", "page_sort", "kanit_turu", "bilgi_turu",
                     "baglam", "tablo_no", "satir_no", "filename"}
BASE_VIEW = "v_sunum_taban"  # kaynak veritabanında; sunum veritabanına konmaz
OLD_BASE_VIEW = "_findings_base"  # eski yer (sunum veritabanı); kurulum siler
# Kullanıcıya açık view'ların beklenen sütunları.
EXPECTED_COLUMNS = {
    "measurements_by_group": ["parameter", "group_order", "group_name", "group_value", "unit", "p_value",
                              "evidence", "source", "page"],
    "treatments": ["treatment_type", "treatment_name", "clinical_context", "evidence", "source", "page"],
    "clinical_outcomes": ["finding", "statistical_result", "clinical_context", "evidence", "source", "page"],
    "general_clinical_findings": ["clinical_finding", "clinical_context", "evidence", "source", "page"],
}


class SunumError(Exception):
    pass


def measurement_columns(n_groups):
    """measurements view'ı için group_1, group_1_value, …, group_N, group_N_value sütunları."""
    parts = []
    for k in range(1, n_groups + 1):
        parts.append(f"MAX(CASE WHEN x.group_order = {k} THEN x.group_name END) AS group_{k}")
        parts.append(f"MAX(CASE WHEN x.group_order = {k} THEN x.group_value END) AS group_{k}_value")
    return ",\n    ".join(parts)


def expected_measurement_columns(n_groups):
    cols = ["parameter"]
    for k in range(1, n_groups + 1):
        cols += [f"group_{k}", f"group_{k}_value"]
    return cols + ["unit", "p_value", "evidence", "source", "page"]


def database_names(conn):
    """(kaynak, sunum). Kaynak = bağlantının veritabanı (.env MYSQL_DATABASE)."""
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT DATABASE()")
        source = cursor.fetchone()[0]
    finally:
        cursor.close()
    target = os.getenv("MYSQL_SUNUM_DATABASE", DEFAULT_SUNUM_DB)
    if not source:
        raise SunumError("Bağlantıda veritabanı seçili değil: .env'de MYSQL_DATABASE ayarlanmalı.")
    for label, name in (("MYSQL_DATABASE", source), ("MYSQL_SUNUM_DATABASE", target)):
        if not IDENT.match(name):
            raise SunumError(f"{label} yalnız harf, rakam ve _ içermeli (en çok 64 karakter): {name!r}")
    if source == target:
        raise SunumError("MYSQL_SUNUM_DATABASE kaynak veritabanıyla (MYSQL_DATABASE) aynı olamaz.")
    return source, target


def max_group_count(conn, source):
    """Kaynaktaki v_sunum_taban'da Table 1 satırı başına en fazla grup (kayıt) sayısı."""
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT COALESCE(MAX(n), 0) FROM (SELECT COUNT(*) AS n FROM `{source}`.`{BASE_VIEW}` "
                       f"WHERE kanit_turu = 'tablo' AND tablo_no = 1 GROUP BY row_key) AS g")
        return int(cursor.fetchone()[0])
    finally:
        cursor.close()


def render(text, values):
    def sub(match):
        key = match.group(1)
        if key not in values:
            raise SunumError(f"Bilinmeyen yer tutucu: {{{{{key}}}}}")
        return values[key]
    return PLACEHOLDER.sub(sub, text)


def _explain(exc, target, source):
    errno = getattr(exc, "errno", None)
    if errno in (1044, 1045):
        return (f"Yetki yok ({errno}). Kurulumu yapan kullanıcıda gerekenler: CREATE (veritabanı oluşturma), "
                f"`{target}` üzerinde CREATE VIEW ve DROP, `{source}` üzerinde SELECT, SHOW VIEW ve "
                f"({BASE_VIEW} için) CREATE VIEW ve DROP.")
    if errno in (1142, 1143):
        return (f"Tablo/view yetkisi yok ({errno}). `{target}` üzerinde CREATE VIEW / DROP, `{source}` tablo ve "
                f"view'larında SELECT, `{source}`.{BASE_VIEW} için CREATE VIEW / DROP gerekir.")
    if errno == 1146:
        return (f"Kaynak nesne bulunamadı ({errno}): `{source}`.v_pilot_bulgular kurulu mu? Önce: python -m src.sunum --kur")
    return None


def install(conn=None, *, min_groups=MIN_GROUPS, out=print):
    """Sunum veritabanını kurar. DDL örtük commit yapar: açık bir transaction içinde çağırmayın.

    min_groups: measurements'ta en az bu kadar group_N sütunu (testler 3 gruplu örnek için kullanır)."""
    own = conn is None
    if own:
        from src.database import get_connection
        conn = get_connection()
    try:
        source, target = database_names(conn)
        files = sorted(SQL_DIR.glob("*.sql"))
        if not files:
            raise SunumError(f"SQL dosyası yok: {SQL_DIR}")
        from src.database import _split_sql_statements
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s "
                           "AND TABLE_NAME = 'v_pilot_bulgular'", (source,))
            if not cursor.fetchone()[0]:
                raise SunumError(f"`{source}`.v_pilot_bulgular yok. Önce kaynak sunum nesnelerini kurun: "
                                 f"python -m src.sunum --kur")
            conn.rollback()  # okuma anlık görüntüsünü kapat; DDL'den önce açık transaction kalmasın
            try:
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{target}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
            except Exception as exc:  # noqa: BLE001 - mesaj açıklanır
                raise SunumError(_explain(exc, target, source) or f"`{target}` oluşturulamadı: {exc}") from exc
            n_groups = None
            installed = []
            for path in files:
                # Yorumlar önce atılır, yer tutucular yalnız ifadelerde doldurulur (yorumdaki adlar dokunulmaz).
                statements = _split_sql_statements(path.read_text(encoding="utf-8"))
                values = {"KAYNAK_DB": source, "SUNUM_DB": target}
                if any("{{" + GROUP_PLACEHOLDER + "}}" in s for s in statements):
                    n_groups = max(min_groups, max_group_count(conn, source))
                    conn.rollback()
                    values[GROUP_PLACEHOLDER] = measurement_columns(n_groups)
                for statement in statements:
                    statement = render(statement, values)
                    try:
                        cursor.execute(statement)
                    except Exception as exc:  # noqa: BLE001 - mesaj açıklanır
                        why = _explain(exc, target, source)
                        raise SunumError(f"{path.name}: {why or exc}") from exc
                installed.append(path.name)
            conn.commit()
        finally:
            cursor.close()
        views = list_views(conn, target)
        out(f"Kaynak: {source}  ->  sunum veritabanı: {target}")
        out(f"Çalıştırılan dosyalar ({len(installed)}): {', '.join(installed)}")
        if n_groups is not None:
            out(f"measurements: {n_groups} grup sütunu (group_1 … group_{n_groups})")
        out("View'lar: " + ", ".join(f"{name} ({count})" for name, count in views))
        return {"source": source, "target": target, "files": installed, "groups": n_groups, "views": views}
    finally:
        if own:
            conn.close()


def list_views(conn, target):
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT TABLE_NAME FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME", (target,))
        names = [r[0] for r in cursor.fetchall()]
        out = []
        for name in names:
            cursor.execute(f"SELECT COUNT(*) FROM `{target}`.`{name}`")
            out.append((name, cursor.fetchone()[0]))
        conn.rollback()
        return out
    finally:
        cursor.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m src.sunum_hoca", description="Hoca sunum veritabanı (İngilizce view'lar)")
    parser.add_argument("--kur", action="store_true", help="sunum veritabanını ve view'ları kur (tekrar çalıştırılabilir)")
    parser.add_argument("--durum", action="store_true", help="kurulu view'lar ve satır sayıları")
    args = parser.parse_args(argv)
    if not (args.kur or args.durum):
        parser.print_help()
        return 0
    try:
        if args.kur:
            install()
        else:
            from src.database import get_connection
            conn = get_connection()
            try:
                _, target = database_names(conn)
                for name, count in list_views(conn, target):
                    print(f"  {name:<28} {count}")
            finally:
                conn.close()
    except SunumError as exc:
        print(f"Hata: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
