
"""
LLM ile hazırlanmış Türkçe özet çevirilerini makaleler.ozet_tr'ye yükler.

Çalıştırma:
    python -m src.load_translations --dry-run
    python -m src.load_translations            # gerçek yükleme
    python -m src.load_translations --file başka.json

Girdi (varsayılan data/clinical/abstract_translations.json):
    [{"document_id": "...", "ozet_tr": "...", "model": "gpt-6-luna"}, ...]
Çeviriler Sinem'in src.abstract_translation.prepare_abstract_translations
çıktısından gelir; bu modül LLM çağırmaz, yalnızca doğrular ve yazar.

Yazılan: ozet_tr, ozet_tr_kaynak='llm_ceviri', ozet_tr_model=<model>.

Kontroller (geçmeyen kayıt yazılmaz, raporlanır):
- document_id bizim makaleler satırlarımızdan birinde var (document_id IS NOT NULL);
- ozet_tr ve model boş değil; aynı document_id dosyada bir kez geçiyor;
- satırın ozet_tr_kaynak'ı 'orijinal' değil (PDF'teki Türkçe özetin üzerine
  yazılmaz);
- kaynağı bilinmeyen (ozet_tr_kaynak NULL) dolu bir ozet_tr: metin çeviriyle
  birebir aynıysa 'etiketlenecek' (metne dokunmadan yalnızca kaynak/model
  doldurulur; ör. --init'ten önce yüklenmiş çeviriler), farklıysa reddedilir;
- çevirideki her sayı aynı makalenin abstract_en'inde de geçiyor (sayılar
  Sinem'in _numbers'ı ile çıkarılır: ondalık virgül = nokta).
Sinem'in validate_translation'ı (sayı adetleri ve %, <, >, ≤, ≥ işaretleri
birebir aynı) ek bilgi olarak raporlanır.

Güvenlik: hocanın satırlarına (document_id IS NULL) yazılmaz; UPDATE koşulu
"document_id = %s AND document_id IS NOT NULL AND ozet_tr_kaynak <=> ..."
şeklindedir; tek transaction; hocanın satırlarının parmak izi önce/sonra
karşılaştırılır. Aynı veriyle tekrar çalıştırmak hiçbir şeyi değiştirmez.
"""

import argparse
import json
import sys
from pathlib import Path

from src.abstract_translation import _numbers, validate_translation
from src.article_metadata import _as_text, professor_rows_fingerprint
from src.database import get_connection


DEFAULT_FILE = Path("data/clinical/abstract_translations.json")
SOURCE_LLM = "llm_ceviri"
SOURCE_ORIGINAL = "orijinal"


def load_entries(path):
    with open(path, encoding="utf-8") as file:
        entries = json.load(file)
    if not isinstance(entries, list):
        raise ValueError(f"{path}: liste bekleniyordu")
    return entries


def load_our_rows(cursor):
    """Bizim makaleler satırlarımız: document_id -> alanlar."""
    cursor.execute(
        "SELECT makale_id, document_id, abstract_en, ozet_tr, ozet_tr_kaynak, ozet_tr_model "
        "FROM makaleler WHERE document_id IS NOT NULL"
    )
    keys = ("makale_id", "document_id", "abstract_en", "ozet_tr", "ozet_tr_kaynak", "ozet_tr_model")
    rows = [dict(zip(keys, (_as_text(v) for v in row))) for row in cursor.fetchall()]
    return {row["document_id"]: row for row in rows}


def extra_numbers(abstract_en, ozet_tr):
    """Çeviride geçip abstract_en'de hiç geçmeyen sayılar (sıralı)."""
    source = _numbers(abstract_en)
    return sorted(n for n in _numbers(ozet_tr) if n not in source)


def plan_entry(entry, rows, seen):
    """
    Tek kayıt için karar: {'durum': yazilacak|zaten_ayni|reddedildi, 'neden': ...}.
    """
    plan = {"document_id": entry.get("document_id") if isinstance(entry, dict) else None,
            "makale_id": None, "durum": "reddedildi", "neden": None, "sinem_kontrolu": None}

    def reject(reason):
        plan["neden"] = reason
        return plan

    if not isinstance(entry, dict):
        return reject("kayıt bir nesne değil")
    document_id = entry.get("document_id")
    ozet_tr = entry.get("ozet_tr")
    model = entry.get("model")

    if not isinstance(document_id, str) or not document_id:
        return reject("document_id yok")
    if document_id in seen:
        return reject("document_id dosyada birden fazla kez geçiyor")
    seen.add(document_id)
    row = rows.get(document_id)
    if row is None:
        return reject("document_id bizim makaleler satırlarımızda yok")
    plan["makale_id"] = row["makale_id"]

    if not isinstance(ozet_tr, str) or not ozet_tr.strip():
        return reject("ozet_tr boş")
    ozet_tr = ozet_tr.strip()
    if not isinstance(model, str) or not model.strip():
        return reject("model boş")
    model = model.strip()

    if row["ozet_tr_kaynak"] == SOURCE_ORIGINAL:
        return reject("satırda PDF'ten gelen orijinal Türkçe özet var (ozet_tr_kaynak='orijinal')")
    # Kaynağı bilinmeyen (NULL) mevcut özet: metin çeviriyle birebir aynıysa
    # yalnızca kaynak/model etiketlenir; farklıysa hiç yazılmaz.
    unlabelled_same = row["ozet_tr_kaynak"] is None and row["ozet_tr"] == ozet_tr
    if row["ozet_tr_kaynak"] is None and row["ozet_tr"] is not None and not unlabelled_same:
        return reject("satırda kaynağı bilinmeyen ve çeviriden farklı bir ozet_tr var")
    if not row["abstract_en"] or not row["abstract_en"].strip():
        return reject("abstract_en boş; sayılar karşılaştırılamaz")

    extra = extra_numbers(row["abstract_en"], ozet_tr)
    if extra:
        return reject(f"abstract_en'de olmayan sayılar: {', '.join(extra)}")

    try:
        validate_translation(row["abstract_en"], ozet_tr)
        plan["sinem_kontrolu"] = "geçti"
    except ValueError as exc:
        plan["sinem_kontrolu"] = f"uyarı: {exc}"

    plan.update(ozet_tr=ozet_tr, model=model)
    if (row["ozet_tr"], row["ozet_tr_kaynak"], row["ozet_tr_model"]) == (ozet_tr, SOURCE_LLM, model):
        plan["durum"] = "zaten_ayni"
    elif unlabelled_same:
        plan["durum"] = "etiketlenecek"
    else:
        plan["durum"] = "yazilacak"
    return plan


