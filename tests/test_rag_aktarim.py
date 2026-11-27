"""src.rag_aktarim regresyon testleri. Gerçek veritabanına bağlanılmaz: aktarım testleri bellek içi sahte bağlantı
kullanır."""
import copy
import json
import re
import unittest
from pathlib import Path

from src import rag_aktarim as A
from src.pilot_clinical import import_records

DOC_ID = 'd' * 64
DOC = {'document_id': DOC_ID, 'filename': 'x.pdf', 'pages': []}
META = {'cikti_dizini': 'data/rag_cikarim/x/20260101-000000', 'model': 'm', 'pdf': 'x.pdf'}

TEXT = 'Atrial fibrillation (AF) was diagnosed in 40 patients. Chest pain was reported in most cases.'
G_AGE = '|Age, years|62|58|60|0.031|'
G_SEX = '|Male<br>Female|7<br>3|12<br>8|19<br>11|0.5|'
P2 = ('**Table 1:** Baseline characteristics\n|Variable|Event (n=10)|No event (n=20)|Total (n=30)|p|\n'
      '|---|---|---|---|---|\n|Laboratory findings|||||\n' + G_AGE + '\n' + G_SEX + '\n')
R_STROKE = '|Stroke|2.42 (2.17–2.71)|7.1|'
P3 = '**Table 2.** Risks\n|Outcome|Relative risk (95% CI)|5-y risk, %|\n|---|---|---|\n' + R_STROKE + '\n'
R_HF = '|Heart failure|4.99|13.7|'
P4 = '|Outcome|Relative risk (95% CI)|5-y risk, %|\n|---|---|---|\n' + R_HF + '\n'          # başlıksız devam
Q_ROW = '|Obesity|Weight loss|Decreased|'
P5 = '**Table 3.** Benefits\n|Risk factor|Recommendation|AF symptoms|\n|---|---|---|\n' + Q_ROW + '\n'
D_ROW = '|PET STRESS|85%|86%|'
P6 = '**Table 4.** Tests\n|Test|SEN|SPE|\n|---|---|---|\n' + D_ROW + '\n'
U_ROW = '|Score|5|'
P7 = 'Some text.\n\n|Item|Value|\n|---|---|\n' + U_ROW + '\n'                              # başlıksız, devam değil
E_ROW = '|ELR|2.73|'
P8 = '|**TABL**|**E 5:**Regression analysis|\n|---|---|\n|**Variable**|**OR**|\n' + E_ROW + '\n'
LEFT = 'Earlier sentence here. The data suggest that COVID-19 may play a role in ACS, especially'
RIGHT = 'STEMI, due to increased thrombogenicity, in 42% of cases. Other findings followed.'
PART1 = 'The data suggest that COVID-19 may play a role in ACS, especially'
PART2 = 'STEMI, due to increased thrombogenicity, in 42% of cases.'
PAGES = {1: TEXT, 2: P2, 3: P3, 4: P4, 5: P5, 6: P6, 7: P7, 8: P8, 9: LEFT, 10: RIGHT}


def text(fact, quote=TEXT, page=1, **kw):
    return dict(bilgi_turu=kw.pop('bilgi_turu', 'tani'), baglam='study_finding', klinik_bilgi_en=fact,
                kanit_cumle=quote, sayfa=page, kanit_turu='metin', kategoriler=['diseases'], **kw)


def cell(fact, row, page, value=None, **kw):
    rec = dict(bilgi_turu=kw.pop('bilgi_turu', 'olcum'), baglam='study_finding', klinik_bilgi_en=fact,
               kanit_cumle=row, sayfa=page, kanit_turu='tablo', kategoriler=['x'], **kw)
    if value is not None:
        rec['deger'] = value
    return rec


def boundary(**kw):
    rec = dict(bilgi_turu='tani', baglam='literature_background', klinik_bilgi_en='STEMI', sayfa=9, sayfa_bitis=10,
               kanit_parcalari=[{'sayfa': 9, 'metin': PART1}, {'sayfa': 10, 'metin': PART2}], birlestirme='sayfa',
               kanit_cumle=PART1 + ' ' + PART2, kanit_turu='metin', kategoriler=['diseases'])
    rec.update(kw)
    return rec


