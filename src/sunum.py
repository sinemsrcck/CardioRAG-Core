"""
Sunum görünümleri, reddedilen çıkarımlar ve kaynak izleme.

    python -m src.sunum --kur      # her veritabanında bir kez (tekrar çalıştırılabilir)

Kurar: kalite tabloları (data_quality.ensure_schema), cikarim_reddedilenler,
hata_onlem (+ ONLEM_ESLEME içeriği), v_bulgu_notu, v_cardiorag_bulgular,
v_bulgu_kaynak, v_halusinasyon_onlemleri, RAG için v_rag_sorgular / v_rag_kaynaklar /
v_rag_iddialar (salt okunur), pilot kayıtlar için v_pilot_bulgular (salt okunur) ve
kaynak_bul(arama) procedure'ü.
klinik_bulgular'ın yapısına, v_tani_tedavi / v_sonuclar'a ve
halusinasyon_degerlendirme'ye dokunmaz. DDL örtük COMMIT yapar: açık bir
transaction içinde çağırmayın.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

SUNUM_FILE = Path(__file__).resolve().parent.parent / "sql" / "sunum_core.sql"
ONLEM_SURUMU = "onlem-v2"

# hata_turu -> (halüsinasyon/hata türü, uygulanan önlem, kuralın yeri). Sabit ve sürümlü.
ONLEM_ESLEME = {
    # LLM öncesi ön eleme
    "metaveri": ("Makale künyesi/başlığının bulgu sanılması", "Ön sayfa metaverisi LLM'e gönderilmez",
                 "clinical_candidates.mark_article_metadata"),
    "baslik": ("Bölüm başlığının bulgu sanılması", "Başlık satırları LLM'e gönderilmez",
               "clinical_pipeline.prepare_candidates"),
    "sayfa_asan": ("Tek kaynağa bağlanamayan, sayfa sınırını aşan cümle",
                   "Kanıt tek bir sayfaya ve chunk'a bağlanmalı; aşan cümle gönderilmez",
                   "clinical_pipeline.prepare_candidates"),
    "tablo_karmasik": ("Karmaşık tabloda yanlış satır/sütun ilişkilendirme",
                       "Basit olmayan tablolar insan incelemesine bırakılır", "clinical_review._source_binding"),
    "tablo_baglami_gecersiz": ("Tablo bağlamının kaynakla uyuşmaması", "Tablo bağlamı yeniden üretilip karşılaştırılır",
                               "clinical_review._source_binding"),
    "guvenli_parca_yok": ("Kaynakta doğrulanamayan metin", "Doğrulanabilir parçası olmayan aday gönderilmez",
                          "clinical_pipeline.prepare_candidates"),
    # LLM yanıtının doğrulanması
    "alinti_birebir_degil": ("Uydurma veya değiştirilmiş kanıt alıntısı", "Alıntı aday cümlesinde birebir aranır",
                             "clinical_extraction._validate_response"),
    "alan_alintida_yok": ("Kanıtta olmayan alan değeri (uydurma ilaç, doz, grup, p, GA, n…)",
                          "Dolu her alan alıntıda birebir aranır", "clinical_pipeline.validate_finding"),
    "bos_bulgu": ("İçeriksiz bulgu", "En az bir klinik alan zorunlu", "clinical_extraction._validate_response"),
    "bulgu_alanlari": ("Şema dışı yanıt (eksik/fazla alan)", "Sıkı yanıt şeması", "clinical_pipeline.validate_finding"),
    "bulgu_gecersiz": ("Geçersiz bulgu", "Bulgu reddedilir", "clinical_pipeline.validate_finding"),
    "birden_fazla_mudahale": ("Ayrı gözlemlerin tek bulguda birleştirilmesi", "Bir bulgu = tek müdahale",
                              "clinical_pipeline.validate_finding"),
    "yontem_istatistik": ("İstatistik/analiz yönteminin (ROC analysis, regresyon…) tanı yöntemi sanılması",
                          "diagnostic_method sabit istatistik yöntemi listesine karşı denetlenir; reddedilir",
                          "clinical_pipeline.validate_finding"),
    "diagnostic_relationship_uncertain": ("Desteklenmeyen tanı yöntemi / tablo ilişkisi",
                                          "Yerel dil bilgisi ve tipli tablo kuralları; belirsiz ilişki kaydedilmez",
                                          "clinical_validation.assess_diagnostic_relationship / assess_typed_table"),
    "diagnostic_relationship_contradicted": ("Kaynakla çelişen tanı yöntemi / tablo ilişkisi",
                                             "Yerel dil bilgisi ve tipli tablo kuralları",
                                             "clinical_validation.assess_diagnostic_relationship / assess_typed_table"),
    "diagnostic_relationship_not_applicable": ("İlişki kurulamayan bulgu", "İlişki kontrolü; kaydedilmez",
                                               "clinical_validation.assess_clinical_relationships"),
    # Satır oluşturma
    "guvenli_parca_disinda": ("Kaynakta doğrulanamayan alıntı (markdown / chunk sınırı)",
                              "Alıntı tek bir doğrulanabilir parçanın içinde olmalı", "clinical_pipeline.build_row"),
    "mudahale_alani_yok": ("Müdahalesiz, bağlamsız değer", "drug / procedure / diagnostic_method zorunlu",
                           "clinical_pipeline.build_row"),
    "kanit_chunk_yok": ("Kaynak chunk'ı bulunamayan kanıt", "Alıntı güncel bir chunk'ta olmalı",
                        "clinical_pipeline.build_row"),
    # validate_row / kayıt
    "kanit_birebir_degil": ("Kaynak belgede olmayan kanıt", "Kanıt sayfada ve güncel chunk'ta birebir aranır",
                            "clinical_validation.validate_source / validate_row"),
    "yanlis_sayfa": ("Yanlış sayfa atfı", "Kanıt belirtilen sayfada olmalı", "clinical_validation.validate_row"),
    "kanitta_olmayan_sayi": ("Uydurma sayı (kanıtta olmayan değer)", "Alanlardaki her sayı kanıtta aranır",
                             "clinical_validation.validate_row"),
    "deger_kumesi_disi": ("İzinli değerler dışında sınıflandırma", "Değer kümeleri (enum) kontrolü",
                          "clinical_validation.validate_row"),
    "clinical_relationship_uncertain": ("Kanıtla desteklenmeyen alan ilişkisi (ilaç–doz, grup–sonuç…)",
                                        "Yerel ilişki kuralları; belirsiz ilişki kaydedilmez",
                                        "clinical_validation.validate_relationship_row"),
    "clinical_relationship_contradicted": ("Kanıtla çelişen alan ilişkisi", "Yerel ilişki kuralları",
                                           "clinical_validation.validate_relationship_row"),
    "icd10_yok": ("Var olmayan ICD-10 kodu", "Kod icd10_who'da olmalı", "clinical_validation.validate_row"),
    "alan_cok_uzun": ("Sütun sınırını aşan değer", "Sütun sınırı kontrolü", "clinical_pipeline.preview_rows"),
}
ASAMALAR = ("on_eleme", "yanit_dogrulama", "satir_olusturma", "validate_row", "save")


# ---------------------------------------------------------------------------
# Kurulum
# ---------------------------------------------------------------------------

PROCEDURE_SQL = r"""
CREATE PROCEDURE kaynak_bul(IN arama VARCHAR(200))
BEGIN
    DECLARE desen VARCHAR(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
    -- Arama metni değer olarak kullanılır (dinamik SQL yok); % _ \ joker değil, düz karakter.
    SET desen = CONCAT('%', REPLACE(REPLACE(REPLACE(TRIM(COALESCE(arama, '')), '\\', '\\\\'), '%', '\\%'), '_', '\\_'), '%');
    SELECT k.*
    FROM v_bulgu_kaynak AS k
    WHERE CHAR_LENGTH(TRIM(COALESCE(arama, ''))) > 0
      AND (k.mudahale COLLATE utf8mb4_0900_ai_ci LIKE desen
        OR k.tani COLLATE utf8mb4_0900_ai_ci LIKE desen
        OR k.hasta_grubu COLLATE utf8mb4_0900_ai_ci LIKE desen
        OR k.sonuc COLLATE utf8mb4_0900_ai_ci LIKE desen
        OR k.kanit_cumle COLLATE utf8mb4_0900_ai_ci LIKE desen)
    ORDER BY k.filename, k.sayfa, k.bulgu_id;
END
"""


def ensure_schema(conn):
    """Kalite + sunum tabloları, VIEW'lar, procedure ve önlem eşlemesi. Tekrar çalıştırılabilir."""
    from src.data_quality import ensure_schema as ensure_quality
    from src.database import _split_sql_statements

    ensure_quality(conn)  # v_cardiorag_bulgular kalite sayılarını v_kalite_bulgu'dan okur
    cursor = conn.cursor()
    try:
        for statement in _split_sql_statements(SUNUM_FILE.read_text(encoding="utf-8")):
            cursor.execute(statement)
        cursor.execute("DROP PROCEDURE IF EXISTS kaynak_bul")
        cursor.execute(PROCEDURE_SQL)
        cursor.execute("DELETE FROM hata_onlem WHERE esleme_surumu <> %s", (ONLEM_SURUMU,))
        cursor.executemany(
            "INSERT INTO hata_onlem (hata_turu, halusinasyon_turu, onlem, kural_yeri, esleme_surumu) "
            "VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE halusinasyon_turu = VALUES(halusinasyon_turu), "
            "onlem = VALUES(onlem), kural_yeri = VALUES(kural_yeri), esleme_surumu = VALUES(esleme_surumu)",
            [(code, *values, ONLEM_SURUMU) for code, values in sorted(ONLEM_ESLEME.items())])
        conn.commit()
    finally:
        cursor.close()


def schema_ready(conn):
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
                       "AND TABLE_NAME IN ('cikarim_reddedilenler', 'hata_onlem')")
        return cursor.fetchone()[0] == 2
    finally:
        cursor.close()


