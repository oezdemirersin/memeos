# -*- coding: utf-8 -*-
"""
selftest_bp – Selbsttest für MemeOS (Phase B, Modul B8).

Routen (Login nötig, JSON):
    GET /api/selftest         → alle Checks
    GET /api/selftest/quick   → nur Datenpfad, Konfiguration, Hintergrund (für die Kopfzeile)

Antwort:
    {'ok': bool,                      # False, sobald ein Check mit severity 'crit' fehlschlägt
     'quick': bool,
     'checks': [{'id', 'name', 'ok', 'detail', 'severity': 'crit'|'warn'|'info'}],
     'summary': {'passed', 'crit', 'warn', 'info', 'total'},
     'duration_ms': int,
     'generated_at': ISO-Zeit}

Regeln: kein app-Import auf Modulebene (zirkulär), keine externen Aufrufe, keine Schreibzugriffe
außer einer Testdatei je Datenordner (wird sofort wieder gelöscht). Andere Phase-B-Module
(scheduler, render_queue, memeos_render) sind optional – ihr Fehlen wird als 'info' gemeldet.
"""
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime
from functools import wraps

from flask import Blueprint, current_app, has_request_context, jsonify, redirect, request, session

from models import (db, User, City, CityKnowledge, MemeTemplate, RenderJob,
                    MemePost, MemeEvent, AppSettings)

bp = Blueprint('selftest', __name__)
log = logging.getLogger('selftest')

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Schlüsselwörter in Pfaden, die der Routen-Check NICHT aufruft (Seiteneffekte oder externe Dienste)
ROUTE_EXCLUDE = (
    'download', 'export', 'logout', 'login', 'canva/connect', 'canva/callback', 'disconnect',
    'trending/refresh', 'news/fetch', 'refresh', 'fetch', 'backup', 'selftest',
    '/renders/', '/uploads/', 'send', 'notify', 'migrate', 'sync', 'publish', 'webhook',
    'instagram', 'generate', 'scan', 'import',
    'weather',   # /api/automation/weather/<id>/preview ruft Open-Meteo (extern) und schreibt lat/lon
    'studio',    # /api/studio/<id> lädt bei fehlendem Hintergrund extern nach und schreibt in die DB
)

# URL-Parameter → Beispielobjekt (nur int-Konverter; alles andere wird übersprungen)
_PARAM_SOURCES = {
    'city_id':     lambda: City.query.filter_by(active=True).order_by(City.id).first(),
    'tmpl_id':     lambda: MemeTemplate.query.order_by(MemeTemplate.id).first(),
    'template_id': lambda: MemeTemplate.query.order_by(MemeTemplate.id).first(),
    'post_id':     lambda: MemePost.query.order_by(MemePost.id).first(),
    'job_id':      lambda: RenderJob.query.order_by(RenderJob.id).first(),
    'ev_id':       lambda: MemeEvent.query.order_by(MemeEvent.id).first(),
    'event_id':    lambda: MemeEvent.query.order_by(MemeEvent.id).first(),
}

# SECRET_KEY-Werte, die als „Default“ gelten
_DEFAULT_SECRETS = {
    '', 'dein-geheimer-schluessel', 'dev', 'secret', 'changeme', 'change-me', 'change_me',
    'memeos', 'memeos-secret', 'memeos-dev-secret', 'supersecret', 'geheim', 'password', 'test',
}

_MIGRATION_COLUMNS = [
    ('memo_inspiration_source', 'city_id'),
    ('meme_template', 'preview_url'),
    ('city', 'lat'),
]
_MIGRATION_TABLES = ['export_job', 'render_task']


# ═══════════════════════════════════════════════════════════════════════════════
# Hilfen
# ═══════════════════════════════════════════════════════════════════════════════

