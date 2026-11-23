"""Otomatik çıkarım (*_findings_auto.json) ile doğrulanmış kayıtları (*_dogrulanmis.json) karşılaştırır.

SALT OKUNUR: girdi dosyalarını değiştirmez, veritabanına bağlanmaz, LLM çağırmaz.

Eşleştirme: klinik_bilgi_en normalize edilir (küçük harf, tire/boşluk sadeleştirme) ve
difflib.SequenceMatcher oranı hesaplanır. Bütün çiftler skora göre azalan sırada
açgözlü biçimde bire bir atanır (her auto ve her doğrulanmış kayıt en fazla bir kez).
    skor >= 0.9      -> eşleşme (precision/recall'a sayılır)
    0.6 <= skor < 0.9 -> kısmi (ayrı raporlanır, eşleşme sayılmaz)
Toleranslı eşleşme: katı eşleşmede kalanlar arasında, aynı sayfada (sayfa aralıkları kesişiyor),
normalize kısa ifade en az 4 kelimeyse ve uzun ifadenin içinde kelime sınırında birebir geçiyorsa
eşleşme sayılır (bire bir). Raporda 'kati' ve 'toleransli' metrikleri ayrı verilir.
Yalnızca bilgi_turu, baglam, klinik_bilgi_en ve sayfa kullanılır; fazladan alanlar yok sayılır.

Kullanım:
    python -m evaluation.degerlendir_auto --auto <auto.json> \
        --dogrulanmis <d1.json> [<d2.json> ...] --rapor <rapor.json> [--ingilizce-olmayani-ele]
"""
import argparse
import difflib
import json
import re
from collections import Counter
from pathlib import Path

ESLESME_ESIGI = 0.9
KISMI_ESIGI = 0.6
TOLERANS_KELIME = 4
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


def _sayfalar(kayit):
    """Kaydın kapsadığı fiziksel sayfalar (sayfa..sayfa_bitis); geçersizse boş küme."""
    bas, bit = kayit.get('sayfa'), kayit.get('sayfa_bitis')
    if not isinstance(bas, int) or isinstance(bas, bool):
        return set()
    if not isinstance(bit, int) or isinstance(bit, bool) or bit < bas:
        bit = bas
    return set(range(bas, bit + 1))


def _iceriyor(kisa, uzun):
    """Normalize kısa ifade uzun ifadenin içinde kelime sınırlarında birebir geçiyor mu."""
    return re.search(r'(?<!\w)' + re.escape(kisa) + r'(?!\w)', uzun) is not None


def toleransli_eslestir(auto, dogru, kullanilan_a, kullanilan_d):
    """Katı eşleşmede kalan kayıtlar için içerme eşleşmesi (bire bir, açgözlü).

    Aynı sayfada (sayfa aralıkları kesişiyor) ve iki ifadeden kısa olanı en az TOLERANS_KELIME kelimeyse
    ve uzun olanın içinde birebir geçiyorsa eşleşme sayılır. Uzun kısa ifadeler önce atanır."""
    na = {i: normalize(auto[i].get('klinik_bilgi_en')) for i in range(len(auto)) if i not in kullanilan_a}
    nd = {j: normalize(dogru[j].get('klinik_bilgi_en')) for j in range(len(dogru)) if j not in kullanilan_d}
    sa = {i: _sayfalar(auto[i]) for i in na}
    sd = {j: _sayfalar(dogru[j]) for j in nd}
    adaylar = []
    for i, a in na.items():
        for j, d in nd.items():
            if not a or not d or not (sa[i] & sd[j]):
                continue
            kisa, uzun = (a, d) if len(a) <= len(d) else (d, a)
            if len(kisa.split()) >= TOLERANS_KELIME and _iceriyor(kisa, uzun):
                adaylar.append((len(kisa) / len(uzun), len(kisa), i, j))
    adaylar.sort(key=lambda c: (-c[1], -c[0], c[2], c[3]))
    ka, kd, sonuc = set(), set(), []
    for oran, _, i, j in adaylar:
        if i in ka or j in kd:
            continue
        ka.add(i)
        kd.add(j)
        sonuc.append((i, j, round(oran, 4)))
    return sonuc


