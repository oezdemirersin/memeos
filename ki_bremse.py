# -*- coding: utf-8 -*-
"""Kostenbremsen für KI-Aufrufe – mehrere Grenzen, die unabhängig voneinander greifen.

Warum mehrere? Ein Monatsbudget schützt vor dem langsamen Auflaufen, aber nicht vor einem
Fehler: eine Schleife, die in zehn Minuten hundert Aufrufe macht, hat das Monatsbudget
verbraucht, bevor jemand hinsieht. Darum vier Grenzen, von denen jede allein stoppt:

    1. Notaus          ein Schalter, der sofort alles anhält (`ki_aus`)
    2. Monatsbudget    Euro im laufenden Kalendermonat  (`ai_budget_month_eur`)
    3. Tagesbudget     Euro seit Mitternacht            (`ki_budget_tag_eur`)
    4. Stundenzähler   Anzahl Aufrufe in den letzten 60 Minuten (`ki_aufrufe_pro_stunde`)

Die ersten beiden gab es schon, die unteren beiden sind die eigentliche Sicherung gegen
einen Fehler. Alle Werte stehen in AppSettings und sind im Bedienteil einstellbar.

**Eine Grenze, die standardmäßig aus ist, ist keine Grenze.** Deshalb gilt hier: fehlt eine
Einstellung, greift die Vorgabe aus diesem Modul – nicht „unbegrenzt". Unbegrenzt gibt es
nur, wenn jemand den Wert ausdrücklich auf 0 setzt, und das sagt die Auskunft dann auch.

Benutzung an jeder Aufrufstelle:

    sperre = ki_bremse.pruefen('trending_auto')
    if sperre:
        return '', sperre          # fertiger deutscher Satz für den Aufrufer

    ... Aufruf ...
    ki_bremse.aufruf_buchen('trending_auto')

`aufruf_buchen` zählt nur die Aufrufe; die Euro kommen aus dem vorhandenen AiUsageLog.
"""

import logging
import threading
from collections import deque
from datetime import datetime, timedelta

log = logging.getLogger('ki_bremse')

# ── Vorgaben: greifen, solange niemand etwas anderes einstellt ────────────────
# Bewusst niedrig. Wer mehr braucht, hebt sie bewusst an – das ist der richtige Weg herum.
VORGABE_MONAT_EUR = 10.0
VORGABE_TAG_EUR = 2.0
VORGABE_STUNDE_AUFRUFE = 120

# Obergrenzen für die Einstellungen selbst: ein Tippfehler („1000" statt „10") soll nicht
# versehentlich alle Bremsen aushebeln.
MAX_MONAT_EUR = 500.0
MAX_TAG_EUR = 200.0
MAX_STUNDE_AUFRUFE = 5000

SCHLUESSEL = {
    'notaus': 'ki_aus',
    'monat': 'ai_budget_month_eur',      # Name aus dem Bestand, nicht umbenennen
    'tag': 'ki_budget_tag_eur',
    'stunde': 'ki_aufrufe_pro_stunde',
}

# Aufrufe der letzten Stunde, im Arbeitsspeicher. Nach einem Neustart leer – das ist
# gewollt: der Stundenzähler schützt vor einer laufenden Schleife, nicht vor Altlasten.
_aufrufe = deque()
_sperre = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
# Einstellungen lesen
# ═══════════════════════════════════════════════════════════════════════════════

def _einstellung(name, vorgabe):
    """Rohwert aus AppSettings oder None, wenn es die Tabelle (noch) nicht gibt."""
    try:
        from models import AppSettings
        roh = AppSettings.get(name, None)
    except Exception as ex:                          # pragma: no cover – frühe Startphase
        log.debug('ki_bremse: %s nicht lesbar (%s)', name, ex)
        return None
    if roh is None or str(roh).strip() == '':
        return None
    return str(roh).strip()