def installed_objects(conn):
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
                       "AND TABLE_NAME IN ('cikarim_reddedilenler', 'hata_onlem', 'kalite_isaretleri', "
                       "'kalite_calistirmalari', 'v_bulgu_notu', 'v_cardiorag_bulgular', 'v_bulgu_kaynak', "
                       "'v_halusinasyon_onlemleri', 'v_kalite_ozet', 'v_kalite_bulgu', "
                       "'v_rag_sorgular', 'v_rag_kaynaklar', 'v_rag_iddialar', 'v_pilot_bulgular') "
                       "ORDER BY TABLE_TYPE, TABLE_NAME")
        objects = [(name, "VIEW" if kind == "VIEW" else "TABLO") for name, kind in cursor.fetchall()]
        cursor.execute("SELECT ROUTINE_NAME FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = DATABASE() "
                       "AND ROUTINE_TYPE = 'PROCEDURE' AND ROUTINE_NAME = 'kaynak_bul'")
        objects += [(r[0], "PROCEDURE") for r in cursor.fetchall()]
        cursor.execute("SELECT COUNT(*) FROM hata_onlem")
        return objects, cursor.fetchone()[0]
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# Reddedilen çıkarımların yazımı (deterministik, tekrar etmez)
# ---------------------------------------------------------------------------