def build_plan(cursor, entries):
    rows = load_our_rows(cursor)
    seen = set()
    return [plan_entry(entry, rows, seen) for entry in entries]


def apply_plan(conn, cursor, plans):
    """'yazilacak' ve 'etiketlenecek' kayıtları yazar. Çağıran transaction'ı yönetir."""
    written = 0
    for plan in plans:
        if plan["durum"] == "etiketlenecek":
            # Metne dokunulmaz; yalnızca kaynak/model doldurulur.
            cursor.execute(
                "UPDATE makaleler SET ozet_tr_kaynak = %s, ozet_tr_model = %s "
                "WHERE document_id = %s AND document_id IS NOT NULL "
                "AND ozet_tr_kaynak IS NULL AND ozet_tr = %s",
                (SOURCE_LLM, plan["model"], plan["document_id"], plan["ozet_tr"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Beklenmeyen satır sayısı ({cursor.rowcount}): {plan['makale_id']}")
            written += 1
            continue
        if plan["durum"] != "yazilacak":
            continue
        cursor.execute(
            "UPDATE makaleler SET ozet_tr = %s, ozet_tr_kaynak = %s, ozet_tr_model = %s "
            "WHERE document_id = %s AND document_id IS NOT NULL "
            "AND (ozet_tr_kaynak IS NULL OR ozet_tr_kaynak = %s) "
            "AND NOT (ozet_tr_kaynak IS NULL AND ozet_tr IS NOT NULL)",
            (plan["ozet_tr"], SOURCE_LLM, plan["model"], plan["document_id"], SOURCE_LLM),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"Beklenmeyen satır sayısı ({cursor.rowcount}): {plan['makale_id']}")
        written += 1
    return written


def run(path=DEFAULT_FILE, dry_run=True, conn=None):
    """Planı (ve dry_run=False ise yazımı) döndürür: {'plans', 'written', 'professor_rows'}."""
    entries = load_entries(path)
    own = conn is None
    conn = conn or get_connection()
    conn.autocommit = True
    conn.start_transaction()
    cursor = conn.cursor()
    try:
        before = professor_rows_fingerprint(cursor)
        plans = build_plan(cursor, entries)
        written = 0 if dry_run else apply_plan(conn, cursor, plans)
        after = professor_rows_fingerprint(cursor)
        if after != before:
            raise RuntimeError(f"Hocanın satırları değişti! önce={before} sonra={after}")
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return {"plans": plans, "written": written, "professor_rows": after}
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        if own:
            conn.close()


def print_report(result, dry_run):
    for plan in result["plans"]:
        print(f"{plan['makale_id'] or plan['document_id']}")
        print(f"  durum : {plan['durum']}" + (f" ({plan['neden']})" if plan["neden"] else ""))
        if plan.get("model"):
            print(f"  model : {plan['model']}   ozet_tr: {len(plan['ozet_tr'])} karakter")
        if plan["sinem_kontrolu"]:
            print(f"  validate_translation (Sinem): {plan['sinem_kontrolu']}")
    counts = {}
    for plan in result["plans"]:
        counts[plan["durum"]] = counts.get(plan["durum"], 0) + 1
    print(f"\nÖzet: {counts}")
    print(f"Hocanın satırları: {result['professor_rows'][0]} satır, "
          f"parmak izi {result['professor_rows'][1]}")
    if dry_run:
        print("DRY-RUN: veritabanına hiçbir şey yazılmadı.")
    else:
        print(f"YAZILDI: {result['written']} satır.")


def main():
    parser = argparse.ArgumentParser(description="LLM Türkçe özet çevirilerini makaleler'e yükler.")
    parser.add_argument("--dry-run", action="store_true", help="yalnızca rapor; yazma yok")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    args = parser.parse_args()
    if not args.file.exists():
        sys.exit(f"Dosya yok: {args.file}")
    result = run(args.file, dry_run=args.dry_run)
    print_report(result, args.dry_run)
    if any(p["durum"] == "reddedildi" for p in result["plans"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
