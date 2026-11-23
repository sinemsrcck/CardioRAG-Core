"""RAG çıkarım çalıştırmasını doğrulanmış (verified) JSON kayıtlarıyla 8 kategori bazında karşılaştırır.
Veritabanına bağlanmaz; yalnız <calistirma_dizini>/karsilastirma.json yazar.

    python -m src.rag_klinik_karsilastir --calistirma data/rag_cikarim/<pdf>/<zaman>
    python -m src.rag_klinik_karsilastir --calistirma <dizin> --dogrulanmis a.json b.json

Eşleşme (kategori içinde, bire bir, açgözlü): aynı sayfa + kanıt benzer (normalize eşit / biri diğerini içerir /
kelime Jaccard >= 0.6) + klinik bilgi benzer (içerme / Jaccard >= 0.5); measurements_by_group'ta değer de eşit
olmalı (ikisi de doluysa). Doğrulanmış kayıtlar aynı yönlendirme kuralıyla (categories_of) kategorilere ayrılır.
"""
import argparse
import json
import re
from pathlib import Path

from src.rag_klinik_cikarim import CATEGORIES, _norm, route

PRESETS = {
    'who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf': [
        'data/review/who_jcpsp_2020/troponin_metin_benim_dogrulanmis.json',
        'data/review/who_jcpsp_2020/zehra_troponin_metin_dogrulanmis.json',
        'data/review/troponin/zehra_troponin_tablo_dogrulanmis(1).json'],
    'TK-cardiovascular37-2-1.pdf': [
        'data/review/tk_cardiovascular37_2026/zehra_eozinofil_tablo_dogrulanmis.json',
        'data/review/tk_cardiovascular37_2026/zehra_eozinofil_metin_dogrulanmis.json'],
    'who-Out-of-hospital cardiac arrest in countries of the Gulf Cooperation.pdf': [
        'data/review/who_ohca_gcc_2020/zehra_ohca_metin_dogrulanmis.json'],
    'pubM-Atrial Fibrillation.pdf': [
        'data/review/pubm_af_2025/af_metin_tani_dogrulanmis.json',
        'data/review/pubm_af_2025/af_metin_kalan_dogrulanmis.json',
        'data/review/pubm_af_2025/af_tablo_secili_dogrulanmis.json'],
    'pubM-Clinical Updates in Coronary Artery Disease A comprehensive review.pdf': [
        'data/review/pubm_cad_2024/cad_tani_guncel_dogrulanmis.json',
        'data/review/pubm_cad_2024/zehra_cad_metin_dogrulanmis.json'],
    'TK-cardiovascular36-2-1.pdf': [
        'data/review/pilot/zehra_dogrulanmis_v3_3.json',
        'data/review/pilot/pilot_findings_v3_3_sinem_sql_verified.json'],
}


