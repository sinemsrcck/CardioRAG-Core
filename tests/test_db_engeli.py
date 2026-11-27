"""tests/db_engeli/sitecustomize.py: veritabanı bağlantısını engeller ama mysql.connector'ı önceden yüklemez.

Her senaryo ayrı bir alt süreçte, PYTHONPATH=tests/db_engeli ile çalışır. Hiçbir senaryo ağa çıkmaz: bağlantı kurucu
çağrısı engel tarafından ağ denemesinden ÖNCE durdurulur (host olarak yerel ve geçersiz bir port verilir).
"""
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'tests' / 'db_engeli'


def run_guarded(code):
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join([str(GUARD), str(ROOT)])
    return subprocess.run([sys.executable, '-c', code], cwd=str(ROOT), env=env, capture_output=True, text=True,
                          timeout=120)


@unittest.skipUnless(importlib.util.find_spec('mysql') and importlib.util.find_spec('mysql.connector'),
                     'mysql.connector kurulu değil')
class DatabaseGuardTests(unittest.TestCase):
    def test_guard_does_not_preload_mysql(self):
        result = run_guarded('import sys; print("mysql.connector" in sys.modules)')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'False')

    def test_import_check_passes_under_guard(self):
        # test_rag_klinik_cikarim.NoDatabaseTests ile aynı iddia, engel AÇIKKEN.
        code = ('import sys, src.rag_klinik_cikarim, src.rag_klinik_karsilastir, src.rag_anlam_denetimi; '
                'sys.exit(1 if "src.database" in sys.modules or "mysql.connector" in sys.modules else 0)')
        result = run_guarded(code)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_connect_blocked_after_import(self):
        for code in ('import mysql.connector as m; m.connect(host="127.0.0.1", port=1, connection_timeout=1)',
                     'from mysql.connector import connect; connect(host="127.0.0.1", port=1, connection_timeout=1)',
                     'from mysql.connector.connection import MySQLConnection; MySQLConnection(host="127.0.0.1", port=1)'):
            with self.subTest(code=code):
                result = run_guarded(code)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('VeritabaniEngeli', result.stderr)
                self.assertIn('engellendi', result.stderr)

    def test_guard_applies_in_child_processes(self):
        code = ('import subprocess, sys; r = subprocess.run([sys.executable, "-c", "import mysql.connector as m; '
                'm.connect(host=\'127.0.0.1\', port=1)"], capture_output=True, text=True); '
                'sys.exit(0 if "VeritabaniEngeli" in r.stderr else 1)')
        result = run_guarded(code)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
