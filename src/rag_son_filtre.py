"""RAG çıkarım kayıtları için LLM'siz son filtre.

Girdi kayıt listesi (kayitlar.json) okunur; açık olan kurallara takılan kayıtlar atılır.
Çıktı: filtrelenmiş JSON + atılan kayıtlar (sebebiyle). Girdi dosyası değişmez, mevcut çıktıların
üzerine yazılmaz. Veritabanı ve LLM kullanılmaz.

Kurallar (her biri ayrı açılıp kapanabilir; varsayılan yalnızca ingilizce_olmayan açık). oneri ve
degersiz_parca 6 makalede doğrulanmış kayıtları yanlış attığı için varsayılanda kapalıdır:
    ingilizce_olmayan : kanıt (yoksa klinik bilgi) İngilizce değil (is_non_english: Latin dışı harf oranı
                        yüksek ya da belirgin biçimde Fransızca)
    oneri             : baglam = recommendation
    degersiz_parca    : bilgi_turu klinik_sonuc/olcum, klinik_bilgi_en <= 3 kelime ve rakam içermiyor

Kullanım:
    python -m src.rag_son_filtre --girdi kayitlar.json --cikti filtreli.json \
        [--atilan atilan.json] [--kurallar ingilizce_olmayan [oneri] [degersiz_parca]]
"""
import argparse
import json
import re
import unicodedata
from pathlib import Path

DEGERSIZ_TURLER = {'klinik_sonuc', 'olcum'}
DEGERSIZ_AZAMI_KELIME = 3
_RAKAM = re.compile(r'\d')


_FR = {'les', 'des', 'du', 'une', 'dans', 'pour', 'avec', 'est', 'sont', 'qui', 'que', 'sur', 'par',
       'aux', 'été', 'ont', 'cette', 'ces', 'chez', 'entre', 'leur', 'pays', 'élevé'}
_EN = {'the', 'and', 'of', 'in', 'with', 'was', 'were', 'to', 'for', 'is', 'are', 'from', 'by', 'that',
       'this', 'on', 'or', 'had', 'have', 'than'}


def is_non_english(text, *, max_non_latin=0.3):
    """Latin dışı harf oranı yüksek (Arapça vb.) ya da belirgin biçimde Fransızca metin."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    non_latin = sum('LATIN' not in unicodedata.name(c, '') for c in letters)
    if non_latin / len(letters) > max_non_latin:
        return True
    words = re.findall(r"[^\W\d_]+", text.lower())
    fr = sum(w in _FR for w in words)
    en = sum(w in _EN for w in words)
    return fr >= 3 and fr > 2 * en


def ingilizce_olmayan(kayit):
    metin = kayit.get('kanit_cumle') or kayit.get('klinik_bilgi_en') or ''
    return isinstance(metin, str) and is_non_english(metin)


def oneri(kayit):
    return kayit.get('baglam') == 'recommendation'


def degersiz_parca(kayit):
    bilgi = kayit.get('klinik_bilgi_en')
    if kayit.get('bilgi_turu') not in DEGERSIZ_TURLER or not isinstance(bilgi, str):
        return False
    return len(bilgi.split()) <= DEGERSIZ_AZAMI_KELIME and not _RAKAM.search(bilgi)


KURALLAR = {'ingilizce_olmayan': ingilizce_olmayan, 'oneri': oneri, 'degersiz_parca': degersiz_parca}
VARSAYILAN_KURALLAR = ('ingilizce_olmayan',)


def filtrele(kayitlar, kurallar=VARSAYILAN_KURALLAR):
    """(kalan, atilan) döndürür. atilan: {'sira', 'sebep', 'kayit'}; sıra girdideki 1 tabanlı konum.

    Bir kayıt birden çok kurala takılırsa ilk takıldığı kural (kurallar sırasıyla) sebep olarak yazılır."""
    bilinmeyen = [k for k in kurallar if k not in KURALLAR]
    if bilinmeyen:
        raise ValueError(f'Bilinmeyen kural: {", ".join(bilinmeyen)}')
    kalan, atilan = [], []
    for sira, kayit in enumerate(kayitlar, 1):
        sebep = next((k for k in kurallar if isinstance(kayit, dict) and KURALLAR[k](kayit)), None)
        if sebep is None:
            kalan.append(kayit)
        else:
            atilan.append({'sira': sira, 'sebep': sebep, 'kayit': kayit})
    return kalan, atilan


def _yeni_yaz(yol, veri):
    yol.parent.mkdir(parents=True, exist_ok=True)
    with open(yol, 'x', encoding='utf-8') as f:
        f.write(json.dumps(veri, ensure_ascii=False, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--girdi', required=True)
    p.add_argument('--cikti', required=True)
    p.add_argument('--atilan', help='varsayılan: <cikti>.atilan.json')
    p.add_argument('--kurallar', nargs='*', default=list(VARSAYILAN_KURALLAR), choices=list(KURALLAR),
                   help='açık kurallar (varsayılan: ingilizce_olmayan)')
    args = p.parse_args(argv)
    cikti = Path(args.cikti)
    atilan_yol = Path(args.atilan) if args.atilan else cikti.with_suffix('.atilan.json')
    if cikti.exists() or atilan_yol.exists():
        raise FileExistsError(f'Mevcut dosyanın üzerine yazılmadı: {cikti} / {atilan_yol}')
    kayitlar = json.loads(Path(args.girdi).read_text(encoding='utf-8'))
    if not isinstance(kayitlar, list):
        raise ValueError('Girdi kayıt listesi olmalı')
    kalan, atilan = filtrele(kayitlar, args.kurallar)
    _yeni_yaz(cikti, kalan)
    _yeni_yaz(atilan_yol, atilan)
    sayac = {k: sum(a['sebep'] == k for a in atilan) for k in args.kurallar}
    print(f'Girdi: {len(kayitlar)} | kalan: {len(kalan)} | atılan: {len(atilan)} {sayac}')
    print(f'Çıktı: {cikti} | Atılanlar: {atilan_yol}')


if __name__ == '__main__':
    main()
