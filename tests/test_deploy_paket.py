# -*- coding: utf-8 -*-
"""Tests für den Weg, auf dem Inhalt zu einem Deploy kommt.

`instance/` steht in .gitignore: ein frischer Deploy auf Render hat die zwanzig Städte aus
dem Seed und sonst nichts. tools/deploy_paket.py packt den vorhandenen Bestand ein,
app._paket_einspielen() übernimmt ihn beim Start.

Zwei Stellen können hier still Daten vernichten, und beide sehen harmlos aus:

* Eine einfache Dateikopie der Datenbank. Im WAL-Modus steht das zuletzt Geschriebene noch
  in memeos.db-wal; eine Kopie allein ist ein Stand von vorgestern.
* Das Einspielen, ohne das -wal der ALTEN Datenbank wegzuräumen. SQLite prüft nicht, ob ein
  -wal zur Datei gehört, und spielt es auf die neue Datenbank.

Beides wird hier scharf geprüft.

    python3 -m pytest tests/test_deploy_paket.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest

WURZEL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WURZEL)
sys.path.insert(0, os.path.join(WURZEL, 'tools'))

# Keine Schlüssel, kein Netz, keine Hintergrunddienste. Den Datenpfad setzt dieses Modul
# bewusst NICHT (siehe tests/test_aufraeumen.py) – `app` wird erst in setUpClass geholt.
os.environ.update({k: '' for k in (
    'ANTHROPIC_API_KEY', 'CLOUDINARY_URL', 'CANVA_CLIENT_ID', 'CANVA_CLIENT_SECRET',
    'CANVA_REFRESH_TOKEN', 'CONTENT_OS_URL', 'CONTENT_OS_KEY', 'RAPIDAPI_KEY',
    'DEEPSEEK_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_TOKEN')})
os.environ.setdefault('MEMEOS_SCHEDULER', '0')
os.environ.setdefault('MEMEOS_WORKERS', '0')
os.environ.setdefault('MEMEOS_OFFLINE', '1')
os.environ.setdefault('DATABASE_URL', 'sqlite://')


def _db_mit_marke(pfad, wert, offenes_wal=False):
    """Eine kleine Datenbank im WAL-Modus.

    Mit offenes_wal=True bleibt der Schreibvorgang im -wal stehen, statt in die Hauptdatei
    eingearbeitet zu werden – genau der Zustand, in dem eine Dateikopie zu wenig mitnimmt.
    Dafür braucht es zweierlei: wal_autocheckpoint aus, und die Verbindung muss offen
    bleiben, denn die letzte sich schliessende Verbindung arbeitet das -wal noch ein.
    Die Verbindung wird dann zurückgegeben; der Aufrufer hält sie bis zum Testende.
    """
    c = sqlite3.connect(pfad)
    c.execute('pragma journal_mode=wal')
    if offenes_wal:
        c.execute('pragma wal_autocheckpoint=0')
    c.execute('create table if not exists marke (wert text)')
    c.execute('delete from marke')
    c.execute('insert into marke values (?)', (wert,))
    c.commit()
    if offenes_wal:
        return c
    c.execute('pragma wal_checkpoint(truncate)')
    c.commit()
    c.close()
    return pfad


def _marke_lesen(pfad):
    c = sqlite3.connect(pfad)
    try:
        return c.execute('select wert from marke').fetchone()[0]
    finally:
        c.close()


class TestPacken(unittest.TestCase):
    """tools/deploy_paket.py – was eingepackt wird und in welcher Form."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='paket_quelle_')
        self.addCleanup(__import__('shutil').rmtree, self.tmp, True)
        for name in ('uploads', 'exports', 'renders', 'nachbau_cache'):
            os.makedirs(os.path.join(self.tmp, name), exist_ok=True)

    def _packen(self, **kw):
        import deploy_paket as DP
        altes_root = os.environ.get('MEMEOS_DATA_ROOT')
        os.environ['MEMEOS_DATA_ROOT'] = self.tmp
        try:
            return DP.packen(ziel=os.path.join(self.tmp, 'p.tar.gz'), **kw)
        finally:
            if altes_root is None:
                os.environ.pop('MEMEOS_DATA_ROOT', None)
            else:
                os.environ['MEMEOS_DATA_ROOT'] = altes_root

    def test_datenbank_wird_wal_sicher_gesichert(self):
        """Der schärfste Test: der letzte Schreibvorgang steht nur im -wal.

        Eine Kopie von memeos.db allein brächte hier 'alt' mit. Nur die Backup-
        Schnittstelle von sqlite3 nimmt den Stand aus dem -wal mit."""
        db = os.path.join(self.tmp, 'memeos.db')
        _db_mit_marke(db, 'alt')
        offen = _db_mit_marke(db, 'neu_nur_im_wal', offenes_wal=True)
        self.addCleanup(offen.close)
        self.assertGreater(os.path.getsize(db + '-wal'), 0,
                           'Aufbau misslungen: es liegt gar kein -wal daneben')

        ziel, _dateien, _zettel = self._packen()
        aus = tempfile.mkdtemp(prefix='paket_aus_')
        self.addCleanup(__import__('shutil').rmtree, aus, True)
        with tarfile.open(ziel) as tar:
            tar.extractall(aus)
        self.assertEqual(_marke_lesen(os.path.join(aus, '_paket', 'memeos.db')),
                         'neu_nur_im_wal')

    def test_alles_liegt_unter_paket(self):
        """Das Archiv darf beim Auspacken nichts überschreiben: alles unter _paket/."""
        _db_mit_marke(os.path.join(self.tmp, 'memeos.db'), 'x')
        with open(os.path.join(self.tmp, 'uploads', 'bild.png'), 'wb') as fh:
            fh.write(b'\x89PNG\r\n\x1a\n' + b'0' * 40)
        ziel, dateien, _zettel = self._packen()
        with tarfile.open(ziel) as tar:
            namen = tar.getnames()
        self.assertEqual(dateien, 1)
        self.assertTrue(namen, 'leeres Archiv')
        for n in namen:
            self.assertTrue(n == '_paket' or n.startswith('_paket/'),
                            f'{n} liegt ausserhalb von _paket/ und wuerde beim '
                            f'Auspacken Vorhandenes ueberschreiben')
        self.assertIn('_paket/uploads/bild.png', namen)
        self.assertIn('_paket/memeos.db', namen)

    def test_renders_nur_auf_wunsch(self):
        """Die fertigen Bilder sind der grosse Brocken – nur mit --mit-renders."""
        _db_mit_marke(os.path.join(self.tmp, 'memeos.db'), 'x')
        with open(os.path.join(self.tmp, 'renders', 'r.png'), 'wb') as fh:
            fh.write(b'\x89PNG' + b'0' * 20)
        with open(os.path.join(self.tmp, 'uploads', 'u.png'), 'wb') as fh:
            fh.write(b'\x89PNG' + b'0' * 20)

        ziel, _d, _z = self._packen()
        with tarfile.open(ziel) as tar:
            ohne = tar.getnames()
        self.assertNotIn('_paket/renders/r.png', ohne)

        ziel2, _d2, _z2 = self._packen(mit_renders=True)
        with tarfile.open(ziel2) as tar:
            mit = tar.getnames()
        self.assertIn('_paket/renders/r.png', mit)

    def test_beipackzettel_zaehlt_den_inhalt(self):
        """Der Zettel ist die Gegenprobe nach dem Einspielen – er muss stimmen."""
        db = os.path.join(self.tmp, 'memeos.db')
        c = sqlite3.connect(db)
        c.execute('pragma journal_mode=wal')
        c.execute('create table meme_template (id integer primary key, name text)')
        c.executemany('insert into meme_template (name) values (?)',
                      [('a',), ('b',), ('c',)])
        c.commit()
        c.close()
        _ziel, _dateien, zettel = self._packen()
        self.assertEqual(zettel['inhalt']['meme_template'], 3)


