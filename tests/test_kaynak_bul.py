"""kaynak_bul / v_bulgu_kaynak: çok parçalı kanıtın bütün parçaları aranabilir; tek parçalı kayıtların davranışı
değişmez. Veritabanına bağlanılmaz: SQL kaynak metni denetlenir ve görünüm ifadesi Python'da birebir taklit edilir."""
import json
import re
import unittest
from pathlib import Path

from src.pilot_clinical import MAX_PARTS, _note_with_parts
from src.sunum import PROCEDURE_SQL

SQL = Path('sql/sunum_core.sql').read_text(encoding='utf-8')
VIEW = SQL[SQL.index('CREATE OR REPLACE VIEW v_bulgu_kaynak AS'):]
VIEW = VIEW[:VIEW.index(';')]
# Değişiklikten önceki sütunlar (ad ve sıra korunmalı)
OLD_COLUMNS = ['bulgu_id', 'makale_id', 'filename', 'makale_basligi', 'doi_link', 'sayfa', 'chunk_id', 'chunk_sayfasi',
               'chunk_index', 'kanit_turu', 'kanit_cumle', 'aday_id', 'aday_cumlesi', 'mudahale', 'mudahale_turu', 'tani',
               'hasta_grubu', 'sonuc', 'gecen_kontroller', 'insan_onayi', 'pipeline_version', 'model', 'review_status',
               'cikarma_yontemi']
OLD_TERMS = ['k.mudahale', 'k.tani', 'k.hasta_grubu', 'k.sonuc', 'k.kanit_cumle']


def view_columns():
    """SELECT listesindeki sütun adları (AS takma adı ya da b.<ad>), üst düzey virgüllere göre."""
    body = VIEW[VIEW.index('SELECT') + len('SELECT'):VIEW.index('\nFROM v_bulgu_notu')]
    body = '\n'.join(line for line in body.splitlines() if not line.strip().startswith('--'))
    items, depth, current = [], 0, ''
    for ch in body:
        depth += ch == '('
        depth -= ch == ')'
        if ch == ',' and depth == 0:
            items.append(current)
            current = ''
        else:
            current += ch
    items.append(current)
    names = []
    for item in items:
        item = ' '.join(item.split())
        alias = re.search(r'\bAS (\w+)$', item)
        names.append(alias.group(1) if alias else item.split('.')[-1])
    return names


def parts_text(taslak_notu):
    """v_bulgu_kaynak.kanit_parcalari_metin ifadesinin Python karşılığı (JSON_VALID / ARRAY / ilk 3 STRING metin,
    CONCAT_WS(' ') NULL'ları atlar, NULLIF(..., '')). Görünümle aynı kalmalı."""
    try:
        note = json.loads(taslak_notu) if isinstance(taslak_notu, str) else None
    except ValueError:
        return None
    if not isinstance(note, dict) or not isinstance(note.get('kanit_parcalari'), list):
        return None
    texts = [p['metin'] for p in note['kanit_parcalari'][:3] if isinstance(p, dict) and isinstance(p.get('metin'), str)]
    return ' '.join(texts) or None


def search(rows, term, with_parts=True):
    """kaynak_bul WHERE koşulunun Python karşılığı (LIKE %terim% büyük/küçük harf duyarsız)."""
    term = term.strip().casefold()
    fields = ['mudahale', 'tani', 'hasta_grubu', 'sonuc', 'kanit_cumle'] + (['kanit_parcalari_metin'] if with_parts else [])
    return [r['bulgu_id'] for r in rows if term and any(term in str(r.get(f) or '').casefold() for f in fields)]


PART1 = 'The HbA1c/C-pep-'
PART2 = 'tide ratio was associated with ischemic events in the cohort.'
SINGLE = {'bulgu_id': 1, 'kanit_cumle': 'Aspirin 100 mg was given to all patients.', 'tani': None,
          'taslak_notu': '{"tablo_no": 1}'}
