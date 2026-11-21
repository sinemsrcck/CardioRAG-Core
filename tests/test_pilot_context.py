import unittest
from src.pilot_clinical import validate_records, TYPES, CONTEXTS
from src.pilot_auto_extract import SYSTEM

class TestPilotContext(unittest.TestCase):
    def test_contexts_and_types(self):
        self.assertIn('tani_kriteri', TYPES)
        self.assertIn('exclusion_criterion', CONTEXTS)
        self.assertIn('retrospective study design', SYSTEM)

    def test_duplicate_with_same_context(self):
        base = dict(bilgi_turu='tani', baglam='study_finding', klinik_bilgi_en='atrial fibrillation', kanit_cumle='atrial fibrillation', sayfa=1)
        a, errors = validate_records([base, dict(base)], {1:'atrial fibrillation'})
        # Tekrar sessizce atlanmaz; açık 'tekrar' hatası olarak raporlanır.
        self.assertEqual((len(a),len(errors)),(1,1))
        self.assertTrue(errors[0]['hata'].startswith('tekrar'))

    def test_different_context_retained(self):
        base = dict(bilgi_turu='tani', baglam='study_finding', klinik_bilgi_en='atrial fibrillation', kanit_cumle='atrial fibrillation', sayfa=1)
        a, errors = validate_records([base, dict(base,baglam='literature_background')], {1:'atrial fibrillation'})
        self.assertEqual((len(a),len(errors)),(2,0))

    def test_invalid_context(self):
        a, errors = validate_records([dict(bilgi_turu='tani',baglam='unknown',klinik_bilgi_en='AF',kanit_cumle='AF',sayfa=1)], {1:'AF'})
        self.assertEqual((len(a),len(errors)),(0,1))

if __name__ == '__main__': unittest.main()