def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def rejection_record(source, record, *, pipeline_version, model):
    """Bir red kaydı -> tablo satırı. satir_hash çalıştırma kimliği ve zamandan bağımsızdır."""
    asama = "save" if record.get("asama") == "save_clinical_findings" else record["asama"]
    if asama not in ASAMALAR:
        raise ValueError(f"bilinmeyen aşama: {asama}")
    hata = record["hata_turu"]
    aciklama = record.get("aciklama")
    alt = (str(aciklama).split(":")[0] if hata == "deger_kumesi_disi"
           and str(aciklama or "").startswith("clinical_relationship_") else "")
    bulgu, satir = record.get("bulgu"), record.get("satir")
    kanit = ((satir or {}).get("kanit_cumle") if isinstance(satir, dict) else None) or (
        bulgu.get("evidence_quote") if isinstance(bulgu, dict) else None)
    aday_id = source.get("aday_id") or record.get("aday_id")
    identity = {"aday_id": aday_id, "pipeline_version": pipeline_version, "model": model or "", "asama": asama,
                "hata_turu": hata, "alt_tur": alt, "bulgu": bulgu, "kanit_cumle": kanit}
    return {
        "satir_hash": hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest(),
        "aday_id": aday_id, "pipeline_version": pipeline_version, "model": model or "",
        "document_id": source.get("document_id"), "makale_id": source.get("makale_id"),
        "dosya": source.get("dosya"), "sayfa": source.get("sayfa"), "kaynak_turu": source.get("kaynak_turu"),
        "asama": asama, "hata_turu": hata, "alt_tur": alt, "aciklama": aciklama,
        "bulgu": None if bulgu is None else _canonical(bulgu),
        "satir": None if satir is None else _canonical(satir),
        "aday_cumlesi": source.get("cumle"), "kanit_cumle": kanit,
    }


COLUMNS = ("satir_hash", "aday_id", "pipeline_version", "model", "document_id", "makale_id", "dosya", "sayfa",
           "kaynak_turu", "asama", "hata_turu", "alt_tur", "aciklama", "bulgu", "satir", "aday_cumlesi", "kanit_cumle")


def _insert(conn, rows):
    if not rows:
        return 0
    cursor = conn.cursor()
    try:
        cursor.executemany(f"INSERT IGNORE INTO cikarim_reddedilenler ({', '.join(COLUMNS)}) "
                           f"VALUES ({', '.join(['%s'] * len(COLUMNS))})",
                           [tuple(r[c] for c in COLUMNS) for r in rows])
        return max(cursor.rowcount, 0)
    finally:
        cursor.close()


def write_rejections(conn, candidate, records, *, pipeline_version, model):
    """Aday düzeyindeki redler (yanit_dogrulama / satir_olusturma / validate_row / save). Commit etmez."""
    return _insert(conn, [rejection_record(candidate, r, pipeline_version=pipeline_version, model=model)
                          for r in records])


def write_exclusions(conn, excluded, *, pipeline_version):
    """LLM öncesi ayrılan adaylar (on_eleme; model ''). Commit etmez."""
    return _insert(conn, [rejection_record(e, {"asama": "on_eleme", "hata_turu": e["sebep"],
                                               "aciklama": e.get("aciklama")},
                                           pipeline_version=pipeline_version, model="")
                          for e in excluded])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m src.sunum", description="Sunum görünümleri ve reddedilenler")
    parser.add_argument("--kur", action="store_true", help="tabloları, VIEW'ları ve kaynak_bul'u kur")
    args = parser.parse_args(argv)
    if not args.kur:
        parser.print_help()
        return 0
    from src.database import get_connection

    conn = get_connection()
    try:
        ensure_schema(conn)
        objects, mappings = installed_objects(conn)
    finally:
        conn.close()
    print("Kuruldu / güncellendi:")
    for name, kind in objects:
        print(f"  {kind:<9} {name}")
    print(f"hata_onlem: {mappings} eşleme ({ONLEM_SURUMU})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
