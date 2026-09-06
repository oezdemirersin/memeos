"""render_queue.py – Persistente Render-Queue → Vorrat (Phase B4).

Ersetzt fachlich den alten Bulk-Generator (ein Thread pro Job ohne Limit): Aufträge liegen als
RenderTask in der Datenbank, N Daemon-Worker holen sie sich mit einem atomaren UPDATE, rendern
und legen das Ergebnis als Entwurf im Vorrat (MemePost) ab. Es gibt keine eigene Review-Insel.

Einbindung (siehe scratchpad/integration/render_queue.md):
    import render_queue
    render_queue.init_app(app)          # in register_extensions(app)

Regeln:
- app.py wird NUR innerhalb von Funktionen importiert (zirkulärer Import).
- Threads starten nur, wenn TESTING falsch ist und MEMEOS_WORKERS nicht '0'.
- Dateien landen unter <MEMEOS_DATA_ROOT>/renders, niemals unter static/.
"""
import os
import json
import time
import uuid
import socket
import inspect
import logging
import secrets
import threading
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, request, jsonify, session, redirect, current_app
from sqlalchemy import func, select, update

from models import db, City, CityKnowledge, MemeTemplate, MemePost, slide_url

log = logging.getLogger(__name__)

bp = Blueprint('render_queue', __name__)

STATUSES = ('pending', 'running', 'done', 'failed', 'cancelled')
STATUS_LABELS = {'pending': 'Wartend', 'running': 'Läuft', 'done': 'Fertig',
                 'failed': 'Fehler', 'cancelled': 'Abgebrochen'}

STALE_RUNNING_MINUTES = 10      # laufende Tasks älter als das gelten nach Neustart als verwaist
RETRY_DELAY_SECONDS = 30        # Wartezeit vor dem automatischen zweiten Versuch (KI nicht erreichbar)
MAX_AUTO_ATTEMPTS = 2           # automatische Versuche insgesamt
IDLE_SLEEP = 2.0                # Pause der Worker ohne Arbeit
BETWEEN_TASKS_SLEEP = 0.5       # Pause je Worker zwischen zwei Tasks
RUN_ONCE_LIMIT = 5              # Tasks pro /api/render/run-once

_worker_threads = []
_worker_count = 0
_stop_event = threading.Event()


# ═══════════════════════════════════════════════════════════════════════════════
# MODELL
# ═══════════════════════════════════════════════════════════════════════════════

