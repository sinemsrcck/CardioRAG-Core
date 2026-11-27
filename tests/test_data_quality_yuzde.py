"""src.data_quality yuzde_100_ustu (r5): yüzde türü sayının kendi yakın bağlamından belirlenir. Veritabanı yok."""
import unittest

from src.data_quality import check_numeric, percent_kind, parse_numbers


def row(**kw):
    return {'satir_hash': 'h', **kw}


def level(r):
    found = [f['seviye'] for f in check_numeric(r) if f['kontrol_kodu'] == 'yuzde_100_ustu']
    return found[0] if found else None


class PercentKindTests(unittest.TestCase):
    def test_threshold_and_change_may_exceed_100(self):
        for deger, outcome in (
                ('>148.3% with sensitivity of 80.0%, specificity of 85.1%', None),      # eşik + yakında oran sözcüğü
                ('≥ 250%', 'troponin rise'),
                ('a 180% increase from baseline', None),
                ('relative increase of 320%', None),
                ('210%', 'delta troponin change %')):                                   # bağlam yalnız değişim
            with self.subTest(deger=deger):
                self.assertIsNone(level(row(deger=deger, sonuc_olcutu_tr=outcome)))

    def test_true_proportions_still_errors(self):
        self.assertEqual(level(row(deger='120%', sonuc_olcutu_tr='sensitivity')), 'hata')   # mevcut senaryo
        self.assertEqual(level(row(deger='sensitivity of 104%')), 'hata')
        self.assertEqual(level(row(deger='specificity 101.5%, change 5%')), 'hata')        # oran sözcüğü sayıya yakın

    def test_unknown_percent_type_not_accepted(self):
        self.assertEqual(level(row(deger='150%')), 'uyari')                                  # mevcut senaryo
        self.assertEqual(level(row(deger='150%', sonuc_olcutu_tr='sensitivity change')), 'uyari')   # çelişkili bağlam
        self.assertEqual(level(row(deger='135', unit='%')), 'uyari')

    def test_values_at_or_below_100_unaffected_and_detail_reports_type(self):
        self.assertIsNone(level(row(deger='79%', sonuc_olcutu_tr='sensitivity')))
        flags = [f for f in check_numeric(row(deger='sensitivity of 104%')) if f['kontrol_kodu'] == 'yuzde_100_ustu']
        self.assertEqual(flags[0]['ayrinti']['yuzde_turu'], {'104.0': 'oran'})

    def test_percent_kind_uses_local_context(self):
        text = '>148.3% with sensitivity of 80.0%'
        numbers = parse_numbers(text)
        self.assertEqual(percent_kind(text, numbers[0], text), 'esik')
        self.assertEqual(percent_kind(text, numbers[1], text), 'oran')


if __name__ == '__main__':
    unittest.main()