def _login_required(f):
    """Wie login_required in app.py, nur ohne app-Import: /api/ → 401 JSON, sonst Redirect."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Nicht angemeldet'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


def _appmod():
    """Das app-Modul, ohne es doppelt zu laden (bei `python app.py` heißt es __main__)."""
    mod = sys.modules.get('app')
    if mod is not None and hasattr(mod, '_DATA_ROOT'):
        return mod
    main = sys.modules.get('__main__')
    if main is not None and hasattr(main, '_DATA_ROOT') and hasattr(main, 'app'):
        return main
    try:
        import app as mod  # noqa: F811  (nur innerhalb der Funktion – kein zirkulärer Import)
        return mod
    except Exception as ex:  # pragma: no cover
        log.warning(f'app-Modul nicht erreichbar: {ex}')
        return None


def _data_root():
    mod = _appmod()
    root = getattr(mod, '_DATA_ROOT', None) if mod else None
    return root or os.getenv('MEMEOS_DATA_ROOT') or os.path.join(_PROJECT_DIR, 'instance')


def _optional_module(name):
    """Projektmodul importieren, wenn es existiert; sonst None (kein Absturz)."""
    if name in sys.modules:
        return sys.modules[name]
    if not os.path.isfile(os.path.join(_PROJECT_DIR, f'{name}.py')):
        return None
    try:
        return __import__(name)
    except Exception as ex:
        log.warning(f'Modul {name} nicht importierbar: {ex}')
        return None


def _env_or_attr(name, default=''):
    mod = _appmod()
    val = getattr(mod, name, None) if mod else None
    if val is None:
        val = os.getenv(name, default)
    return (val or '').strip() if isinstance(val, str) else val


def _setting(key, default=''):
    try:
        return (AppSettings.get(key, default) or default) or ''
    except Exception:
        return default


def _truthy(v):
    return str(v or '').strip().lower() in ('1', 'true', 'ja', 'yes', 'on', 'an')


def _check(cid, name, ok, detail, severity='warn'):
    return {'id': cid, 'name': name, 'ok': bool(ok), 'detail': detail, 'severity': severity}


def _safe(cid, name, fn, severity='crit'):
    """Führt einen Check aus; eine Exception im Check selbst wird zum fehlgeschlagenen Check."""
    try:
        result = fn()
    except Exception as ex:
        log.exception(f'Selbsttest-Check {cid} abgestürzt')
        return [_check(cid, name, False, f'Check abgestürzt: {type(ex).__name__}: {ex}', severity)]
    if isinstance(result, dict):
        return [result]
    return list(result or [])


def _copy_session_into(test_session):
    """Sitzung des aufrufenden Nutzers in den Test-Client übernehmen (mindestens logged_in)."""
    if has_request_context():
        for key in ('logged_in', 'user_id', 'username', 'role', 'csrf_token'):
            if key in session:
                test_session[key] = session[key]
    test_session.setdefault('logged_in', True)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Routen
# ═══════════════════════════════════════════════════════════════════════════════

def check_routes(flask_app):
    examples = {}
    for param, getter in _PARAM_SOURCES.items():
        try:
            obj = getter()
            examples[param] = obj.id if obj is not None else None
        except Exception:
            examples[param] = None

    client = flask_app.test_client()
    with client.session_transaction() as s:
        _copy_session_into(s)

    tested, failed, skipped_effects, skipped_params = 0, [], 0, []
    seen = set()
    for rule in sorted(flask_app.url_map.iter_rules(), key=lambda r: r.rule):
        path = rule.rule
        if 'GET' not in (rule.methods or ()):
            continue
        if not (path == '/' or path.startswith('/api/')):
            continue
        if any(token in path for token in ROUTE_EXCLUDE):
            skipped_effects += 1
            continue
        params = re.findall(r'<(?:([a-z_]+):)?([a-zA-Z_][a-zA-Z0-9_]*)>', path)
        url = path
        usable = True
        for conv, name in params:
            if conv != 'int' or examples.get(name) is None:
                usable = False
                break
            url = url.replace(f'<int:{name}>', str(examples[name]))
        if not usable:
            skipped_params.append(path)
            continue
        if url in seen:
            continue
        seen.add(url)
        tested += 1
        try:
            resp = client.get(url)
            if resp.status_code >= 500:
                failed.append(f'{url} → {resp.status_code}')
        except Exception as ex:
            failed.append(f'{url} → {type(ex).__name__}: {str(ex)[:120]}')

    parts = [f'{tested} GET-Routen geprüft']
    if failed:
        parts.append(f'{len(failed)} fehlgeschlagen: ' + '; '.join(failed[:12])
                     + (' …' if len(failed) > 12 else ''))
    if skipped_effects:
        parts.append(f'{skipped_effects} wegen Seiteneffekten/externen Diensten übersprungen')
    if skipped_params:
        parts.append(f'{len(skipped_params)} ohne Beispieldaten übersprungen')
    return _check('routes', 'Routen', not failed, ', '.join(parts), 'crit')


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Datenpfad
# ═══════════════════════════════════════════════════════════════════════════════

def check_data_root():
    root = _data_root()
    problems = []
    for sub in ('uploads', 'renders', 'exports', 'fonts'):
        folder = os.path.join(root, sub)
        if not os.path.isdir(folder):
            problems.append(f'{sub}: Ordner fehlt')
            continue
        probe = os.path.join(folder, f'.selftest_{uuid.uuid4().hex}.tmp')
        try:
            with open(probe, 'w') as fh:
                fh.write('ok')
            os.remove(probe)
        except Exception as ex:
            problems.append(f'{sub}: nicht beschreibbar ({type(ex).__name__})')
            try:
                if os.path.exists(probe):
                    os.remove(probe)
            except Exception:
                pass
    detail = f'{root}: ' + ('uploads, renders, exports, fonts vorhanden und beschreibbar'
                            if not problems else '; '.join(problems))
    return _check('data_root', 'Datenpfad', not problems, detail, 'crit')


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Templates
# ═══════════════════════════════════════════════════════════════════════════════

def check_templates():
    render_mod = _optional_module('memeos_render')
    upload_dir = os.path.join(_data_root(), 'uploads')
    templates = MemeTemplate.query.filter_by(active=True, render_type='pil').order_by(MemeTemplate.id).all()
    problems, remote_only = [], 0
    for t in templates:
        label = f'#{t.id} {t.name}'
        name = os.path.basename(t.preview_image or '')
        has_local = bool(name) and os.path.isfile(os.path.join(upload_dir, name))
        has_remote = (t.preview_url or '').strip().startswith('http')
        if not has_local and has_remote:
            remote_only += 1
        elif not has_local:
            problems.append(f'{label}: kein Hintergrundbild')
        try:
            cfg = json.loads(t.pil_config or '{}')
            if not isinstance(cfg, dict):
                raise ValueError('kein Objekt')
        except Exception as ex:
            problems.append(f'{label}: pil_config nicht parsebar ({ex})')
            continue
        if render_mod is not None and hasattr(render_mod, 'validate_config'):
            errors = render_mod.validate_config(cfg) or []
            if errors:
                problems.append(f'{label}: ' + '; '.join(str(e) for e in errors[:3]))
    parts = [f'{len(templates)} aktive PIL-Templates']
    if remote_only:
        parts.append(f'{remote_only} nur per preview_url (wird beim Rendern nachgeladen)')
    if render_mod is None:
        parts.append('memeos_render fehlt – Config nur auf JSON geprüft')
    if problems:
        parts.append('Probleme: ' + '; '.join(problems[:10]) + (' …' if len(problems) > 10 else ''))
    return _check('templates', 'Templates', not problems, ', '.join(parts), 'warn')


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Schriften
# ═══════════════════════════════════════════════════════════════════════════════

def check_fonts():
    from PIL import ImageFont
    render_mod = _optional_module('memeos_render')
    builtin_dir = getattr(render_mod, 'FONT_BASE_DIR', None) or os.path.join(_PROJECT_DIR, 'fonts')
    user_dir = getattr(render_mod, 'USER_FONT_DIR', None) or os.path.join(_data_root(), 'fonts')
    loaded, broken_builtin, broken_user = 0, [], []
    for folder, bucket in ((builtin_dir, broken_builtin), (user_dir, broken_user)):
        if not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if not fn.lower().endswith(('.ttf', '.otf', '.ttc')):
                continue
            try:
                ImageFont.truetype(os.path.join(folder, fn), 40)
                loaded += 1
            except Exception as ex:
                bucket.append(f'{fn} ({type(ex).__name__})')
    if loaded == 0 and not broken_builtin and not broken_user:
        return _check('fonts', 'Schriften', False, f'Keine Schriftdateien in {builtin_dir}', 'crit')
    parts = [f'{loaded} Schriften ladbar']
    if broken_builtin:
        parts.append('defekt (mitgeliefert): ' + ', '.join(broken_builtin))
    if broken_user:
        parts.append('defekt (eigene): ' + ', '.join(broken_user))
    severity = 'crit' if broken_builtin or loaded == 0 else 'warn'
    return _check('fonts', 'Schriften', not (broken_builtin or broken_user), ', '.join(parts), severity)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Konfiguration
# ═══════════════════════════════════════════════════════════════════════════════

def check_config(flask_app):
    checks = []
    mod = _appmod()

    ai_key = _env_or_attr('ANTHROPIC_API_KEY')
    checks.append(_check('cfg_anthropic', 'Konfiguration: Anthropic-Key', bool(ai_key),
                         'gesetzt' if ai_key else 'ANTHROPIC_API_KEY fehlt – KI-Funktionen aus', 'warn'))

    cloud = bool(os.getenv('CLOUDINARY_URL', '').strip())
    if mod is not None and hasattr(mod, '_cloudinary_connected'):
        try:
            cloud = bool(mod._cloudinary_connected())
        except Exception:
            pass
    checks.append(_check('cfg_cloudinary', 'Konfiguration: Cloudinary', cloud,
                         'verbunden' if cloud else 'CLOUDINARY_URL fehlt – Bilder nur lokal (gehen beim Deploy verloren)',
                         'warn'))

    tg_token = _setting('telegram_token').strip() or os.getenv('TELEGRAM_BOT_TOKEN', '').strip() \
        or os.getenv('TELEGRAM_TOKEN', '').strip()
    tg_chat = _setting('telegram_chat_id').strip() or os.getenv('TELEGRAM_CHAT_ID', '').strip()
    missing = [n for n, v in (('Token', tg_token), ('Chat-ID', tg_chat)) if not v]
    checks.append(_check('cfg_telegram', 'Konfiguration: Telegram', not missing,
                         'Token und Chat-ID gesetzt' if not missing else f'fehlt: {", ".join(missing)} (Einstellungen)',
                         'warn'))

    canva = False
    if mod is not None and hasattr(mod, '_canva_is_connected'):
        try:
            canva = bool(mod._canva_is_connected())
        except Exception:
            canva = False
    checks.append(_check('cfg_canva', 'Konfiguration: Canva', canva,
                         'verbunden' if canva else 'nicht verbunden (nur für Template-Import nötig)', 'info'))

    rapid = os.getenv('RAPIDAPI_KEY', '').strip() or _setting('rapidapi_key').strip()
    checks.append(_check('cfg_rapidapi', 'Konfiguration: RapidAPI', bool(rapid),
                         'gesetzt' if rapid else 'RAPIDAPI_KEY fehlt (nur für Trending/Instagram-Abfragen)', 'info'))

    secret = flask_app.secret_key
    secret_str = secret.decode('utf-8', 'ignore') if isinstance(secret, bytes) else str(secret or '')
    secret_ok = secret_str.strip().lower() not in _DEFAULT_SECRETS and len(secret_str) >= 16
    checks.append(_check('cfg_secret', 'Konfiguration: SECRET_KEY', secret_ok,
                         'individuell gesetzt' if secret_ok else 'Default- oder zu kurzer SECRET_KEY – Sessions unsicher',
                         'crit'))

    admin_pw = _env_or_attr('ADMIN_PASSWORD')
    try:
        db_users = User.query.filter_by(active=True).count()
    except Exception:
        db_users = 0
    login_ok = bool(admin_pw) or db_users > 0
    if login_ok:
        detail = ', '.join(p for p in (
            'ADMIN_PASSWORD gesetzt' if admin_pw else '',
            f'{db_users} aktive DB-Benutzer' if db_users else '') if p)
    else:
        detail = 'weder ADMIN_PASSWORD noch aktiver DB-Benutzer – niemand kann sich anmelden'
    checks.append(_check('cfg_login', 'Konfiguration: Anmeldung', login_ok, detail, 'crit'))
    return checks


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Hintergrund
# ═══════════════════════════════════════════════════════════════════════════════

def _threads_in_module(mod):
    """Thread-Objekte, die ein Modul auf Modulebene hält (direkt, in Listen oder Dicts wie _state)."""
    import threading
    found = []
    for value in list(vars(mod).values()):
        if isinstance(value, threading.Thread):
            found.append(value)
        elif isinstance(value, (list, tuple)):
            found.extend(v for v in value if isinstance(v, threading.Thread))
        elif isinstance(value, dict):
            found.extend(v for v in value.values() if isinstance(v, threading.Thread))
    return found


def _thread_status(mod_name, fn_names, env_flag, label, flask_app):
    cid = f'bg_{mod_name}'
    name = f'Hintergrund: {label}'
    mod = _optional_module(mod_name)
    if mod is None:
        return _check(cid, name, True, 'Modul fehlt (noch nicht eingebaut)', 'info')
    disabled = os.getenv(env_flag, '1').strip() == '0' or bool(flask_app.config.get('TESTING'))
    fn = next((getattr(mod, n) for n in fn_names if callable(getattr(mod, n, None))), None)
    raw = None
    if fn is not None:
        try:
            raw = fn()
        except Exception as ex:
            return _check(cid, name, False, f'{fn.__name__}() abgestürzt: {ex}', 'warn')
        alive = bool(raw)
        source = f'{fn.__name__}()'
    else:
        threads = _threads_in_module(mod)
        if not threads:
            if disabled:
                return _check(cid, name, True, f'nicht gestartet ({env_flag}=0 oder Testmodus)', 'info')
            return _check(cid, name, True,
                          f'Modul vorhanden, aber keine Funktion {"/".join(fn_names)}() und kein Thread-Objekt',
                          'info')
        alive = any(t.is_alive() for t in threads)
        raw = sum(1 for t in threads if t.is_alive())
        source = 'Thread-Objekte'
    if alive:
        detail = f'{raw} Thread(s) aktiv' if isinstance(raw, int) and not isinstance(raw, bool) else 'läuft'
        return _check(cid, name, True, f'{detail} ({source})', 'warn')
    if disabled:
        return _check(cid, name, True, f'nicht gestartet ({env_flag}=0 oder Testmodus)', 'info')
    return _check(cid, name, False, f'läuft nicht ({source}) – Neustart nötig', 'warn')


def check_background(flask_app):
    checks = [
        _thread_status('scheduler', ('scheduler_alive', 'is_alive', 'alive', 'running', '_alive'),
                       'MEMEOS_SCHEDULER', 'Scheduler', flask_app),
        _thread_status('render_queue', ('workers_alive', 'is_alive', 'alive', '_alive_workers'), 'MEMEOS_WORKERS', 'Render-Worker', flask_app),
    ]
    paused = None
    sched = _optional_module('scheduler')
    pause_fn = next((getattr(sched, n) for n in ('is_master_paused', 'is_paused')
                     if sched is not None and callable(getattr(sched, n, None))), None)
    if pause_fn is not None:
        try:
            paused = bool(pause_fn())
        except Exception:
            paused = None
    if paused is None:
        raw = _setting('master_pause') or _setting('master_paused') or _setting('scheduler_paused')
        paused = _truthy(raw)
    checks.append(_check('bg_pause', 'Hintergrund: Master-Pause', True,
                         'AKTIV – automatische Läufe stehen' if paused else 'nicht aktiv', 'info'))
    return checks


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Daten
# ═══════════════════════════════════════════════════════════════════════════════

def check_data():
    checks = []
    cities = City.query.filter_by(active=True).order_by(City.name).all()
    with_knowledge = {row[0] for row in db.session.query(CityKnowledge.city_id)
                      .filter(CityKnowledge.active.is_(True)).distinct().all()}
    without = [c.name for c in cities if c.id not in with_knowledge]
    checks.append(_check('data_knowledge', 'Daten: Städte ohne Stadtwissen', not without,
                         (f'{len(without)} von {len(cities)} Städten: ' + ', '.join(without[:8])
                          + (', …' if len(without) > 8 else '')) if without
                         else f'alle {len(cities)} aktiven Städte haben Einträge', 'warn'))

    no_image = [f'#{t.id} {t.name}' for t in MemeTemplate.query.filter_by(active=True).order_by(MemeTemplate.id).all()
                if not (t.preview_image or '').strip() and not (t.preview_url or '').strip()]
    checks.append(_check('data_templates', 'Daten: Templates ohne Bild', not no_image,
                         (f'{len(no_image)}: ' + ', '.join(no_image[:8])) if no_image else 'alle aktiven Templates haben ein Bild',
                         'warn'))

    now = datetime.utcnow()
    overdue = MemePost.query.filter(MemePost.status == 'geplant',
                                    MemePost.scheduled_at.isnot(None),
                                    MemePost.scheduled_at < now,
                                    MemePost.published_at.is_(None)).order_by(MemePost.scheduled_at).all()
    checks.append(_check('data_overdue', 'Daten: Überfällige geplante Posts', not overdue,
                         (f'{len(overdue)} geplant in der Vergangenheit, nicht veröffentlicht: '
                          + ', '.join(f'#{p.id} ({p.scheduled_at:%d.%m. %H:%M})' for p in overdue[:6])) if overdue
                         else 'keine', 'warn'))

    no_rss = [c.name for c in cities if not (c.rss_url or '').strip()]
    checks.append(_check('data_rss', 'Daten: Städte ohne RSS-Feed', not no_rss,
                         (f'{len(no_rss)} von {len(cities)}: ' + ', '.join(no_rss[:8]) + (', …' if len(no_rss) > 8 else ''))
                         if no_rss else 'alle Städte haben einen Feed', 'info'))
    return checks


# ═══════════════════════════════════════════════════════════════════════════════
# 7b. Studio (nur Registrierung – die Routen selbst werden bewusst nicht aufgerufen)
# ═══════════════════════════════════════════════════════════════════════════════

_STUDIO_ROUTES = ('/studio/<int:template_id>', '/api/studio/<int:template_id>')


def check_studio(flask_app):
    """Ersatz für den Routen-Check: GET /api/studio/<id> würde bei fehlendem lokalen
    Hintergrundbild extern nachladen und in die DB schreiben. Darum prüfen wir nur, dass
    das Blueprint registriert ist und seine Routen in der url_map stehen."""
    if 'studio' not in flask_app.blueprints:
        return _check('studio', 'Studio', False,
                      'Blueprint studio_bp nicht registriert – /studio/<id> ist nicht erreichbar',
                      'warn')
    rules = {r.rule for r in flask_app.url_map.iter_rules()}
    missing = [r for r in _STUDIO_ROUTES if r not in rules]
    if missing:
        return _check('studio', 'Studio', False, 'Route fehlt: ' + ', '.join(missing), 'warn')
    return _check('studio', 'Studio', True,
                  f'Blueprint registriert, {len(_STUDIO_ROUTES)} Kernrouten vorhanden '
                  f'(nicht aufgerufen: Seiteneffekte)', 'info')


# ═══════════════════════════════════════════════════════════════════════════════
# 7c. Speicherplatz
# ═══════════════════════════════════════════════════════════════════════════════

DISK_WARN_PERCENT = 80
DISK_CRIT_PERCENT = 95


def _disk_percent(report):
    """Belegung in Prozent aus dem Bericht von app._disk_report() lesen (tolerant gegenüber
    der genauen Feldbenennung)."""
    if not isinstance(report, dict):
        return None, {}
    for key in ('percent', 'used_percent', 'percent_used', 'usage_percent', 'used_pct'):
        val = report.get(key)
        if isinstance(val, (int, float)):
            return float(val), report
    total = report.get('total') or report.get('total_bytes') or report.get('total_mb')
    used = report.get('used') or report.get('used_bytes') or report.get('used_mb')
    free = report.get('free') or report.get('free_bytes') or report.get('free_mb')
    if isinstance(total, (int, float)) and total > 0:
        if not isinstance(used, (int, float)) and isinstance(free, (int, float)):
            used = total - free
        if isinstance(used, (int, float)):
            return used / float(total) * 100.0, report
    return None, report


def check_disk():
    """Nur wenn app._disk_report() existiert: Warnung ab 80 %, kritisch ab 95 % Belegung."""
    mod = _appmod()
    fn = getattr(mod, '_disk_report', None) if mod else None
    if not callable(fn):
        return []
    report = fn()
    percent, raw = _disk_percent(report)
    detail_extra = raw.get('detail') or raw.get('text') or ''
    if percent is None:
        return [_check('disk', 'Speicherplatz', True,
                       f'Bericht ohne Prozentangabe: {json.dumps(report, default=str)[:200]}',
                       'info')]
    parts = [f'{percent:.0f} % belegt']
    for key, label in (('free_human', 'frei'), ('free_mb', 'frei (MB)'), ('path', 'Pfad')):
        if raw.get(key) not in (None, ''):
            parts.append(f'{label}: {raw[key]}')
    if detail_extra:
        parts.append(str(detail_extra))
    detail = ', '.join(parts)
    if percent >= DISK_CRIT_PERCENT:
        return [_check('disk', 'Speicherplatz', False,
                       f'{detail} – Platte fast voll, Datenbank-Schreibvorgänge scheitern gleich',
                       'crit')]
    if percent >= DISK_WARN_PERCENT:
        return [_check('disk', 'Speicherplatz', False, f'{detail} – aufräumen einplanen', 'warn')]
    return [_check('disk', 'Speicherplatz', True, detail, 'info')]


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Migrationen
# ═══════════════════════════════════════════════════════════════════════════════

def check_migrations():
    from sqlalchemy import inspect as sa_inspect
    insp = sa_inspect(db.engine)
    tables = set(insp.get_table_names())
    missing = []
    for table, column in _MIGRATION_COLUMNS:
        if table not in tables:
            missing.append(f'Tabelle {table}')
            continue
        cols = {c['name'] for c in insp.get_columns(table)}
        if column not in cols:
            missing.append(f'{table}.{column}')
    for table in _MIGRATION_TABLES:
        if table not in tables:
            missing.append(f'Tabelle {table}')
    detail = 'alle erwarteten Spalten/Tabellen vorhanden' if not missing else 'fehlt: ' + ', '.join(missing)
    return _check('migrations', 'Migrationen', not missing, detail, 'warn')


# ═══════════════════════════════════════════════════════════════════════════════
# Lauf
# ═══════════════════════════════════════════════════════════════════════════════

def run_checks(flask_app, quick=False, skip=()):
    """Alle Checks ausführen und als JSON-fähiges Dict zurückgeben."""
    started = time.time()
    plan = [
        ('routes',      'Routen',        lambda: check_routes(flask_app),     'crit', False),
        ('data_root',   'Datenpfad',     check_data_root,                     'crit', True),
        ('templates',   'Templates',     check_templates,                     'warn', False),
        ('fonts',       'Schriften',     check_fonts,                         'crit', False),
        ('config',      'Konfiguration', lambda: check_config(flask_app),     'crit', True),
        ('background',  'Hintergrund',   lambda: check_background(flask_app), 'warn', True),
        ('data',        'Daten',         check_data,                          'warn', False),
        ('studio',      'Studio',        lambda: check_studio(flask_app),     'warn', False),
        ('disk',        'Speicherplatz', check_disk,                          'warn', True),
        ('migrations',  'Migrationen',   check_migrations,                    'warn', False),
    ]
    checks = []
    for cid, name, fn, severity, in_quick in plan:
        if quick and not in_quick:
            continue
        if cid in skip:
            continue
        checks.extend(_safe(cid, name, fn, severity))
    try:
        db.session.rollback()   # nichts aus dem Selbsttest darf hängen bleiben
    except Exception:
        pass
    failed = [c for c in checks if not c['ok']]
    summary = {
        'total':  len(checks),
        'passed': len(checks) - len(failed),
        'crit':   sum(1 for c in failed if c['severity'] == 'crit'),
        'warn':   sum(1 for c in failed if c['severity'] == 'warn'),
        'info':   sum(1 for c in failed if c['severity'] == 'info'),
    }
    return {
        'ok': summary['crit'] == 0,
        'quick': bool(quick),
        'checks': checks,
        'summary': summary,
        'duration_ms': int((time.time() - started) * 1000),
        'generated_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
    }


def _skip_from_request():
    raw = request.args.get('skip', '')
    return tuple(s.strip() for s in raw.split(',') if s.strip())


@bp.route('/api/selftest')
@_login_required
def api_selftest():
    result = run_checks(current_app._get_current_object(), quick=False, skip=_skip_from_request())
    return jsonify(result)


@bp.route('/api/selftest/quick')
@_login_required
def api_selftest_quick():
    result = run_checks(current_app._get_current_object(), quick=True, skip=_skip_from_request())
    return jsonify(result)


def init_app(flask_app):
    """Vom Integrator in register_extensions(app) aufrufen. Startet keine Threads."""
    if bp.name in flask_app.blueprints:
        return
    flask_app.register_blueprint(bp)
    log.info('Selbsttest registriert: GET /api/selftest, GET /api/selftest/quick')