def draft(records, existing=()):
    return A.build_draft(records, PAGES, META, list(existing))


def only(result):
    assert len(result['adaylar']) == 1, (result['adaylar'], result['inceleme'])
    return result['adaylar'][0]


class MappingTests(unittest.TestCase):
    def test_text_record_pending_with_note_and_no_extra_fields(self):
        rec = only(draft([text('Atrial fibrillation (AF)', otomatik_duzeltme=['kanit_cumle'], review_status='verified')]))
        self.assertEqual(rec['review_status'], 'pending')                         # verified girdi bile pending
        self.assertNotIn('kategoriler', rec)
        self.assertNotIn('otomatik_duzeltme', rec)
        self.assertEqual(rec['taslak_notu']['kaynak'], A.KAYNAK)
        self.assertEqual(rec['taslak_notu']['otomatik_duzeltme'], ['kanit_cumle'])
        self.assertEqual(rec['kanit_cumle'], TEXT)

    def test_multi_part_first_part_all_parts_kept(self):
        rec = only(draft([boundary()]))
        self.assertEqual(rec['kanit_cumle'], PART1)                                # importer sözleşmesi
        self.assertEqual(rec['kanit_parcalari'], [{'sayfa': 9, 'metin': PART1}, {'sayfa': 10, 'metin': PART2}])
        self.assertEqual((rec['sayfa'], rec['sayfa_bitis'], rec['birlestirme']), (9, 10, 'sayfa'))
        bad = draft([boundary(kanit_cumle='başka metin')])
        self.assertEqual((bad['adaylar'], bad['inceleme'][0]['asama']), ([], 'esleme'))

    def test_group_table_identity_section_and_roles(self):
        recs = [cell('Age, years', G_AGE, 2, '62', hasta_grubu='Event (n=10)', p='0.031'),
                cell('Age, years', G_AGE, 2, '58', hasta_grubu='No event (n=20)', p='0.031'),
                cell('Age, years', G_AGE, 2, '60', hasta_grubu='Total (n=30)', p='0.031',
                     otomatik_duzeltme=['hasta_grubu:deger_bicimi=Median (Q1-Q3)'])]
        result = draft(recs)
        self.assertEqual(len(result['adaylar']), 3, result['inceleme'])
        notes = [r['taslak_notu'] for r in result['adaylar']]
        self.assertTrue(all(n['tablo_no'] == 1 and n['tablo_basligi'] == 'Baseline characteristics' for n in notes))
        self.assertEqual({n['satir_no'] for n in notes}, {A.SATIR_NO_OFSET + 1})
        self.assertTrue(all(n['alt_bolum'] == 'Laboratory findings' and n['bolum_goster'] is True for n in notes))
        self.assertTrue(all(r['sonuc_olcutu_tr'] == 'Age, years' for r in result['adaylar']))    # satır değişkeni
        total = next(r for r in result['adaylar'] if r['hasta_grubu'] == 'Total (n=30)')
        self.assertEqual((total['taslak_notu']['sutun_rolu'], total['taslak_notu']['deger_bicimi']),
                         ('toplam', 'Median (Q1-Q3)'))
        self.assertEqual(total['taslak_notu']['sutun'], 'Total (n=30)')

    def test_stacked_variables_get_distinct_row_numbers(self):
        recs = [cell(f, G_SEX, 2, v, hasta_grubu=g) for f, g, v in
                (('Male', 'Event (n=10)', '7'), ('Male', 'No event (n=20)', '12'),
                 ('Female', 'Event (n=10)', '3'), ('Female', 'No event (n=20)', '8'))]
        result = draft(recs)
        rows = {r['klinik_bilgi_en']: r['taslak_notu']['satir_no'] for r in result['adaylar']}
        self.assertEqual(len(result['adaylar']), 4, result['inceleme'])
        self.assertNotEqual(rows['Male'], rows['Female'])
        self.assertEqual(A.group_harmony(result['adaylar']), [])

    def test_ungrouped_numeric_cells_use_row_variable_and_header_effect(self):
        result = draft([cell('Stroke', R_STROKE, 3, '2.42 (2.17–2.71)', sonuc_olcutu_tr='Relative risk (95% CI)'),
                        cell('Stroke', R_STROKE, 3, '7.1', sonuc_olcutu_tr='5-y risk, %')])
        got = sorted((r['sonuc_olcutu_tr'], r['etki_turu'], r['taslak_notu']['sutun']) for r in result['adaylar'])
        self.assertEqual(got, [('Stroke', 'RR', 'Relative risk (95% CI)'), ('Stroke', 'oran / yuzde', '5-y risk, %')])

    def test_continuation_table_number_and_unknown_table_held(self):
        result = draft([cell('Heart failure', R_HF, 4, '4.99'), cell('Score', U_ROW, 7, '5')])
        self.assertEqual([r['taslak_notu']['tablo_no'] for r in result['adaylar']], [2])        # önceki sayfadan devam
        self.assertEqual(len(result['inceleme']), 1)
        self.assertIn('tablo numarası', result['inceleme'][0]['neden'])

    def test_caption_inside_split_header_row(self):
        rec = only(draft([cell('ELR', E_ROW, 8, '2.73', bilgi_turu='klinik_sonuc')]))
        self.assertEqual((rec['taslak_notu']['tablo_no'], rec['taslak_notu']['sutun'], rec['etki_turu']), (5, 'OR', 'OR'))

    def test_qualitative_effect_needs_single_intervention(self):
        decreased = cell('Obesity', Q_ROW, 5, 'Decreased', bilgi_turu='klinik_sonuc', sonuc_olcutu_tr='AF symptoms')
        intervention = cell('Weight loss', Q_ROW, 5, bilgi_turu='tedavi')
        result = draft([intervention, decreased])
        effect = next(r for r in result['adaylar'] if r.get('deger') == 'Decreased')
        self.assertEqual((effect['sonuc_olcutu_tr'], effect['etki_turu'], effect['mudahale_en']),
                         ('AF symptoms', 'sayisal deger yok', 'Weight loss'))
        treat = next(r for r in result['adaylar'] if r['bilgi_turu'] == 'tedavi')
        self.assertEqual(treat['mudahale_en'], 'Weight loss')
        two = draft([intervention, cell('Obesity', Q_ROW, 5, bilgi_turu='tedavi'), decreased])
        self.assertTrue(any('birden çok müdahale' in r['neden'] for r in two['inceleme']))

    def test_diagnostic_performance_column(self):
        rec = only(draft([cell('PET STRESS', D_ROW, 6, '85%', sonuc_olcutu_tr='SEN')]))
        self.assertEqual((rec['sonuc_olcutu_tr'], rec['mudahale_en'], rec['outcome_type'], rec['etki_turu']),
                         ('SEN', 'PET STRESS', 'tanısal performans', 'oran / yuzde'))

    def test_display_label_keeps_numeric_superscript(self):
        self.assertEqual(A.display_label('Al Hasan, 2019_(14)_<sup>_a_</sup>'), 'Al Hasan, 2019')
        self.assertEqual(A.display_label('BMI kg/m<sup>2</sup>'), 'BMI kg/m<sup>2</sup>')