def _metrikler(auto, dogru, ciftler):
    eslesen_d = {j for _, j, _ in ciftler}
    tur_dogru = sum(auto[i].get('bilgi_turu') == dogru[j].get('bilgi_turu') for i, j, _ in ciftler)
    baglam_dogru = sum(auto[i].get('baglam') == dogru[j].get('baglam') for i, j, _ in ciftler)
    karisiklik = Counter(f"{auto[i].get('bilgi_turu')} -> {dogru[j].get('bilgi_turu')}"
                         for i, j, _ in ciftler if auto[i].get('bilgi_turu') != dogru[j].get('bilgi_turu'))
    baglam_karisiklik = Counter(f"{auto[i].get('baglam')} -> {dogru[j].get('baglam')}"
                                for i, j, _ in ciftler if auto[i].get('baglam') != dogru[j].get('baglam'))
    tur_toplam = Counter(k.get('bilgi_turu') for k in dogru)
    tur_eslesen = Counter(dogru[j].get('bilgi_turu') for j in eslesen_d)
    return {
        'eslesen': len(ciftler),
        'precision': _oran(len(ciftler), len(auto)),
        'recall': _oran(len(ciftler), len(dogru)),
        'bilgi_turu_dogrulugu': _oran(tur_dogru, len(ciftler)),
        'baglam_dogrulugu': _oran(baglam_dogru, len(ciftler)),
        'bilgi_turu_hatalari': dict(karisiklik.most_common()),
        'baglam_hatalari': dict(baglam_karisiklik.most_common()),
        'bilgi_turu_recall': {t: {'dogrulanmis': n, 'eslesen': tur_eslesen[t], 'recall': _oran(tur_eslesen[t], n)}
                              for t, n in sorted(tur_toplam.items(), key=lambda x: (-x[1], str(x[0])))},
    }


def degerlendir(auto, dogru, dogru_kaynak):
    """Yalnızca bilgi_turu, baglam, klinik_bilgi_en, sayfa(/sayfa_bitis) kullanılır; diğer alanlar
    (kategoriler, deger, unit, hasta_grubu, otomatik_duzeltme ...) yok sayılır."""
    ciftler = eslestir(auto, dogru)
    kati = [c for c in ciftler if c[2] >= ESLESME_ESIGI]
    kismi = [c for c in ciftler if c[2] < ESLESME_ESIGI]
    kati_a = {i for i, _, _ in kati}
    kati_d = {j for _, j, _ in kati}
    ek = toleransli_eslestir(auto, dogru, kati_a, kati_d)
    toleransli = kati + ek
    tol_a = kati_a | {i for i, _, _ in ek}
    tol_d = kati_d | {j for _, j, _ in ek}
    kismi_a = {i for i, _, _ in kismi}
    kismi_d = {j for _, j, _ in kismi}

    def cift_ozeti(i, j, skor):
        return {'skor': round(skor, 4), 'auto': _ozet(auto[i], i),
                'dogrulanmis': {'kaynak': dogru_kaynak[j], **_ozet(dogru[j], None)}}

    return {
        'esikler': {'eslesme': ESLESME_ESIGI, 'kismi': KISMI_ESIGI, 'tolerans_kelime': TOLERANS_KELIME},
        'sayilar': {'auto': len(auto), 'dogrulanmis': len(dogru), 'kati_eslesen': len(kati),
                    'toleransli_eslesen': len(toleransli), 'yalniz_toleransli': len(ek), 'kismi': len(kismi),
                    'eslesmeyen_auto': len(auto) - len(tol_a),
                    'eslesmeyen_dogrulanmis': len(dogru) - len(tol_d)},
        'kati': _metrikler(auto, dogru, kati),
        'toleransli': _metrikler(auto, dogru, toleransli),
        'toleransli_ek_eslesmeler': [cift_ozeti(*c) for c in ek],
        'kismi_eslesmeler': [cift_ozeti(*c) for c in sorted(kismi, key=lambda c: c[0])],
        # Eşleşmeyen listeler toleranslı eşleşmeye göredir (iki ölçütte de eşleşmeyenler).
        'eslesmeyen_auto': [{**_ozet(k, i), 'kismi_eslesmesi_var': i in kismi_a}
                            for i, k in enumerate(auto) if i not in tol_a],
        'eslesmeyen_dogrulanmis': [{'kaynak': dogru_kaynak[j], **_ozet(k, None),
                                    'kismi_eslesmesi_var': j in kismi_d}
                                   for j, k in enumerate(dogru) if j not in tol_d],
    }