class RenderTask(db.Model):
    __tablename__ = 'render_task'
    id            = db.Column(db.Integer, primary_key=True)
    batch_id      = db.Column(db.String(36), index=True, nullable=False)
    kind          = db.Column(db.String(10), default='single', nullable=False)   # single | series
    template_id   = db.Column(db.Integer, db.ForeignKey('meme_template.id'), nullable=True, index=True)
    series        = db.Column(db.String(200), nullable=True)
    city_id       = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=False, index=True)
    status        = db.Column(db.String(12), default='pending', nullable=False, index=True)
    attempts      = db.Column(db.Integer, default=0, nullable=False)
    worker        = db.Column(db.String(40))
    error         = db.Column(db.Text)
    fit_score     = db.Column(db.Integer)
    fit_reasoning = db.Column(db.Text)
    post_id       = db.Column(db.Integer, db.ForeignKey('meme_post.id'), nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    started_at    = db.Column(db.DateTime)
    finished_at   = db.Column(db.DateTime)

    template = db.relationship('MemeTemplate', foreign_keys=[template_id])
    city     = db.relationship('City', foreign_keys=[city_id])
    post     = db.relationship('MemePost', foreign_keys=[post_id])

    @property
    def label(self):
        if self.kind == 'series':
            return self.series or ''
        return self.template.name if self.template else ''

    @property
    def retry_pending(self):
        """Automatischer zweiter Versuch steht noch aus (KI war nicht erreichbar)."""
        return (self.status == 'failed'
                and (self.attempts or 0) < MAX_AUTO_ATTEMPTS
                and 'KI nicht erreichbar' in (self.error or ''))

    def to_dict(self):
        post = self.post
        image_url = ''
        if post:
            image_url = post.image_url or (slide_url(post.image_path) if post.image_path else '')
        return {
            'id': self.id,
            'batch_id': self.batch_id,
            'kind': self.kind,
            'label': self.label,
            'template_id': self.template_id,
            'template_name': self.template.name if self.template else '',
            'series': self.series or '',
            'city_id': self.city_id,
            'city_name': self.city.name if self.city else '',
            'status': self.status,
            'status_label': STATUS_LABELS.get(self.status, self.status),
            'attempts': self.attempts or 0,
            'worker': self.worker or '',
            'error': self.error or '',
            'retry_pending': self.retry_pending,
            'fit_score': self.fit_score,
            'fit_reasoning': self.fit_reasoning or '',
            'post_id': self.post_id,
            'image_url': image_url,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# HILFEN
# ═══════════════════════════════════════════════════════════════════════════════

def _login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Nicht angemeldet'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


def _appmod():
    import app as appmod   # erst zur Laufzeit – app.py importiert dieses Modul
    return appmod


def _render_dir():
    appmod = _appmod()
    path = getattr(appmod, '_RENDER_DIR', None) or os.path.join(appmod._DATA_ROOT, 'renders')
    os.makedirs(path, exist_ok=True)
    return path


def _workers_configured():
    try:
        return max(0, int(os.getenv('MEMEOS_WORKERS', '3')))
    except ValueError:
        return 3


def _workers_disabled():
    return current_app.config.get('TESTING') or os.getenv('MEMEOS_WORKERS', '3').strip() == '0'


def _alive_workers():
    return sum(1 for t in _worker_threads if t.is_alive())


def _counts(query_filter=None):
    q = db.session.query(RenderTask.status, func.count(RenderTask.id))
    if query_filter is not None:
        q = q.filter(query_filter)
    counts = {s: 0 for s in STATUSES}
    for status, n in q.group_by(RenderTask.status).all():
        counts[status] = n
    return counts


def _series_templates(series):
    return (MemeTemplate.query.filter_by(active=True)
            .filter(MemeTemplate.series == series)
            .order_by(MemeTemplate.series_position.is_(None), MemeTemplate.series_position, MemeTemplate.name)
            .all())


def _pil_render(appmod, template, vars_dict, city):
    """appmod._pil_render aufrufen – mit city, falls die Signatur das kennt (Renderer-Umbau B1)."""
    fn = appmod._pil_render
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if 'city' in params:
        return fn(template, vars_dict, city=city)
    return fn(template, vars_dict)


def _write_png(png_bytes, filename):
    path = os.path.join(_render_dir(), filename)
    with open(path, 'wb') as fh:
        fh.write(png_bytes)
    return path


def _mark_failed(task, error):
    task.status = 'failed'
    task.error = (error or 'Unbekannter Fehler')[:2000]
    task.finished_at = datetime.utcnow()


# ═══════════════════════════════════════════════════════════════════════════════
# VERARBEITUNG
# ═══════════════════════════════════════════════════════════════════════════════

def _process_single(appmod, task, city, written):
    template = MemeTemplate.query.get(task.template_id) if task.template_id else None
    if not template:
        _mark_failed(task, 'Template nicht gefunden')
        return

    res = appmod._claude_fit_and_vars(city, template)
    task.fit_score = res.get('fit_score')
    task.fit_reasoning = (res.get('reasoning') or '')[:2000]
    if res.get('error'):
        _mark_failed(task, res['error'])
        return
    vars_dict = res.get('vars') or {}

    render_type = template.render_type if template.render_type in ('pil', 'manual') else 'pil'
    if render_type == 'manual':
        _mark_failed(task, 'Manuelles Template: nur Brief')
        return

    png = _pil_render(appmod, template, vars_dict, city)
    if not png:
        _mark_failed(task, 'Kein Hintergrundbild / PIL-Rendering fehlgeschlagen')
        return

    filename = f'task_{task.id}_{int(time.time())}.png'
    path = _write_png(png, filename)
    written.append(path)
    cloud_url = appmod._upload_cloudinary(path, folder='memeos/renders')

    notes = {
        'fit_score': res.get('fit_score'),
        'reasoning': res.get('reasoning') or '',
        'vars': vars_dict,
        'batch_id': task.batch_id,
        'task_id': task.id,
    }
    if res.get('warning'):
        notes['warning'] = res['warning']
    post = MemePost(
        city_id=city.id,
        template_id=template.id,
        title=f'{city.name} – {template.name}',
        image_path=filename,
        image_url=cloud_url or ('/renders/' + filename),
        post_type='feed',
        status='entwurf',
        notes=json.dumps(notes, ensure_ascii=False),
    )
    db.session.add(post)
    db.session.flush()

    template.use_count = (template.use_count or 0) + 1
    task.post_id = post.id
    task.status = 'done'
    task.error = res.get('warning') or None
    task.finished_at = datetime.utcnow()
    appmod._mark_knowledge_used(city.id, vars_dict, template.id)   # commitet die Session
    db.session.commit()


def _process_series(appmod, task, city, written):
    templates = _series_templates(task.series)
    if not templates:
        _mark_failed(task, f'Keine aktiven Templates in Serie „{task.series}“')
        return

    paths, slide_results, fit_scores = [], [], []
    ki_errors, last_ki_error = 0, ''
    ts = int(time.time())
    for idx, template in enumerate(templates, start=1):
        res = appmod._claude_fit_and_vars(city, template)
        entry = {'template_id': template.id, 'template': template.name,
                 'position': template.series_position,
                 'fit_score': res.get('fit_score'), 'reasoning': res.get('reasoning') or '',
                 'vars': res.get('vars') or {}}
        if res.get('error'):
            ki_errors += 1
            last_ki_error = res['error']
            entry['error'] = res['error']
            slide_results.append(entry)
            continue
        if res.get('warning'):
            entry['warning'] = res['warning']
        render_type = template.render_type if template.render_type in ('pil', 'manual') else 'pil'
        if render_type == 'manual':
            entry['error'] = 'Manuelles Template: nur Brief'
            slide_results.append(entry)
            continue
        png = _pil_render(appmod, template, res.get('vars') or {}, city)
        if not png:
            entry['error'] = 'Kein Hintergrundbild / PIL-Rendering fehlgeschlagen'
            slide_results.append(entry)
            continue
        filename = f'task_{task.id}_s{template.series_position or idx}_{ts}.png'
        written.append(_write_png(png, filename))
        paths.append(filename)
        entry['file'] = filename
        if res.get('fit_score') is not None:
            fit_scores.append(res['fit_score'])
        template.use_count = (template.use_count or 0) + 1
        appmod._mark_knowledge_used(city.id, res.get('vars') or {}, template.id)
        slide_results.append(entry)

    if not paths:
        if ki_errors and ki_errors == len(templates):
            _mark_failed(task, last_ki_error or 'KI nicht erreichbar')
        else:
            reasons = '; '.join(sorted({e.get('error', '') for e in slide_results if e.get('error')}))
            _mark_failed(task, f'Kein Slide gerendert ({reasons or "unbekannt"})')
        return

    cloud_url = appmod._upload_cloudinary(os.path.join(_render_dir(), paths[0]), folder='memeos/renders')
    notes = {'series': task.series, 'slides': slide_results,
             'rendered': len(paths), 'total': len(templates),
             'batch_id': task.batch_id, 'task_id': task.id}
    post = MemePost(
        city_id=city.id,
        template_id=templates[0].id,
        title=f'{city.name} – {task.series}',
        image_path=paths[0],
        image_url=cloud_url or ('/renders/' + paths[0]),
        carousel_paths=json.dumps(paths),
        post_type='carousel',
        status='entwurf',
        notes=json.dumps(notes, ensure_ascii=False),
    )
    db.session.add(post)
    db.session.flush()

    task.post_id = post.id
    task.fit_score = int(round(sum(fit_scores) / len(fit_scores))) if fit_scores else None
    missing = len(templates) - len(paths)
    task.fit_reasoning = (f'{len(paths)}/{len(templates)} Slides gerendert'
                          + (f', {missing} übersprungen' if missing else ''))
    task.status = 'done'
    task.error = None
    task.finished_at = datetime.utcnow()
    db.session.commit()


def _process_task(task_id):
    """Einen geclaimten Task vollständig verarbeiten (im App-Kontext des Aufrufers)."""
    appmod = _appmod()
    written = []
    try:
        task = RenderTask.query.get(task_id)
        if not task:
            return
        city = City.query.get(task.city_id)
        if not city:
            _mark_failed(task, 'Stadt nicht gefunden')
            db.session.commit()
            return
        if task.kind == 'series':
            _process_series(appmod, task, city, written)
        else:
            _process_single(appmod, task, city, written)
        db.session.commit()
    except Exception as ex:
        log.error(f'RenderTask {task_id}: {type(ex).__name__}: {ex}')
        db.session.rollback()
        for path in written:
            try:
                os.remove(path)
            except OSError:
                pass
        task = RenderTask.query.get(task_id)
        if task:
            _mark_failed(task, f'{type(ex).__name__}: {str(ex)[:500]}')
            db.session.commit()


def _claim(token):
    """Ersten pending-Task atomar auf running setzen. → Task-ID oder None."""
    now = datetime.utcnow()
    first_pending = (select(RenderTask.id)
                     .where(RenderTask.status == 'pending')
                     .order_by(RenderTask.id)
                     .limit(1)
                     .scalar_subquery())
    stmt = (update(RenderTask)
            .where(RenderTask.id == first_pending, RenderTask.status == 'pending')
            .values(status='running', worker=token, started_at=now, finished_at=None,
                    attempts=RenderTask.attempts + 1)
            .execution_options(synchronize_session=False))
    result = db.session.execute(stmt)
    db.session.commit()
    if result.rowcount != 1:
        return None
    row = db.session.execute(
        select(RenderTask.id).where(RenderTask.worker == token, RenderTask.status == 'running')
    ).first()
    return row[0] if row else None


def _requeue_due_retries():
    """Fehlgeschlagene Tasks (KI nicht erreichbar, erster Versuch) nach 30 s wieder auf pending."""
    cutoff = datetime.utcnow() - timedelta(seconds=RETRY_DELAY_SECONDS)
    due = (RenderTask.query.filter(RenderTask.status == 'failed',
                                   RenderTask.attempts < MAX_AUTO_ATTEMPTS,
                                   RenderTask.error.like('%KI nicht erreichbar%'),
                                   RenderTask.finished_at <= cutoff)
           .all())
    for task in due:
        task.status = 'pending'
        task.worker = None
        task.started_at = None
        task.finished_at = None
    if due:
        db.session.commit()
    return len(due)


def _reset_stale_running():
    cutoff = datetime.utcnow() - timedelta(minutes=STALE_RUNNING_MINUTES)
    stale = (RenderTask.query.filter(RenderTask.status == 'running')
             .filter(db.or_(RenderTask.started_at.is_(None), RenderTask.started_at < cutoff))
             .all())
    for task in stale:
        task.status = 'pending'
        task.worker = None
        task.started_at = None
    if stale:
        db.session.commit()
        log.info(f'RenderQueue: {len(stale)} verwaiste Tasks zurück auf pending')
    return len(stale)


def _worker_loop(flask_app, name):
    log.info(f'RenderQueue: Worker {name} gestartet')
    while not _stop_event.is_set():
        try:
            with flask_app.app_context():
                token = f'{name}:{secrets.token_hex(3)}'
                task_id = _claim(token)
                if task_id is None:
                    _requeue_due_retries()
                else:
                    _process_task(task_id)
        except Exception as ex:
            log.error(f'RenderQueue Worker {name}: {type(ex).__name__}: {ex}')
            try:
                with flask_app.app_context():
                    db.session.rollback()
            except Exception:
                pass
            task_id = None
        if task_id is None:
            _stop_event.wait(IDLE_SLEEP)
        else:
            _stop_event.wait(BETWEEN_TASKS_SLEEP)


def _start_workers(flask_app, n):
    global _worker_count
    _worker_count = n
    host = socket.gethostname().split('.')[0][:8] or 'w'
    for i in range(1, n + 1):
        name = f'{host}-{os.getpid()}-{i}'
        t = threading.Thread(target=_worker_loop, args=(flask_app, name),
                             name=f'render-worker-{i}', daemon=True)
        t.start()
        _worker_threads.append(t)


def run_once(limit=RUN_ONCE_LIMIT):
    """Bis zu `limit` pending-Tasks synchron verarbeiten (Tests / Betrieb ohne Worker)."""
    _requeue_due_retries()
    processed = []
    for _ in range(max(1, min(int(limit), RUN_ONCE_LIMIT))):
        task_id = _claim(f'run-once:{secrets.token_hex(3)}')
        if task_id is None:
            break
        _process_task(task_id)
        task = RenderTask.query.get(task_id)
        if task:
            processed.append(task.to_dict())
    return processed


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTEN
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_cities(spec):
    """city_ids-Angabe → (Liste City, skipped). 'all' | 'with_wiki' | [ids]."""
    skipped = []
    if spec in (None, '', 'all'):
        return City.query.filter_by(active=True).order_by(City.name).all(), skipped
    if spec == 'with_wiki':
        has_wiki = (select(CityKnowledge.id)
                    .where(CityKnowledge.city_id == City.id, CityKnowledge.active == True)
                    .exists())
        return City.query.filter_by(active=True).filter(has_wiki).order_by(City.name).all(), skipped
    if not isinstance(spec, list):
        raise ValueError("city_ids muss eine Liste, 'all' oder 'with_wiki' sein")
    ids = []
    for raw in spec:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            skipped.append({'city': str(raw), 'reason': 'Ungültige Stadt-ID'})
    found = {c.id: c for c in City.query.filter(City.id.in_(ids)).all()} if ids else {}
    cities = []
    for cid in ids:
        if cid in found and found[cid] not in cities:
            cities.append(found[cid])
        elif cid not in found:
            skipped.append({'city': f'ID {cid}', 'reason': 'Stadt nicht gefunden'})
    return cities, skipped


def _recent_city_ids(kind, template_id, series, days):
    """Städte, die dieses Template / diese Serie in den letzten `days` Tagen schon als MemePost haben."""
    if not days or days <= 0:
        return set()
    cutoff = datetime.utcnow() - timedelta(days=days)
    q = db.session.query(MemePost.city_id).filter(MemePost.created_at >= cutoff)
    if kind == 'single':
        q = q.filter(MemePost.template_id == template_id)
    else:
        marker = json.dumps({'series': series}, ensure_ascii=False)[1:-1]    # "series": "…"
        q = q.filter(MemePost.post_type == 'carousel', MemePost.notes.like(f'%{marker}%'))
    return {row[0] for row in q.distinct().all()}


def _queued_city_ids(kind, template_id, series):
    q = db.session.query(RenderTask.city_id).filter(RenderTask.status.in_(('pending', 'running')),
                                                    RenderTask.kind == kind)
    if kind == 'single':
        q = q.filter(RenderTask.template_id == template_id)
    else:
        q = q.filter(RenderTask.series == series)
    return {row[0] for row in q.distinct().all()}


@bp.route('/api/render/batch', methods=['POST'])
@_login_required
def api_render_batch_create():
    """Body: {template_id ODER series, city_ids: [ids] | 'all' | 'with_wiki', skip_recent_days (default 30)}"""
    d = request.get_json(silent=True) or {}
    template_id = d.get('template_id')
    series = (d.get('series') or '').strip() or None
    if not template_id and not series:
        return jsonify({'error': 'template_id oder series erforderlich'}), 400
    if template_id and series:
        return jsonify({'error': 'Entweder template_id oder series angeben, nicht beides'}), 400

    template = None
    if template_id:
        try:
            template_id = int(template_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'template_id ungültig'}), 400
        template = MemeTemplate.query.get(template_id)
        if not template:
            return jsonify({'error': 'Template nicht gefunden'}), 404
        kind, label = 'single', template.name
    else:
        if not _series_templates(series):
            return jsonify({'error': f'Serie „{series}“ hat keine aktiven Templates'}), 404
        kind, label = 'series', series

    try:
        skip_days = int(d.get('skip_recent_days', 30) or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'skip_recent_days muss eine Zahl sein'}), 400

    try:
        cities, skipped = _resolve_cities(d.get('city_ids', 'all'))
    except ValueError as ex:
        return jsonify({'error': str(ex)}), 400
    if not cities and not skipped:
        return jsonify({'error': 'Keine Städte gefunden'}), 400

    recent = _recent_city_ids(kind, template_id, series, skip_days)
    queued = _queued_city_ids(kind, template_id, series)

    batch_id = str(uuid.uuid4())
    created = 0
    for city in cities:
        if city.id in queued:
            skipped.append({'city': city.name, 'reason': 'Bereits in der Warteschlange'})
            continue
        if city.id in recent:
            skipped.append({'city': city.name, 'reason': f'Schon in den letzten {skip_days} Tagen erzeugt'})
            continue
        db.session.add(RenderTask(batch_id=batch_id, kind=kind,
                                  template_id=template_id if kind == 'single' else None,
                                  series=series if kind == 'series' else None,
                                  city_id=city.id, status='pending'))
        created += 1
    db.session.commit()
    return jsonify({'batch_id': batch_id, 'created': created, 'skipped': skipped,
                    'kind': kind, 'label': label,
                    'workers': _worker_count, 'alive': _alive_workers()}), 201


