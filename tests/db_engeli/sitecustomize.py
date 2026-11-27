"""Test çalıştırmaları için veritabanı bağlantı engeli.

Kullanım (veritabanı gerektirmeyen testler):
    PYTHONPATH=tests/db_engeli python -m unittest ...

mysql.connector bu dosya tarafından ÖNCEDEN YÜKLENMEZ: 'modül yüklenmedi' iddiasındaki import testleri (ör.
test_modules_do_not_import_database) yanlış başarısızlık üretmez. Herhangi bir kod mysql.connector'ı (ya da bağlantı alt
modüllerini) yüklediği anda bağlantı kurucuları engellenir; ağ bağlantısı denenmeden RuntimeError verir. PYTHONPATH
alt süreçlere aktarıldığı için engel alt süreçlerde de geçerlidir.
"""
import importlib.abc
import sys

MESSAGE = 'Veritabanı bağlantısı bu test çalıştırmasında engellendi (tests/db_engeli)'


class VeritabaniEngeli(RuntimeError):
    pass


def _blocked(*args, **kwargs):
    raise VeritabaniEngeli(MESSAGE)


def _patch_package(module):
    module.connect = _blocked


def _patch_class(name):
    def patch(module):
        cls = getattr(module, name, None)
        if cls is not None:
            cls.__init__ = _blocked
            if hasattr(cls, 'connect'):
                cls.connect = _blocked
    return patch


PATCHES = {
    'mysql.connector': _patch_package,
    'mysql.connector.connection': _patch_class('MySQLConnection'),
    'mysql.connector.connection_cext': _patch_class('CMySQLConnection'),
    'mysql.connector.pooling': lambda m: (setattr(m, 'connect', _blocked) if hasattr(m, 'connect') else None,
                                          _patch_class('MySQLConnectionPool')(m)),
}


class _Finder(importlib.abc.MetaPathFinder):
    """Yalnız PATCHES'teki modüller için: asıl bulucunun spec'ini alır, modül çalıştırıldıktan hemen sonra yamalar."""

    def find_spec(self, name, path, target=None):
        if name not in PATCHES:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, 'find_spec'):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is not None:
                break
        else:
            return None
        loader = spec.loader
        if loader is None or not hasattr(loader, 'exec_module'):
            return spec
        original = loader.exec_module

        def exec_module(module, _original=original, _name=name):
            _original(module)
            PATCHES[_name](module)
        loader.exec_module = exec_module
        return spec


if not any(isinstance(f, _Finder) for f in sys.meta_path):
    sys.meta_path.insert(0, _Finder())
for _name, _patch in PATCHES.items():           # önceden yüklenmişse (beklenmez) yine de yamalanır
    if _name in sys.modules:
        _patch(sys.modules[_name])
