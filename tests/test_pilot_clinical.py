import unittest
from src.pilot_clinical import validate_records

class PilotTests(unittest.TestCase):
    def test_valid_multiple_findings_same_page(self):
        pages = {1: 'Patients reported chest pain and dyspnea.'}
        rows = [dict(bilgi_turu='semptom', klinik_bilgi_en=f, sayfa=1, kanit_cumle=pages[1]) for f in ('chest pain', 'dyspnea')]
        good, errors = validate_records(rows, pages)
        self.assertEqual((len(good), errors), (2, []))

    def test_reject_unproven(self):
        good, errors = validate_records([dict(bilgi_turu='tani', klinik_bilgi_en='stroke', sayfa=1, kanit_cumle='heart failure')], {1:'heart failure'})
        self.assertFalse(good)
        self.assertEqual(len(errors), 1)

    def test_deduplicate(self):
        row = dict(bilgi_turu='tani', klinik_bilgi_en='heart failure', sayfa=1, kanit_cumle='heart failure')
        good, errors = validate_records([row, row], {1:'heart failure'})
        self.assertEqual(len(good), 1)

if __name__ == '__main__':
    unittest.main()