class TestEinspielen(unittest.TestCase):
    """app._paket_einspielen() – der Tausch beim Start."""

    @classmethod
    def setUpClass(cls):
        import app as A
        cls.A = A

    def setUp(self):
        import shutil
        self.tmp = tempfile.mkdtemp(prefix='paket_platte_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paket = os.path.join(self.tmp, '_paket')
        os.makedirs(os.path.join(self.paket, 'uploads'))
        os.makedirs(os.path.join(self.tmp, 'uploads'))
        self.db = os.path.join(self.tmp, 'memeos.db')

        A = self.A
        self._alt = (A._DATA_ROOT, A._DB_PATH, A._PAKET_DIR)
        A._DATA_ROOT, A._DB_PATH, A._PAKET_DIR = self.tmp, self.db, self.paket
        self.addCleanup(self._zuruecksetzen)

    def _zuruecksetzen(self):
        self.A._DATA_ROOT, self.A._DB_PATH, self.A._PAKET_DIR = self._alt

    def _erledigt_ordner(self):
        return [n for n in os.listdir(self.tmp) if n.startswith('_paket_erledigt_')]

    def _beiseite(self):
        return sorted(n for n in os.listdir(self.tmp)
                      if n.startswith('memeos.db.vor_paket_') and not n.endswith(('-wal', '-shm')))

    def test_datenbank_und_dateien_kommen_an(self):
        _db_mit_marke(self.db, 'bestand_vor_ort')
        _db_mit_marke(os.path.join(self.paket, 'memeos.db'), 'aus_dem_paket')
        with open(os.path.join(self.paket, 'uploads', 'hintergrund.png'), 'wb') as fh:
            fh.write(b'\x89PNG' + b'1' * 30)

        self.A._paket_einspielen()

        self.assertEqual(_marke_lesen(self.db), 'aus_dem_paket')
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, 'uploads', 'hintergrund.png')),
                        'die Bilder aus dem Paket sind nicht angekommen')
        self.assertEqual(len(self._erledigt_ordner()), 1,
                         '_paket wurde nicht als erledigt weggeräumt')

    def test_kein_fremdes_wal_neben_der_neuen_datenbank(self):
        """Der Fall, der still eine Datenbank zerlegt.

        Die alte Datenbank hat ein -wal mit Inhalt. Bleibt es liegen, spielt SQLite es
        beim nächsten Start auf die neue Datei – sie prüft nicht, ob es dazugehört."""
        _db_mit_marke(self.db, 'alter_bestand')
        offen = _db_mit_marke(self.db, 'alter_bestand_im_wal', offenes_wal=True)
        self.addCleanup(offen.close)
        self.assertGreater(os.path.getsize(self.db + '-wal'), 0, 'Aufbau misslungen')
        _db_mit_marke(os.path.join(self.paket, 'memeos.db'), 'aus_dem_paket')

        self.A._paket_einspielen()

        self.assertFalse(os.path.exists(self.db + '-wal'),
                         'neben der neuen Datenbank liegt noch das -wal der alten')
        self.assertFalse(os.path.exists(self.db + '-shm'),
                         'neben der neuen Datenbank liegt noch das -shm der alten')
        c = sqlite3.connect(self.db)
        try:
            self.assertEqual(c.execute('pragma integrity_check').fetchone()[0], 'ok')
        finally:
            c.close()
        self.assertEqual(_marke_lesen(self.db), 'aus_dem_paket')

    def test_alte_datenbank_bleibt_vollstaendig_erhalten(self):
        """Nichts wird gelöscht: die vorherige Datenbank muss samt ihrem -wal
        beiseiteliegen und für sich allein lesbar sein."""
        _db_mit_marke(self.db, 'wertvoll')
        offen = _db_mit_marke(self.db, 'wertvoll_nur_im_wal', offenes_wal=True)
        self.addCleanup(offen.close)
        _db_mit_marke(os.path.join(self.paket, 'memeos.db'), 'aus_dem_paket')

        self.A._paket_einspielen()

        beiseite = self._beiseite()
        self.assertEqual(len(beiseite), 1, f'keine Sicherung der alten Datenbank: {beiseite}')
        alt = os.path.join(self.tmp, beiseite[0])
        self.assertTrue(os.path.exists(alt + '-wal'),
                        'die alte Datenbank wurde von ihrem -wal getrennt und ist '
                        'damit auf dem Stand von vor dem letzten Schreibvorgang')
        self.assertEqual(_marke_lesen(alt), 'wertvoll_nur_im_wal')

    def test_zweiter_start_spielt_nicht_erneut_ein(self):
        """Nach dem Einspielen darf ein Neustart die Arbeit nicht zurückdrehen."""
        _db_mit_marke(self.db, 'egal')
        _db_mit_marke(os.path.join(self.paket, 'memeos.db'), 'aus_dem_paket')
        self.A._paket_einspielen()

        # Inzwischen wurde gearbeitet.
        _db_mit_marke(self.db, 'inzwischen_gearbeitet')
        vorher = self._beiseite()

        # Der Paketordner wird wieder an seinen Platz gelegt – so, wie es aussähe,
        # wenn die Umbenennung beim ersten Mal nicht geklappt hätte.
        erledigt = os.path.join(self.tmp, self._erledigt_ordner()[0])
        os.rename(erledigt, self.paket)

        self.A._paket_einspielen()

        self.assertEqual(_marke_lesen(self.db), 'inzwischen_gearbeitet',
                         'das Paket wurde ein zweites Mal eingespielt und hat die '
                         'Arbeit seither überschrieben')
        self.assertEqual(self._beiseite(), vorher)

    def test_ohne_paket_passiert_nichts(self):
        import shutil
        shutil.rmtree(self.paket)
        _db_mit_marke(self.db, 'unberuehrt')
        self.A._paket_einspielen()
        self.assertEqual(_marke_lesen(self.db), 'unberuehrt')
        self.assertEqual(self._beiseite(), [])

    def test_paket_ohne_datenbank_laesst_die_datenbank_in_ruhe(self):
        """Ein Paket darf auch nur Bilder nachliefern."""
        _db_mit_marke(self.db, 'unberuehrt')
        with open(os.path.join(self.paket, 'uploads', 'neu.png'), 'wb') as fh:
            fh.write(b'\x89PNG' + b'2' * 30)

        self.A._paket_einspielen()

        self.assertEqual(_marke_lesen(self.db), 'unberuehrt')
        self.assertEqual(self._beiseite(), [],
                         'ohne Datenbank im Paket darf nichts beiseitegelegt werden')
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, 'uploads', 'neu.png')))