BROKEN = {'bulgu_id': 2, 'kanit_cumle': 'Chest pain was reported.', 'taslak_notu': 'geçersiz json'}
MULTI = {'bulgu_id': 3, 'kanit_cumle': PART1,
         'taslak_notu': _note_with_parts({'kanit_parcalari': [{'sayfa': 4, 'metin': PART1}, {'sayfa': 5, 'metin': PART2}]})}


def rows():
    out = []
    for row in (SINGLE, BROKEN, MULTI):
        out.append(dict(row, kanit_parcalari_metin=parts_text(row['taslak_notu'])))
    return out


class ViewTests(unittest.TestCase):
    def test_existing_columns_unchanged_new_columns_appended(self):
        names = view_columns()
        self.assertEqual(names[:len(OLD_COLUMNS)], OLD_COLUMNS)
        self.assertEqual(names[len(OLD_COLUMNS):], ['kanit_parcalari_metin', 'sayfa_bitis'])

    def test_parts_expression_covers_every_allowed_part_safely(self):
        self.assertEqual(MAX_PARTS, 3)       # görünüm 3 parçayı okur; sınır büyürse görünüm de güncellenmeli
        for index in range(MAX_PARTS):
            self.assertIn(f"'$.kanit_parcalari[{index}].metin'", VIEW)
        self.assertNotIn("'$.kanit_parcalari[3].metin'", VIEW)
        self.assertIn('CASE WHEN JSON_VALID(b.taslak_notu) THEN CASE WHEN JSON_TYPE', VIEW)   # geçersiz JSON hata vermez
        self.assertIn("CONCAT_WS(' '", VIEW)
        self.assertIn('b.kanit_sayfa_bitis AS sayfa_bitis', VIEW)

    def test_installer_splits_view_as_one_statement(self):
        from src.database import _split_sql_statements
        found = [s for s in _split_sql_statements(SQL) if 'VIEW v_bulgu_kaynak' in s]
        self.assertEqual(len(found), 1)
        self.assertIn('kanit_parcalari_metin', found[0])
        self.assertTrue(found[0].rstrip().endswith('b.chunk_id'))

    def test_importer_note_matches_view_paths(self):
        note = json.loads(MULTI['taslak_notu'])
        self.assertEqual([sorted(p) for p in note['kanit_parcalari']], [['metin', 'sayfa'], ['metin', 'sayfa']])
        self.assertEqual(parts_text(MULTI['taslak_notu']), PART1 + ' ' + PART2)
        self.assertIsNone(parts_text(SINGLE['taslak_notu']))
        self.assertIsNone(parts_text(BROKEN['taslak_notu']))


class ProcedureTests(unittest.TestCase):
    def test_procedure_searches_parts_and_keeps_existing_terms(self):
        where = PROCEDURE_SQL[PROCEDURE_SQL.index('WHERE'):PROCEDURE_SQL.index('ORDER BY')]
        for term in OLD_TERMS + ['k.kanit_parcalari_metin']:
            self.assertIn(f'{term} COLLATE utf8mb4_0900_ai_ci LIKE desen', where)
        self.assertEqual(where.count(' OR '), len(OLD_TERMS))                 # yalnız bir koşul eklendi

    def test_single_part_results_unchanged(self):
        data = rows()
        for term in ('Aspirin', 'chest pain', 'patients', 'HbA1c', 'olmayan terim', ' '):
            before = [i for i in search(data, term, with_parts=False) if i != 3]
            after = [i for i in search(data, term) if i != 3]
            self.assertEqual(before, after, term)

    def test_multi_part_found_by_any_part_and_across_boundary(self):
        data = rows()
        self.assertEqual(search(data, 'ischemic events'), [3])              # yalnız ikinci parçada
        self.assertEqual(search(data, 'ischemic events', with_parts=False), [])   # önceki davranış: bulunamıyordu
        self.assertEqual(search(data, 'C-pep- tide ratio'), [3])            # sınırı geçen ifade (tek boşluk)
        self.assertEqual(search(data, 'HbA1c'), [3])                        # ilk parça (kanit_cumle) aynen


if __name__ == '__main__':
    unittest.main()