def sql_row(rec, row_id, **kw):
    note = dict(rec.get('taslak_notu') or {})
    if rec.get('kanit_parcalari'):
        note['kanit_parcalari'] = rec['kanit_parcalari']
    row = {'id': row_id, 'document_id': DOC_ID, 'bilgi_turu': rec['bilgi_turu'], 'baglam': rec['baglam'],
           'klinik_bilgi_en': rec['klinik_bilgi_en'], 'kanit_sayfa': rec['sayfa'],
           'kanit_sayfa_bitis': rec.get('sayfa_bitis'), 'kanit_cumle': rec['kanit_cumle'], 'kanit_turu': rec['kanit_turu'],
           'taslak_notu': json.dumps(note), 'review_status': 'verified', 'cikarma_yontemi': 'manuel'}
    for field in A.COMPARE_FIELDS:
        row[field] = rec.get(field)
    row.update(kw)
    return row


class DuplicateTests(unittest.TestCase):
    def test_exact_possible_and_new(self):
        exact = text('Atrial fibrillation (AF)')
        similar = text('Chest pain was reported in most cases', bilgi_turu='semptom')
        new = text('Chest pain', bilgi_turu='semptom')
        existing = [sql_row(exact, 1), sql_row(text('Chest pain was reported in most cases.', bilgi_turu='semptom'), 2)]
        result = draft([exact, similar, new], existing)
        self.assertEqual([r['mevcut_id'] for r in result['kesin_tekrar']], [1])
        self.assertEqual([(r['asama'], r['mevcut_idler']) for r in result['inceleme']], [('olasi_tekrar', [2])])
        self.assertEqual([r['klinik_bilgi_en'] for r in result['adaylar']], ['Chest pain'])

    def test_different_type_is_not_exact_duplicate(self):
        rec = text('Atrial fibrillation (AF)')
        result = draft([rec], [sql_row(rec, 1, bilgi_turu='diger')])
        self.assertEqual(result['kesin_tekrar'], [])
        self.assertEqual(result['inceleme'][0]['asama'], 'olasi_tekrar')               # silinmez, incelemeye

    def test_remaining_single_group_held_for_review(self):
        a = cell('Age, years', G_AGE, 2, '62', hasta_grubu='Event (n=10)', p='0.031')
        b = cell('Age, years', G_AGE, 2, '58', hasta_grubu='No event (n=20)', p='0.031')
        existing = [sql_row(dict(b, kanit_cumle=G_AGE), 1)]
        result = draft([a, b], existing)
        self.assertEqual(result['adaylar'], [])
        self.assertEqual(sorted(r['asama'] for r in result['inceleme']), ['grup_uyumu', 'olasi_tekrar'])