def _yuzde(x):
    return '-' if x is None else f'{x * 100:.1f}%'


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--auto', required=True, help='*_findings_auto.json')
    p.add_argument('--dogrulanmis', required=True, nargs='+', help='bir veya daha çok *_dogrulanmis.json')
    p.add_argument('--rapor', help='JSON rapor yolu (verilmezse yalnızca konsol özeti)')
    p.add_argument('--ingilizce-olmayani-ele', action='store_true',
                   help='auto kayıtlarından kanit_cumle İngilizce olmayanları bellekte ele '
                        '(src.rag_son_filtre.is_non_english); dosya değişmez')
    args = p.parse_args(argv)

    auto = kayitlari_oku(args.auto)
    elenen = 0
    if args.ingilizce_olmayani_ele:
        from src.rag_son_filtre import is_non_english
        once = len(auto)
        auto = [k for k in auto if not is_non_english(str(k.get('kanit_cumle') or k.get('klinik_bilgi_en') or ''))]
        elenen = once - len(auto)
        print(f'İngilizce olmayan {elenen} auto kayıt bellekte elendi')
    dogru, kaynak = [], []
    for yol in args.dogrulanmis:
        kayitlar = kayitlari_oku(yol)
        dogru.extend(kayitlar)
        kaynak.extend([str(yol)] * len(kayitlar))

    sonuc = {'auto_dosyasi': str(args.auto), 'dogrulanmis_dosyalari': [str(y) for y in args.dogrulanmis],
             'ingilizce_olmayan_elenen': elenen if args.ingilizce_olmayani_ele else None,
             **degerlendir(auto, dogru, kaynak)}

    s = sonuc['sayilar']
    print(f"auto: {s['auto']} | doğrulanmış: {s['dogrulanmis']} | kısmi ({KISMI_ESIGI}-{ESLESME_ESIGI}): {s['kismi']}")
    for ad, etiket in (('kati', f'katı (>= {ESLESME_ESIGI})'), ('toleransli', f'toleranslı (+içerme, >= {TOLERANS_KELIME} kelime)')):
        m = sonuc[ad]
        print(f"{etiket}: eşleşen {m['eslesen']} | precision {_yuzde(m['precision'])} | recall {_yuzde(m['recall'])}"
              f" | bilgi_turu {_yuzde(m['bilgi_turu_dogrulugu'])} | baglam {_yuzde(m['baglam_dogrulugu'])}")
    print('toleranslı bilgi_turu recall: ' + ', '.join(f"{t} {v['eslesen']}/{v['dogrulanmis']}"
                                                     for t, v in sonuc['toleransli']['bilgi_turu_recall'].items()))
    if args.rapor:
        rapor = Path(args.rapor)
        rapor.parent.mkdir(parents=True, exist_ok=True)
        rapor.write_text(json.dumps(sonuc, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Rapor: {rapor}')
    return sonuc


if __name__ == '__main__':
    main()