def _zahl(name, vorgabe, obergrenze):
    """Zahl aus den Einstellungen. Fehlt sie, gilt die Vorgabe – NICHT „unbegrenzt".

    Eine ausdrückliche 0 heißt „keine Grenze"; das ist eine bewusste Entscheidung und
    taucht in auskunft() als Warnung auf."""
    roh = _einstellung(name, vorgabe)
    if roh is None:
        return float(vorgabe)
    try:
        wert = float(roh.replace(',', '.'))
    except ValueError:
        log.warning('ki_bremse: %s=%r ist keine Zahl – Vorgabe %s gilt', name, roh, vorgabe)
        return float(vorgabe)
    # NaN besteht KEINEN Vergleich: weder 'wert < 0' noch 'verbraucht >= wert' wird je wahr.
    # Ein 'NaN' in den Einstellungen hätte die Grenze lautlos abgeschaltet – genau die Art
    # Fehler, gegen die dieses Modul da ist. Unendlich ebenso.
    if wert != wert or wert in (float('inf'), float('-inf')):
        log.warning('ki_bremse: %s=%r ist keine brauchbare Zahl – Vorgabe %s gilt',
                    name, roh, vorgabe)
        return float(vorgabe)
    if wert < 0:
        return float(vorgabe)
    return min(float(wert), float(obergrenze))


def notaus_an():
    return str(_einstellung(SCHLUESSEL['notaus'], '0') or '0').lower() in ('1', 'true', 'ja', 'an', 'on')