# ---------------------------------------------------------------------------
# Sahte veritabanı (yalnız testler)
# ---------------------------------------------------------------------------

COLUMNS = ('id', 'makale_id', 'document_id', 'bilgi_turu', 'baglam', 'klinik_bilgi', 'klinik_bilgi_en', 'kanit_sayfa',
           'kanit_sayfa_bitis', 'kanit_cumle', 'kanit_turu', 'birlestirme', 'hasta_grubu', 'deger', 'unit', 'p', 'ci95',
           'etki_turu', 'comparator', 'sonuc_olcutu_tr', 'mudahale_en', 'mudahale_turu', 'outcome_type', 'taslak_notu',
           'review_status', 'cikarma_yontemi', 'satir_hash')


class FakeConn:
    def __init__(self, rows=(), on_insert=None):
        self.rows = [dict({c: None for c in COLUMNS}, **r) for r in rows]
        self.saved = copy.deepcopy(self.rows)
        self.on_insert, self.commits, self.rollbacks, self.statements = on_insert, 0, 0, []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        self.saved = copy.deepcopy(self.rows)

    def rollback(self):
        self.rollbacks += 1
        self.rows = copy.deepcopy(self.saved)


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.result, self.rowcount, self.description = conn, [], 0, None

    def execute(self, sql, params=()):
        conn, sql = self.conn, ' '.join(str(sql).split())
        conn.statements.append(sql)
        params = tuple(params or ())
        if sql.startswith('SELECT filename FROM pdf_belgeleri'):
            self.result = [('x.pdf',)] if params[0] == DOC_ID else []
        elif sql.startswith('SHOW COLUMNS'):
            self.result = [(c,) for c in COLUMNS]
        elif sql.startswith('INSERT IGNORE INTO klinik_bulgular'):
            cols = re.findall(r'`(\w+)`', sql)
            row = dict({c: None for c in COLUMNS}, **dict(zip(cols, params)))
            if any(r['satir_hash'] == row['satir_hash'] for r in conn.rows):
                self.rowcount = 0
                return
            row['id'] = max([r['id'] for r in conn.rows] or [0]) + 1
            conn.rows.append(row)
            self.rowcount = 1
            if conn.on_insert:
                conn.on_insert(conn)
        elif sql.startswith('SELECT * FROM klinik_bulgular'):
            self.description = [(c,) for c in COLUMNS]
            self.result = [tuple(r[c] for c in COLUMNS) for r in sorted(conn.rows, key=lambda r: r['id'])]
        elif sql.startswith('SELECT satir_hash FROM klinik_bulgular WHERE satir_hash IN'):
            self.result = [(r['satir_hash'],) for r in conn.rows if r['satir_hash'] in params]
        elif sql.startswith('SELECT COUNT(*) FROM klinik_bulgular WHERE id IN'):
            self.result = [(sum(1 for r in conn.rows if r['id'] in params and r['cikarma_yontemi'] == 'rag'
                                and r['review_status'] == 'verified'),)]
        elif sql.startswith('SELECT id, document_id'):
            self.result = [tuple(r.get(c) for c in A.SQL_COLUMNS) for r in conn.rows
                           if r['document_id'] == params[0] and r['bilgi_turu']]
        else:
            raise AssertionError(f'beklenmeyen SQL: {sql}')

    def fetchall(self):
        return list(self.result)

    def fetchone(self):
        return self.result[0] if self.result else None

    def close(self):
        pass


