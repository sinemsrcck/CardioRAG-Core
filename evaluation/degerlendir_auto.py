"""Otomatik çıkarım (*_findings_auto.json) ile doğrulanmış kayıtları (*_dogrulanmis.json) karşılaştırır.

SALT OKUNUR: girdi dosyalarını değiştirmez, veritabanına bağlanmaz, LLM çağırmaz.

Eşleştirme: klinik_bilgi_en normalize edilir (küçük harf, tire/boşluk sadeleştirme) ve
difflib.SequenceMatcher oranı hesaplanır. Bütün çiftler skora göre azalan sırada
açgözlü biçimde bire bir atanır (her auto ve her doğrulanmış kayıt en fazla bir kez).
    skor >= 0.9      -> eşleşme (precision/recall'a sayılır)
    0.6 <= skor < 0.9 -> kısmi (ayrı raporlanır, eşleşme sayılmaz)

Kullanım:
    python -m evaluation.degerlendir_auto --auto <auto.json> \
        --dogrulanmis <d1.json> [<d2.json> ...] --rapor <rapor.json>
"""
import argparse
import difflib
import json
import re
from collections import Counter
from pathlib import Path

ESLESME_ESIGI = 0.9
KISMI_ESIGI = 0.6
_TIRE = re.compile(r'[‐-―−_-]+')
_BOSLUK = re.compile(r'\s+')


def normalize(metin):
    metin = _TIRE.sub(' ', str(metin or '').lower())
    return _BOSLUK.sub(' ', metin).strip()


def kayitlari_oku(yol):
    veri = json.loads(Path(yol).read_text(encoding='utf-8'))
    if isinstance(veri, dict):
        veri = veri.get('findings', [])
    if not isinstance(veri, list):
        raise ValueError(f'Kayıt listesi bekleniyordu: {yol}')
    return [k for k in veri if isinstance(k, dict)]


def eslestir(auto, dogru):
    """Bire bir açgözlü eşleştirme; (auto_idx, dogru_idx, skor) listesi döner (skor >= KISMI_ESIGI)."""
    na = [normalize(k.get('klinik_bilgi_en')) for k in auto]
    nd = [normalize(k.get('klinik_bilgi_en')) for k in dogru]
    ciftler = []
    for i, a in enumerate(na):
        for j, d in enumerate(nd):
            m = difflib.SequenceMatcher(None, a, d, autojunk=False)
            # Hızlı üst sınırlar eşiğin altındaysa tam oranı hesaplama.
            if not a or not d or m.real_quick_ratio() < KISMI_ESIGI or m.quick_ratio() < KISMI_ESIGI:
                continue
            skor = m.ratio()
            if skor >= KISMI_ESIGI:
                ciftler.append((skor, i, j))
    ciftler.sort(key=lambda c: (-c[0], c[1], c[2]))
    kullanilan_a, kullanilan_d, sonuc = set(), set(), []
    for skor, i, j in ciftler:
        if i in kullanilan_a or j in kullanilan_d:
            continue
        kullanilan_a.add(i)
        kullanilan_d.add(j)
        sonuc.append((i, j, skor))
    return sonuc


def _oran(pay, payda):
    return round(pay / payda, 4) if payda else None


def _ozet(kayit, idx=None):
    ozet = {k: kayit.get(k) for k in ('bilgi_turu', 'baglam', 'sayfa', 'kanit_turu', 'klinik_bilgi_en')}
    if idx is not None:
        ozet = {'sira': idx + 1, **ozet}
    return ozet


