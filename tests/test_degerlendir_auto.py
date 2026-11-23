import unittest

from evaluation.degerlendir_auto import degerlendir


def kayit(metin, sayfa=1, tur='klinik_sonuc', **ek):
    return dict(bilgi_turu=tur, baglam='study_finding', klinik_bilgi_en=metin, sayfa=sayfa, **ek)


class Degerlendir(unittest.TestCase):
    def test_kati_ve_toleransli_ayri(self):
        auto = [kayit('Survival to discharge was 6.3%', kategoriler=['x'], deger='6.3', unit='%'),
                kayit('our review showed ELR has moderate discriminative ability in patients', tur='olcum'),
                kayit('the low rate of survival was reported in all studies', sayfa=2),  # sayfa farklı
                kayit('low rate')]                                            # < 4 kelime -> eşleşmez
        dogru = [kayit('survival to discharge was 6.3%'),
                 kayit('ELR has moderate discriminative ability', tur='olcum'),
                 kayit('the low rate of survival', sayfa=3)]
        r = degerlendir(auto, dogru, ['d'] * len(dogru))
        self.assertEqual(r['kati']['eslesen'], 1)
        self.assertEqual(r['toleransli']['eslesen'], 2)
        self.assertEqual(r['toleransli']['bilgi_turu_dogrulugu'], 1.0)
        self.assertEqual(r['sayilar']['eslesmeyen_auto'], 2)
        self.assertEqual(r['sayilar']['eslesmeyen_dogrulanmis'], 1)

    def test_kelime_siniri(self):
        auto = [kayit('rate of survival to discharge')]
        dogru = [kayit('the overall rate of survival to discharges was low')]
        self.assertEqual(degerlendir(auto, dogru, ['d'])['toleransli']['eslesen'], 0)


if __name__ == '__main__':
    unittest.main()