EXISTING = {'id': 1, 'document_id': DOC_ID, 'bilgi_turu': 'tani', 'baglam': 'study_finding', 'klinik_bilgi_en': 'AF',
            'kanit_sayfa': 1, 'kanit_cumle': TEXT, 'kanit_turu': 'metin', 'review_status': 'verified',
            'cikarma_yontemi': 'manuel', 'satir_hash': 'h' * 64, 'taslak_notu': '{"tablo_no": 1, "satir_no": 3}'}


def verified_draft(*records):
    result = draft(list(records))
    out = [A.serializable(r) for r in result['adaylar']]
    return out


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.records = verified_draft(text('Atrial fibrillation (AF)'), text('Chest pain', bilgi_turu='semptom'))
        self.records[0]['review_status'] = 'verified'                              # insan onayı (test)

    def test_dry_run_writes_only_verified_then_rolls_back(self):
        conn = FakeConn([EXISTING])
        result = A.aktar(A.KorumaliBaglanti(conn, yazma=True), DOC, PAGES, self.records)
        self.assertEqual((result['yazilan'], result['atlanan_onaysiz'], result['kalici']), (1, 1, False))
        self.assertEqual((conn.commits, len(conn.rows)), (0, 1))                   # ROLLBACK: yalnız mevcut satır
        self.assertEqual(conn.rows[0]['satir_hash'], 'h' * 64)

    def test_write_requires_commit_permission(self):
        conn = FakeConn([EXISTING])
        with self.assertRaises(A.AdaptorHatasi):
            A.aktar(A.KorumaliBaglanti(conn, yazma=True, commit_izni=False), DOC, PAGES, self.records, yaz=True)
        conn = FakeConn([EXISTING])
        result = A.aktar(A.KorumaliBaglanti(conn, yazma=True, commit_izni=True), DOC, PAGES, self.records, yaz=True)
        self.assertEqual((result['kalici'], conn.commits, len(conn.rows)), (True, 1, 2))
        self.assertEqual((conn.rows[1]['cikarma_yontemi'], conn.rows[1]['review_status']), ('rag', 'verified'))

    def test_guard_blocks_update_delete_ddl(self):
        guarded = A.KorumaliBaglanti(FakeConn([EXISTING]), yazma=True)
        for sql in ('UPDATE klinik_bulgular SET deger=1', 'DELETE FROM klinik_bulgular', 'DROP TABLE klinik_bulgular',
                    'REPLACE INTO klinik_bulgular VALUES (1)', 'INSERT INTO klinik_bulgular (id) VALUES (9)',
                    'ALTER TABLE klinik_bulgular ADD x INT'):
            with self.assertRaises(A.AdaptorHatasi, msg=sql):
                guarded.cursor().execute(sql)
        readonly = A.KorumaliBaglanti(FakeConn([EXISTING]), yazma=False)
        with self.assertRaises(A.AdaptorHatasi):
            readonly.cursor().execute('INSERT IGNORE INTO klinik_bulgular (`id`) VALUES (%s)', (2,))

    def test_any_field_change_of_existing_rows_detected(self):
        def tamper(conn):
            conn.rows[0]['deger'] = 'değişti'                                     # mevcut satırın tek alanı
        conn = FakeConn([EXISTING], on_insert=tamper)
        with self.assertRaises(A.AdaptorHatasi) as ctx:
            A.aktar(A.KorumaliBaglanti(conn, yazma=True), DOC, PAGES, self.records)
        self.assertIn('değişti', str(ctx.exception))
        self.assertEqual(conn.rows[0]['deger'], None)                              # ROLLBACK

    def test_refuses_unverified_foreign_or_clashing_records(self):
        conn = A.KorumaliBaglanti(FakeConn([EXISTING]), yazma=True)
        pending = [dict(r, review_status='pending') for r in self.records]
        with self.assertRaises(A.AdaptorHatasi):
            A.aktar(conn, DOC, PAGES, pending)                                     # verified yok
        foreign = [dict(self.records[0], taslak_notu='{"kaynak": "baska"}')]
        with self.assertRaises(A.AdaptorHatasi):
            A.aktar(conn, DOC, PAGES, foreign)
        clash = dict(EXISTING, id=2, satir_hash=A.satir_hash(DOC_ID, self.records[0]))
        with self.assertRaises(A.AdaptorHatasi) as ctx:
            A.aktar(A.KorumaliBaglanti(FakeConn([EXISTING, clash]), yazma=True), DOC, PAGES, self.records)
        self.assertIn('satir_hash', str(ctx.exception))

    def test_row_number_collision_with_existing_table_rows_stops(self):
        recs = verified_draft(cell('Stroke', R_STROKE, 3, '7.1', sonuc_olcutu_tr='5-y risk, %'))
        recs[0]['review_status'] = 'verified'
        note = json.loads(recs[0]['taslak_notu'])
        taken = dict(EXISTING, taslak_notu=json.dumps({'tablo_no': note['tablo_no'], 'satir_no': note['satir_no']}))
        with self.assertRaises(A.AdaptorHatasi) as ctx:
            A.aktar(A.KorumaliBaglanti(FakeConn([taken]), yazma=True), DOC, PAGES, recs)
        self.assertIn('satir_no', str(ctx.exception))

    def test_multi_part_record_transferred_with_all_parts(self):
        recs = verified_draft(boundary())
        recs[0]['review_status'] = 'verified'
        conn = FakeConn([EXISTING])
        A.aktar(A.KorumaliBaglanti(conn, yazma=True, commit_izni=True), DOC, PAGES, recs, yaz=True)
        row = conn.rows[-1]
        self.assertEqual((row['kanit_cumle'], row['kanit_sayfa_bitis'], row['birlestirme']), (PART1, 10, 'sayfa'))
        self.assertEqual(json.loads(row['taslak_notu'])['kanit_parcalari'],
                         [{'sayfa': 9, 'metin': PART1}, {'sayfa': 10, 'metin': PART2}])

    def test_satir_hash_matches_import_records(self):
        for rec in (self.records[0], verified_draft(boundary())[0]):
            rec = dict(rec, review_status='verified')
            conn = FakeConn()
            import_records(DOC, [rec], connection=conn, pages=PAGES, cikarma_yontemi='rag')
            self.assertEqual(conn.rows[0]['satir_hash'], A.satir_hash(DOC_ID, rec))


class ImporterAndSqlTests(unittest.TestCase):
    def test_import_records_extraction_method_backward_compatible(self):
        rec = dict(verified_draft(text('Atrial fibrillation (AF)'))[0], review_status='verified')
        conn = FakeConn()
        import_records(DOC, [rec], connection=conn, pages=PAGES)
        self.assertEqual(conn.rows[0]['cikarma_yontemi'], 'manuel')                # varsayılan değişmedi
        with self.assertRaises(ValueError):
            import_records(DOC, [rec], connection=FakeConn(), pages=PAGES, cikarma_yontemi='otomatik')

    def test_view_shows_flagged_sections_and_keeps_existing_rule(self):
        sql = Path('sql/sunum_hoca/00_ortak.sql').read_text(encoding='utf-8')
        clause = sql[sql.index('CASE WHEN t.ad IS NOT NULL'):sql.index('AS bolum_oneki')]
        self.assertIn("'$.bolum_goster'", clause)
        self.assertIn("= 'BOOLEAN'", clause)
        self.assertIn('t.ad IS NOT NULL', clause)                                  # mevcut kural korunur
        self.assertNotIn(';', clause)


if __name__ == '__main__':
    unittest.main()