def degerlendir(auto, dogru, dogru_kaynak):
    ciftler = eslestir(auto, dogru)
    tam = [c for c in ciftler if c[2] >= ESLESME_ESIGI]
    kismi = [c for c in ciftler if c[2] < ESLESME_ESIGI]
    eslesen_a = {i for i, _, _ in tam}
    eslesen_d = {j for _, j, _ in tam}
    kismi_a = {i for i, _, _ in kismi}
    kismi_d = {j for _, j, _ in kismi}

    tur_dogru = sum(auto[i].get('bilgi_turu') == dogru[j].get('bilgi_turu') for i, j, _ in tam)
    baglam_dogru = sum(auto[i].get('baglam') == dogru[j].get('baglam') for i, j, _ in tam)
    karisiklik = Counter(f"{auto[i].get('bilgi_turu')} -> {dogru[j].get('bilgi_turu')}"
                         for i, j, _ in tam if auto[i].get('bilgi_turu') != dogru[j].get('bilgi_turu'))
    baglam_karisiklik = Counter(f"{auto[i].get('baglam')} -> {dogru[j].get('baglam')}"
                                for i, j, _ in tam if auto[i].get('baglam') != dogru[j].get('baglam'))

    tur_toplam = Counter(k.get('bilgi_turu') for k in dogru)
    tur_eslesen = Counter(dogru[j].get('bilgi_turu') for j in eslesen_d)
    tur_recall = {t: {'dogrulanmis': n, 'eslesen': tur_eslesen[t], 'recall': _oran(tur_eslesen[t], n)}
                  for t, n in sorted(tur_toplam.items(), key=lambda x: (-x[1], str(x[0])))}

    def cift_ozeti(i, j, skor):
        return {'skor': round(skor, 4), 'auto': _ozet(auto[i], i),
                'dogrulanmis': {'kaynak': dogru_kaynak[j], **_ozet(dogru[j], None)}}

    return {
        'esikler': {'eslesme': ESLESME_ESIGI, 'kismi': KISMI_ESIGI},
        'sayilar': {'auto': len(auto), 'dogrulanmis': len(dogru), 'eslesen': len(tam),
                    'kismi': len(kismi), 'eslesmeyen_auto': len(auto) - len(eslesen_a),
                    'eslesmeyen_dogrulanmis': len(dogru) - len(eslesen_d)},
        'precision': _oran(len(tam), len(auto)),
        'recall': _oran(len(tam), len(dogru)),
        'eslesenlerde': {
            'bilgi_turu_dogrulugu': _oran(tur_dogru, len(tam)),
            'baglam_dogrulugu': _oran(baglam_dogru, len(tam)),
            'bilgi_turu_hatalari': dict(karisiklik.most_common()),
            'baglam_hatalari': dict(baglam_karisiklik.most_common()),
        },
        'bilgi_turu_recall': tur_recall,
        'kismi_eslesmeler': [cift_ozeti(*c) for c in sorted(kismi, key=lambda c: c[0])],
        'eslesmeyen_auto': [{**_ozet(k, i), 'kismi_eslesmesi_var': i in kismi_a}
                            for i, k in enumerate(auto) if i not in eslesen_a],
        'eslesmeyen_dogrulanmis': [{'kaynak': dogru_kaynak[j], **_ozet(k, None),
                                    'kismi_eslesmesi_var': j in kismi_d}
                                   for j, k in enumerate(dogru) if j not in eslesen_d],
    }


def _yuzde(x):
    return '-' if x is None else f'{x * 100:.1f}%'


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--auto', required=True, help='*_findings_auto.json')
    p.add_argument('--dogrulanmis', required=True, nargs='+', help='bir veya daha çok *_dogrulanmis.json')
    p.add_argument('--rapor', help='JSON rapor yolu (verilmezse yalnızca konsol özeti)')
    args = p.parse_args(argv)

    auto = kayitlari_oku(args.auto)
    dogru, kaynak = [], []
    for yol in args.dogrulanmis:
        kayitlar = kayitlari_oku(yol)
        dogru.extend(kayitlar)
        kaynak.extend([str(yol)] * len(kayitlar))

    sonuc = {'auto_dosyasi': str(args.auto), 'dogrulanmis_dosyalari': [str(y) for y in args.dogrulanmis],
             **degerlendir(auto, dogru, kaynak)}

    s, e = sonuc['sayilar'], sonuc['eslesenlerde']
    print(f"auto: {s['auto']} | doğrulanmış: {s['dogrulanmis']} | eşleşen (>= {ESLESME_ESIGI}): {s['eslesen']}"
          f" | kısmi ({KISMI_ESIGI}-{ESLESME_ESIGI}): {s['kismi']}")
    print(f"precision: {_yuzde(sonuc['precision'])} | recall: {_yuzde(sonuc['recall'])}")
    print(f"eşleşenlerde bilgi_turu doğruluğu: {_yuzde(e['bilgi_turu_dogrulugu'])}"
          f" | baglam doğruluğu: {_yuzde(e['baglam_dogrulugu'])}")
    print('bilgi_turu recall: ' + ', '.join(f"{t} {v['eslesen']}/{v['dogrulanmis']}"
                                           for t, v in sonuc['bilgi_turu_recall'].items()))
    if args.rapor:
        rapor = Path(args.rapor)
        rapor.parent.mkdir(parents=True, exist_ok=True)
        rapor.write_text(json.dumps(sonuc, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Rapor: {rapor}')
    return sonuc


if __name__ == '__main__':
    main()