def load_records(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    records = data if isinstance(data, list) else data.get('kayitlar', [])
    return [r for r in records if isinstance(r, dict)]


def tokens(text):
    return set(re.findall(r'\w+', _norm(text)))


def jaccard(a, b):
    a, b = tokens(a), tokens(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def similar(a, b, threshold):
    a_n, b_n = _norm(a), _norm(b)
    if not a_n or not b_n:
        return False
    return a_n == b_n or a_n in b_n or b_n in a_n or jaccard(a, b) >= threshold


def matches(x, y, category):
    if x.get('sayfa') != y.get('sayfa'):
        return False
    if not similar(x.get('kanit'), y.get('kanit'), 0.6):
        return False
    if category != 'measurements' and not similar(x.get('klinik_bilgi'), y.get('klinik_bilgi'), 0.5):
        return False
    if category == 'measurements_by_group' and x.get('deger') and y.get('deger'):
        return _norm(x['deger']) == _norm(y['deger'])
    return True


def compare_category(extracted, verified, category):
    used, pairs = set(), []
    for i, x in enumerate(extracted):
        for j, y in enumerate(verified):
            if j not in used and matches(x, y, category):
                used.add(j)
                pairs.append((i, j))
                break
    matched_x = {i for i, _ in pairs}
    n_x, n_y, n_m = len(extracted), len(verified), len(pairs)
    return {
        'cikarilan': n_x, 'dogrulanmis': n_y, 'eslesen': n_m,
        'kesinlik': round(n_m / n_x, 3) if n_x else None,
        'duyarlilik': round(n_m / n_y, 3) if n_y else None,
        'kacirilan_ornek': [{k: verified[j].get(k) for k in ('sayfa', 'klinik_bilgi', 'deger', 'hasta_grubu', 'kanit')}
                            for j in range(n_y) if j not in used][:8],
        'fazla_ornek': [{k: extracted[i].get(k) for k in ('sayfa', 'klinik_bilgi', 'deger', 'hasta_grubu', 'kanit')}
                        for i in range(n_x) if i not in matched_x][:8],
    }


def fact_level(extracted, verified):
    """Kategori bağımsız: sayfa + kanıt + bilgi eşleşmesi (çıkarım mı yönlendirme mi kaçırdı, ayırmak için)."""
    def flat(records):
        return [{'sayfa': r.get('sayfa'), 'kanit': r.get('kanit_cumle'), 'klinik_bilgi': r.get('klinik_bilgi_en')
                 or r.get('klinik_bilgi'), 'deger': r.get('deger')} for r in records]
    result = compare_category(flat(extracted), flat(verified), 'kayit')
    return {k: result[k] for k in ('cikarilan', 'dogrulanmis', 'eslesen', 'kesinlik', 'duyarlilik')}


def compare(run_dir, verified_paths=None):
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / 'calistirma.json').read_text(encoding='utf-8'))
    pdf_name = meta['pdf']
    paths = verified_paths or PRESETS.get(pdf_name)
    if not paths:
        raise SystemExit(f'{pdf_name} için doğrulanmış dosya bilinmiyor; --dogrulanmis verin')
    extracted = load_records(run_dir / 'kayitlar.json')
    verified = [r for p in paths for r in load_records(p) if r.get('review_status') == 'verified']
    routed_x, routed_y = route(extracted, pdf_name), route(verified, pdf_name)
    per_category = {c: compare_category(routed_x[c], routed_y[c], c) for c in CATEGORIES}
    totals = {k: sum(per_category[c][k] for c in CATEGORIES) for k in ('cikarilan', 'dogrulanmis', 'eslesen')}
    totals['kesinlik'] = round(totals['eslesen'] / totals['cikarilan'], 3) if totals['cikarilan'] else None
    totals['duyarlilik'] = round(totals['eslesen'] / totals['dogrulanmis'], 3) if totals['dogrulanmis'] else None
    report = {'pdf': pdf_name, 'calistirma': str(run_dir), 'dogrulanmis_dosyalar': list(paths),
              'dogrulanmis_kayit': len(verified), 'cikarilan_kayit': len(extracted),
              'kayit_duzeyi': fact_level(extracted, verified), 'kategori_toplam': totals, 'kategoriler': per_category,
              'yontem': 'aynı sayfa + kanıt benzerliği (Jaccard>=0.6) + bilgi benzerliği (Jaccard>=0.5); '
                        'measurements_by_group için değer eşitliği. Otomatik ölçüdür; klinik doğruluk yerine geçmez.'}
    (run_dir / 'karsilastirma.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m src.rag_klinik_karsilastir')
    parser.add_argument('--calistirma', required=True, help='data/rag_cikarim/<pdf>/<zaman> dizini')
    parser.add_argument('--dogrulanmis', nargs='+', help='doğrulanmış JSON dosyaları (varsayılan: PDF için hazır set)')
    args = parser.parse_args(argv)
    r = compare(args.calistirma, args.dogrulanmis)
    print(f"PDF: {r['pdf']} | çıkarılan {r['cikarilan_kayit']} | doğrulanmış {r['dogrulanmis_kayit']}")
    k = r['kayit_duzeyi']
    print(f"Kayıt düzeyi (kategori bağımsız): eşleşen {k['eslesen']} | kesinlik {k['kesinlik']} | duyarlılık {k['duyarlilik']}")
    print(f"{'kategori':27s} {'çıkarılan':>9s} {'doğrulanmış':>11s} {'eşleşen':>8s} {'kesinlik':>9s} {'duyarlılık':>10s}")
    for c, v in r['kategoriler'].items():
        print(f"{c:27s} {v['cikarilan']:9d} {v['dogrulanmis']:11d} {v['eslesen']:8d} {str(v['kesinlik']):>9s} {str(v['duyarlilik']):>10s}")
    t = r['kategori_toplam']
    print(f"{'TOPLAM':27s} {t['cikarilan']:9d} {t['dogrulanmis']:11d} {t['eslesen']:8d} {str(t['kesinlik']):>9s} {str(t['duyarlilik']):>10s}")
    print(f"Rapor: {Path(args.calistirma) / 'karsilastirma.json'}")


if __name__ == '__main__':
    main()