@bp.route('/api/render/batch/<batch_id>', methods=['GET'])
@_login_required
def api_render_batch_get(batch_id):
    tasks = RenderTask.query.filter_by(batch_id=batch_id).order_by(RenderTask.id).all()
    if not tasks:
        return jsonify({'error': 'Batch nicht gefunden'}), 404
    counts = _counts(RenderTask.batch_id == batch_id)
    retry_pending = sum(1 for t in tasks if t.retry_pending)
    done = counts['pending'] == 0 and counts['running'] == 0 and retry_pending == 0
    return jsonify({'batch_id': batch_id, 'label': tasks[0].label, 'kind': tasks[0].kind,
                    'counts': counts, 'total': len(tasks), 'retry_pending': retry_pending,
                    'done': done, 'tasks': [t.to_dict() for t in tasks]})


@bp.route('/api/render/queue', methods=['GET'])
@_login_required
def api_render_queue():
    counts = _counts()
    rows = (db.session.query(RenderTask.batch_id, func.min(RenderTask.created_at))
            .group_by(RenderTask.batch_id)
            .order_by(func.min(RenderTask.created_at).desc())
            .limit(10).all())
    batches = []
    for batch_id, created_at in rows:
        first = RenderTask.query.filter_by(batch_id=batch_id).order_by(RenderTask.id).first()
        batches.append({'batch_id': batch_id,
                        'created_at': created_at.isoformat() if created_at else None,
                        'kind': first.kind if first else '',
                        'label': first.label if first else '',
                        'counts': _counts(RenderTask.batch_id == batch_id)})
    return jsonify({'counts': counts, 'workers': _worker_count, 'alive': _alive_workers(),
                    'configured_workers': _workers_configured(),
                    'run_once_available': bool(_workers_disabled()),
                    'recent_batches': batches})