def grenzen():
    """Die vier Grenzen als Dict – so, wie sie gerade gelten."""
    return {
        'notaus': notaus_an(),
        'monat_eur': _zahl(SCHLUESSEL['monat'], VORGABE_MONAT_EUR, MAX_MONAT_EUR),
        'tag_eur': _zahl(SCHLUESSEL['tag'], VORGABE_TAG_EUR, MAX_TAG_EUR),
        'stunde_aufrufe': int(_zahl(SCHLUESSEL['stunde'], VORGABE_STUNDE_AUFRUFE,
                                    MAX_STUNDE_AUFRUFE)),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Verbrauch messen
# ═══════════════════════════════════════════════════════════════════════════════

def _kosten_seit(zeitpunkt):
    """Summe der gebuchten Kosten seit `zeitpunkt` in EUR. 0.0, wenn nichts lesbar ist."""
    try:
        from models import db, AiUsageLog
        wert = (db.session.query(db.func.coalesce(db.func.sum(AiUsageLog.cost_eur), 0.0))
                .filter(AiUsageLog.created_at >= zeitpunkt).scalar())
        return float(wert or 0.0)
    except Exception as ex:
        # Lieber durchlassen als die ganze Anwendung an einer Abfrage aufhängen – die
        # anderen Grenzen greifen weiterhin, und der Fehler steht im Protokoll.
        log.warning('ki_bremse: Kosten nicht lesbar: %s', ex)
        try:
            from models import db
            db.session.rollback()
        except Exception:
            pass
        return 0.0


def kosten_monat():
    jetzt = datetime.utcnow()
    return _kosten_seit(jetzt.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def kosten_tag():
    jetzt = datetime.utcnow()
    return _kosten_seit(jetzt.replace(hour=0, minute=0, second=0, microsecond=0))


def aufrufe_letzte_stunde():
    grenze = datetime.utcnow() - timedelta(hours=1)
    with _sperre:
        while _aufrufe and _aufrufe[0] < grenze:
            _aufrufe.popleft()
        return len(_aufrufe)


def aufruf_buchen(feature=''):
    """Einen Aufruf im Stundenzähler vermerken. Direkt NACH dem Aufruf rufen."""
    with _sperre:
        _aufrufe.append(datetime.utcnow())
    return len(_aufrufe)


def zaehler_zuruecksetzen():
    """Nur für Tests und für den Notaus-Knopf."""
    with _sperre:
        _aufrufe.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# Reservierung: Prüfen und Platz nehmen in einem Zug
# ═══════════════════════════════════════════════════════════════════════════════
# Ohne das lassen acht gleichzeitige Worker bei 4,99 von 5,00 EUR alle acht durch: jeder
# prüft, bevor einer gebucht hat. Gemessen wurden dabei 8,82 EUR statt 5,00.
#
# Darum: unter EINER Sperre prüfen und sofort eine Schätzung vormerken. Nach dem Aufruf
# wird die Vormerkung durch den tatsächlichen Verbrauch ersetzt. Die Vormerkungen liegen
# im Arbeitsspeicher – sie müssen nur so lange halten, wie ein Aufruf dauert.

_vormerkungen = {}          # id -> (eur, zeitpunkt)
_vormerk_id = [0]
# Hängt ein Aufruf länger als das, gilt seine Vormerkung als verfallen; sonst bliebe nach
# einem Absturz Geld für immer reserviert.
VORMERKUNG_GILT_SEKUNDEN = 300


def _vormerkungen_summe():
    grenze = datetime.utcnow() - timedelta(seconds=VORMERKUNG_GILT_SEKUNDEN)
    verfallen = [k for k, (_, wann) in _vormerkungen.items() if wann < grenze]
    for k in verfallen:
        log.warning('ki_bremse: Vormerkung %s verfallen – wurde nie abgerechnet', k)
        _vormerkungen.pop(k, None)
    return sum(eur for eur, _ in _vormerkungen.values())


def platz_nehmen(feature='', geschaetzt_eur=0.0):
    """(None, marke) wenn der Aufruf laufen darf – sonst (grund, None).

    `marke` muss anschließend IMMER an abrechnen() zurückgegeben werden, auch wenn der
    Aufruf scheitert. Sonst bleibt der Betrag reserviert, bis er verfällt."""
    with _sperre:
        grund = _pruefen_ohne_sperre(feature, zusaetzlich_eur=_vormerkungen_summe())
        if grund:
            return grund, None
        _vormerk_id[0] += 1
        marke = _vormerk_id[0]
        _vormerkungen[marke] = (max(0.0, float(geschaetzt_eur or 0.0)), datetime.utcnow())
        _aufrufe.append(datetime.utcnow())
        return None, marke


def abrechnen(marke):
    """Eine Vormerkung auflösen. Der echte Verbrauch steht da längst im AiUsageLog."""
    if marke is None:
        return
    with _sperre:
        _vormerkungen.pop(marke, None)


def vormerkungen_zuruecksetzen():
    """Nur für Tests."""
    with _sperre:
        _vormerkungen.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# Die Prüfung
# ═══════════════════════════════════════════════════════════════════════════════

def _pruefen_ohne_sperre(feature='', zusaetzlich_eur=0.0):
    """Wie pruefen(), rechnet aber bereits vorgemerkte Beträge mit ein.
    Erwartet, dass der Aufrufer die Sperre hält (oder sie nicht braucht)."""
    g = grenzen()
    if g['notaus']:
        return ('KI-Notaus ist gesetzt – es wird kein einziger Aufruf gemacht. '
                'Im Bedienteil unter Einstellungen wieder freigeben.')

    if g['stunde_aufrufe'] > 0:
        grenze = datetime.utcnow() - timedelta(hours=1)
        while _aufrufe and _aufrufe[0] < grenze:
            _aufrufe.popleft()
        bisher = len(_aufrufe)
        if bisher >= g['stunde_aufrufe']:
            return (f'Stundengrenze erreicht: {bisher} KI-Aufrufe in den letzten 60 Minuten '
                    f'(erlaubt sind {g["stunde_aufrufe"]}). Das ist die Sicherung gegen eine '
                    f'Schleife – sie löst sich von selbst, sobald die Stunde weiterrückt.')

    extra = max(0.0, float(zusaetzlich_eur or 0.0))
    if g['tag_eur'] > 0:
        heute = kosten_tag() + extra
        if heute >= g['tag_eur']:
            return (f'Tagesbudget erreicht: {heute:.2f} von {g["tag_eur"]:.2f} EUR seit '
                    f'Mitternacht. Morgen geht es weiter, oder das Tagesbudget anheben.')

    if g['monat_eur'] > 0:
        monat = kosten_monat() + extra
        if monat >= g['monat_eur']:
            return (f'Monatsbudget erreicht: {monat:.2f} von {g["monat_eur"]:.2f} EUR in '
                    f'diesem Monat.')
    return None


def pruefen(feature=''):
    """None, wenn ein Aufruf erlaubt ist – sonst ein fertiger deutscher Satz.

    Für den einfachen Fall ohne Reservierung. Wer mehrere Aufrufe gleichzeitig macht,
    nimmt platz_nehmen()/abrechnen(), sonst laufen alle gemeinsam über die Grenze."""
    with _sperre:
        return _pruefen_ohne_sperre(feature, zusaetzlich_eur=_vormerkungen_summe())


def auskunft():
    """Stand aller Bremsen – für das Bedienteil und den Selbsttest."""
    g = grenzen()
    monat, tag = kosten_monat(), kosten_tag()
    stunde = aufrufe_letzte_stunde()
    offen = []
    if g['notaus']:
        offen.append('Notaus ist gesetzt: es läuft kein KI-Aufruf.')
    for name, wert, text in (('monat_eur', g['monat_eur'], 'Monatsbudget'),
                             ('tag_eur', g['tag_eur'], 'Tagesbudget'),
                             ('stunde_aufrufe', g['stunde_aufrufe'], 'Stundengrenze')):
        if wert <= 0:
            offen.append(f'{text} ist ausdrücklich auf 0 gesetzt – diese Grenze greift NICHT.')
    return {
        'notaus': g['notaus'],
        'monat': {'grenze_eur': g['monat_eur'], 'verbraucht_eur': round(monat, 4),
                  'rest_eur': round(max(0.0, g['monat_eur'] - monat), 4) if g['monat_eur'] else None},
        'tag': {'grenze_eur': g['tag_eur'], 'verbraucht_eur': round(tag, 4),
                'rest_eur': round(max(0.0, g['tag_eur'] - tag), 4) if g['tag_eur'] else None},
        'stunde': {'grenze': g['stunde_aufrufe'], 'bisher': stunde,
                   'rest': max(0, g['stunde_aufrufe'] - stunde) if g['stunde_aufrufe'] else None},
        'gesperrt': pruefen() is not None,
        'grund': pruefen(),
        'warnungen': offen,
        'vorgaben': {'monat_eur': VORGABE_MONAT_EUR, 'tag_eur': VORGABE_TAG_EUR,
                     'stunde_aufrufe': VORGABE_STUNDE_AUFRUFE},
    }


def schaetzung(anzahl_aufrufe, eingabe_tokens=2000, ausgabe_tokens=600, modell=None):
    """Was ein geplanter Stapel ungefähr kostet – damit man VORHER fragen kann.

    Gerechnet wird mit app._ai_cost(), also mit genau derselben Funktion, die hinterher
    auch bucht. Eine Schätzung, die anders rechnet als die Abrechnung, ist keine Schätzung,
    sondern eine zweite Wahrheit: die erste Fassung hier multiplizierte rohe Tokenzahlen
    mit einem Preis je TAUSEND Token und griff dazu an der Vorsilbensuche vorbei – 460 EUR
    statt 0,09 EUR, Faktor 5000."""
    try:
        from app import _ai_cost, AI_MODEL_ANTHROPIC
    except Exception as ex:                          # pragma: no cover – defensiv
        log.warning('ki_bremse: Preise nicht lesbar: %s', ex)
        return None
    n = max(0, int(anzahl_aufrufe or 0))
    je_aufruf = _ai_cost(modell or AI_MODEL_ANTHROPIC,
                         max(0, int(eingabe_tokens or 0)),
                         max(0, int(ausgabe_tokens or 0)))
    return {'aufrufe': n, 'modell': modell or AI_MODEL_ANTHROPIC,
            'kosten_eur': round(n * je_aufruf, 4),
            'je_aufruf_eur': round(je_aufruf, 6)}
