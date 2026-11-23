import json
import tempfile
import unittest
from pathlib import Path

from src.rag_son_filtre import KURALLAR, filtrele, is_non_english, main


def kayit(bilgi, tur='klinik_sonuc', baglam='study_finding', kanit=None):
    return dict(bilgi_turu=tur, baglam=baglam, klinik_bilgi_en=bilgi, kanit_cumle=kanit or bilgi, sayfa=1)


FR = kayit('principalement de sexe masculin',
           kanit="Les personnes victimes étaient plus jeunes, principalement de sexe masculin et dans les pays du CCG.")
ONERI = kayit('development of community response models', tur='tedavi', baglam='recommendation')
PARCA = kayit('survival to discharge')
SAYILI = kayit('6.3% survived')                       # rakam var -> kalır
TANI = kayit('trauma', tur='tani')                    # tür klinik_sonuc/olcum değil -> kalır
UZUN = kayit('ELR has moderate discriminative ability', tur='olcum')
HEPSI = [FR, ONERI, PARCA, SAYILI, TANI, UZUN]


class SonFiltre(unittest.TestCase):
    def test_varsayilan_yalniz_ingilizce_olmayan(self):
        kalan, atilan = filtrele(HEPSI)
        self.assertEqual([(a['sira'], a['sebep']) for a in atilan], [(1, 'ingilizce_olmayan')])
        self.assertEqual(kalan, HEPSI[1:])

    def test_tum_kurallar(self):
        kalan, atilan = filtrele(HEPSI, list(KURALLAR))
        self.assertEqual(kalan, [SAYILI, TANI, UZUN])
        self.assertEqual([(a['sira'], a['sebep']) for a in atilan],
                         [(1, 'ingilizce_olmayan'), (2, 'oneri'), (3, 'degersiz_parca')])

    def test_kurallar_tek_tek(self):
        for kural, beklenen in (('ingilizce_olmayan', FR), ('oneri', ONERI), ('degersiz_parca', PARCA)):
            kalan, atilan = filtrele(HEPSI, [kural])
            self.assertEqual([a['kayit'] for a in atilan], [beklenen])
            self.assertEqual(len(kalan), len(HEPSI) - 1)

    def test_is_non_english(self):
        self.assertTrue(is_non_english('السكتة القلبية'))
        self.assertTrue(is_non_english(FR['kanit_cumle']))
        self.assertFalse(is_non_english('Use of emergency medical services was low across the GCC countries.'))
        self.assertFalse(is_non_english('12.5 ± 3.1'))

    def test_kapali_ve_bilinmeyen(self):
        self.assertEqual(filtrele(HEPSI, []), (HEPSI, []))
        with self.assertRaises(ValueError):
            filtrele(HEPSI, ['yok'])

    def test_cli_yazar_ve_uzerine_yazmaz(self):
        with tempfile.TemporaryDirectory() as d:
            girdi, cikti = Path(d) / 'k.json', Path(d) / 'f.json'
            girdi.write_text(json.dumps(HEPSI), encoding='utf-8')
            main(['--girdi', str(girdi), '--cikti', str(cikti), '--kurallar', 'oneri'])
            self.assertEqual(len(json.loads(cikti.read_text(encoding='utf-8'))), 5)
            atilan = json.loads(cikti.with_suffix('.atilan.json').read_text(encoding='utf-8'))
            self.assertEqual([a['sebep'] for a in atilan], ['oneri'])
            cikti2 = Path(d) / 'g.json'
            main(['--girdi', str(girdi), '--cikti', str(cikti2)])
            atilan2 = json.loads(cikti2.with_suffix('.atilan.json').read_text(encoding='utf-8'))
            self.assertEqual([a['sebep'] for a in atilan2], ['ingilizce_olmayan'])
            with self.assertRaises(FileExistsError):
                main(['--girdi', str(girdi), '--cikti', str(cikti)])


if __name__ == '__main__':
    unittest.main()