@bp.route('/api/render/task/<int:task_id>/retry', methods=['POST'])
@_login_required
def api_render_task_retry(task_id):
    task = RenderTask.query.get_or_404(task_id)
    if task.status in ('pending', 'running'):
        return jsonify({'error': f'Task ist bereits {STATUS_LABELS[task.status].lower()}'}), 409
    task.status = 'pending'
    task.worker = None
    task.error = None
    task.started_at = None
    task.finished_at = None
    db.session.commit()
    return jsonify({'ok': True, 'task': task.to_dict()})


@bp.route('/api/render/batch/<batch_id>/cancel', methods=['POST'])
@_login_required
def api_render_batch_cancel(batch_id):
    if not RenderTask.query.filter_by(batch_id=batch_id).first():
        return jsonify({'error': 'Batch nicht gefunden'}), 404
    now = datetime.utcnow()
    n = (RenderTask.query.filter_by(batch_id=batch_id, status='pending')
         .update({'status': 'cancelled', 'finished_at': now}, synchronize_session=False))
    db.session.commit()
    return jsonify({'ok': True, 'cancelled': n, 'counts': _counts(RenderTask.batch_id == batch_id)})


@bp.route('/api/render/batch/<batch_id>', methods=['DELETE'])
@_login_required
def api_render_batch_delete(batch_id):
    """Tasks des Batches löschen – die erzeugten Vorrats-Posts bleiben. Laufende Tasks bleiben stehen."""
    tasks = RenderTask.query.filter_by(batch_id=batch_id).all()
    if not tasks:
        return jsonify({'error': 'Batch nicht gefunden'}), 404
    deleted, kept = 0, 0
    for task in tasks:
        if task.status == 'running':
            kept += 1
            continue
        db.session.delete(task)
        deleted += 1
    db.session.commit()
    return jsonify({'ok': True, 'deleted': deleted, 'kept_running': kept})