class TestBeimStart(unittest.TestCase):
    """Die Stelle im Startvorgang: eingespielt wird VOR der ersten Datenbankverbindung.

    Das lässt sich nur an einem echten Start zeigen, deshalb ein eigener Prozess."""

    def test_import_der_anwendung_spielt_das_paket_ein(self):
        import shutil
        tmp = tempfile.mkdtemp(prefix='paket_start_')
        self.addCleanup(shutil.rmtree, tmp, True)
        paket = os.path.join(tmp, '_paket')
        os.makedirs(os.path.join(paket, 'uploads'))

        # Eine Datenbank mit einer Tabelle, die die Anwendung nicht kennt und deshalb
        # auch nicht anlegen würde: taucht sie nach dem Start auf, kam sie aus dem Paket.
        _db_mit_marke(os.path.join(paket, 'memeos.db'), 'aus_dem_paket')
        with open(os.path.join(paket, 'uploads', 'aus_paket.png'), 'wb') as fh:
            fh.write(b'\x89PNG' + b'3' * 30)

        umgebung = dict(os.environ)
        umgebung.update({k: '' for k in (
            'ANTHROPIC_API_KEY', 'CLOUDINARY_URL', 'CANVA_CLIENT_ID', 'CANVA_CLIENT_SECRET',
            'CANVA_REFRESH_TOKEN', 'CONTENT_OS_URL', 'CONTENT_OS_KEY', 'RAPIDAPI_KEY',
            'DEEPSEEK_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_TOKEN')})
        umgebung.update({'MEMEOS_DATA_ROOT': tmp, 'MEMEOS_SCHEDULER': '0',
                         'MEMEOS_WORKERS': '0', 'MEMEOS_OFFLINE': '1'})
        umgebung.pop('DATABASE_URL', None)   # die Datei auf der Platte soll es sein

        ergebnis = subprocess.run(
            [sys.executable, '-c', 'import app; print("START_OK")'],
            cwd=WURZEL, env=umgebung, capture_output=True, text=True, timeout=180)
        self.assertIn('START_OK', ergebnis.stdout,
                      f'Start fehlgeschlagen:\n{ergebnis.stdout}\n{ergebnis.stderr}')

        db = os.path.join(tmp, 'memeos.db')
        self.assertEqual(_marke_lesen(db), 'aus_dem_paket',
                         'die Anwendung hat ihre eigene Datenbank angelegt, statt die '
                         'aus dem Paket zu übernehmen – eingespielt wird zu spät')
        self.assertTrue(os.path.isfile(os.path.join(tmp, 'uploads', 'aus_paket.png')))
        self.assertTrue([n for n in os.listdir(tmp) if n.startswith('_paket_erledigt_')])


if __name__ == '__main__':
    unittest.main()