@bp.route('/api/render/run-once', methods=['POST'])
@_login_required
def api_render_run_once():
    """Nur ohne Worker (TESTING oder MEMEOS_WORKERS=0): bis zu 5 pending-Tasks synchron abarbeiten."""
    if not _workers_disabled():
        return jsonify({'error': 'run-once nur ohne Worker (MEMEOS_WORKERS=0 oder TESTING)'}), 403
    d = request.get_json(silent=True) or {}
    processed = run_once(d.get('limit', RUN_ONCE_LIMIT))
    return jsonify({'processed': len(processed), 'tasks': processed, 'counts': _counts()})


# ═══════════════════════════════════════════════════════════════════════════════
# INIT
# ═══════════════════════════════════════════════════════════════════════════════

def init_app(flask_app):
    """Blueprint registrieren, Tabelle anlegen, verwaiste Tasks zurücksetzen, Worker starten."""
    global _worker_count
    if 'render_queue' not in flask_app.blueprints:
        flask_app.register_blueprint(bp)
    with flask_app.app_context():
        db.create_all()
        try:
            _reset_stale_running()
        except Exception as ex:
            log.warning(f'RenderQueue: Reset verwaister Tasks fehlgeschlagen: {ex}')
            db.session.rollback()

    n = _workers_configured()
    if flask_app.config.get('TESTING') or n == 0:
        _worker_count = 0
        log.info('RenderQueue: keine Worker (TESTING oder MEMEOS_WORKERS=0) – /api/render/run-once verfügbar')
        return
    if _worker_threads:
        return   # bereits gestartet (z. B. init_app doppelt aufgerufen)
    _start_workers(flask_app, n)
    log.info(f'RenderQueue: {n} Worker gestartet')


def stop_workers(timeout=5.0):
    """Für Tests/Shutdown: Worker-Schleifen beenden."""
    _stop_event.set()
    for t in _worker_threads:
        t.join(timeout)
    _worker_threads.clear()
    _stop_event.clear()
