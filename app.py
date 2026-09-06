import os
import io
import re
import json
import time
import secrets
import hashlib
import threading
import urllib.parse
import uuid
import shutil
import requests
import feedparser
try:
    import memeos_render   # Phase B (B1): Element-Renderer, kein app-Import
except ImportError:   # pragma: no cover – Fallback auf die alte Pillow-Implementierung
    memeos_render = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, session, flash, send_from_directory, abort)
import models as _models
from models import (db, User, City, CityKnowledge, MemeTemplate, RenderJob,
                    NewsItem, ResidentSurvey, AppSettings, AppTodo, AiUsageLog,
                    CityMarketEntry, BuyablePage,
                    MemoInspirationSource, MemoInspirationPost,
                    MemeEvent, ExportJob,
                    MemePost, TrendingTopic, RecycleJob, CityFollowerSnapshot,
                    KNOWLEDGE_CATEGORIES, CATEGORY_MAP, LEGACY_CATEGORY_MAP,
                    normalize_category, slide_url,
                    TEMPLATE_CATEGORIES, TEMPLATE_CAT_MAP,
                    TEMPLATE_GROUPS, TemplateCategory,
                    CollabNiche, CollabIdea, CityCollab)
import anthropic
import logging

# ── Setup ──────────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# ── Settings-Alias (ContentOS-Schreibweise → MemeOS AppSettings) ───────────────
def get_setting(key, default=None):
    return AppSettings.get(key, default)

def set_setting(key, value):
    return AppSettings.set(key, value)


# ── Pfade ──────────────────────────────────────────────────────────────────────
# Alles, was Uploads/Renders/Exports schreibt, landet im Datenpfad. Auf Render ist das die
# gemountete Disk (MEMEOS_DATA_ROOT=/opt/render/project/src/instance); static/ wird bei jedem
# Deploy neu ausgecheckt und ist deshalb für Nutzerdaten ungeeignet.
_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
_DATA_ROOT  = os.getenv('MEMEOS_DATA_ROOT') or os.path.join(_BASE_DIR, 'instance')
_UPLOAD_DIR = os.path.join(_DATA_ROOT, 'uploads')
_RENDER_DIR = os.path.join(_DATA_ROOT, 'renders')
_EXPORT_DIR = os.path.join(_DATA_ROOT, 'exports')
_FONTS_DATA_DIR = os.path.join(_DATA_ROOT, 'fonts')
_DB_PATH    = os.path.join(_BASE_DIR, 'instance', 'memeos.db')
for _d in (os.path.join(_BASE_DIR, 'instance'), _DATA_ROOT,
           _UPLOAD_DIR, _RENDER_DIR, _EXPORT_DIR, _FONTS_DATA_DIR):
    os.makedirs(_d, exist_ok=True)
_models.UPLOAD_DIR = _UPLOAD_DIR   # für slide_url() in MemePost.to_dict


def _migrate_static_files():
    """Einmalige Übernahme: Dateien aus static/uploads und static/renders in den Datenpfad
    kopieren, wenn sie dort noch fehlen. Es wird nichts gelöscht."""
    copied = 0
    for src_dir, dst_dir in ((os.path.join(_BASE_DIR, 'static', 'uploads'), _UPLOAD_DIR),
                             (os.path.join(_BASE_DIR, 'static', 'renders'), _RENDER_DIR)):
        if not os.path.isdir(src_dir) or os.path.realpath(src_dir) == os.path.realpath(dst_dir):
            continue
        try:
            for name in os.listdir(src_dir):
                src = os.path.join(src_dir, name)
                dst = os.path.join(dst_dir, name)
                if os.path.isfile(src) and not os.path.exists(dst):
                    shutil.copy2(src, dst)
                    copied += 1
        except Exception as ex:
            log.warning(f'Übernahme aus {src_dir} unvollständig: {ex}')
    if copied:
        log.info(f'{copied} Datei(en) aus static/ in den Datenpfad übernommen')

_migrate_static_files()


# Platzhalter, die nie als SECRET_KEY gelten dürfen (Beispielwerte aus .env.example u. ä.)
_SECRET_KEY_PLACEHOLDERS = {'dein-geheimer-schluessel', 'changeme', 'change-me', 'change_me', 'secret', 'dev'}


def _load_secret_key():
    """SECRET_KEY aus der Umgebung; sonst aus <DATA_ROOT>/secret_key lesen oder dort einmalig
    erzeugen. Damit überleben Sessions Neustarts, ohne dass ein Standardwert im Code steht."""
    env_key = os.getenv('SECRET_KEY', '').strip()
    if env_key and env_key.lower() in _SECRET_KEY_PLACEHOLDERS:
        # Beispielwert aus .env.example wurde übernommen – nie als echten Schlüssel verwenden
        log.warning('SECRET_KEY hat den Beispielwert aus .env.example – wird ignoriert, '
                    'stattdessen <DATA_ROOT>/secret_key verwendet/erzeugt')
        env_key = ''
    if env_key:
        return env_key
    path = os.path.join(_DATA_ROOT, 'secret_key')
    for attempt in range(5):
        try:
            with open(path, 'r') as fh:
                key = fh.read().strip()
            if key:
                return key
        except FileNotFoundError:
            pass
        except Exception as ex:
            log.warning(f'secret_key nicht lesbar: {ex}')
            break
        try:
            # O_EXCL: nur ein Prozess legt die Datei an, die anderen lesen sie im nächsten Durchlauf
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as fh:
                fh.write(secrets.token_hex(32))
        except FileExistsError:
            time.sleep(0.05 * (attempt + 1))
        except Exception as ex:
            log.warning(f'secret_key nicht speicherbar ({ex}) – Sessions gelten nur bis zum Neustart')
            break
    return secrets.token_hex(32)


app = Flask(__name__)
app.secret_key = _load_secret_key()

app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', f'sqlite:///{_DB_PATH}')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# ── Cloudinary ──────────────────────────────────────────────────────────────────
def _upload_cloudinary(source, folder='memeos', resource_type='auto'):
    """Upload local path, bytes, or URL to Cloudinary. Returns secure_url or None."""
    cloud_env = os.getenv('CLOUDINARY_URL', '')
    if not cloud_env:
        return None
    try:
        import cloudinary
        import cloudinary.uploader
        cloudinary.config(url=cloud_env)
        result = cloudinary.uploader.upload(source, folder=folder, resource_type=resource_type)
        return result.get('secure_url')
    except ImportError:
        log.warning('cloudinary not installed — run: pip install cloudinary')
        return None
    except Exception as e:
        log.error(f'Cloudinary upload failed: {e}')
        return None

def _cloudinary_connected():
    return bool(os.getenv('CLOUDINARY_URL', ''))


def _local_media_path(name):
    """Lokalen Pfad zu einem Upload-/Render-Dateinamen finden (oder None)."""
    if not name:
        return None
    base = os.path.basename(name)
    for folder in (_UPLOAD_DIR, _RENDER_DIR):
        candidate = os.path.join(folder, base)
        if os.path.exists(candidate):
            return candidate
    return None


def _export_job_update(job_id, **fields):
    job = ExportJob.query.get(job_id)
    if not job:
        return
    for k, v in fields.items():
        setattr(job, k, v)
    db.session.commit()


def _build_zip_async(job_id: str, post_ids: list, status_filter: str, city_id_filter):
    import zipfile, io, csv as csv_mod
    with app.app_context():
        try:
            _export_job_update(job_id, status='building')
            q = MemePost.query
            if post_ids:
                q = q.filter(MemePost.id.in_(post_ids))
            else:
                if status_filter:
                    q = q.filter_by(status=status_filter)
                if city_id_filter:
                    q = q.filter_by(city_id=city_id_filter)
            posts = q.order_by(MemePost.scheduled_at, MemePost.created_at).all()
            if not posts:
                _export_job_update(job_id, status='error', error='Keine Posts gefunden')
                return

            fname = f'memeos_export_{datetime.utcnow().strftime("%Y%m%d_%H%M")}_{job_id[:6]}.zip'
            zip_path = os.path.join(_EXPORT_DIR, fname)

            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                csv_buf = io.StringIO()
                writer = csv_mod.DictWriter(csv_buf, fieldnames=[
                    'id', 'city', 'title', 'caption', 'hashtags', 'post_type',
                    'scheduled_at', 'status', 'image_file'
                ])
                writer.writeheader()
                for p in posts:
                    city_slug = (p.city.name if p.city else 'unbekannt').lower().replace(' ', '_')
                    img_filename = 'kein_bild'
                    img_bytes = None
                    if p.image_url and p.image_url.startswith('http'):
                        try:
                            resp = requests.get(p.image_url, timeout=15)
                            if resp.ok:
                                img_bytes = resp.content
                                raw_ext = p.image_url.split('?')[0].rsplit('.', 1)[-1].lower()
                                ext = raw_ext if raw_ext in ('jpg', 'jpeg', 'png', 'webp', 'gif') else 'jpg'
                                img_filename = f'post_{p.id}_{city_slug}.{ext}'
                        except Exception:
                            pass
                    else:
                        candidate = _local_media_path(p.image_path) or _local_media_path(p.image_url)
                        if candidate:
                            base = os.path.basename(candidate)
                            with open(candidate, 'rb') as fh:
                                img_bytes = fh.read()
                            ext = base.rsplit('.', 1)[-1].lower() if '.' in base else 'jpg'
                            img_filename = f'post_{p.id}_{city_slug}.{ext}'
                    if img_bytes:
                        zf.writestr(f'images/{img_filename}', img_bytes)
                    writer.writerow({
                        'id': p.id, 'city': p.city.name if p.city else '',
                        'title': p.title or '', 'caption': p.caption or '',
                        'hashtags': p.hashtags or '', 'post_type': p.post_type or 'feed',
                        'scheduled_at': p.scheduled_at.strftime('%Y-%m-%d %H:%M') if p.scheduled_at else '',
                        'status': p.status, 'image_file': img_filename,
                    })
                zf.writestr('manifest.csv', csv_buf.getvalue())

            _export_job_update(job_id, status='ready', path=zip_path,
                               filename=fname, post_count=len(posts))
        except Exception as e:
            log.error(f'ZIP build failed for job {job_id}: {e}')
            try:
                db.session.rollback()
                _export_job_update(job_id, status='error', error=str(e))
            except Exception:
                pass


app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

db.init_app(app)

# ── Env ────────────────────────────────────────────────────────────────────────
ADMIN_USERNAME    = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD    = os.getenv('ADMIN_PASSWORD', '')   # leer → Env-Login deaktiviert (kein Standardwert mehr)
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
CANVA_CLIENT_ID   = os.getenv('CANVA_CLIENT_ID', '')
CANVA_CLIENT_SECRET = os.getenv('CANVA_CLIENT_SECRET', '')
CONTENT_OS_URL    = os.getenv('CONTENT_OS_URL', '')
CONTENT_OS_KEY    = os.getenv('CONTENT_OS_KEY', '')
BASE_URL          = os.getenv('BASE_URL', 'http://localhost:5200')
CANVA_REDIRECT_URI = BASE_URL + '/canva/callback'

# ── CSRF ───────────────────────────────────────────────────────────────────────
_CSRF_EXEMPT = {'/login', '/logout', '/canva/callback', '/ping', '/survey/submit'}

@app.before_request
def csrf_protect():
    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
        if request.path in _CSRF_EXEMPT or request.path.startswith('/api/'):
            return
        token = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
        if not token or token != session.get('csrf_token'):
            abort(403)

@app.context_processor
def inject_csrf():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return dict(csrf_token=session['csrf_token'])

# ── Auth ───────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ── Einfache Bremsen pro IP (In-Memory reicht; pro Worker getrennt) ────────────
_rate_lock = threading.Lock()
_rate_buckets: dict = {}   # (scope, ip) -> [timestamps]

def _client_ip():
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or 'unbekannt'

def _rate_hits(scope, window_sec):
    """Anzahl der Treffer im Fenster (ohne neuen Treffer zu zählen)."""
    key = (scope, _client_ip())
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_buckets.get(key, []) if now - t < window_sec]
        _rate_buckets[key] = hits
        return len(hits)

def _rate_record(scope):
    key = (scope, _client_ip())
    with _rate_lock:
        _rate_buckets.setdefault(key, []).append(time.time())

def _rate_clear(scope):
    key = (scope, _client_ip())
    with _rate_lock:
        _rate_buckets.pop(key, None)

_LOGIN_MAX_FAILS   = 10      # Fehlversuche
_LOGIN_WINDOW_SEC  = 600     # pro 10 Minuten
_SURVEY_MAX_SUBMIT = 5       # Submits
_SURVEY_WINDOW_SEC = 60      # pro Minute


@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        if _rate_hits('login', _LOGIN_WINDOW_SEC) >= _LOGIN_MAX_FAILS:
            error = 'Zu viele Fehlversuche. Bitte in 10 Minuten erneut versuchen.'
            return render_template('login.html', error=error), 429
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '')
        # DB-User
        user = User.query.filter_by(username=u, active=True).first()
        if user and user.check_password(p):
            session['logged_in'] = True
            session['username']  = u
            user.last_login = datetime.utcnow()
            db.session.commit()
            _rate_clear('login')
            return redirect(url_for('dashboard'))
        # Env-Fallback – nur wenn ein Passwort gesetzt ist
        elif ADMIN_PASSWORD and u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
            session['logged_in'] = True
            session['username']  = u
            _rate_clear('login')
            return redirect(url_for('dashboard'))
        else:
            _rate_record('login')
            if not ADMIN_PASSWORD and User.query.filter_by(active=True).count() == 0:
                error = 'Kein Admin-Passwort gesetzt (ADMIN_PASSWORD)'
            else:
                error = 'Ungültige Zugangsdaten'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/ping')
def ping():
    return 'ok'

# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.route('/')
@login_required
def dashboard():
    stats = {
        'cities':     City.query.filter_by(active=True).count(),
        'templates':  MemeTemplate.query.filter_by(active=True).count(),
        'pending':    RenderJob.query.filter(RenderJob.status.in_(['pending','running'])).count(),
        'review':     RenderJob.query.filter(RenderJob.status.in_(['done','review'])).count(),
        'done':       RenderJob.query.filter_by(status='approved').count(),
        'news':       NewsItem.query.filter_by(status='scored').count(),
        'knowledge':  CityKnowledge.query.filter_by(active=True).count(),
    }
    recent_jobs = RenderJob.query.order_by(RenderJob.created_at.desc()).limit(10).all()
    cities      = City.query.filter_by(active=True).order_by(City.name).all()
    templates   = MemeTemplate.query.filter_by(active=True).order_by(MemeTemplate.name).all()
    todos       = AppTodo.query.filter_by(done=False).order_by(AppTodo.priority.desc(), AppTodo.created_at.desc()).all()

    today = datetime.utcnow().strftime('%m-%d')
    seasonal_templates = [t for t in templates if t.seasonal_from and t.seasonal_to
                          and t.seasonal_from <= today <= t.seasonal_to]

    canva_connected = _canva_is_connected()
    ai_cost_month = _ai_cost_this_month()

    market_summary = {
        'total':       CityMarketEntry.query.count(),
        'owned':       CityMarketEntry.query.filter_by(status='owned').count(),
        'want':        CityMarketEntry.query.filter_by(status='want_to_buy').count(),
        'found':       CityMarketEntry.query.filter_by(status='found_pages').count(),
        'in_contact':  BuyablePage.query.filter(BuyablePage.contact_status.in_(['antwortet','aktiv','in_verhandlung'])).count(),
    }
    inspo_counts = {
        'new':   MemoInspirationPost.query.filter_by(status='new').count(),
        'saved': MemoInspirationPost.query.filter_by(is_saved=True).count(),
    }
    vorrat_counts = {
        'entwurf':          MemePost.query.filter_by(status='entwurf').count(),
        'bereit':           MemePost.query.filter_by(status='bereit').count(),
        'geplant':          MemePost.query.filter_by(status='geplant').count(),
        'veroeffentlicht':  MemePost.query.filter_by(status='veroeffentlicht').count(),
    }

    return render_template('dashboard.html',
        stats=stats,
        recent_jobs=recent_jobs,
        cities=cities,
        templates=templates,
        todos=todos,
        seasonal_templates=seasonal_templates,
        canva_connected=canva_connected,
        ai_cost_month=ai_cost_month,
        categories=KNOWLEDGE_CATEGORIES,
        category_map=CATEGORY_MAP,
        template_categories=[(c.key, c.label, c.emoji, c.group)
            for c in TemplateCategory.query.filter_by(active=True).order_by(TemplateCategory.sort_order).all()],
        template_groups=TEMPLATE_GROUPS,
        market_summary=market_summary,
        inspo_counts=inspo_counts,
        vorrat_counts=vorrat_counts,
        now=datetime.utcnow(),
    )

# ── Static renders ─────────────────────────────────────────────────────────────
@app.route('/renders/<path:filename>')
@login_required
def serve_render(filename):
    return send_from_directory(_RENDER_DIR, filename)

@app.route('/uploads/<path:filename>')
@login_required
def serve_upload(filename):
    return send_from_directory(_UPLOAD_DIR, filename)

# ═══════════════════════════════════════════════════════════════════════════════
# CITY API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/cities', methods=['GET'])
@login_required
def api_cities_list():
    cities = City.query.order_by(City.name).all()
    return jsonify([{
        'id': c.id, 'name': c.name, 'state': c.state,
        'population': c.population, 'active': c.active,
        'instagram_handle': c.instagram_handle,
        'knowledge_count': c.knowledge_count(),
        'render_count': c.render_count(),
    } for c in cities])


@app.route('/api/cities/stats')
@login_required
def api_cities_stats():
    """Single-query aggregated city overview — replaces N parallel /api/city/<id>/dashboard calls."""
    from sqlalchemy import func as sqlfunc
    cities = City.query.filter_by(active=True).order_by(City.name).all()
    city_ids = [c.id for c in cities]

    # Post counts per city+status in one query
    raw_counts = db.session.query(
        MemePost.city_id, MemePost.status, sqlfunc.count(MemePost.id)
    ).filter(MemePost.city_id.in_(city_ids)).group_by(MemePost.city_id, MemePost.status).all()
    counts_map: dict = {}
    for cid, status, cnt in raw_counts:
        counts_map.setdefault(cid, {})[status] = cnt

    # Ø engagement_rate pro Stadt (letzte 30 Tage) – engagement_rate ist eine Python-Property,
    # deshalb werden die Posts geladen und in Python gemittelt (kein SQL-avg möglich).
    cutoff = datetime.utcnow() - timedelta(days=30)
    er_posts = MemePost.query.filter(
        MemePost.city_id.in_(city_ids),
        MemePost.status == 'veroeffentlicht',
        MemePost.published_at >= cutoff,
        MemePost.perf_reach.isnot(None),
    ).all()
    er_lists: dict = {}
    for p in er_posts:
        er = p.engagement_rate
        if er is not None:
            er_lists.setdefault(p.city_id, []).append(er)
    er_map = {cid: round(sum(v) / len(v), 2) for cid, v in er_lists.items() if v}

    # Wiki-knowledge count per city in one query
    wiki_rows = db.session.query(
        CityKnowledge.city_id, sqlfunc.count(CityKnowledge.id)
    ).filter(CityKnowledge.city_id.in_(city_ids), CityKnowledge.active == True)\
     .group_by(CityKnowledge.city_id).all()
    wiki_map = {cid: cnt for cid, cnt in wiki_rows}

    # Top-3 trending keywords per city (ignoring hidden topics)
    trend_rows = db.session.query(TrendingTopic).filter(
        TrendingTopic.city_id.in_(city_ids), TrendingTopic.ignored == False
    ).order_by(TrendingTopic.city_id, TrendingTopic.trend_score.desc()).all()
    trend_map: dict = {}
    for t in trend_rows:
        lst = trend_map.setdefault(t.city_id, [])
        if len(lst) < 3:
            lst.append(t.keyword)

    # Latest published post date per city
    last_pub_rows = db.session.query(
        MemePost.city_id, db.func.max(MemePost.published_at)
    ).filter(
        MemePost.city_id.in_(city_ids), MemePost.status == 'veroeffentlicht'
    ).group_by(MemePost.city_id).all()
    last_pub_map = {cid: dt for cid, dt in last_pub_rows if dt}

    # Latest follower snapshot + previous-week snapshot for growth
    latest_snap_rows = db.session.query(
        CityFollowerSnapshot.city_id,
        db.func.max(CityFollowerSnapshot.recorded_at).label('latest_at')
    ).filter(CityFollowerSnapshot.city_id.in_(city_ids)).group_by(CityFollowerSnapshot.city_id).all()
    latest_counts: dict = {}
    week_ago_counts: dict = {}
    week_ago = datetime.utcnow() - timedelta(days=7)
    for cid, lat in latest_snap_rows:
        snap = CityFollowerSnapshot.query.filter_by(city_id=cid)\
               .order_by(CityFollowerSnapshot.recorded_at.desc()).first()
        if snap:
            latest_counts[cid] = snap.count
        prev = CityFollowerSnapshot.query.filter(
            CityFollowerSnapshot.city_id == cid,
            CityFollowerSnapshot.recorded_at <= week_ago
        ).order_by(CityFollowerSnapshot.recorded_at.desc()).first()
        if prev:
            week_ago_counts[cid] = prev.count

    result = []
    for c in cities:
        pc = counts_map.get(c.id, {})
        total = sum(pc.values())
        followers = latest_counts.get(c.id)
        prev_followers = week_ago_counts.get(c.id)
        growth = (followers - prev_followers) if (followers is not None and prev_followers is not None) else None
        last_pub = last_pub_map.get(c.id)
        result.append({
            'city': {
                'id': c.id, 'name': c.name, 'state': c.state or '',
                'accent_color': c.accent_color or '#3b82f6',
                'brand_bg': c.brand_bg or '#ffffff',
                'brand_text_color': c.brand_text_color or '#000000',
                'brand_font': c.brand_font or 'Arial',
                'population': c.population, 'instagram_handle': c.instagram_handle or '',
                'rss_url': c.rss_url or '',
            },
            'post_counts': {s: pc.get(s, 0)
                            for s in ['entwurf', 'bereit', 'geplant', 'veroeffentlicht', 'archiviert']},
            'total_posts': total,
            'avg_er': er_map.get(c.id),
            'wiki_count': wiki_map.get(c.id, 0),
            'trending_keywords': trend_map.get(c.id, []),
            'followers': followers,
            'followers_growth_7d': growth,
            'last_published_at': last_pub.isoformat() if last_pub else None,
        })
    return jsonify(result)


@app.route('/api/city/<int:city_id>/followers', methods=['POST'])
@login_required
def api_city_save_followers(city_id):
    City.query.get_or_404(city_id)
    d = request.json or {}
    count = d.get('count')
    if count is None or not isinstance(count, int) or count < 0:
        return jsonify({'error': 'count muss eine positive Ganzzahl sein'}), 400
    snap = CityFollowerSnapshot(city_id=city_id, count=count)
    db.session.add(snap)
    db.session.commit()
    prev = CityFollowerSnapshot.query.filter(
        CityFollowerSnapshot.city_id == city_id,
        CityFollowerSnapshot.recorded_at < snap.recorded_at
    ).order_by(CityFollowerSnapshot.recorded_at.desc()).first()
    growth = (count - prev.count) if prev else None
    return jsonify({'ok': True, 'count': count, 'growth': growth})


@app.route('/api/cities', methods=['POST'])
@login_required
def api_city_create():
    d = request.json or {}
    if not d.get('name'):
        return jsonify({'error': 'Name fehlt'}), 400
    if City.query.filter_by(name=d['name']).first():
        return jsonify({'error': 'Stadt existiert bereits'}), 409
    city = City(
        name=d['name'].strip(),
        state=d.get('state', ''),
        population=d.get('population'),
        instagram_handle=d.get('instagram_handle', ''),
        tiktok_handle=d.get('tiktok_handle', ''),
        accent_color=d.get('accent_color', '#3b82f6'),
        rss_url=d.get('rss_url', ''),
        notes=d.get('notes', ''),
        active=d.get('active', True),
    )
    db.session.add(city)
    db.session.commit()
    return jsonify({'id': city.id, 'name': city.name}), 201

@app.route('/api/cities/<int:city_id>', methods=['PUT'])
@login_required
def api_city_update(city_id):
    city = City.query.get_or_404(city_id)
    d = request.json or {}
    for field in ['name','state','population','instagram_handle','tiktok_handle',
                  'accent_color','brand_bg','brand_text_color','brand_font',
                  'rss_url','notes','active']:
        if field in d:
            setattr(city, field, d[field])
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/cities/<int:city_id>', methods=['DELETE'])
@login_required
def api_city_delete(city_id):
    city = City.query.get_or_404(city_id)
    db.session.delete(city)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/cities/bulk-import', methods=['POST'])
@login_required
def api_cities_bulk_import():
    d = request.json or {}
    names = d.get('cities', [])
    created = 0
    for item in names:
        if isinstance(item, str):
            name, state, pop = item.strip(), '', None
        else:
            name  = item.get('name', '').strip()
            state = item.get('state', '')
            pop   = item.get('population')
        if not name or City.query.filter_by(name=name).first():
            continue
        db.session.add(City(name=name, state=state, population=pop))
        created += 1
    db.session.commit()
    return jsonify({'created': created})

# ── City-Wiki API ──────────────────────────────────────────────────────────────

@app.route('/api/cities/<int:city_id>/knowledge', methods=['GET'])
@login_required
def api_knowledge_list(city_id):
    City.query.get_or_404(city_id)
    entries = CityKnowledge.query.filter_by(city_id=city_id)\
                .order_by(CityKnowledge.category, CityKnowledge.confidence.desc()).all()
    return jsonify([{
        'id': e.id, 'category': e.category, 'category_label': e.category_label,
        'category_color': e.category_color, 'name': e.name, 'description': e.description,
        'confidence': e.confidence, 'source': e.source, 'source_badge': e.source_badge,
        'used_count': e.used_count, 'active': e.active,
        'on_cooldown': e.on_cooldown,
        'cooldown_until': e.cooldown_until.isoformat() if e.cooldown_until else None,
    } for e in entries])

@app.route('/api/cities/<int:city_id>/knowledge', methods=['POST'])
@login_required
def api_knowledge_create(city_id):
    City.query.get_or_404(city_id)
    d = request.json or {}
    if not d.get('name') or not d.get('category'):
        return jsonify({'error': 'Name und Kategorie erforderlich'}), 400
    e = CityKnowledge(
        city_id=city_id,
        category=normalize_category(d['category']),
        name=d['name'].strip(),
        description=d.get('description', ''),
        confidence=int(d.get('confidence', 70)),
        source=d.get('source', 'manual'),
    )
    db.session.add(e)
    db.session.commit()
    return jsonify({'id': e.id}), 201

@app.route('/api/knowledge/<int:entry_id>', methods=['PUT'])
@login_required
def api_knowledge_update(entry_id):
    e = CityKnowledge.query.get_or_404(entry_id)
    d = request.json or {}
    for field in ['name','description','confidence','source','active','category']:
        if field in d:
            setattr(e, field, normalize_category(d[field]) if field == 'category' else d[field])
    if 'cooldown_days' in d and d['cooldown_days']:
        e.cooldown_until = datetime.utcnow() + timedelta(days=int(d['cooldown_days']))
    elif d.get('clear_cooldown'):
        e.cooldown_until = None
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/knowledge/<int:entry_id>', methods=['DELETE'])
@login_required
def api_knowledge_delete(entry_id):
    e = CityKnowledge.query.get_or_404(entry_id)
    db.session.delete(e)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/cities/<int:city_id>/knowledge/ai-generate', methods=['POST'])
@login_required
def api_knowledge_ai_generate(city_id):
    city = City.query.get_or_404(city_id)
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'Kein Anthropic API Key'}), 400
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        categories_str = ', '.join([f"{k} ({label})" for k, label, _ in KNOWLEDGE_CATEGORIES])
        prompt = f"""Du bist ein Experte für deutsche Städte und lokale Meme-Kultur.
Generiere City-Wiki-Einträge für {city.name} ({city.state}, ~{city.population or '?'} Einwohner).

Kategorien: {categories_str}

Antworte NUR mit einem JSON-Array. Jeder Eintrag hat:
- category: eine der Kategorien oben
- name: konkreter Ortsname/Begriff (max 50 Zeichen)
- description: kurze Erklärung warum dieser Ort in diese Kategorie passt (max 100 Zeichen)
- confidence: 0-100 (wie sicher bist du?)

Generiere 3-5 Einträge pro vorhandener Kategorie, insgesamt 30-50 Einträge.
Sei möglichst spezifisch und lokal — generische Antworten wie "Stadtpark" sind wertlos.
Denke an bekannte Memes, Klischees, tatsächliche Problemorte etc."""

        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=4000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = msg.content[0].text.strip()
        # Extrahiere JSON
        import re
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            return jsonify({'error': 'KI hat kein gültiges JSON zurückgegeben'}), 500
        entries_data = json.loads(match.group(0))

        _log_ai_usage('city_wiki_generate', 'claude-haiku-4-5-20251001',
                      msg.usage.input_tokens, msg.usage.output_tokens)

        created = 0
        for e_data in entries_data:
            cat = e_data.get('category', '')
            name = e_data.get('name', '').strip()
            if not cat or not name or cat not in CATEGORY_MAP:
                continue
            exists = CityKnowledge.query.filter_by(city_id=city_id, name=name).first()
            if exists:
                continue
            entry = CityKnowledge(
                city_id=city_id,
                category=cat,
                name=name,
                description=e_data.get('description', ''),
                confidence=int(e_data.get('confidence', 60)),
                source='ai',
            )
            db.session.add(entry)
            created += 1

        db.session.commit()
        return jsonify({'created': created})
    except Exception as ex:
        log.error(f'AI Wiki Generate Error: {ex}')
        return jsonify({'error': str(ex)}), 500

# ═══════════════════════════════════════════════════════════════════════════════
# TEMPLATE API
# ═══════════════════════════════════════════════════════════════════════════════

def _validate_pil_config(cfg):
    """Prüft eine pil_config (dict oder JSON-Text) über memeos_render.validate_config.
    Liefert eine Liste deutscher Fehlertexte; leer = ok. Ohne memeos_render nur JSON-Syntaxprüfung."""
    if memeos_render is None:
        if isinstance(cfg, str):
            try:
                json.loads(cfg)
            except Exception as ex:
                return [f'pil_config ist kein gültiges JSON: {ex}']
        return []
    try:
        return list(memeos_render.validate_config(cfg) or [])
    except Exception as ex:
        return [f'pil_config nicht prüfbar: {ex}']


def _tmpl_dict(t):
    cat_row = TemplateCategory.query.filter_by(key=t.category).first()
    cat_info = (cat_row.label, cat_row.emoji, cat_row.group) if cat_row else TEMPLATE_CAT_MAP.get(t.category, (t.category, '', ''))
    return {
        'id': t.id, 'name': t.name, 'description': t.description,
        'canva_url': t.canva_url or '',
        'render_type': t.render_type if t.render_type in ('pil', 'manual') else 'pil',
        'pil_config': t.pil_config or '{}',
        'required_vars': t.get_required_vars(),
        'tags': t.get_tags(),
        'category': t.category,
        'category_label': cat_info[0],
        'category_emoji': cat_info[1],
        'category_group': cat_info[2] if len(cat_info) > 2 else '',
        'rating': t.rating or 0,
        'preview_image': t.preview_image,
        'preview_url': t.preview_url or (f'/uploads/{t.preview_image}' if t.preview_image else ''),
        'example_text': t.example_text,
        'notes': t.notes or '',
        'has_canva': t.has_canva(),   # bedeutet: Canva-Link hinterlegt (kein Autofill mehr)
        'use_count': t.use_count,
        'active': t.active,
        'seasonal_from': t.seasonal_from or '',
        'seasonal_to': t.seasonal_to or '',
        'min_population': t.min_population,
        'series': t.series or '',
        'series_position': t.series_position,
    }

@app.route('/api/templates', methods=['GET'])
@login_required
def api_templates_list():
    templates = MemeTemplate.query.order_by(MemeTemplate.name).all()
    return jsonify([_tmpl_dict(t) for t in templates])

@app.route('/api/templates', methods=['POST'])
@login_required
def api_template_create():
    d = request.json or {}
    if not d.get('name'):
        return jsonify({'error': 'Name fehlt'}), 400
    render_type = d.get('render_type') or 'pil'
    if render_type not in ('pil', 'manual'):
        return jsonify({'error': "render_type muss 'pil' oder 'manual' sein"}), 400
    if d.get('pil_config'):
        errors = _validate_pil_config(d['pil_config'])
        if errors:
            return jsonify({'error': 'pil_config ungültig', 'details': errors}), 400
    t = MemeTemplate(
        name=d['name'].strip(),
        description=d.get('description', ''),
        canva_url=d.get('canva_url', ''),
        render_type=render_type,
        required_vars=json.dumps(d.get('required_vars', [])),
        tags=json.dumps(d.get('tags', [])),
        category=d.get('category', 'allgemein'),
        rating=int(d.get('rating', 0)),
        example_text=d.get('example_text', ''),
        notes=d.get('notes', ''),
        seasonal_from=d.get('seasonal_from', ''),
        seasonal_to=d.get('seasonal_to', ''),
        min_population=int(d.get('min_population', 0)),
        series=d.get('series', '') or None,
        series_position=int(d['series_position']) if d.get('series_position') else None,
    )
    if d.get('pil_config'):
        t.pil_config = json.dumps(d['pil_config']) if isinstance(d['pil_config'], dict) else d['pil_config']
    db.session.add(t)
    db.session.commit()
    return jsonify({'id': t.id, 'template': _tmpl_dict(t)}), 201

@app.route('/api/templates/<int:tmpl_id>', methods=['PUT'])
@login_required
def api_template_update(tmpl_id):
    t = MemeTemplate.query.get_or_404(tmpl_id)
    d = request.json or {}
    if 'render_type' in d and d['render_type'] not in ('pil', 'manual'):
        return jsonify({'error': "render_type muss 'pil' oder 'manual' sein"}), 400
    for field in ['name','description','canva_url','render_type',
                  'category','rating','example_text','notes',
                  'seasonal_from','seasonal_to','min_population','active',
                  'series','series_position']:
        if field in d:
            setattr(t, field, d[field])
    if 'required_vars' in d:
        t.required_vars = json.dumps(d['required_vars'])
    if 'tags' in d:
        t.tags = json.dumps(d['tags'])
    if 'pil_config' in d:
        errors = _validate_pil_config(d['pil_config'])
        if errors:
            return jsonify({'error': 'pil_config ungültig', 'details': errors}), 400
        t.pil_config = json.dumps(d['pil_config']) if isinstance(d['pil_config'], dict) else d['pil_config']
    db.session.commit()
    return jsonify({'ok': True, 'template': _tmpl_dict(t)})

@app.route('/api/templates/<int:tmpl_id>/rate', methods=['POST'])
@login_required
def api_template_rate(tmpl_id):
    t = MemeTemplate.query.get_or_404(tmpl_id)
    stars = int((request.json or {}).get('rating', 0))
    if stars < 0 or stars > 5:
        return jsonify({'error': 'Rating 0-5'}), 400
    t.rating = stars
    db.session.commit()
    return jsonify({'ok': True, 'rating': t.rating})

# ── TEMPLATE CATEGORIES ──────────────────────────────────────────────────────

@app.route('/api/template-categories', methods=['GET'])
@login_required
def api_tcat_list():
    cats = TemplateCategory.query.order_by(TemplateCategory.sort_order, TemplateCategory.label).all()
    return jsonify([c.to_dict() for c in cats])

@app.route('/api/template-categories', methods=['POST'])
@login_required
def api_tcat_create():
    d = request.json or {}
    key = (d.get('key') or '').strip().lower().replace(' ', '_')
    if not key or not d.get('label'):
        return jsonify({'error': 'key und label erforderlich'}), 400
    if TemplateCategory.query.filter_by(key=key).first():
        return jsonify({'error': f'Kategorie "{key}" existiert bereits'}), 409
    max_order = db.session.query(db.func.max(TemplateCategory.sort_order)).scalar() or 0
    c = TemplateCategory(
        key=key, label=d['label'].strip(),
        emoji=d.get('emoji', '📋'),
        group=d.get('group', 'format'),
        sort_order=max_order + 10,
    )
    db.session.add(c)
    db.session.commit()
    return jsonify(c.to_dict()), 201

@app.route('/api/template-categories/<int:cat_id>', methods=['PUT'])
@login_required
def api_tcat_update(cat_id):
    c = TemplateCategory.query.get_or_404(cat_id)
    d = request.json or {}
    for f in ['label', 'emoji', 'group', 'sort_order', 'active']:
        if f in d:
            setattr(c, f, d[f])
    db.session.commit()
    return jsonify(c.to_dict())

@app.route('/api/template-categories/<int:cat_id>', methods=['DELETE'])
@login_required
def api_tcat_delete(cat_id):
    c = TemplateCategory.query.get_or_404(cat_id)
    in_use = MemeTemplate.query.filter_by(category=c.key).count()
    if in_use:
        return jsonify({'error': f'Wird von {in_use} Template(s) verwendet — erst Templates umkategorisieren'}), 409
    db.session.delete(c)
    db.session.commit()
    return jsonify({'ok': True})

# ── TEMPLATE DELETE ───────────────────────────────────────────────────────────

@app.route('/api/templates/<int:tmpl_id>', methods=['DELETE'])
@login_required
def api_template_delete(tmpl_id):
    t = MemeTemplate.query.get_or_404(tmpl_id)
    db.session.delete(t)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/templates/<int:tmpl_id>/upload-preview', methods=['POST'])
@login_required
def api_template_upload_preview(tmpl_id):
    t = MemeTemplate.query.get_or_404(tmpl_id)
    if 'file' not in request.files:
        return jsonify({'error': 'Keine Datei'}), 400
    f = request.files['file']
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'jpg'
    if ext not in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
        return jsonify({'error': 'Nur Bilder erlaubt'}), 400
    filename = f'template_{tmpl_id}_{int(time.time())}.{ext}'
    local_path = os.path.join(_UPLOAD_DIR, filename)
    f.save(local_path)
    t.preview_image = filename
    # Zusätzlich nach Cloudinary, damit der Hintergrund einen Deploy überlebt
    cloud_url = _upload_cloudinary(local_path, folder='memeos/templates', resource_type='image')
    if cloud_url:
        t.preview_url = cloud_url
    db.session.commit()
    return jsonify({'filename': filename, 'preview_url': t.preview_url or f'/uploads/{filename}',
                    'cloud': bool(cloud_url)})

# ═══════════════════════════════════════════════════════════════════════════════
# GENERATOR API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/generate', methods=['POST'])
@login_required
def api_generate():
    d = request.json or {}
    template_id = d.get('template_id')
    city_id     = d.get('city_id')
    if not template_id or not city_id:
        return jsonify({'error': 'template_id und city_id erforderlich'}), 400

    t    = MemeTemplate.query.get_or_404(template_id)
    city = City.query.get_or_404(city_id)

    job = RenderJob(template_id=t.id, city_id=city.id, status='pending')
    db.session.add(job)
    db.session.commit()

    thread = threading.Thread(target=_run_generate_job, args=(app, job.id), daemon=True)
    thread.start()

    return jsonify({'job_id': job.id, 'status': 'pending'})

@app.route('/api/generate/bulk', methods=['POST'])
@login_required
def api_generate_bulk():
    d = request.json or {}
    template_id = d.get('template_id')
    city_ids    = d.get('city_ids', [])
    if not template_id or not city_ids:
        return jsonify({'error': 'template_id und city_ids erforderlich'}), 400

    MemeTemplate.query.get_or_404(template_id)
    job_ids = []
    for cid in city_ids:
        city = City.query.get(cid)
        if not city:
            continue
        job = RenderJob(template_id=template_id, city_id=cid, status='pending')
        db.session.add(job)
        db.session.flush()
        job_ids.append(job.id)
    db.session.commit()

    for jid in job_ids:
        t = threading.Thread(target=_run_generate_job, args=(app, jid), daemon=True)
        t.start()
        time.sleep(0.3)

    return jsonify({'job_ids': job_ids, 'count': len(job_ids)})

@app.route('/api/jobs', methods=['GET'])
@login_required
def api_jobs_list():
    status_filter = request.args.get('status')
    q = RenderJob.query
    if status_filter:
        statuses = status_filter.split(',')
        q = q.filter(RenderJob.status.in_(statuses))
    jobs = q.order_by(RenderJob.created_at.desc()).limit(100).all()
    return jsonify([_job_to_dict(j) for j in jobs])

@app.route('/api/jobs/<int:job_id>', methods=['GET'])
@login_required
def api_job_get(job_id):
    job = RenderJob.query.get_or_404(job_id)
    return jsonify(_job_to_dict(job))

@app.route('/api/jobs/<int:job_id>/review', methods=['POST'])
@login_required
def api_job_review(job_id):
    job = RenderJob.query.get_or_404(job_id)
    d = request.json or {}
    action = d.get('action')  # approve | reject
    if action not in ('approve', 'reject'):
        return jsonify({'error': 'action muss approve oder reject sein'}), 400

    job.status      = 'approved' if action == 'approve' else 'rejected'
    job.review_note = d.get('note', '')
    job.reviewed_at = datetime.utcnow()
    db.session.commit()

    if action == 'approve' and d.get('send_to_content_os'):
        threading.Thread(target=_send_to_content_os, args=(app, job.id), daemon=True).start()

    return jsonify({'ok': True, 'status': job.status})

@app.route('/api/jobs/<int:job_id>/resend', methods=['POST'])
@login_required
def api_job_resend(job_id):
    job = RenderJob.query.get_or_404(job_id)
    threading.Thread(target=_send_to_content_os, args=(app, job.id), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/jobs/<int:job_id>', methods=['DELETE'])
@login_required
def api_job_delete(job_id):
    job = RenderJob.query.get_or_404(job_id)
    if job.image_filename:
        try:
            os.remove(os.path.join(_RENDER_DIR, job.image_filename))
        except Exception:
            pass
    db.session.delete(job)
    db.session.commit()
    return jsonify({'ok': True})

def _job_to_dict(j):
    post = MemePost.query.filter_by(render_job_id=j.id).first()
    return {
        'id': j.id,
        'post_id': post.id if post else None,
        'template_id': j.template_id,
        'template_name': j.template.name if j.template else '',
        'city_id': j.city_id,
        'city_name': j.city.name if j.city else '',
        'status': j.status,
        'status_label': j.status_label,
        'fit_score': j.fit_score,
        'fit_color': j.fit_color,
        'fit_reasoning': j.fit_reasoning,
        'vars_used': j.get_vars(),
        'manual_brief': j.manual_brief,
        'image_filename': j.image_filename,
        'image_url': url_for('serve_render', filename=j.image_filename) if j.image_filename else None,
        'error_message': j.error_message,
        'review_note': j.review_note,
        'sent_to_content_os': j.sent_to_content_os,
        'created_at': j.created_at.isoformat() if j.created_at else None,
        'completed_at': j.completed_at.isoformat() if j.completed_at else None,
    }

# ═══════════════════════════════════════════════════════════════════════════════
# GENERATE LOGIC (Background Thread)
# ═══════════════════════════════════════════════════════════════════════════════

# Höchstens 4 Render-Jobs gleichzeitig (Bulk-Generator startet weiterhin je einen Thread).
# Ein persistentes Queue-System folgt in Phase B – hier reicht die Bremse.
_render_semaphore = threading.Semaphore(4)


def _save_render(png_bytes, filename):
    """PNG in den Render-Ordner schreiben, optional nach Cloudinary. → (lokaler Pfad, cloud_url|None)"""
    path = os.path.join(_RENDER_DIR, filename)
    with open(path, 'wb') as f:
        f.write(png_bytes)
    cloud_url = _upload_cloudinary(path, folder='memeos/renders')
    return path, cloud_url


def _ensure_post_for_job(job, city, template, filename, cloud_url, result):
    """Nach erfolgreichem Render einen Vorrats-Eintrag anlegen, falls für diesen Job noch keiner existiert."""
    if MemePost.query.filter_by(render_job_id=job.id).first():
        return None
    notes = {
        'fit_score': result.get('fit_score'),
        'reasoning': result.get('reasoning') or '',
        'vars': result.get('vars') or {},
    }
    post = MemePost(
        city_id=city.id,
        render_job_id=job.id,
        template_id=template.id,
        title=f'{city.name} – {template.name}',
        image_path=filename,
        image_url=cloud_url or ('/renders/' + filename),
        post_type='feed',
        status='entwurf',
        notes=json.dumps(notes, ensure_ascii=False),
    )
    db.session.add(post)
    return post


def _run_generate_job(flask_app, job_id):
    with _render_semaphore:
        with flask_app.app_context():
            job = RenderJob.query.get(job_id)
            if not job:
                return
            job.status = 'running'
            db.session.commit()
            try:
                template = job.template
                city     = job.city

                # 1) Fit-Score + Variablen via Claude (ehrlich: Fehler → failed, kein Rendern)
                result = _claude_fit_and_vars(city, template)
                job.fit_score     = result.get('fit_score')
                job.fit_reasoning = result.get('reasoning') or ''
                job.vars_used     = json.dumps(result.get('vars') or {}, ensure_ascii=False)
                job.manual_brief  = result.get('brief') or ''

                if result.get('error'):
                    job.status = 'failed'
                    job.error_message = result['error']
                    job.completed_at = datetime.utcnow()
                    db.session.commit()
                    return

                # Hinweis (z. B. "Kein Anthropic-Key: nur Stadtname eingesetzt") landet in error_message,
                # blockiert das Rendern aber nicht.
                warning = result.get('warning')

                # 2) Rendern – Weg hängt vom render_type ab
                render_type = template.render_type if template.render_type in ('pil', 'manual') else 'pil'
                if render_type == 'manual':
                    job.status = 'done'
                    job.error_message = 'Manuelles Template: nur Brief'
                else:
                    png_bytes = None
                    render_error = None
                    if _template_bg_path(template):
                        png_bytes = _pil_render(template, result.get('vars') or {}, city=city)
                        if not png_bytes:
                            render_error = 'PIL-Rendering fehlgeschlagen — nur Brief verfügbar'
                    else:
                        render_error = 'Kein Template-Bild hochgeladen — nur Brief verfügbar'

                    if png_bytes:
                        filename = f'render_{job.id}_{int(time.time())}.png'
                        _, cloud_url = _save_render(png_bytes, filename)
                        job.image_filename = filename
                        if cloud_url:
                            job.image_url = cloud_url
                            log.info(f'Job {job_id}: uploaded to Cloudinary → {cloud_url}')
                        job.status = 'done'
                        job.error_message = warning
                        _ensure_post_for_job(job, city, template, filename, cloud_url, result)
                    else:
                        job.status = 'done'
                        job.error_message = '; '.join(x for x in (render_error, warning) if x)

                job.completed_at = datetime.utcnow()

                # Verwendete Knowledge-Einträge markieren + Template use_count erhöhen
                _mark_knowledge_used(city.id, result.get('vars') or {}, template.id)
                template.use_count = (template.use_count or 0) + 1
                db.session.commit()

            except Exception as ex:
                log.error(f'Generate Job {job_id} Error: {ex}')
                db.session.rollback()
                job = RenderJob.query.get(job_id)
                if job:
                    job.status = 'failed'
                    job.error_message = str(ex)
                    job.completed_at = datetime.utcnow()
                    db.session.commit()


def _carousel_templates(series=None, category=None):
    """Aktive Templates einer Serie (nach series_position), Fallback Kategorie (nach Name)."""
    q = MemeTemplate.query.filter_by(active=True)
    if series:
        return (q.filter(MemeTemplate.series == series)
                 .order_by(MemeTemplate.series_position.is_(None), MemeTemplate.series_position, MemeTemplate.name)
                 .all())
    return q.filter_by(category=category).order_by(MemeTemplate.name).all()


def _generate_carousel(flask_app, post_id, series=None, category=None):
    """Rendert alle Templates einer Serie (Fallback: Kategorie) für eine Stadt und speichert sie als
    EIN MemePost mit post_type='carousel'. Jeder Slide bekommt dasselbe Vars-Ergebnis wie ein
    Einzel-Render desselben Templates (_claude_fit_and_vars)."""
    with _render_semaphore:
        with flask_app.app_context():
            post = MemePost.query.get(post_id)
            if not post:
                return
            try:
                city = post.city
                templates = _carousel_templates(series=series, category=category)
                paths = post.get_carousel_paths()  # manuell vorangestellte Slides bleiben erhalten
                slide_results = []
                rendered = 0
                ki_errors = 0

                for template in templates:
                    result = _claude_fit_and_vars(city, template)
                    entry = {'template_id': template.id, 'template': template.name,
                             'fit_score': result.get('fit_score'),
                             'reasoning': result.get('reasoning') or '',
                             'vars': result.get('vars') or {}}
                    if result.get('error'):
                        ki_errors += 1
                        entry['error'] = result['error']
                        slide_results.append(entry)
                        continue
                    if result.get('warning'):
                        entry['warning'] = result['warning']
                    render_type = template.render_type if template.render_type in ('pil', 'manual') else 'pil'
                    png_bytes = None
                    if render_type == 'pil' and _template_bg_path(template):
                        png_bytes = _pil_render(template, result.get('vars') or {}, city=city)
                    if png_bytes:
                        filename = f'carousel_{post.id}_{template.id}_{int(time.time())}.png'
                        _save_render(png_bytes, filename)
                        paths.append(filename)
                        rendered += 1
                        _mark_knowledge_used(city.id, result.get('vars') or {}, template.id)
                        template.use_count = (template.use_count or 0) + 1
                    else:
                        entry['error'] = ('Manuelles Template: nur Brief' if render_type == 'manual'
                                          else 'Kein Hintergrundbild / PIL-Rendering fehlgeschlagen')
                    slide_results.append(entry)

                post.carousel_paths = json.dumps(paths)
                post.notes = json.dumps({'series': series, 'category': category,
                                         'slides': slide_results}, ensure_ascii=False)
                if paths and not post.image_path:
                    post.image_path = paths[0]
                    post.image_url = post.image_url or slide_url(paths[0])
                if rendered == 0 and (ki_errors or not templates or not paths):
                    post.status = 'failed'
                else:
                    post.status = 'entwurf'
                db.session.commit()
                log.info(f'Carousel Post {post_id}: {rendered} Slides gerendert, {ki_errors} KI-Fehler, Status {post.status}')
            except Exception as ex:
                log.error(f'Carousel Post {post_id} Error: {ex}')
                db.session.rollback()
                post = MemePost.query.get(post_id)
                if post:
                    post.status = 'failed'
                    post.notes = json.dumps({'error': str(ex)}, ensure_ascii=False)
                    db.session.commit()


@app.route('/api/generate/carousel', methods=['POST'])
@login_required
def api_generate_carousel():
    """Erzeugt EIN Karussell aus allen aktiven Templates einer Serie (MemeTemplate.series) für eine Stadt.
    Body: {city_id, series} – 'category' wird für Altaufrufer weiterhin akzeptiert.
    Optional: manual_paths (bereits hochgeladene Slide-Dateinamen), die vor den generierten Slides stehen."""
    d = request.json or {}
    city_id  = d.get('city_id')
    series   = (d.get('series') or '').strip() or None
    category = (d.get('category') or '').strip() or None
    if not city_id or not (series or category):
        return jsonify({'error': 'city_id und series (oder category) erforderlich'}), 400
    city = City.query.get_or_404(city_id)

    templates = _carousel_templates(series=series, category=category)
    if not templates:
        return jsonify({'error': f'Keine aktiven Templates für {"Serie" if series else "Kategorie"} „{series or category}“'}), 404

    manual_paths = d.get('manual_paths', [])

    post = MemePost(
        city_id=city.id,
        title=f'{city.name} – {series or category}',
        post_type='carousel',
        status='rendering',
        carousel_paths=json.dumps(manual_paths),
    )
    db.session.add(post)
    db.session.commit()

    thread = threading.Thread(target=_generate_carousel, args=(app, post.id, series, category), daemon=True)
    thread.start()

    return jsonify({'post_id': post.id, 'status': 'rendering', 'template_count': len(templates)})


@app.route('/api/templates/series', methods=['GET'])
@login_required
def api_template_series():
    """Alle Serien mit Anzahl aktiver Templates – für die Karussell-Auswahl."""
    rows = (db.session.query(MemeTemplate.series, db.func.count(MemeTemplate.id))
            .filter(MemeTemplate.active == True, MemeTemplate.series.isnot(None), MemeTemplate.series != '')
            .group_by(MemeTemplate.series).order_by(MemeTemplate.series).all())
    return jsonify([{'series': s, 'template_count': n} for s, n in rows])


@app.route('/api/posts/<int:post_id>/carousel', methods=['GET'])
@login_required
def api_carousel_get(post_id):
    post = MemePost.query.get_or_404(post_id)
    slides = post.get_slides() if post.post_type == 'carousel' else []
    return jsonify({
        'id': post.id,
        'city_name': post.city.name if post.city else '',
        'status': post.status,
        'title': post.title,
        'image_urls': [s['url'] for s in slides],
        'slides': slides,
        'slide_count': len(slides),
        'notes': post.notes or '',
    })


def _send_carousel_to_content_os(flask_app, post_id):
    with flask_app.app_context():
        post = MemePost.query.get(post_id)
        if not post or not CONTENT_OS_URL:
            return
        paths = post.get_carousel_paths()
        if not paths:
            return
        try:
            meta = {
                'title':   post.title,
                'city':    post.city.name if post.city else '',
                'caption': post.caption or '',
                'source':  'memeos',
            }
            headers = {}
            if CONTENT_OS_KEY:
                headers['X-MemeOS-Key'] = CONTENT_OS_KEY

            files = []
            opened = []
            for p in paths:
                img_path = _local_media_path(p)
                if img_path:
                    fh = open(img_path, 'rb')
                    opened.append(fh)
                    files.append(('images', (os.path.basename(img_path), fh, 'image/png')))

            r = requests.post(
                CONTENT_OS_URL.rstrip('/') + '/api/memeos/receive',
                files=files, data={'meta': json.dumps(meta)},
                headers=headers, timeout=45,
            )
            for fh in opened:
                fh.close()

            if r.ok:
                post.status = 'bereit'
                db.session.commit()
                log.info(f'Carousel Post {post_id}: an Content OS gesendet ({len(paths)} Bilder)')
            else:
                log.warning(f'ContentOS Carousel Bridge Error: {r.status_code} {r.text[:150]}')
        except Exception as ex:
            log.error(f'Carousel Post {post_id} Sende-Fehler: {ex}')


@app.route('/api/posts/<int:post_id>/send-to-content-os', methods=['POST'])
@login_required
def api_carousel_send(post_id):
    post = MemePost.query.get_or_404(post_id)
    if not post.get_carousel_paths():
        return jsonify({'ok': False, 'error': 'Karussell hat keine Bilder'}), 400
    thread = threading.Thread(target=_send_carousel_to_content_os, args=(app, post.id), daemon=True)
    thread.start()
    return jsonify({'ok': True, 'status': 'sending'})


def _claude_fit_and_vars(city, template):
    """Fit-Score + Variablenwerte für Stadt × Template.

    Rückgabe immer ein Dict mit fit_score (int|None), reasoning, vars (dict), brief.
    - Ohne API-Key: kein Fehler, aber nur city_name wird belegt; 'warning' erklärt das.
    - KI nicht erreichbar / unbrauchbare Antwort (nach einem Retry): 'error' gesetzt, fit_score None.
    """
    required_vars = template.get_required_vars()

    if not ANTHROPIC_API_KEY:
        return {
            'fit_score': None,
            'reasoning': 'Kein Anthropic-Key',
            'vars': {v: city.name for v in required_vars if v == 'city_name'},
            'brief': '',
            'warning': 'Kein Anthropic-Key: nur Stadtname eingesetzt',
        }

    now = datetime.utcnow()
    knowledge = CityKnowledge.query.filter_by(city_id=city.id, active=True)\
                    .filter(db.or_(CityKnowledge.cooldown_until.is_(None),
                                   CityKnowledge.cooldown_until < now))\
                    .order_by(CityKnowledge.confidence.desc()).all()

    knowledge_str = '\n'.join([
        f"- [{e.category}] {e.name} (Konfidenz: {e.confidence}, Quelle: {e.source})"
        + (f": {e.description}" if e.description else '')
        for e in knowledge
    ]) or 'Keine Knowledge-Einträge vorhanden'

    vars_str = ', '.join(required_vars) if required_vars else 'keine'

    prompt = f"""Du bist Meme-Experte für deutsche Stadtseiten auf Instagram.

Stadt: {city.name} ({city.state}, ~{city.population or '?'} Einwohner)

Meme-Template: {template.name}
Beschreibung: {template.description or 'keine'}
Beispiel-Text: {template.example_text or 'keiner'}
Benötigte Variablen: {vars_str}

Stadt-Wissen ({city.name}):
{knowledge_str}

Bewerte:
1. Wie gut passt dieses Template zu {city.name}? (fit_score: 0–100)
   < 40 = passt nicht, 40–70 = okay, > 70 = sehr gut
2. Welche konkreten Werte sollen für die Variablen eingesetzt werden?
3. Schreibe einen kurzen "Manual Brief" für den Fall dass kein Canva-Template vorhanden ist
   (was soll der Meme-Creator machen?)

Antworte NUR mit JSON:
{{
  "fit_score": <Zahl 0-100>,
  "reasoning": "<kurze Begründung, max 100 Zeichen>",
  "vars": {{"variable_name": "konkreter Wert", ...}},
  "brief": "<was soll der Creator machen, max 200 Zeichen>"
}}"""

    last_error = ''
    for attempt in range(2):                     # ein Retry nach 2 s
        if attempt:
            time.sleep(2)
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            msg = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=500,
                messages=[{'role': 'user', 'content': prompt}]
            )
            raw = msg.content[0].text.strip()
            _log_ai_usage('fit_score', 'claude-haiku-4-5-20251001',
                          msg.usage.input_tokens, msg.usage.output_tokens)
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if not match:
                last_error = 'Antwort ohne JSON'
                continue
            data = json.loads(match.group(0))
            vars_dict = data.get('vars') if isinstance(data.get('vars'), dict) else {}
            vars_dict = {str(k): str(v) for k, v in vars_dict.items() if v is not None}
            # city_name immer sicher belegen, falls verlangt
            if 'city_name' in required_vars and not vars_dict.get('city_name'):
                vars_dict['city_name'] = city.name
            try:
                fit = int(data.get('fit_score'))
                fit = max(0, min(100, fit))
            except Exception:
                fit = None
            return {
                'fit_score': fit,
                'reasoning': str(data.get('reasoning') or '')[:500],
                'vars': vars_dict,
                'brief': str(data.get('brief') or '')[:1000],
            }
        except Exception as ex:
            last_error = f'{type(ex).__name__}: {str(ex)[:120]}'
            log.error(f'Claude Fit-Score Error (Versuch {attempt + 1}): {ex}')

    return {'error': f'KI nicht erreichbar: {last_error or "unbekannt"}',
            'fit_score': None, 'reasoning': '', 'vars': {}, 'brief': ''}


def _mark_knowledge_used(city_id, vars_dict, template_id):
    for category, value in vars_dict.items():
        entry = CityKnowledge.query.filter_by(
            city_id=city_id, category=category, name=value, active=True
        ).first()
        if entry:
            entry.used_count  = (entry.used_count or 0) + 1
            entry.last_used_at = datetime.utcnow()
            entry.cooldown_until = datetime.utcnow() + timedelta(days=14)
    db.session.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# CANVA API
# ═══════════════════════════════════════════════════════════════════════════════

def _canva_get_token():
    if not CANVA_CLIENT_ID or not CANVA_CLIENT_SECRET:
        return None
    tokens = _canva_load_tokens()
    access_token = tokens.get('access_token')
    expires_at   = tokens.get('expires_at', '')
    try:
        if access_token and expires_at:
            if datetime.fromisoformat(expires_at) > datetime.now() + timedelta(minutes=5):
                return access_token
    except Exception:
        pass
    refresh_token = tokens.get('refresh_token') or AppSettings.get('canva_refresh_token_backup')
    if not refresh_token:
        return None
    try:
        r = requests.post('https://api.canva.com/rest/v1/oauth/token', data={
            'grant_type':    'refresh_token',
            'refresh_token': refresh_token,
            'client_id':     CANVA_CLIENT_ID,
            'client_secret': CANVA_CLIENT_SECRET,
        }, timeout=15)
        if r.ok:
            data = r.json()
            new_tokens = {
                'access_token':  data.get('access_token'),
                'refresh_token': data.get('refresh_token', refresh_token),
                'expires_at':    (datetime.now() + timedelta(seconds=data.get('expires_in', 3600))).isoformat(),
            }
            _canva_save_tokens(new_tokens)
            return new_tokens['access_token']
    except Exception as ex:
        log.warning(f'Canva Token Refresh Error: {ex}')
    return None


def _canva_is_connected():
    if not CANVA_CLIENT_ID or not CANVA_CLIENT_SECRET:
        return False
    if AppSettings.get('canva_explicitly_disconnected') == '1':
        return False
    tokens = _canva_load_tokens()
    access_token = tokens.get('access_token')
    expires_at   = tokens.get('expires_at', '')
    try:
        if access_token and expires_at:
            if datetime.fromisoformat(expires_at) > datetime.now() + timedelta(minutes=5):
                return True
    except Exception:
        pass
    return bool(tokens.get('refresh_token') or AppSettings.get('canva_refresh_token_backup'))


def _canva_load_tokens():
    raw = AppSettings.get('canva_tokens', '{}')
    try: return json.loads(raw)
    except: return {}


def _canva_save_tokens(tokens):
    AppSettings.set('canva_tokens', json.dumps(tokens))


# Canva-Autofill wurde entfernt (braucht Canva Enterprise). Die OAuth-Verbindung bleibt für die
# Export-API (PNG/PPTX), die der Template-Import in Phase B nutzt – Scopes: design:content:read design:meta:read.


# ═══════════════════════════════════════════════════════════════════════════════
# PIL RENDERER (lokale Alternative zu Canva — kein Brand-Template nötig)
# ═══════════════════════════════════════════════════════════════════════════════

_FONTS_DIR = os.path.join(_BASE_DIR, 'fonts')   # mitgelieferte Schriften (Repo)
_FONT_FILES = {
    'anton': 'anton.ttf',   # Bebas-Neue-artige Headline-Schrift
    'bold':  'bold.ttf',    # Oswald Bold
}
_font_cache = {}

def _font_path(name):
    """Schriftdatei suchen: erst <DATA_ROOT>/fonts (eigene Uploads), dann Repo-Ordner fonts/.
    Unbekannte Namen werden als <name>.ttf/.otf probiert; Fallback anton.ttf."""
    candidates = []
    mapped = _FONT_FILES.get(name)
    if mapped:
        candidates.append(mapped)
    safe = os.path.basename(str(name or ''))
    if safe:
        candidates += [safe, f'{safe}.ttf', f'{safe}.otf']
    candidates.append(_FONT_FILES['anton'])
    for folder in (_FONTS_DATA_DIR, _FONTS_DIR):
        for fn in candidates:
            p = os.path.join(folder, fn)
            if os.path.isfile(p):
                return p
    return None

def _pil_font(name, size):
    key = (name, size)
    if key not in _font_cache:
        path = _font_path(name)
        try:
            _font_cache[key] = ImageFont.truetype(path, size) if path else ImageFont.load_default()
        except Exception:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def _pil_fit_text(draw, text, font_name, max_size, min_size, max_width, max_height=None):
    """Größtmögliche Fontgröße finden, bei der `text` (mit Zeilenumbruch) in max_width (und optional max_height) passt."""
    size = max_size
    while size >= min_size:
        font = _pil_font(font_name, size)
        words = text.split()
        lines, cur = [], ''
        for w in words:
            trial = f'{cur} {w}'.strip()
            if draw.textlength(trial, font=font) <= max_width:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        line_height = font.size * 1.15
        total_height = line_height * len(lines)
        if max_height is None or total_height <= max_height:
            return font, lines, line_height
        size -= 2
    font = _pil_font(font_name, min_size)
    return font, [text], font.size * 1.15


def _template_bg_path(template):
    """Lokaler Pfad des Template-Hintergrunds. Liegt die Datei nicht (mehr) im Upload-Ordner
    (z. B. nach einem Deploy), wird sie einmal von template.preview_url heruntergeladen."""
    name = os.path.basename(template.preview_image or '')
    if name:
        local = os.path.join(_UPLOAD_DIR, name)
        if os.path.exists(local):
            return local
    url = (template.preview_url or '').strip()
    if not url.startswith('http'):
        return None
    if not name:
        ext = url.split('?')[0].rsplit('.', 1)[-1].lower()
        ext = ext if ext in ('png', 'jpg', 'jpeg', 'webp', 'gif') else 'png'
        name = f'template_{template.id}_bg.{ext}'
    local = os.path.join(_UPLOAD_DIR, name)
    try:
        resp = requests.get(url, timeout=20)
        if not resp.ok or not resp.content:
            log.warning(f'Template {template.id}: Hintergrund von {url} nicht ladbar ({resp.status_code})')
            return None
        with open(local, 'wb') as fh:
            fh.write(resp.content)
        if template.preview_image != name:
            template.preview_image = name
            db.session.commit()
        return local
    except Exception as ex:
        log.warning(f'Template {template.id}: Hintergrund-Download fehlgeschlagen: {ex}')
        return None


def _city_brand(city):
    """Brandfarben/-schrift einer Stadt als Dict für Renderer und Frontend."""
    return {
        'bg':     (city.brand_bg if city and city.brand_bg else '#ffffff'),
        'text':   (city.brand_text_color if city and city.brand_text_color else '#000000'),
        'accent': (city.accent_color if city and city.accent_color else '#3b82f6'),
        'font':   (city.brand_font if city and city.brand_font else 'Arial'),
    }


def _pil_render(template, vars_dict, city=None):
    """Rendert ein Template lokal mit Pillow. Delegiert an memeos_render (Phase B, Element-Renderer
    mit text/image/cover/rect, brand:-Farben); ohne das Modul greift die alte Implementierung.
    Rückgabe: PNG-Bytes oder None (kein Hintergrund)."""
    if memeos_render is None:
        return _pil_render_legacy(template, vars_dict)
    src_path = _template_bg_path(template)
    if not src_path:
        return None
    try:
        config = json.loads(template.pil_config or '{}')
    except Exception:
        config = {}
    try:
        return memeos_render.render(src_path, config, vars_dict or {},
                                    brand=_city_brand(city) if city else None)
    except FileNotFoundError as ex:
        log.warning(f'PIL Render Template {template.id}: {ex}')
        return None


def _pil_render_legacy(template, vars_dict):
    """Alte Pillow-Implementierung (nur Text/Bild-Elemente); Fallback ohne memeos_render.

    pil_config-Format:
    {
      "elements": [
        {"var": "problem_place", "type": "text", "x": 50, "y": 300, "width": 600,
         "height": 200, "font": "anton", "max_size": 64, "min_size": 24,
         "color": "#FFFFFF", "align": "left", "stroke": "#000000", "stroke_width": 2},
        {"var": "city_logo", "type": "image", "x": 20, "y": 20, "width": 120, "height": 120}
      ]
    }
    """
    src_path = _template_bg_path(template)
    if not src_path:
        return None

    try:
        config = json.loads(template.pil_config or '{}')
    except Exception:
        config = {}
    elements = config.get('elements', [])

    img = Image.open(src_path).convert('RGBA')
    draw = ImageDraw.Draw(img)

    for el in elements:
        var_key = el.get('var')
        value = vars_dict.get(var_key)
        if value is None:
            continue
        el_type = el.get('type', 'text')
        x, y = int(el.get('x', 0)), int(el.get('y', 0))

        if el_type == 'text':
            width  = int(el.get('width', img.width - x))
            height = el.get('height')
            height = int(height) if height else None
            font, lines, line_height = _pil_fit_text(
                draw, str(value),
                font_name=el.get('font', 'anton'),
                max_size=int(el.get('max_size', 64)),
                min_size=int(el.get('min_size', 24)),
                max_width=width, max_height=height,
            )
            color        = el.get('color', '#FFFFFF')
            stroke       = el.get('stroke')
            stroke_width = int(el.get('stroke_width', 0))
            align        = el.get('align', 'left')
            cy = y
            for line in lines:
                line_width = draw.textlength(line, font=font)
                if align == 'center':
                    lx = x + (width - line_width) / 2
                elif align == 'right':
                    lx = x + width - line_width
                else:
                    lx = x
                draw.text((lx, cy), line, font=font, fill=color,
                          stroke_width=stroke_width, stroke_fill=stroke)
                cy += line_height

        elif el_type == 'image':
            # value = URL oder lokaler Pfad zu einem austauschbaren Bildelement (z. B. Stadt-Logo)
            width  = int(el.get('width', 100))
            height = int(el.get('height', 100))
            try:
                if str(value).startswith('http'):
                    resp = requests.get(value, timeout=10)
                    overlay = Image.open(io.BytesIO(resp.content)).convert('RGBA')
                else:
                    overlay = Image.open(value).convert('RGBA')
                overlay = overlay.resize((width, height))
                img.paste(overlay, (x, y), overlay)
            except Exception as ex:
                log.warning(f'PIL Render: Bildelement {var_key} fehlgeschlagen: {ex}')

    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='PNG')
    return buf.getvalue()


@app.route('/canva/connect')
@login_required
def canva_connect():
    if not CANVA_CLIENT_ID:
        flash('CANVA_CLIENT_ID nicht gesetzt', 'danger')
        return redirect(url_for('dashboard'))
    code_verifier  = secrets.token_urlsafe(64)
    code_challenge = hashlib.sha256(code_verifier.encode()).digest()
    import base64
    code_challenge_b64 = base64.urlsafe_b64encode(code_challenge).rstrip(b'=').decode()
    session['canva_code_verifier'] = code_verifier
    params = {
        'client_id':              CANVA_CLIENT_ID,
        'redirect_uri':           CANVA_REDIRECT_URI,
        'response_type':          'code',
        'scope':                  'design:content:read design:meta:read',   # Export-API (PNG/PPTX) für den Template-Import
        'code_challenge':         code_challenge_b64,
        'code_challenge_method':  'S256',
        'state':                  'memeos_canva_auth',
    }
    url = 'https://www.canva.com/api/oauth/authorize?' + urllib.parse.urlencode(params)
    return redirect(url)


@app.route('/canva/callback')
def canva_callback():
    code  = request.args.get('code')
    error = request.args.get('error')
    if error or not code:
        return redirect('/?tab=settings&canva=error')
    code_verifier = session.pop('canva_code_verifier', '')
    try:
        token_data = {
            'grant_type':    'authorization_code',
            'code':          code,
            'redirect_uri':  CANVA_REDIRECT_URI,
            'client_id':     CANVA_CLIENT_ID,
            'code_verifier': code_verifier,
        }
        if CANVA_CLIENT_SECRET:
            token_data['client_secret'] = CANVA_CLIENT_SECRET
        r = requests.post('https://api.canva.com/rest/v1/oauth/token', data=token_data, timeout=15)
        if r.ok:
            data = r.json()
            tokens = {
                'access_token':  data.get('access_token'),
                'refresh_token': data.get('refresh_token'),
                'expires_at':    (datetime.now() + timedelta(seconds=data.get('expires_in', 3600))).isoformat(),
            }
            _canva_save_tokens(tokens)
            if data.get('refresh_token'):
                AppSettings.set('canva_refresh_token_backup', data['refresh_token'])
            AppSettings.set('canva_explicitly_disconnected', '0')
            return redirect('/?tab=settings&canva=connected')
    except Exception as ex:
        log.error(f'Canva Callback Error: {ex}')
    return redirect('/?tab=settings&canva=error')


@app.route('/canva/disconnect', methods=['POST'])
@login_required
def canva_disconnect():
    _canva_save_tokens({})
    AppSettings.set('canva_explicitly_disconnected', '1')
    return redirect('/?tab=settings')


@app.route('/api/canva/status')
@login_required
def api_canva_status():
    return jsonify({
        'connected': _canva_is_connected(),
        'client_id_set': bool(CANVA_CLIENT_ID),
    })

# ═══════════════════════════════════════════════════════════════════════════════
# NEWS RSS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/news/fetch', methods=['POST'])
@login_required
def api_news_fetch():
    d = request.json or {}
    city_id = d.get('city_id')
    cities  = [City.query.get_or_404(city_id)] if city_id else City.query.filter_by(active=True).filter(City.rss_url != '').all()

    total = 0
    for city in cities:
        if not city.rss_url:
            continue
        try:
            feed = feedparser.parse(city.rss_url)
            for entry in feed.entries[:20]:
                url  = entry.get('link', '')
                if NewsItem.query.filter_by(url=url).first():
                    continue
                pub = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    import calendar
                    pub = datetime.fromtimestamp(calendar.timegm(entry.published_parsed))
                item = NewsItem(
                    city_id=city.id,
                    headline=entry.get('title', '')[:500],
                    url=url,
                    source_name=feed.feed.get('title', ''),
                    published_at=pub,
                )
                db.session.add(item)
                total += 1
        except Exception as ex:
            log.warning(f'RSS Fetch Error [{city.name}]: {ex}')
    db.session.commit()

    if ANTHROPIC_API_KEY and total > 0:
        threading.Thread(target=_score_news_items, args=(app,), daemon=True).start()

    return jsonify({'fetched': total})


def _score_news_items(flask_app):
    with flask_app.app_context():
        unscoredItems = NewsItem.query.filter_by(status='new').limit(20).all()
        if not unscoredItems:
            return
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        templates = MemeTemplate.query.filter_by(active=True).all()
        templates_str = '\n'.join([f"- ID:{t.id} {t.name}: {t.description or ''}" for t in templates])

        for item in unscoredItems:
            try:
                prompt = f"""Bewerte diese Nachricht für Instagram-Memes einer Stadtseite.

Nachricht: "{item.headline}"
Stadt: {item.city.name}

Verfügbare Meme-Templates:
{templates_str or 'Keine Templates verfügbar'}

Antworte NUR mit JSON:
{{
  "meme_score": <0-100>,
  "reasoning": "<kurze Begründung, max 80 Zeichen>",
  "suggested_template_id": <Template-ID oder null>
}}

meme_score:
- 0-30: ungeeignet (zu lokal, zu langweilig, kein Humor-Potenzial)
- 30-60: möglich
- 60-100: sehr meme-würdig (Skandal, Kurioses, lokales Klischee bestätigt)"""

                msg = client.messages.create(
                    model='claude-haiku-4-5-20251001',
                    max_tokens=200,
                    messages=[{'role': 'user', 'content': prompt}]
                )
                raw = msg.content[0].text.strip()
                _log_ai_usage('news_score', 'claude-haiku-4-5-20251001',
                              msg.usage.input_tokens, msg.usage.output_tokens)
                import re
                match = re.search(r'\{.*\}', raw, re.DOTALL)
                if match:
                    data = json.loads(match.group(0))
                    item.meme_score   = int(data.get('meme_score', 0))
                    item.meme_reasoning = data.get('reasoning', '')
                    tmpl_id = data.get('suggested_template_id')
                    if tmpl_id:
                        item.suggested_template_id = int(tmpl_id)
                    item.status = 'scored'
            except Exception as ex:
                log.warning(f'News Score Error: {ex}')
        db.session.commit()


@app.route('/api/news', methods=['GET'])
@login_required
def api_news_list():
    city_id = request.args.get('city_id', type=int)
    status  = request.args.get('status', 'scored')
    q = NewsItem.query
    if city_id:
        q = q.filter_by(city_id=city_id)
    if status:
        q = q.filter_by(status=status)
    items = q.order_by(NewsItem.meme_score.desc(), NewsItem.fetched_at.desc()).limit(100).all()
    return jsonify([{
        'id': n.id, 'city_name': n.city.name,
        'headline': n.headline, 'url': n.url,
        'published_at': n.published_at.isoformat() if n.published_at else None,
        'meme_score': n.meme_score,
        'meme_score_color': n.meme_score_color,
        'meme_reasoning': n.meme_reasoning,
        'suggested_template_id': n.suggested_template_id,
        'suggested_template_name': n.suggested_template.name if n.suggested_template else None,
        'status': n.status,
    } for n in items])

@app.route('/api/news/<int:news_id>', methods=['PUT'])
@login_required
def api_news_update(news_id):
    item = NewsItem.query.get_or_404(news_id)
    d = request.json or {}
    if 'status' in d:
        item.status = d['status']
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/news/<int:news_id>/generate', methods=['POST'])
@login_required
def api_news_generate(news_id):
    item = NewsItem.query.get_or_404(news_id)
    if not item.suggested_template_id:
        return jsonify({'error': 'Kein Template vorgeschlagen'}), 400
    job = RenderJob(
        template_id=item.suggested_template_id,
        city_id=item.city_id,
        status='pending',
    )
    db.session.add(job)
    item.status = 'used'
    db.session.commit()
    threading.Thread(target=_run_generate_job, args=(app, job.id), daemon=True).start()
    return jsonify({'job_id': job.id})

# ═══════════════════════════════════════════════════════════════════════════════
# RESIDENT SURVEY
# ═══════════════════════════════════════════════════════════════════════════════

SURVEY_QUESTIONS = [
    {'key': 'worst_traffic',    'text': 'Welche Kreuzung / Ampel nervt dich am meisten?'},
    {'key': 'problem_place',    'text': 'Welcher Ort in der Stadt gilt als gefährlich oder problematisch?'},
    {'key': 'youth_spot',       'text': 'Wo hängen Jugendliche ab? (Park, Platz, Treffpunkt)'},
    {'key': 'food_spot',        'text': 'Das beste / bekannteste Lokal der Stadt?'},
    {'key': 'school_rep',       'text': 'Welches Gymnasium / welche Schule hat den besten / schlechtesten Ruf?'},
    {'key': 'rich_area',        'text': 'Welcher Stadtteil gilt als teuer / reich?'},
    {'key': 'poor_area',        'text': 'Welcher Stadtteil gilt als "rough" / günstig?'},
    {'key': 'student_area',     'text': 'Wo wohnen die meisten Studenten?'},
    {'key': 'landmark',         'text': 'Was ist das bekannteste Wahrzeichen der Stadt?'},
    {'key': 'local_event',      'text': 'Welches Event ist DAS Stadtfest / Highlight des Jahres?'},
    {'key': 'local_sport',      'text': 'Welcher Sportverein repräsentiert die Stadt am meisten?'},
    {'key': 'local_klischee',   'text': 'Was ist das größte Klischee über deine Stadt?'},
    {'key': 'dialect_word',     'text': 'Gibt es einen typischen lokalen Ausdruck oder Dialektwort?'},
    {'key': 'local_meme',       'text': 'Gibt es ein bekanntes lokales Meme oder Running Gag über die Stadt?'},
    {'key': 'gentrified_area',  'text': 'Welcher Stadtteil hat sich in den letzten Jahren stark verändert?'},
    {'key': 'tourist_spot',     'text': 'Wohin bringen einheimische Touristen als erstes?'},
    {'key': 'avoid_spot',       'text': 'Wo würdest du nachts lieber nicht alleine sein?'},
    {'key': 'pride_spot',       'text': 'Worauf sind die Einwohner am meisten stolz?'},
    {'key': 'hated_thing',      'text': 'Was nervt die Einwohner am meisten an ihrer Stadt?'},
    {'key': 'local_celeb',      'text': 'Gibt es eine bekannte Person die aus der Stadt stammt?'},
]

@app.route('/api/surveys', methods=['GET'])
@login_required
def api_surveys_list():
    surveys = ResidentSurvey.query.order_by(ResidentSurvey.created_at.desc()).all()
    return jsonify([{
        'id': s.id, 'city_name': s.city.name, 'city_id': s.city_id,
        'respondent': s.respondent, 'status': s.status,
        'token': s.token,
        'survey_url': url_for('survey_form', token=s.token, _external=True),
        'submitted_at': s.submitted_at.isoformat() if s.submitted_at else None,
        'created_at': s.created_at.isoformat(),
    } for s in surveys])

@app.route('/api/surveys', methods=['POST'])
@login_required
def api_survey_create():
    d = request.json or {}
    city_id = d.get('city_id')
    if not city_id:
        return jsonify({'error': 'city_id fehlt'}), 400
    City.query.get_or_404(city_id)
    survey = ResidentSurvey(
        city_id=city_id,
        token=secrets.token_urlsafe(32),
        respondent=d.get('respondent', ''),
    )
    db.session.add(survey)
    db.session.commit()
    return jsonify({
        'id': survey.id,
        'token': survey.token,
        'survey_url': url_for('survey_form', token=survey.token, _external=True),
    }), 201

@app.route('/survey/<token>')
def survey_form(token):
    survey = ResidentSurvey.query.filter_by(token=token).first_or_404()
    if survey.status == 'completed':
        return render_template('survey_done.html', city=survey.city)
    return render_template('survey.html', survey=survey, city=survey.city,
                           questions=SURVEY_QUESTIONS)

@app.route('/survey/submit', methods=['POST'])
def survey_submit():
    if _rate_hits('survey', _SURVEY_WINDOW_SEC) >= _SURVEY_MAX_SUBMIT:
        return 'Zu viele Übermittlungen. Bitte in einer Minute erneut versuchen.', 429
    _rate_record('survey')
    token = request.form.get('token')
    survey = ResidentSurvey.query.filter_by(token=token).first_or_404()
    if survey.status == 'completed':
        return render_template('survey_done.html', city=survey.city)
    answers = {}
    for q in SURVEY_QUESTIONS:
        val = request.form.get(q['key'], '').strip()
        if val:
            answers[q['key']] = val
    survey.answers      = json.dumps(answers)
    survey.status       = 'completed'
    survey.submitted_at = datetime.utcnow()
    survey.respondent   = request.form.get('respondent', survey.respondent)
    db.session.commit()
    return render_template('survey_done.html', city=survey.city)

@app.route('/api/surveys/<int:survey_id>/import', methods=['POST'])
@login_required
def api_survey_import(survey_id):
    survey = ResidentSurvey.query.get_or_404(survey_id)
    if survey.status != 'completed':
        return jsonify({'error': 'Fragebogen noch nicht ausgefüllt'}), 400

    answers = survey.get_answers()
    category_map_survey = {
        'worst_traffic':   'traffic_spot',
        'problem_place':   'problem_place',
        'avoid_spot':      'problem_place',
        'youth_spot':      'youth_spot',
        'food_spot':       'food_spot',
        'school_rep':      'school',
        'rich_area':       'stadtteil_reich',
        'poor_area':       'stadtteil_arm',
        'student_area':    'stadtteil_student',
        'landmark':        'landmark',
        'tourist_spot':    'landmark',
        'local_event':     'event',
        'local_sport':     'sport',
        'local_klischee':  'klischee',
        'pride_spot':      'klischee',
        'hated_thing':     'klischee',
        'dialect_word':    'dialect',
        'local_meme':      'local_meme',
        'gentrified_area': 'stadtteil_student',
        'local_celeb':     'local_meme',
    }

    imported = 0
    for q_key, value in answers.items():
        category = category_map_survey.get(q_key)
        if not category or not value:
            continue
        exists = CityKnowledge.query.filter_by(
            city_id=survey.city_id, name=value
        ).first()
        if exists:
            continue
        entry = CityKnowledge(
            city_id=survey.city_id,
            category=category,
            name=value[:200],
            description=f'Aus Einwohner-Fragebogen ({survey.respondent or "anonym"})',
            confidence=85,
            source='resident',
        )
        db.session.add(entry)
        imported += 1

    survey.status      = 'imported'
    survey.imported_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'imported': imported})

# ═══════════════════════════════════════════════════════════════════════════════
# CONTENT OS BRIDGE
# ═══════════════════════════════════════════════════════════════════════════════

def _send_to_content_os(flask_app, job_id):
    with flask_app.app_context():
        job  = RenderJob.query.get(job_id)
        if not job or not CONTENT_OS_URL:
            return
        try:
            payload = {
                'title':       f'{job.city.name} — {job.template.name}',
                'city':        job.city.name,
                'template':    job.template.name,
                'fit_score':   job.fit_score,
                'vars_used':   job.get_vars(),
                'manual_brief': job.manual_brief,
                'source':      'memeos',
            }
            headers = {}
            if CONTENT_OS_KEY:
                headers['X-MemeOS-Key'] = CONTENT_OS_KEY

            if job.image_filename:
                img_path = os.path.join(_RENDER_DIR, job.image_filename)
                if os.path.exists(img_path):
                    with open(img_path, 'rb') as f:
                        r = requests.post(
                            CONTENT_OS_URL.rstrip('/') + '/api/memeos/receive',
                            files={'image': (job.image_filename, f, 'image/png')},
                            data={'meta': json.dumps(payload)},
                            headers=headers, timeout=30
                        )
                else:
                    r = requests.post(
                        CONTENT_OS_URL.rstrip('/') + '/api/memeos/receive',
                        json=payload, headers=headers, timeout=15
                    )
            else:
                r = requests.post(
                    CONTENT_OS_URL.rstrip('/') + '/api/memeos/receive',
                    json=payload, headers=headers, timeout=15
                )

            if r.ok:
                job.sent_to_content_os = True
                job.sent_at            = datetime.utcnow()
                job.status             = 'sent'
                db.session.commit()
            else:
                log.warning(f'ContentOS Bridge Error: {r.status_code} {r.text[:100]}')
        except Exception as ex:
            log.error(f'ContentOS Bridge Exception: {ex}')

# ═══════════════════════════════════════════════════════════════════════════════
# TO-DO
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/todos', methods=['GET'])
@login_required
def api_todos_list():
    todos = AppTodo.query.order_by(AppTodo.priority.desc(), AppTodo.created_at.desc()).all()
    return jsonify([{
        'id': t.id, 'text': t.text, 'category': t.category,
        'done': t.done, 'priority': t.priority,
        'created_at': t.created_at.isoformat(),
    } for t in todos])

@app.route('/api/todos', methods=['POST'])
@login_required
def api_todo_create():
    d = request.json or {}
    if not d.get('text'):
        return jsonify({'error': 'Text fehlt'}), 400
    t = AppTodo(
        text=d['text'].strip(),
        category=d.get('category', 'idee'),
        priority=int(d.get('priority', 0)),
    )
    db.session.add(t)
    db.session.commit()
    return jsonify({'id': t.id}), 201

@app.route('/api/todos/<int:todo_id>', methods=['PUT'])
@login_required
def api_todo_update(todo_id):
    t = AppTodo.query.get_or_404(todo_id)
    d = request.json or {}
    for field in ['text', 'category', 'done', 'priority']:
        if field in d:
            setattr(t, field, d[field])
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/todos/<int:todo_id>', methods=['DELETE'])
@login_required
def api_todo_delete(todo_id):
    t = AppTodo.query.get_or_404(todo_id)
    db.session.delete(t)
    db.session.commit()
    return jsonify({'ok': True})

# ═══════════════════════════════════════════════════════════════════════════════
# SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/settings', methods=['GET'])
@login_required
def api_settings_get():
    _tg_token = (AppSettings.get('telegram_token', '') or '').strip()
    return jsonify({
        'content_os_url':       CONTENT_OS_URL or AppSettings.get('content_os_url', ''),
        'canva_connected':      _canva_is_connected(),
        'canva_client_id':      bool(CANVA_CLIENT_ID),
        'ai_key_set':           bool(ANTHROPIC_API_KEY),
        'ai_cost_month':        _ai_cost_this_month(),
        # Token nie im Klartext ausgeben – nur ob gesetzt + die letzten 4 Zeichen
        'telegram_token_set':   bool(_tg_token),
        'telegram_token_hint':  _tg_token[-4:] if _tg_token else '',
        'telegram_chat_id':     AppSettings.get('telegram_chat_id', ''),
        'alert_threshold_days': AppSettings.get('alert_threshold_days', '3'),
        'data_root':            _DATA_ROOT,
    })

@app.route('/api/settings', methods=['POST'])
@login_required
def api_settings_save():
    d = request.json or {}
    for key in ('content_os_url', 'telegram_chat_id', 'alert_threshold_days'):
        if key in d:
            AppSettings.set(key, d[key])
    # Leeres Token-Feld heißt "unverändert lassen" (das Feld zeigt den Token ja nicht mehr an)
    if d.get('telegram_token'):
        AppSettings.set('telegram_token', str(d['telegram_token']).strip())
    return jsonify({'ok': True})


@app.route('/api/settings/telegram/test', methods=['POST'])
@login_required
def api_telegram_test():
    token   = AppSettings.get('telegram_token', '').strip()
    chat_id = AppSettings.get('telegram_chat_id', '').strip()
    if not token or not chat_id:
        return jsonify({'error': 'Token und Chat-ID in Einstellungen speichern'}), 400
    try:
        resp = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': '✅ MemeOS Telegram-Verbindung funktioniert!', 'parse_mode': 'HTML'},
            timeout=8
        )
        if resp.ok:
            return jsonify({'ok': True})
        return jsonify({'error': resp.json().get('description', 'Telegram API Fehler')}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/backup')
@login_required
def api_backup():
    from flask import send_file
    import io
    data = {
        'exported_at': datetime.utcnow().isoformat(),
        'version': '1.0',
        'cities': [{'id': c.id, 'name': c.name, 'state': c.state, 'population': c.population,
                    'instagram_handle': c.instagram_handle, 'accent_color': c.accent_color,
                    'rss_url': c.rss_url, 'notes': c.notes}
                   for c in City.query.all()],
        'posts': [{'id': p.id, 'city_id': p.city_id, 'title': p.title, 'caption': p.caption,
                   'hashtags': p.hashtags, 'status': p.status, 'post_type': p.post_type,
                   'image_url': p.image_url,
                   'scheduled_at': p.scheduled_at.isoformat() if p.scheduled_at else None,
                   'published_at': p.published_at.isoformat() if p.published_at else None,
                   'perf_likes': p.perf_likes, 'perf_saves': p.perf_saves,
                   'perf_reach': p.perf_reach, 'perf_comments': p.perf_comments}
                  for p in MemePost.query.all()],
        'trending': [{'id': t.id, 'city_id': t.city_id, 'keyword': t.keyword,
                      'trend_score': t.trend_score, 'source': t.source, 'ignored': t.ignored}
                     for t in TrendingTopic.query.all()],
        'follower_snapshots': [{'city_id': s.city_id, 'count': s.count,
                                 'recorded_at': s.recorded_at.isoformat()}
                                for s in CityFollowerSnapshot.query.order_by(
                                    CityFollowerSnapshot.city_id, CityFollowerSnapshot.recorded_at).all()],
    }
    buf = io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
    buf.seek(0)
    fname = f'memeos_backup_{datetime.utcnow().strftime("%Y%m%d_%H%M")}.json'
    return send_file(buf, mimetype='application/json', as_attachment=True, download_name=fname)


@app.route('/api/follower-chart')
@login_required
def api_follower_chart():
    from collections import defaultdict
    city_id = request.args.get('city_id', type=int)
    days    = request.args.get('days', 30, type=int)
    cutoff  = datetime.utcnow() - timedelta(days=days)

    cities = ([City.query.get_or_404(city_id)] if city_id
              else City.query.filter_by(active=True).order_by(City.name).all())
    city_ids = [c.id for c in cities]

    snaps = CityFollowerSnapshot.query.filter(
        CityFollowerSnapshot.city_id.in_(city_ids),
        CityFollowerSnapshot.recorded_at >= cutoff,
    ).order_by(CityFollowerSnapshot.city_id, CityFollowerSnapshot.recorded_at).all()

    snap_map: dict = defaultdict(list)
    for s in snaps:
        snap_map[s.city_id].append({'date': s.recorded_at.strftime('%Y-%m-%d'), 'count': s.count})

    datasets = [
        {'city_id': c.id, 'city_name': c.name, 'color': c.accent_color or '#3b82f6',
         'data': snap_map.get(c.id, [])}
        for c in cities if c.id in snap_map
    ]
    return jsonify({'datasets': datasets, 'days': days})


@app.route('/api/performance/timeline')
@login_required
def api_performance_timeline():
    from collections import defaultdict
    days   = request.args.get('days', 30, type=int)
    cutoff = datetime.utcnow() - timedelta(days=days)
    posts  = MemePost.query.filter(
        MemePost.status == 'veroeffentlicht',
        MemePost.published_at >= cutoff,
        MemePost.perf_reach.isnot(None),
    ).order_by(MemePost.published_at).all()

    weekly: dict = defaultdict(list)
    for p in posts:
        if p.engagement_rate and p.published_at:
            week = p.published_at.strftime('%Y-W%V')
            weekly[week].append(p.engagement_rate)

    return jsonify({'timeline': [
        {'week': wk, 'avg_er': round(sum(ers) / len(ers), 2), 'count': len(ers)}
        for wk, ers in sorted(weekly.items())
    ]})


# ═══════════════════════════════════════════════════════════════════════════════
# STATS + KI-KOSTEN
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/stats')
@login_required
def api_stats():
    return jsonify({
        'cities':     City.query.filter_by(active=True).count(),
        'templates':  MemeTemplate.query.filter_by(active=True).count(),
        'pending':    RenderJob.query.filter(RenderJob.status.in_(['pending','running'])).count(),
        'review':     RenderJob.query.filter(RenderJob.status.in_(['done'])).count(),
        'approved':   RenderJob.query.filter_by(status='approved').count(),
        'sent':       RenderJob.query.filter_by(status='sent').count(),
        'knowledge':  CityKnowledge.query.filter_by(active=True).count(),
        'news':       NewsItem.query.filter_by(status='scored').count(),
        'ai_cost_month': _ai_cost_this_month(),
    })

def _ai_cost_this_month():
    first_day = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    from sqlalchemy import func
    result = db.session.query(func.sum(AiUsageLog.cost_eur))\
                .filter(AiUsageLog.created_at >= first_day).scalar()
    return round(result or 0, 4)

def _log_ai_usage(feature, model, input_tokens, output_tokens):
    # Claude Haiku pricing (rough EUR estimate)
    cost = (input_tokens * 0.0008 + output_tokens * 0.004) / 1000 * 0.92
    entry = AiUsageLog(feature=feature, model=model,
                       input_tokens=input_tokens, output_tokens=output_tokens,
                       cost_eur=cost)
    db.session.add(entry)
    db.session.commit()

# ═══════════════════════════════════════════════════════════════════════════════
# USERS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/users', methods=['GET'])
@login_required
def api_users_list():
    users = User.query.all()
    return jsonify([{
        'id': u.id, 'username': u.username, 'email': u.email,
        'role': u.role, 'active': u.active,
        'last_login': u.last_login.isoformat() if u.last_login else None,
    } for u in users])

@app.route('/api/users', methods=['POST'])
@login_required
def api_user_create():
    d = request.json or {}
    if not d.get('username') or not d.get('password'):
        return jsonify({'error': 'Username und Passwort erforderlich'}), 400
    if User.query.filter_by(username=d['username']).first():
        return jsonify({'error': 'Username bereits vergeben'}), 409
    u = User(username=d['username'], email=d.get('email', ''), role=d.get('role', 'admin'))
    u.set_password(d['password'])
    db.session.add(u)
    db.session.commit()
    return jsonify({'id': u.id}), 201

@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@login_required
def api_user_delete(user_id):
    u = User.query.get_or_404(user_id)
    db.session.delete(u)
    db.session.commit()
    return jsonify({'ok': True})

# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════════════════

def _seed_todos():
    todos = [
        ('HTML-Templates für neue Memes testen (Playwright-Rendering) — später wenn neue Templates entstehen', 'feature', 1),
        ('Canva API verbinden unter Einstellungen', 'feature', 1),
        ('Erste 10 Städte im City-Wiki anlegen', 'feature', 1),
        ('Erste Meme-Templates mit Canva Template-IDs verknüpfen', 'feature', 0),
        ('Einwohner-Fragebogen für Pilot-Städte versenden', 'idee', 0),
        ('ContentOS Bridge URL konfigurieren', 'feature', 0),
        ('RSS-Feeds für Städte eintragen', 'idee', 0),
    ]
    if AppTodo.query.count() == 0:
        for text, cat, prio in todos:
            db.session.add(AppTodo(text=text, category=cat, priority=prio))
        db.session.commit()

def _seed_cities():
    starter_cities = [
        ('Darmstadt', 'Hessen', 160000),
        ('Frankfurt', 'Hessen', 770000),
        ('Wiesbaden', 'Hessen', 280000),
        ('Mainz', 'Rheinland-Pfalz', 220000),
        ('Mannheim', 'Baden-Württemberg', 310000),
        ('Heidelberg', 'Baden-Württemberg', 160000),
        ('Offenbach', 'Hessen', 130000),
        ('Hanau', 'Hessen', 100000),
        ('Kaiserslautern', 'Rheinland-Pfalz', 100000),
        ('Braunschweig', 'Niedersachsen', 250000),
        ('Berlin', 'Berlin', 3700000),
        ('Hamburg', 'Hamburg', 1900000),
        ('München', 'Bayern', 1500000),
        ('Köln', 'Nordrhein-Westfalen', 1100000),
        ('Stuttgart', 'Baden-Württemberg', 630000),
        ('Düsseldorf', 'Nordrhein-Westfalen', 640000),
        ('Dortmund', 'Nordrhein-Westfalen', 590000),
        ('Essen', 'Nordrhein-Westfalen', 580000),
        ('Leipzig', 'Sachsen', 600000),
        ('Nürnberg', 'Bayern', 530000),
    ]
    if City.query.count() == 0:
        for name, state, pop in starter_cities:
            db.session.add(City(name=name, state=state, population=pop))
        db.session.commit()

def _seed_template_categories():
    if TemplateCategory.query.count() == 0:
        for i, (key, label, emoji, group) in enumerate(TEMPLATE_CATEGORIES):
            db.session.add(TemplateCategory(key=key, label=label, emoji=emoji, group=group, sort_order=i))
        db.session.commit()

_SEED_EVENTS = [
    # (name, emoji, type, date_from, date_to, recurring, lead_days, relevance, cats)
    ('Sommer',        '☀️', 'saisonal', '06-21','09-22', True,  14, 5, ['sommer','hitze']),
    ('Winter',        '❄️', 'saisonal', '12-21','03-20', True,  14, 4, ['winter','schnee']),
    ('Weihnachten',   '🎄', 'saisonal', '12-20','12-26', True,  21, 5, ['weihnachten']),
    ('Silvester',     '🎆', 'saisonal', '12-31','12-31', True,  14, 5, ['silvester']),
    ('Karneval',      '🎭', 'saisonal', '02-10','02-16', True,  14, 5, ['karneval']),
    ('Ostern',        '🐣', 'saisonal', '03-25','04-05', True,  10, 4, ['ostern']),
    ('Valentinstag',  '❤️', 'saisonal', '02-14','02-14', True,   7, 4, ['valentinstag']),
    ('Halloween',     '🎃', 'saisonal', '10-31','10-31', True,  14, 5, ['halloween']),
    ('Frühling',      '🌸', 'saisonal', '03-20','06-20', True,   7, 3, ['fruehling']),
    ('Herbst',        '🍂', 'saisonal', '09-23','12-20', True,   7, 3, ['herbst']),
    ('Hitzewelle',    '🌡️','wetter',   None,    None,  False,   1, 5, ['hitze']),
    ('Erster Schnee', '☃️','wetter',   None,    None,  False,   0, 5, ['schnee','winter']),
    ('Starkregen',    '⛈️','wetter',   None,    None,  False,   0, 4, ['regen','gewitter']),
    ('Sturmwarnung',  '🌪️','wetter',   None,    None,  False,   0, 4, ['regen']),
]

def _seed_events():
    if MemeEvent.query.count() == 0:
        for ev in _SEED_EVENTS:
            name, emoji, etype, dfrom, dto, rec, lead, rel, cats = ev
            db.session.add(MemeEvent(
                name=name, emoji=emoji, event_type=etype,
                date_from=dfrom, date_to=dto, recurring=rec,
                lead_days=lead, meme_relevance=rel,
                suggested_cats=json.dumps(cats),
            ))
        db.session.commit()

def _seed_recycle_settings():
    defaults = {
        'recycle_min_age_days':       '180',
        'recycle_min_follower_growth': '20',
        'recycle_min_stars':           '3',
        'recycle_max_per_month':       '4',
        'recycle_excluded_cats':       '["news"]',
    }
    for k, v in defaults.items():
        if not AppSettings.query.filter_by(key=k).first():
            db.session.add(AppSettings(key=k, value=v))
    db.session.commit()

def _migrate_legacy_data():
    """Datenmigrationen, die bei jedem Start idempotent laufen."""
    # 1) Alte Stadtwissen-Kategorien auf die Registry abbilden
    for old, new in LEGACY_CATEGORY_MAP.items():
        try:
            db.session.execute(db.text('UPDATE city_knowledge SET category = :new WHERE category = :old'),
                               {'new': new, 'old': old})
            db.session.commit()
        except Exception:
            db.session.rollback()
    # 2) Canva-Autofill ist weg: render_type 'canva' → 'pil' (wenn PIL-Konfiguration da) sonst 'manual'
    for sql in (
        "UPDATE meme_template SET render_type='pil' WHERE render_type='canva' "
        "AND pil_config IS NOT NULL AND pil_config != '{}' AND pil_config != ''",
        "UPDATE meme_template SET render_type='manual' WHERE render_type='canva'",
        "UPDATE meme_template SET render_type='pil' WHERE render_type IS NULL OR render_type=''",
    ):
        try:
            db.session.execute(db.text(sql))
            db.session.commit()
        except Exception:
            db.session.rollback()


with app.app_context():
    db.create_all()
    for _col_sql in [
        'ALTER TABLE meme_post ADD COLUMN carousel_paths TEXT',
        'ALTER TABLE meme_template ADD COLUMN series TEXT',
        'ALTER TABLE meme_template ADD COLUMN series_position INTEGER',
        'ALTER TABLE meme_template ADD COLUMN preview_url VARCHAR(1000)',
        'ALTER TABLE memo_inspiration_source ADD COLUMN city_id INTEGER REFERENCES city(id)',
        'ALTER TABLE memo_inspiration_source ADD COLUMN platform VARCHAR(20) DEFAULT \'instagram\'',
        'CREATE INDEX IF NOT EXISTS ix_memo_inspiration_source_city_id ON memo_inspiration_source (city_id)',
        'ALTER TABLE city ADD COLUMN lat FLOAT',   # B5 Wetter-Events (Open-Meteo); models.City.lat
        'ALTER TABLE city ADD COLUMN lon FLOAT',   # B5
    ]:
        try:
            db.session.execute(db.text(_col_sql))
            db.session.commit()
        except Exception:
            db.session.rollback()
    _migrate_legacy_data()
    _seed_todos()
    _seed_cities()
    _seed_template_categories()
    _seed_events()
    _seed_recycle_settings()

# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
# EVENTS API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/events', methods=['GET'])
@login_required
def api_events_list():
    q = MemeEvent.query.order_by(MemeEvent.event_type, MemeEvent.name)
    events = q.all()
    result = [e.to_dict() for e in events]
    # Sort by days_until (upcoming first), None/past at bottom
    result.sort(key=lambda x: (x['days_until'] is None or x['days_until'] < 0, x['days_until'] or 9999))
    return jsonify(result)

@app.route('/api/events/upcoming', methods=['GET'])
@login_required
def api_events_upcoming():
    """Events die in den nächsten X Tagen anstehen (innerhalb lead_days-Fenster)."""
    horizon = int(request.args.get('days', 30))
    events = MemeEvent.query.filter_by(active=True).all()
    upcoming = []
    for e in events:
        d = e.days_until()
        if d is not None and -3 <= d <= horizon:
            ed = e.to_dict()
            if d <= (e.lead_days or 0):
                ed['_urgent'] = True
            upcoming.append(ed)
    # Laufende Events zuerst, dann nach Tagen bis zum Beginn
    upcoming.sort(key=lambda x: (0 if x['active_now'] else 1, x['days_until']))
    return jsonify(upcoming)

@app.route('/api/events', methods=['POST'])
@login_required
def api_event_create():
    d = request.json or {}
    if not d.get('name'):
        return jsonify({'error': 'Name fehlt'}), 400
    e = MemeEvent(
        name=d['name'].strip(),
        description=d.get('description', ''),
        event_type=d.get('event_type', 'saisonal'),
        date_from=d.get('date_from', ''),
        date_to=d.get('date_to', ''),
        recurring=bool(d.get('recurring', False)),
        lead_days=int(d.get('lead_days', 7)),
        city_scope=json.dumps(d.get('city_scope', [])),
        meme_relevance=int(d.get('meme_relevance', 3)),
        suggested_cats=json.dumps(d.get('suggested_cats', [])),
        emoji=d.get('emoji', '📅'),
        notes=d.get('notes', ''),
    )
    db.session.add(e)
    db.session.commit()
    return jsonify(e.to_dict()), 201

@app.route('/api/events/<int:ev_id>', methods=['PUT'])
@login_required
def api_event_update(ev_id):
    e = MemeEvent.query.get_or_404(ev_id)
    d = request.json or {}
    for f in ['name','description','event_type','date_from','date_to','recurring',
              'lead_days','meme_relevance','emoji','notes','active']:
        if f in d:
            setattr(e, f, d[f])
    if 'city_scope' in d:
        e.city_scope = json.dumps(d['city_scope'])
    if 'suggested_cats' in d:
        e.suggested_cats = json.dumps(d['suggested_cats'])
    db.session.commit()
    return jsonify(e.to_dict())

@app.route('/api/events/<int:ev_id>', methods=['DELETE'])
@login_required
def api_event_delete(ev_id):
    e = MemeEvent.query.get_or_404(ev_id)
    db.session.delete(e)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/events/notify', methods=['POST'])
@login_required
def api_events_notify():
    """Sendet Telegram-Benachrichtigung für alle Events innerhalb ihres lead_days-Fensters."""
    token   = (get_setting('telegram_token') or '').strip()
    chat_id = (get_setting('telegram_chat_id') or '').strip()
    if not token or not chat_id:
        return jsonify({'error': 'Telegram nicht konfiguriert'}), 400
    events = MemeEvent.query.filter_by(active=True).all()
    sent, skipped = 0, 0
    now = datetime.utcnow()
    for e in events:
        d = e.days_until()
        if d is None or d < 0:
            continue
        if d > (e.lead_days or 0):
            continue
        # Höchstens eine Nachricht pro Event in 20 Stunden
        if e.notified_at and (now - e.notified_at) < timedelta(hours=20):
            skipped += 1
            continue
        msg_lines = [f"{e.emoji} *{e.name}*"]
        if e.is_active_today():
            msg_lines.append("🔥 Läuft gerade!")
        elif d == 0:
            msg_lines.append("🔥 Heute!")
        elif d == 1:
            msg_lines.append("⚡ Morgen!")
        else:
            msg_lines.append(f"📅 In {d} Tagen")
        if e.description:
            msg_lines.append(e.description)
        cats = e.get_suggested_cats()
        if cats:
            msg_lines.append("💡 Kategorien: " + ", ".join(cats))
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={'chat_id': chat_id, 'text': '\n'.join(msg_lines), 'parse_mode': 'Markdown'},
                timeout=8,
            )
            if resp.ok:
                e.notified_at = now
                sent += 1
            else:
                log.warning(f'Telegram notify failed for event {e.id}: {resp.status_code} {resp.text[:120]}')
        except Exception as ex:
            log.warning(f'Telegram notify failed for event {e.id}: {ex}')
    db.session.commit()
    return jsonify({'ok': True, 'sent': sent, 'skipped_recent': skipped})

# ═══════════════════════════════════════════════════════════════════════════════
# KATEGORIE-MIX (Freshness) API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/city/<int:city_id>/category-mix', methods=['GET'])
@login_required
def api_category_mix(city_id):
    days = int(request.args.get('days', 30))
    cutoff = datetime.utcnow() - timedelta(days=days)
    jobs = (RenderJob.query
            .filter_by(city_id=city_id)
            .filter(RenderJob.status.in_(['approved','done']))
            .filter(RenderJob.completed_at >= cutoff)
            .all())
    counts = {}
    for j in jobs:
        cat = j.template.category if j.template else 'unbekannt'
        counts[cat] = counts.get(cat, 0) + 1
    total = sum(counts.values())
    # Load target mix from settings
    raw = get_setting('category_target_mix') or '{}'
    try: target = json.loads(raw)
    except: target = {}
    result = []
    for cat, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        pct = round(cnt / total * 100) if total else 0
        t_pct = target.get(cat, 0)
        result.append({'category': cat, 'count': cnt, 'pct': pct, 'target_pct': t_pct,
                        'delta': pct - t_pct})
    return jsonify({'days': days, 'total': total, 'mix': result})

@app.route('/api/settings/category-mix', methods=['GET'])
@login_required
def api_get_category_mix():
    """Ziel-Mix der Template-Kategorien in Prozent: {'mix': {'pov': 20, ...}}."""
    raw = get_setting('category_target_mix') or '{}'
    try:
        mix = json.loads(raw)
        if not isinstance(mix, dict):
            mix = {}
    except Exception:
        mix = {}
    return jsonify({'mix': mix})

@app.route('/api/settings/category-mix', methods=['POST'])
@login_required
def api_save_category_mix():
    d = request.json or {}
    mix = d.get('mix', {})
    if not isinstance(mix, dict):
        return jsonify({'error': 'mix muss ein Objekt {kategorie: prozent} sein'}), 400
    set_setting('category_target_mix', json.dumps(mix))
    return jsonify({'ok': True, 'mix': mix})

# ═══════════════════════════════════════════════════════════════════════════════
# DUPLICATE CHECK API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/check-duplicate', methods=['GET'])
@login_required
def api_check_duplicate():
    city_id     = request.args.get('city_id', type=int)
    template_id = request.args.get('template_id', type=int)
    category    = request.args.get('category', '')
    days_tmpl   = int(request.args.get('days_template', 30))
    days_cat    = int(request.args.get('days_category', 7))
    warnings    = []

    if not city_id:
        return jsonify({'ok': True, 'warnings': []})

    cutoff_tmpl = datetime.utcnow() - timedelta(days=days_tmpl)
    cutoff_cat  = datetime.utcnow() - timedelta(days=days_cat)

    if template_id:
        dupe = (RenderJob.query
                .filter_by(city_id=city_id, template_id=template_id)
                .filter(RenderJob.status.in_(['approved','done']))
                .filter(RenderJob.completed_at >= cutoff_tmpl)
                .first())
        if dupe:
            warnings.append({
                'type': 'template',
                'message': f'Dieses Template wurde für diese Stadt in den letzten {days_tmpl} Tagen bereits verwendet',
                'last_used': dupe.completed_at.isoformat() if dupe.completed_at else None,
            })

    if category and category not in ('allgemein', 'sonstige'):
        tmpl = MemeTemplate.query.get(template_id) if template_id else None
        cat_check = category or (tmpl.category if tmpl else None)
        if cat_check:
            cat_jobs = (RenderJob.query
                        .join(MemeTemplate, RenderJob.template_id == MemeTemplate.id)
                        .filter(RenderJob.city_id == city_id)
                        .filter(MemeTemplate.category == cat_check)
                        .filter(RenderJob.status.in_(['approved','done']))
                        .filter(RenderJob.completed_at >= cutoff_cat)
                        .count())
            if cat_jobs >= 2:
                warnings.append({
                    'type': 'category',
                    'message': f'Kategorie „{cat_check}" wurde in den letzten {days_cat} Tagen bereits {cat_jobs}× für diese Stadt verwendet',
                })

    return jsonify({'ok': True, 'warnings': warnings, 'has_warnings': len(warnings) > 0})

# ═══════════════════════════════════════════════════════════════════════════════
# RECYCLE SETTINGS API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/settings/recycle', methods=['GET'])
@login_required
def api_recycle_settings_get():
    return jsonify({
        'min_age_days':        int(get_setting('recycle_min_age_days') or 180),
        'min_follower_growth': int(get_setting('recycle_min_follower_growth') or 20),
        'min_stars':           int(get_setting('recycle_min_stars') or 3),
        'max_per_month':       int(get_setting('recycle_max_per_month') or 4),
        'excluded_cats':       json.loads(get_setting('recycle_excluded_cats') or '["news"]'),
    })

@app.route('/api/settings/recycle', methods=['POST'])
@login_required
def api_recycle_settings_save():
    d = request.json or {}
    mapping = {
        'min_age_days':        'recycle_min_age_days',
        'min_follower_growth': 'recycle_min_follower_growth',
        'min_stars':           'recycle_min_stars',
        'max_per_month':       'recycle_max_per_month',
    }
    for k, sk in mapping.items():
        if k in d:
            set_setting(sk, str(int(d[k])))
    if 'excluded_cats' in d:
        set_setting('recycle_excluded_cats', json.dumps(d['excluded_cats']))
    return jsonify({'ok': True})

# ═══════════════════════════════════════════════════════════════════════════════
# MARKT API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/market', methods=['GET'])
@login_required
def api_market_list():
    entries = CityMarketEntry.query.order_by(CityMarketEntry.rank).all()
    return jsonify([{
        'id': e.id, 'name': e.name, 'state': e.state,
        'population': e.population, 'rank': e.rank,
        'status': e.status, 'status_label': e.status_label,
        'status_color': e.status_color,
        'notes': e.notes,
        'buyable_count': e.buyable_pages.count(),
    } for e in entries])

@app.route('/api/market/<int:entry_id>/status', methods=['PUT'])
@login_required
def api_market_status(entry_id):
    e = CityMarketEntry.query.get_or_404(entry_id)
    d = request.json or {}
    if 'status' in d:
        e.status = d['status']
        if d['status'] == 'owned' and d.get('city_id'):
            e.city_id = d['city_id']
    if 'notes' in d:
        e.notes = d['notes']
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/market/<int:entry_id>/pages', methods=['GET'])
@login_required
def api_market_pages(entry_id):
    pages = BuyablePage.query.filter_by(market_entry_id=entry_id)\
                .order_by(BuyablePage.created_at.desc()).all()
    return jsonify([{
        'id': p.id, 'instagram_url': p.instagram_url, 'handle': p.handle,
        'followers': p.followers, 'price_ask': p.price_ask,
        'contact_status': p.contact_status, 'contact_label': p.contact_label,
        'contact_color': p.contact_color, 'contact_notes': p.contact_notes,
        'created_at': p.created_at.isoformat(),
    } for p in pages])

@app.route('/api/market/<int:entry_id>/pages', methods=['POST'])
@login_required
def api_market_page_add(entry_id):
    CityMarketEntry.query.get_or_404(entry_id)
    d = request.json or {}
    if not d.get('instagram_url') and not d.get('handle'):
        return jsonify({'error': 'URL oder Handle erforderlich'}), 400
    p = BuyablePage(
        market_entry_id=entry_id,
        instagram_url=d.get('instagram_url', ''),
        handle=d.get('handle', ''),
        followers=d.get('followers'),
        price_ask=d.get('price_ask'),
        contact_status=d.get('contact_status', 'neu'),
        contact_notes=d.get('contact_notes', ''),
    )
    db.session.add(p)
    # Auto-update market status if was 'none' or 'want_to_buy'
    entry = CityMarketEntry.query.get(entry_id)
    if entry.status in ('none', 'want_to_buy'):
        entry.status = 'found_pages'
    db.session.commit()
    return jsonify({'id': p.id}), 201

@app.route('/api/market/pages/<int:page_id>', methods=['PUT'])
@login_required
def api_market_page_update(page_id):
    p = BuyablePage.query.get_or_404(page_id)
    d = request.json or {}
    for field in ['instagram_url','handle','followers','price_ask',
                  'contact_status','contact_notes']:
        if field in d:
            setattr(p, field, d[field])
    db.session.commit()
    return jsonify({'ok': True, 'contact_label': p.contact_label, 'contact_color': p.contact_color})

@app.route('/api/market/pages/<int:page_id>', methods=['DELETE'])
@login_required
def api_market_page_delete(page_id):
    p = BuyablePage.query.get_or_404(page_id)
    db.session.delete(p)
    db.session.commit()
    return jsonify({'ok': True})

# ═══════════════════════════════════════════════════════════════════════════════
# INSPIRATION API
# ═══════════════════════════════════════════════════════════════════════════════

_INSPO_PLATFORMS = ('instagram', 'tiktok', 'facebook', 'x', 'reddit', 'sonstige')

def _inspo_city_id(value):
    """city_id aus dem Request prüfen: None/leer → None, sonst muss die Stadt existieren."""
    if value in (None, '', 0, '0'):
        return None
    cid = int(value)
    if not City.query.get(cid):
        raise ValueError('Stadt nicht gefunden')
    return cid

@app.route('/api/inspiration/sources', methods=['GET'])
@login_required
def api_inspo_sources():
    sources = MemoInspirationSource.query.order_by(MemoInspirationSource.username).all()
    return jsonify([s.to_dict() for s in sources])

@app.route('/api/inspiration/sources', methods=['POST'])
@login_required
def api_inspo_source_add():
    d = request.json or {}
    username = d.get('username', '').strip().lstrip('@')
    if not username:
        return jsonify({'error': 'Username fehlt'}), 400
    if MemoInspirationSource.query.filter_by(username=username).first():
        return jsonify({'error': 'Quelle bereits vorhanden'}), 409
    try:
        city_id = _inspo_city_id(d.get('city_id'))
    except (ValueError, TypeError) as ex:
        return jsonify({'error': str(ex) or 'city_id ungültig'}), 400
    platform = (d.get('platform') or 'instagram').strip().lower()
    if platform not in _INSPO_PLATFORMS:
        platform = 'sonstige'
    s = MemoInspirationSource(username=username, notes=d.get('notes', ''),
                              city_id=city_id, platform=platform)
    db.session.add(s)
    db.session.commit()
    return jsonify(s.to_dict()), 201

@app.route('/api/inspiration/sources/<int:src_id>', methods=['PUT'])
@login_required
def api_inspo_source_update(src_id):
    """Stadt-Zuordnung, Plattform und Notizen einer Quelle ändern."""
    s = MemoInspirationSource.query.get_or_404(src_id)
    d = request.json or {}
    if 'city_id' in d:
        try:
            s.city_id = _inspo_city_id(d['city_id'])
        except (ValueError, TypeError) as ex:
            return jsonify({'error': str(ex) or 'city_id ungültig'}), 400
    if 'platform' in d:
        platform = (d.get('platform') or 'instagram').strip().lower()
        s.platform = platform if platform in _INSPO_PLATFORMS else 'sonstige'
    if 'notes' in d:
        s.notes = d['notes'] or ''
    db.session.commit()
    return jsonify(s.to_dict())

@app.route('/api/inspiration/sources/<int:src_id>', methods=['DELETE'])
@login_required
def api_inspo_source_delete(src_id):
    s = MemoInspirationSource.query.get_or_404(src_id)
    db.session.delete(s)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/inspiration/posts', methods=['GET'])
@login_required
def api_inspo_posts():
    status  = request.args.get('status', 'new')
    src_id  = request.args.get('source_id', type=int)
    q = MemoInspirationPost.query
    if status == 'saved':
        q = q.filter_by(is_saved=True)
    elif status and status != 'all':
        q = q.filter_by(status=status)
    if src_id:
        q = q.filter_by(source_id=src_id)
    posts = q.order_by(MemoInspirationPost.created_at.desc()).limit(200).all()
    return jsonify([{
        'id': p.id, 'source_id': p.source_id,
        'username': p.source.username if p.source else '',
        'city_id': p.source.city_id if p.source else None,
        'city_name': p.source.city.name if p.source and p.source.city else '',
        'platform': (p.source.platform or 'instagram') if p.source else 'instagram',
        'instagram_code': p.instagram_code,
        'image_url': p.image_url, 'caption': p.caption,
        'like_count': p.like_count, 'media_type': p.media_type,
        'status': p.status, 'is_saved': p.is_saved,
        'meme_idea': p.meme_idea,
        'ai_relevant': p.ai_relevant,
        'ai_theme': p.ai_theme or '',
        'ai_reasoning': p.ai_reasoning or '',
        'post_date': p.post_date.isoformat() if p.post_date else None,
    } for p in posts])

@app.route('/api/inspiration/posts/add', methods=['POST'])
@login_required
def api_inspo_post_add():
    d = request.json or {}
    src_id = d.get('source_id')
    if not src_id:
        return jsonify({'error': 'source_id fehlt'}), 400
    code = d.get('instagram_code', f'manual_{int(time.time())}')
    if MemoInspirationPost.query.filter_by(instagram_code=code).first():
        return jsonify({'error': 'Post bereits vorhanden'}), 409
    p = MemoInspirationPost(
        source_id=src_id,
        instagram_code=code,
        image_url=d.get('image_url', ''),
        caption=d.get('caption', ''),
        like_count=d.get('like_count'),
        media_type=d.get('media_type', 'image'),
        meme_idea=d.get('meme_idea', ''),
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({'id': p.id}), 201

@app.route('/api/inspiration/posts/<int:post_id>', methods=['PUT'])
@login_required
def api_inspo_post_update(post_id):
    p = MemoInspirationPost.query.get_or_404(post_id)
    d = request.json or {}
    for field in ['status', 'is_saved', 'meme_idea']:
        if field in d:
            setattr(p, field, d[field])
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/inspiration/posts/<int:post_id>', methods=['DELETE'])
@login_required
def api_inspo_post_delete(post_id):
    p = MemoInspirationPost.query.get_or_404(post_id)
    db.session.delete(p)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/inspiration/posts/<int:post_id>/generate', methods=['POST'])
@login_required
def api_inspo_post_generate(post_id):
    p = MemoInspirationPost.query.get_or_404(post_id)
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'Kein API Key'}), 400
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        templates = MemeTemplate.query.filter_by(active=True).all()
        tmpl_str = '\n'.join([f"- ID:{t.id} {t.name}" for t in templates])
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=300,
            messages=[{'role': 'user', 'content': f"""Analysiere diesen Instagram-Post als Meme-Inspiration:

Caption: {p.caption or '(keine)'}
Likes: {p.like_count or '?'}
Von: @{p.source.username if p.source else '?'}

Verfügbare Meme-Templates:
{tmpl_str}

Antworte mit JSON:
{{"meme_idea": "<konkrete Meme-Idee für eine deutsche Stadtseite, max 150 Zeichen>", "suggested_template_id": <ID oder null>}}"""}]
        )
        raw = msg.content[0].text.strip()
        _log_ai_usage('inspo_analyze', 'claude-haiku-4-5-20251001',
                      msg.usage.input_tokens, msg.usage.output_tokens)
        import re
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            p.meme_idea = data.get('meme_idea', '')
            p.status = 'saved'
            p.is_saved = True
            db.session.commit()
            return jsonify({'meme_idea': p.meme_idea})
    except Exception as ex:
        return jsonify({'error': str(ex)}), 500
    return jsonify({'error': 'KI-Fehler'}), 500

# ═══════════════════════════════════════════════════════════════════════════════
# INSPIRATION — KI-THEMATISCHE SORTIERUNG
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/inspiration/ai-sort', methods=['POST'])
@login_required
def api_inspo_ai_sort():
    """Lässt Claude unsortierte Posts thematisch einordnen + Stadtwissen extrahieren."""
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'Kein API Key'}), 400
    count = int(request.json.get('count', 20) if request.json else 20)
    posts = MemoInspirationPost.query.filter(
        MemoInspirationPost.ai_relevant.is_(None),
        MemoInspirationPost.status.in_(['new', 'saved'])
    ).limit(count).all()
    if not posts:
        return jsonify({'ok': True, 'processed': 0, 'message': 'Alle Posts bereits sortiert'})

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    processed = 0
    knowledge_added = 0
    import re
    # Einzige Taxonomie: KNOWLEDGE_CATEGORIES (Schlüssel + Label), unbekannte Antworten → 'sonstiges'
    categories_str = '\n'.join(f'- {k}: {label}' for k, label, _ in KNOWLEDGE_CATEGORIES)
    for p in posts:
        city = p.source.city if p.source else None      # kann None sein (Quelle ohne Stadt)
        city_name = city.name if city else 'unbekannte Stadt'
        city_id   = city.id if city else None
        try:
            msg = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=600,
                messages=[{'role': 'user', 'content': f"""Du analysierst einen Instagram-Post aus {city_name} für ein lokales Meme-Konto.

Von: @{p.source.username if p.source else 'unbekannt'}
Caption: {(p.caption or '')[:500]}
Likes: {p.like_count or '?'}

Beantworte zwei Dinge als JSON:

1. Ist der Post relevant als Meme-Inspiration (Humor, Stadtleben, lokale Themen)?
2. Extrahiere stadtspezifisches Meme-Wissen aus dem Post — also alles, was man wissen muss, um gute Memes über {city_name} zu machen: Viertel-Reputationen, Running Gags, Personen, Slang, Problemorte, Stadtwitze, Rivalitäten etc.

Erlaubte Kategorien (nutze GENAU den Schlüssel vor dem Doppelpunkt):
{categories_str}

Antworte NUR mit diesem JSON (kein anderer Text):
{{
  "relevant": true,
  "theme": "Stadtleben",
  "reasoning": "1 Satz Begründung",
  "city_knowledge": [
    {{"category": "local_meme", "content": "In {city_name} sagt man X wenn...", "confidence": 0.85}}
  ]
}}

Wenn kein spezifisches Stadtmeme-Wissen erkennbar ist, lass city_knowledge als leeres Array []."""}]
            )
            _log_ai_usage('inspo_sort', 'claude-haiku-4-5-20251001',
                          msg.usage.input_tokens, msg.usage.output_tokens)
            raw = msg.content[0].text.strip()
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                p.ai_relevant  = bool(data.get('relevant', True))
                p.ai_theme     = data.get('theme', '')[:100]
                p.ai_reasoning = data.get('reasoning', '')
                processed += 1
                # Stadtwissen nur speichern, wenn die Quelle einer Stadt zugeordnet ist
                if city_id:
                    for kw in (data.get('city_knowledge') or []):
                        content = (kw.get('content') or '').strip()
                        cat     = normalize_category(kw.get('category'))
                        try:
                            conf = max(0, min(100, int(float(kw.get('confidence', 0.7)) * 100)))
                        except Exception:
                            conf = 70
                        if content and len(content) > 5:
                            # Duplikat-Check: gleiche Stadt + gleicher Name (grob)
                            exists = CityKnowledge.query.filter_by(
                                city_id=city_id, name=content[:200]
                            ).first()
                            if not exists:
                                db.session.add(CityKnowledge(
                                    city_id=city_id,
                                    category=cat,
                                    name=content[:200],
                                    description='',
                                    confidence=conf,
                                    source='ai',
                                    source_post_id=p.id,
                                ))
                                knowledge_added += 1
        except Exception as ex:
            log.warning(f'KI-Sort Post {p.id}: {ex}')
    db.session.commit()
    return jsonify({'ok': True, 'processed': processed, 'knowledge_added': knowledge_added, 'total': len(posts)})


@app.route('/api/inspiration/posts/<int:post_id>/ai-approve', methods=['POST'])
@login_required
def api_inspo_ai_approve(post_id):
    p = MemoInspirationPost.query.get_or_404(post_id)
    p.ai_relevant = True
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/inspiration/posts/<int:post_id>/ai-reject', methods=['POST'])
@login_required
def api_inspo_ai_reject(post_id):
    p = MemoInspirationPost.query.get_or_404(post_id)
    p.ai_relevant = False
    p.status = 'ignored'
    db.session.commit()
    return jsonify({'ok': True})

# ═══════════════════════════════════════════════════════════════════════════════
# STADTWISSEN API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/knowledge/all', methods=['GET'])
@login_required
def api_knowledge_all():
    """Stadtwissen — gefiltert nach Stadt/Kategorie/Status für Stadtwissen-Seite."""
    city_id  = request.args.get('city_id', type=int)
    category = request.args.get('category')
    verified = request.args.get('verified')   # '1'=verifiziert, '0'=AI-Vorschlag
    q = CityKnowledge.query.filter_by(active=True)
    if city_id:
        q = q.filter_by(city_id=city_id)
    if category:
        q = q.filter_by(category=category)
    if verified == '1':
        q = q.filter(CityKnowledge.source.in_(['verified', 'manual', 'resident']))
    elif verified == '0':
        q = q.filter_by(source='ai')
    entries = q.order_by(CityKnowledge.created_at.desc()).all()
    return jsonify([e.to_dict() for e in entries])


@app.route('/api/knowledge/add', methods=['POST'])
@login_required
def api_knowledge_add():
    """Manuell einen Stadtwissen-Eintrag hinzufügen (verifiziert)."""
    d = request.json or {}
    city_id = d.get('city_id')
    content = (d.get('content') or '').strip()
    if not city_id or not content:
        return jsonify({'error': 'city_id und content erforderlich'}), 400
    e = CityKnowledge(
        city_id=city_id,
        category=normalize_category(d.get('category')),   # Registry-Schlüssel, Legacy wird abgebildet
        name=content[:200],
        description=(d.get('description') or '')[:2000],
        confidence=100,
        source='manual',
    )
    db.session.add(e)
    db.session.commit()
    return jsonify(e.to_dict()), 201


@app.route('/api/knowledge/<int:kid>/verify', methods=['POST'])
@login_required
def api_knowledge_verify(kid):
    """KI-Vorschlag als verifiziert markieren."""
    e = CityKnowledge.query.get_or_404(kid)
    e.source = 'verified'
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/knowledge/<int:kid>/reject', methods=['POST'])
@login_required
def api_knowledge_reject(kid):
    """KI-Vorschlag ablehnen (soft-delete)."""
    e = CityKnowledge.query.get_or_404(kid)
    e.active = False
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/knowledge/stats', methods=['GET'])
@login_required
def api_knowledge_stats():
    """Ausstehende KI-Vorschläge pro Stadt."""
    from sqlalchemy import func
    rows = db.session.query(
        City.id, City.name,
        func.count(CityKnowledge.id).label('total'),
        func.sum(db.case((CityKnowledge.source == 'ai', 1), else_=0)).label('pending'),
    ).outerjoin(CityKnowledge, (CityKnowledge.city_id == City.id) & (CityKnowledge.active == True))\
     .group_by(City.id).order_by(func.count(CityKnowledge.id).desc()).all()
    return jsonify([{
        'city_id': r.id, 'city_name': r.name,
        'total': r.total or 0,
        'pending': int(r.pending or 0),
        'verified': (r.total or 0) - int(r.pending or 0),
    } for r in rows])


# ═══════════════════════════════════════════════════════════════════════════════
# KOOPERATIONEN API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/collab/niches', methods=['GET'])
@login_required
def api_collab_niches_list():
    niches = CollabNiche.query.filter_by(active=True).order_by(CollabNiche.name).all()
    return jsonify([n.to_dict() for n in niches])

@app.route('/api/collab/niches', methods=['POST'])
@login_required
def api_collab_niche_create():
    d = request.json or {}
    if not d.get('name'):
        return jsonify({'error': 'Name erforderlich'}), 400
    n = CollabNiche(name=d['name'].strip(), emoji=d.get('emoji','🤝'),
                    description=d.get('description',''))
    db.session.add(n); db.session.commit()
    return jsonify(n.to_dict()), 201

@app.route('/api/collab/niches/<int:nid>', methods=['PUT'])
@login_required
def api_collab_niche_update(nid):
    n = CollabNiche.query.get_or_404(nid)
    d = request.json or {}
    for f in ['name','emoji','description','active']:
        if f in d: setattr(n, f, d[f])
    db.session.commit()
    return jsonify(n.to_dict())

@app.route('/api/collab/niches/<int:nid>', methods=['DELETE'])
@login_required
def api_collab_niche_delete(nid):
    n = CollabNiche.query.get_or_404(nid)
    db.session.delete(n); db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/collab/ideas', methods=['GET'])
@login_required
def api_collab_ideas_list():
    niche_id = request.args.get('niche_id', type=int)
    q = CollabIdea.query.filter_by(active=True)
    if niche_id:
        q = q.filter_by(niche_id=niche_id)
    return jsonify([i.to_dict() for i in q.order_by(CollabIdea.title).all()])

@app.route('/api/collab/ideas', methods=['POST'])
@login_required
def api_collab_idea_create():
    d = request.json or {}
    if not d.get('title') or not d.get('niche_id'):
        return jsonify({'error': 'Titel und Nische erforderlich'}), 400
    CollabNiche.query.get_or_404(d['niche_id'])
    idea = CollabIdea(niche_id=d['niche_id'], title=d['title'].strip(),
                      description=d.get('description',''),
                      template_text=d.get('template_text',''))
    db.session.add(idea); db.session.commit()
    return jsonify(idea.to_dict()), 201

@app.route('/api/collab/ideas/<int:iid>', methods=['PUT'])
@login_required
def api_collab_idea_update(iid):
    idea = CollabIdea.query.get_or_404(iid)
    d = request.json or {}
    for f in ['title','description','template_text','active']:
        if f in d: setattr(idea, f, d[f])
    db.session.commit()
    return jsonify(idea.to_dict())

@app.route('/api/collab/ideas/<int:iid>', methods=['DELETE'])
@login_required
def api_collab_idea_delete(iid):
    idea = CollabIdea.query.get_or_404(iid)
    db.session.delete(idea); db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/collab/ideas/<int:iid>/cities', methods=['GET'])
@login_required
def api_collab_idea_cities(iid):
    CollabIdea.query.get_or_404(iid)
    collabs = CityCollab.query.filter_by(idea_id=iid)\
                .order_by(CityCollab.city_id).all()
    return jsonify([c.to_dict() for c in collabs])

@app.route('/api/collab/assign', methods=['POST'])
@login_required
def api_collab_assign():
    d = request.json or {}
    if not d.get('idea_id') or not d.get('city_id') or not d.get('partner_name'):
        return jsonify({'error': 'idea_id, city_id und partner_name erforderlich'}), 400
    existing = CityCollab.query.filter_by(
        idea_id=d['idea_id'], city_id=d['city_id']).first()
    if existing:
        existing.partner_name = d['partner_name']
        existing.partner_ig   = d.get('partner_ig','')
        existing.notes        = d.get('notes','')
        existing.status       = d.get('status','aktiv')
        db.session.commit()
        return jsonify(existing.to_dict())
    cc = CityCollab(idea_id=d['idea_id'], city_id=d['city_id'],
                    partner_name=d['partner_name'],
                    partner_ig=d.get('partner_ig',''),
                    notes=d.get('notes',''),
                    status=d.get('status','aktiv'))
    db.session.add(cc); db.session.commit()
    return jsonify(cc.to_dict()), 201

@app.route('/api/collab/<int:cc_id>', methods=['PUT'])
@login_required
def api_collab_update(cc_id):
    cc = CityCollab.query.get_or_404(cc_id)
    d  = request.json or {}
    for f in ['partner_name','partner_ig','status','notes']:
        if f in d: setattr(cc, f, d[f])
    db.session.commit()
    return jsonify(cc.to_dict())

@app.route('/api/collab/<int:cc_id>', methods=['DELETE'])
@login_required
def api_collab_delete(cc_id):
    cc = CityCollab.query.get_or_404(cc_id)
    db.session.delete(cc); db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/collab/state-overview', methods=['GET'])
@login_required
def api_collab_state_overview():
    """Für Bundesland-Strategie: zeigt Kooperationen gruppiert nach Bundesland."""
    collabs = (CityCollab.query.join(City).filter(CityCollab.status == 'aktiv')
               .order_by(City.state, City.name).all())
    result = {}
    for cc in collabs:
        state = cc.city.state or 'Unbekannt'
        result.setdefault(state, []).append({
            'city': cc.city.name, 'partner': cc.partner_name,
            'idea': cc.idea.title if cc.idea else ''
        })
    return jsonify(result)

# ═══════════════════════════════════════════════════════════════════════════════
# BUNDESLAND-STRATEGIE: SCHEDULING-KONFLIKTE
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/scheduling/state-conflicts', methods=['GET'])
@login_required
def api_state_conflicts():
    """Gibt Paare von Städten zurück, die im selben Bundesland ähnliche Memes planen."""
    days = int(request.args.get('days', 3))
    from datetime import date
    today = datetime.utcnow()
    horizon = today + timedelta(days=days)
    posts = (MemePost.query
             .join(City, MemePost.city_id == City.id)
             .filter(MemePost.status.in_(['geplant','bereit']))
             .filter(MemePost.scheduled_at >= today)
             .filter(MemePost.scheduled_at <= horizon)
             .order_by(City.state, MemePost.scheduled_at)
             .all())
    by_state = {}
    for p in posts:
        state = p.city.state or 'Unbekannt'
        by_state.setdefault(state, []).append({
            'city': p.city.name, 'city_id': p.city_id,
            'post_id': p.id, 'title': p.title or '',
            'scheduled_at': p.scheduled_at.isoformat() if p.scheduled_at else None,
            'template_id': p.template_id,
        })
    conflicts = []
    for state, state_posts in by_state.items():
        if len(state_posts) < 2: continue
        # Group by template_id to find duplicates
        by_tmpl = {}
        for sp in state_posts:
            if sp['template_id']:
                by_tmpl.setdefault(sp['template_id'], []).append(sp)
        for tmpl_id, dupes in by_tmpl.items():
            if len(dupes) >= 2:
                conflicts.append({
                    'state': state,
                    'template_id': tmpl_id,
                    'cities': dupes,
                    'warning': f"Gleiches Template in {len(dupes)} Städten in {state} innerhalb von {days} Tagen"
                })
    return jsonify({'conflicts': conflicts, 'days': days,
                    'by_state': {s: ps for s, ps in by_state.items() if len(ps) > 0}})

# ═══════════════════════════════════════════════════════════════════════════════
# HOCHLADEN & EINPLANEN API
# ═══════════════════════════════════════════════════════════════════════════════

ALLOWED_UPLOAD = {'jpg', 'jpeg', 'png', 'webp', 'gif', 'mp4', 'mov'}

@app.route('/api/upload/batch', methods=['POST'])
@login_required
def api_upload_batch():
    files        = request.files.getlist('files')
    city_id      = request.form.get('city_id', type=int)
    as_carousel  = request.form.get('as_carousel') == 'true'
    carousel_title = request.form.get('carousel_title', '').strip()
    if not files:
        return jsonify({'error': 'Keine Dateien'}), 400

    upload_dir = _UPLOAD_DIR
    saved = []  # [{fname, url, ftype, orig_name}]

    for f in files:
        if not f.filename:
            continue
        ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
        if ext not in ALLOWED_UPLOAD:
            continue
        fname = f'upload_{int(time.time())}_{secrets.token_hex(4)}.{ext}'
        path  = os.path.join(upload_dir, fname)
        f.save(path)
        ftype = 'video' if ext in ('mp4', 'mov') else 'image'
        rtype = 'video' if ftype == 'video' else 'image'
        cloud_url = _upload_cloudinary(path, folder='memeos/uploads', resource_type=rtype)
        final_url = cloud_url or f'/uploads/{fname}'
        saved.append({'fname': fname, 'url': final_url, 'ftype': ftype,
                      'orig_name': f.filename.rsplit('.', 1)[0]})

    if not saved:
        return jsonify({'ok': True, 'created': []})

    if as_carousel and len(saved) > 1:
        title = carousel_title or f'Karussell ({len(saved)} Slides)'
        post = MemePost(
            city_id=city_id,
            title=title,
            image_url=saved[0]['url'],
            image_path=saved[0]['fname'],
            post_type='carousel',
            carousel_paths=json.dumps([s['fname'] for s in saved]),
            status='entwurf',
        )
        db.session.add(post)
        db.session.commit()
        return jsonify({'ok': True, 'created': [{
            'id': post.id, 'fname': saved[0]['fname'],
            'title': post.title, 'url': post.image_url,
            'ftype': 'carousel', 'slide_count': len(saved),
            'slide_urls': [s['url'] for s in saved],
        }]})

    created = []
    for s in saved:
        post = MemePost(
            city_id=city_id,
            title=s['orig_name'],
            image_url=s['url'],
            image_path=s['fname'],
            post_type='feed',
            status='entwurf',
        )
        db.session.add(post)
        db.session.flush()
        created.append({'id': post.id, 'fname': s['fname'],
                        'title': post.title, 'url': post.image_url, 'ftype': s['ftype']})
    db.session.commit()
    return jsonify({'ok': True, 'created': created})

@app.route('/api/upload/schedule', methods=['POST'])
@login_required
def api_upload_schedule():
    d     = request.json or {}
    items = d.get('items', [])  # [{post_id, city_id, caption, scheduled_at, post_type, title}]
    saved = []
    for item in items:
        pid = item.get('post_id')
        if not pid:
            continue
        p = MemePost.query.get(pid)
        if not p:
            continue
        p.city_id   = item.get('city_id', p.city_id)
        p.caption   = item.get('caption', '')
        p.title     = item.get('title', p.title)
        p.post_type = item.get('post_type', p.post_type)
        p.status    = 'geplant'
        if item.get('scheduled_at'):
            try:
                p.scheduled_at = datetime.fromisoformat(item['scheduled_at'])
            except:
                p.status = 'bereit'
        else:
            p.status = 'bereit'
        saved.append({'post_id': p.id, 'scheduled_at': p.scheduled_at.isoformat() if p.scheduled_at else None})
    db.session.commit()
    return jsonify({'ok': True, 'saved': len(saved), 'posts': saved})

@app.route('/api/upload/caption/<int:post_id>', methods=['POST'])
@login_required
def api_upload_caption(post_id):
    p = MemePost.query.get_or_404(post_id)
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'Kein API Key'}), 400
    city = p.city or (City.query.get(p.city_id) if p.city_id else None)
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""Du bist Social-Media-Manager einer deutschen Stadt-Meme-Seite.
Stadt: {city.name if city else 'Unbekannt'}
Dateiname: {p.title}

Erstelle 3 verschiedene Instagram-Captions auf Deutsch (locker, witzig, lokaler Humor).
Format: {{"captions": ["...", "...", "..."]}}
Nur JSON."""
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001', max_tokens=500,
            messages=[{'role':'user','content':prompt}]
        )
        raw = msg.content[0].text
        _log_ai_usage('upload_caption', 'claude-haiku-4-5-20251001', msg.usage.input_tokens, msg.usage.output_tokens)
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            return jsonify({'ok': True, 'captions': data.get('captions', [])})
    except Exception as ex:
        return jsonify({'error': str(ex)}), 500
    return jsonify({'error': 'KI-Fehler'}), 500

# ═══════════════════════════════════════════════════════════════════════════════
# CLOUDINARY STATUS + MIGRATE
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/cloudinary/status')
@login_required
def api_cloudinary_status():
    connected = _cloudinary_connected()
    return jsonify({'connected': connected, 'url_set': connected})


@app.route('/api/cloudinary/migrate', methods=['POST'])
@login_required
def api_cloudinary_migrate():
    """Upload all local images (MemePost + RenderJob) to Cloudinary."""
    if not _cloudinary_connected():
        return jsonify({'error': 'CLOUDINARY_URL nicht gesetzt'}), 400

    migrated, skipped, failed = 0, 0, 0

    # MemePost — local /uploads/ paths
    posts = MemePost.query.filter(
        MemePost.image_url.like('/uploads/%')
    ).all()
    for p in posts:
        local = os.path.join(_UPLOAD_DIR, os.path.basename(p.image_url))
        if not os.path.exists(local):
            skipped += 1
            continue
        url = _upload_cloudinary(local, folder='memeos/uploads')
        if url:
            p.image_url = url
            migrated += 1
        else:
            failed += 1

    # RenderJob — local /static/renders/
    jobs = RenderJob.query.filter(
        RenderJob.image_filename.isnot(None),
        db.or_(RenderJob.image_url == None, RenderJob.image_url == '')
    ).all()
    for j in jobs:
        local = os.path.join(_RENDER_DIR, j.image_filename)
        if not os.path.exists(local):
            skipped += 1
            continue
        url = _upload_cloudinary(local, folder='memeos/renders')
        if url:
            j.image_url = url
            migrated += 1
        else:
            failed += 1

    db.session.commit()
    return jsonify({'migrated': migrated, 'skipped': skipped, 'failed': failed})


# ═══════════════════════════════════════════════════════════════════════════════
# VORRAT API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/vorrat', methods=['GET'])
@login_required
def api_vorrat_list():
    status    = request.args.get('status', '')
    city_id   = request.args.get('city_id', type=int)
    post_type = request.args.get('post_type', '')
    search    = request.args.get('q', '').strip()
    page      = max(1, request.args.get('page', 1, type=int))
    per_page  = min(50, max(10, request.args.get('per_page', 20, type=int)))

    q = MemePost.query
    if status:    q = q.filter_by(status=status)
    if city_id:   q = q.filter_by(city_id=city_id)
    if post_type: q = q.filter_by(post_type=post_type)
    if search:
        like = f'%{search}%'
        q = q.filter(db.or_(MemePost.title.ilike(like), MemePost.caption.ilike(like)))

    total = q.count()
    posts = q.order_by(MemePost.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()

    # Status-Counts immer über alle Posts (unabhängig vom Filter)
    from sqlalchemy import func
    raw_counts = db.session.query(MemePost.status, func.count(MemePost.id))\
        .group_by(MemePost.status).all()
    counts = {s: 0 for s in ['entwurf', 'bereit', 'geplant', 'veroeffentlicht', 'archiviert']}
    for s, c in raw_counts:
        if s in counts:
            counts[s] = c

    return jsonify({
        'items':    [p.to_dict() for p in posts],
        'total':    total,
        'page':     page,
        'per_page': per_page,
        'pages':    max(1, (total + per_page - 1) // per_page),
        'counts':   counts,
    })

@app.route('/api/vorrat', methods=['POST'])
@login_required
def api_vorrat_create():
    d = request.json or {}
    if not d.get('city_id'):
        return jsonify({'error': 'city_id fehlt'}), 400
    p = MemePost(
        city_id=d['city_id'],
        render_job_id=d.get('render_job_id'),
        template_id=d.get('template_id'),
        title=d.get('title',''),
        image_url=d.get('image_url',''),
        image_path=d.get('image_path',''),
        caption=d.get('caption',''),
        hashtags=d.get('hashtags',''),
        post_type=d.get('post_type','feed'),
        status=d.get('status','entwurf'),
        notes=d.get('notes',''),
    )
    if d.get('scheduled_at'):
        try: p.scheduled_at = datetime.fromisoformat(d['scheduled_at'])
        except: pass
    db.session.add(p)
    db.session.commit()
    return jsonify(p.to_dict()), 201

@app.route('/api/vorrat/<int:post_id>', methods=['GET','PUT','DELETE'])
@login_required
def api_vorrat_item(post_id):
    p = MemePost.query.get_or_404(post_id)
    if request.method == 'DELETE':
        db.session.delete(p)
        db.session.commit()
        return jsonify({'ok': True})
    if request.method == 'GET':
        return jsonify(p.to_dict())
    d = request.json or {}
    for f in ['title','image_url','caption','hashtags','post_type','status','notes',
              'perf_likes','perf_comments','perf_saves','perf_reach','perf_impressions']:
        if f in d: setattr(p, f, d[f])
    if 'scheduled_at' in d:
        p.scheduled_at = datetime.fromisoformat(d['scheduled_at']) if d['scheduled_at'] else None
        if d['scheduled_at']: p.status = 'geplant'
    if d.get('status') == 'veroeffentlicht' and not p.published_at:
        p.published_at = datetime.utcnow()
    if any(k in d for k in ['perf_likes','perf_comments','perf_saves','perf_reach','perf_impressions']):
        p.perf_updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(p.to_dict())

@app.route('/api/vorrat/<int:post_id>/caption', methods=['POST'])
@login_required
def api_vorrat_caption(post_id):
    p = MemePost.query.get_or_404(post_id)
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'Kein API Key'}), 400
    city = p.city
    tmpl = p.template
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""Du bist Social-Media-Manager für eine deutsche Stadt-Meme-Seite.
Stadt: {city.name} ({city.state or ''})
Template: {tmpl.name if tmpl else 'Stadtmeme'}
Titel/Thema: {p.title or 'Stadtmeme'}
Post-Typ: {p.post_type}

Erstelle eine Instagram-Caption auf Deutsch:
- Ton: locker, witzig, lokaler Humor
- Max 150 Zeichen Caption
- 10-15 relevante Hashtags
Format: {{"caption": "...", "hashtags": "..."}}
Nur JSON."""
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001', max_tokens=400,
            messages=[{'role':'user','content':prompt}]
        )
        raw = msg.content[0].text
        _log_ai_usage('caption_gen', 'claude-haiku-4-5-20251001', msg.usage.input_tokens, msg.usage.output_tokens)
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            p.caption  = data.get('caption', p.caption)
            p.hashtags = data.get('hashtags', p.hashtags)
            db.session.commit()
            return jsonify({'caption': p.caption, 'hashtags': p.hashtags})
    except Exception as ex:
        return jsonify({'error': str(ex)}), 500
    return jsonify({'error': 'KI-Fehler'}), 500

@app.route('/api/vorrat/bulk', methods=['POST'])
@login_required
def api_vorrat_bulk():
    d      = request.json or {}
    ids    = d.get('ids', [])
    action = d.get('action', '')
    new_status = d.get('status', '')
    if not ids or not action:
        return jsonify({'error': 'ids und action fehlen'}), 400
    posts = MemePost.query.filter(MemePost.id.in_(ids)).all()
    count = 0
    if action == 'delete':
        for p in posts:
            db.session.delete(p)
            count += 1
    elif action == 'archive':
        for p in posts:
            p.status = 'archiviert'
            count += 1
    elif action == 'status' and new_status:
        for p in posts:
            p.status = new_status
            if new_status == 'veroeffentlicht' and not p.published_at:
                p.published_at = datetime.utcnow()
            count += 1
    db.session.commit()
    return jsonify({'ok': True, 'affected': count})


@app.route('/api/vorrat/<int:post_id>/duplicate', methods=['POST'])
@login_required
def api_vorrat_duplicate(post_id):
    p = MemePost.query.get_or_404(post_id)
    d = request.json or {}
    new_post = MemePost(
        city_id=d.get('city_id', p.city_id),
        render_job_id=p.render_job_id,
        template_id=p.template_id,
        title=d.get('title', p.title),
        image_path=p.image_path,
        image_url=p.image_url,
        caption=p.caption,
        hashtags=p.hashtags,
        post_type=p.post_type,
        status='entwurf',
        notes=f'Dupliziert von Post #{p.id}',
    )
    db.session.add(new_post)
    db.session.commit()
    return jsonify(new_post.to_dict()), 201


@app.route('/api/vorrat/export-zip/start', methods=['POST'])
@login_required
def api_vorrat_export_zip_start():
    d = request.json or {}
    job_id = str(uuid.uuid4())
    db.session.add(ExportJob(id=job_id, status='pending'))
    db.session.commit()
    t = threading.Thread(
        target=_build_zip_async,
        args=(job_id, d.get('ids', []), d.get('status', 'geplant'), d.get('city_id')),
        daemon=True
    )
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/api/vorrat/export-zip/status/<job_id>')
@login_required
def api_vorrat_export_zip_status(job_id):
    job = ExportJob.query.get(job_id)
    if not job:
        return jsonify({'error': 'Job nicht gefunden'}), 404
    return jsonify(job.to_dict())


def _export_cleanup_later(job_id, path, delay=60):
    """Datei und Tabellenzeile verzögert entfernen (nach dem Download)."""
    def _run():
        time.sleep(delay)
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        with app.app_context():
            try:
                job = ExportJob.query.get(job_id)
                if job:
                    db.session.delete(job)
                    db.session.commit()
            except Exception:
                db.session.rollback()
    threading.Thread(target=_run, daemon=True).start()


@app.route('/api/vorrat/export-zip/download/<job_id>')
@login_required
def api_vorrat_export_zip_download(job_id):
    job = ExportJob.query.get(job_id)
    if not job or job.status != 'ready' or not job.path or not os.path.exists(job.path):
        return jsonify({'error': 'ZIP nicht bereit'}), 400
    from flask import send_file
    _export_cleanup_later(job.id, job.path)
    return send_file(job.path, mimetype='application/zip',
                     as_attachment=True, download_name=job.filename)


@app.route('/api/city/<int:city_id>/dashboard')
@login_required
def api_city_dashboard(city_id):
    city = City.query.get_or_404(city_id)
    from sqlalchemy import func as sqlfunc

    raw = db.session.query(MemePost.status, sqlfunc.count(MemePost.id))\
          .filter(MemePost.city_id == city_id).group_by(MemePost.status).all()
    counts = {s: 0 for s in ['entwurf','bereit','geplant','veroeffentlicht','archiviert']}
    for s, c in raw:
        if s in counts: counts[s] = c

    upcoming = MemePost.query.filter(
        MemePost.city_id == city_id, MemePost.status == 'geplant',
        MemePost.scheduled_at >= datetime.utcnow()
    ).order_by(MemePost.scheduled_at).limit(5).all()

    cutoff = datetime.utcnow() - timedelta(days=30)
    pub = MemePost.query.filter(
        MemePost.city_id == city_id, MemePost.status == 'veroeffentlicht',
        MemePost.published_at >= cutoff, MemePost.perf_reach.isnot(None)
    ).all()
    ers = [p.engagement_rate for p in pub if p.engagement_rate]
    best = max(pub, key=lambda p: p.engagement_rate or 0) if pub else None

    trending = TrendingTopic.query.filter_by(city_id=city_id, ignored=False)\
        .order_by(TrendingTopic.trend_score.desc()).limit(5).all()

    candidates, _rc_settings, _ = _recycle_candidates(city_id=city_id, limit=3)

    return jsonify({
        'city': {
            'id': city.id, 'name': city.name, 'state': city.state or '',
            'accent_color': city.accent_color or '#3b82f6',
            'population': city.population, 'instagram_handle': city.instagram_handle or '',
            'brand': _city_brand(city),
        },
        'post_counts': counts,
        'upcoming': [p.to_dict() for p in upcoming],
        'performance_30d': {
            'post_count': len(pub), 'avg_er': round(sum(ers)/len(ers), 2) if ers else None,
            'total_reach': sum(p.perf_reach or 0 for p in pub),
            'best_post': best.to_dict() if best else None,
        },
        'trending': [t.to_dict() for t in trending],
        'recycle_candidates': candidates,
        'wiki_count': CityKnowledge.query.filter_by(city_id=city_id, active=True).count(),
        'render_count': RenderJob.query.filter_by(city_id=city_id, status='done').count(),
    })


@app.route('/api/vorrat/from-job/<int:job_id>', methods=['POST'])
@login_required
def api_vorrat_from_job(job_id):
    job = RenderJob.query.get_or_404(job_id)
    if MemePost.query.filter_by(render_job_id=job_id).first():
        return jsonify({'error': 'Bereits im Vorrat'}), 409
    p = MemePost(
        city_id=job.city_id,
        render_job_id=job.id,
        template_id=job.template_id,
        title=f"{job.city.name} — {job.template.name}",
        image_url=job.image_url or '',
        status='bereit',
    )
    db.session.add(p)
    db.session.commit()
    return jsonify(p.to_dict()), 201

def _parse_dt(s):
    """'YYYY-MM-DD' oder ISO-Datetime (auch mit 'Z' / Offset) → naives datetime (UTC).
    None bei leerem oder ungültigem Wert."""
    if not s:
        return None
    s = str(s).strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is not None:
        from datetime import timezone
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _first_of_next_month(dt):
    if dt.month == 12:
        return datetime(dt.year + 1, 1, 1)
    return datetime(dt.year, dt.month + 1, 1)


@app.route('/api/kalender', methods=['GET'])
@login_required
def api_kalender():
    from_dt = _parse_dt(request.args.get('from'))
    to_dt   = _parse_dt(request.args.get('to'))
    if from_dt is None:
        from_dt = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if to_dt is None:
        to_dt = _first_of_next_month(from_dt)
    posts = MemePost.query.filter(
        MemePost.scheduled_at >= from_dt,
        MemePost.scheduled_at < to_dt
    ).order_by(MemePost.scheduled_at).all()
    return jsonify([p.to_dict() for p in posts])

@app.route('/api/performance', methods=['GET'])
@login_required
def api_performance():
    days = request.args.get('days', type=int)
    q = MemePost.query.filter_by(status='veroeffentlicht')
    if days:
        q = q.filter(MemePost.published_at >= datetime.utcnow() - timedelta(days=days))
    published = q.all()
    with_perf = [p for p in published if p.perf_likes is not None]
    top_posts = sorted(with_perf, key=lambda p: p.perf_likes or 0, reverse=True)[:20]

    city_stats = {}
    for p in published:
        cid = p.city_id
        if cid not in city_stats:
            city_stats[cid] = {
                'city_name': p.city.name if p.city else '',
                'city_color': p.city.accent_color if p.city else '#3b82f6',
                'count': 0, 'total_likes': 0, 'total_saves': 0, 'total_reach': 0
            }
        s = city_stats[cid]
        s['count']       += 1
        s['total_likes'] += (p.perf_likes or 0)
        s['total_saves'] += (p.perf_saves or 0)
        s['total_reach'] += (p.perf_reach or 0)
    for s in city_stats.values():
        s['avg_likes'] = round(s['total_likes'] / s['count'], 1) if s['count'] else 0

    tmpl_stats = {}
    for p in published:
        if not p.template_id: continue
        tid = p.template_id
        if tid not in tmpl_stats:
            tmpl_stats[tid] = {'template_name': p.template.name if p.template else '',
                                'count': 0, 'total_likes': 0, 'total_saves': 0}
        t = tmpl_stats[tid]
        t['count']       += 1
        t['total_likes'] += (p.perf_likes or 0)
        t['total_saves'] += (p.perf_saves or 0)
    for t in tmpl_stats.values():
        t['avg_likes'] = round(t['total_likes'] / t['count'], 1) if t['count'] else 0

    return jsonify({
        'top_posts':   [p.to_dict() for p in top_posts],
        'city_stats':  sorted(city_stats.values(), key=lambda x: x['avg_likes'], reverse=True),
        'tmpl_stats':  sorted(tmpl_stats.values(), key=lambda x: x['avg_likes'], reverse=True),
        'total_posts': len(published),
        'total_likes': sum(p.perf_likes or 0 for p in with_perf),
        'total_saves': sum(p.perf_saves or 0 for p in with_perf),
        'avg_engagement': round(sum(p.engagement_rate or 0 for p in with_perf) / len(with_perf), 2) if with_perf else 0,
    })

@app.route('/api/bulk/multi', methods=['POST'])
@login_required
def api_bulk_multi():
    d = request.json or {}
    template_ids = d.get('template_ids', [])
    city_ids     = d.get('city_ids', [])
    if not template_ids or not city_ids:
        return jsonify({'error': 'template_ids und city_ids erforderlich'}), 400
    created = []
    for tid in template_ids:
        tmpl = MemeTemplate.query.get(tid)
        if not tmpl: continue
        for cid in city_ids:
            city = City.query.get(cid)
            if not city: continue
            job = RenderJob(template_id=tid, city_id=cid, status='pending')
            db.session.add(job)
            db.session.flush()
            created.append(job.id)
            threading.Thread(target=_run_generate_job, args=(app, job.id), daemon=True).start()
    db.session.commit()
    return jsonify({'created': len(created), 'job_ids': created})


# ═══════════════════════════════════════════════════════════════════════════════
# TRENDING MONITOR
# ═══════════════════════════════════════════════════════════════════════════════

def _recycle_settings():
    """Die 5 Recycling-Einstellungen mit Defaults, typisiert."""
    def _int(key, default):
        try:
            return int(get_setting(key) or default)
        except Exception:
            return default
    try:
        excluded = json.loads(get_setting('recycle_excluded_cats') or '["news"]')
        if not isinstance(excluded, list):
            excluded = []
    except Exception:
        excluded = ['news']
    return {
        'min_age_days':        _int('recycle_min_age_days', 180),
        'min_follower_growth': _int('recycle_min_follower_growth', 20),
        'min_stars':           _int('recycle_min_stars', 3),
        'max_per_month':       _int('recycle_max_per_month', 4),
        'excluded_cats':       [str(c) for c in excluded],
    }


def _follower_growth_since(city_id, since):
    """Follower-Zuwachs der Stadt seit `since` (letzter Snapshot minus Snapshot zum Zeitpunkt).
    None, wenn keine zwei verwertbaren Snapshots vorliegen."""
    if not since:
        return None
    latest = CityFollowerSnapshot.query.filter_by(city_id=city_id)\
                .order_by(CityFollowerSnapshot.recorded_at.desc()).first()
    if not latest:
        return None
    baseline = CityFollowerSnapshot.query.filter(
        CityFollowerSnapshot.city_id == city_id,
        CityFollowerSnapshot.recorded_at <= since
    ).order_by(CityFollowerSnapshot.recorded_at.desc()).first()
    if not baseline:
        baseline = CityFollowerSnapshot.query.filter(
            CityFollowerSnapshot.city_id == city_id,
            CityFollowerSnapshot.recorded_at > since
        ).order_by(CityFollowerSnapshot.recorded_at).first()
    if not baseline or baseline.id == latest.id:
        return None
    return latest.count - baseline.count


def _recycle_score(post, settings=None):
    """Recycle-Score 0–100 basierend auf Performance + Zeit seit Veröffentlichung.
    Zieht 15 Punkte ab, wenn das Follower-Wachstum seit published_at unter recycle_min_follower_growth
    liegt (nur wenn Snapshots vorhanden sind)."""
    if not post.published_at:
        return 0
    settings = settings or _recycle_settings()
    er = post.engagement_rate or 0
    days_ago = (datetime.utcnow() - post.published_at).days
    if days_ago < 14:
        time_factor = 0.0
    elif days_ago < 30:
        time_factor = 0.5
    elif days_ago <= 90:
        time_factor = 1.0
    elif days_ago <= 180:
        time_factor = 0.85
    else:
        time_factor = 0.7
    er_score = min(100, er * 15)
    base = er_score * time_factor
    penalty = min(40, (post.recycle_count or 0) * 20)
    growth = _follower_growth_since(post.city_id, post.published_at)
    if growth is not None and growth < settings['min_follower_growth']:
        penalty += 15
    return max(0, min(100, int(base - penalty)))


def _recycle_slots_left(city_id, settings):
    """max_per_month minus in diesem Monat freigegebene RecycleJobs dieser Stadt."""
    first = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    approved = RecycleJob.query.filter(
        RecycleJob.city_id == city_id,
        RecycleJob.status.in_(['geplant', 'veroeffentlicht']),
        db.or_(
            db.and_(RecycleJob.scheduled_for.isnot(None), RecycleJob.scheduled_for >= first,
                    RecycleJob.scheduled_for < _first_of_next_month(first)),
            db.and_(RecycleJob.scheduled_for.is_(None), RecycleJob.updated_at >= first),
        ),
    ).count()
    return max(0, settings['max_per_month'] - approved)


def _recycle_candidates(city_id=None, min_days=None, limit=None):
    """Kandidatenliste mit angewandten Recycling-Einstellungen – von /api/recycle/candidates
    und /api/city/<id>/dashboard gemeinsam genutzt."""
    settings = _recycle_settings()
    eff_days = max(int(min_days or 0), settings['min_age_days'])
    cutoff = datetime.utcnow() - timedelta(days=eff_days)
    q = MemePost.query.filter(
        MemePost.status == 'veroeffentlicht',
        MemePost.published_at <= cutoff
    )
    if city_id:
        q = q.filter_by(city_id=city_id)
    posts = q.order_by(MemePost.published_at.desc()).all()
    excluded = set(settings['excluded_cats'])
    slots_cache: dict = {}
    result = []
    for p in posts:
        tmpl = p.template
        if tmpl:
            if tmpl.category in excluded:
                continue
            if (tmpl.rating or 0) < settings['min_stars']:
                continue
        d = p.to_dict()
        d['recycle_score'] = _recycle_score(p, settings)
        d['days_since_post'] = (datetime.utcnow() - p.published_at).days if p.published_at else None
        d['open_recycle_jobs'] = RecycleJob.query.filter(
            RecycleJob.source_post_id == p.id,
            RecycleJob.status.in_(['vorschlag', 'geplant'])
        ).count()
        if p.city_id not in slots_cache:
            slots_cache[p.city_id] = _recycle_slots_left(p.city_id, settings)
        d['monthly_slots_left'] = slots_cache[p.city_id]
        d['follower_growth'] = _follower_growth_since(p.city_id, p.published_at)
        result.append(d)
    result.sort(key=lambda x: x['recycle_score'], reverse=True)
    if limit:
        result = result[:limit]
    return result, settings, eff_days


@app.route('/api/trending')
@login_required
def api_trending_list():
    city_id = request.args.get('city_id', type=int)
    show_ignored = request.args.get('ignored', 'false') == 'true'
    q = TrendingTopic.query
    if city_id:
        q = q.filter_by(city_id=city_id)
    if not show_ignored:
        q = q.filter_by(ignored=False)
    topics = q.order_by(TrendingTopic.trend_score.desc(), TrendingTopic.created_at.desc()).all()
    return jsonify([t.to_dict() for t in topics])


@app.route('/api/trending/refresh/<int:city_id>', methods=['POST'])
@login_required
def api_trending_refresh(city_id):
    city = City.query.get_or_404(city_id)
    if not city.rss_url:
        return jsonify({'error': f'Keine RSS-URL für {city.name} konfiguriert. Bitte in Städte-Einstellungen eintragen.'}), 400
    try:
        feed = feedparser.parse(city.rss_url)
        headlines = [e.title for e in feed.entries[:20] if hasattr(e, 'title') and e.title]
        if not headlines:
            return jsonify({'error': 'Keine Artikel im RSS-Feed gefunden'}), 400

        client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY', ''))
        prompt = f"""Du analysierst aktuelle Schlagzeilen aus {city.name} auf ihr Meme-Potenzial für Instagram-Stadtmemes.

Schlagzeilen:
{chr(10).join(f'- {h}' for h in headlines)}

Extrahiere die Top 5 Trending-Themen die sich am besten für virale Stadtmemes eignen.
Antworte NUR mit validem JSON (kein Markdown, kein Text davor/danach):
{{"topics":[{{"keyword":"kurzes prägnantes Schlagwort (max 4 Wörter)","description":"1-2 Sätze Kontext warum das trending ist","trend_score":85}},{{"keyword":"...","description":"...","trend_score":70}}]}}

trend_score: 0-100, wie gut geeignet für einen viralen Stadtmeme."""

        resp = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=800,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = resp.content[0].text.strip()
        start, end = raw.find('{'), raw.rfind('}') + 1
        data = json.loads(raw[start:end])

        dedup_cutoff = datetime.utcnow() - timedelta(hours=48)
        added = 0
        skipped = 0
        for t in data.get('topics', []):
            kw = (t.get('keyword') or '').strip()[:200]
            if not kw:
                continue
            already = db.session.query(TrendingTopic).filter(
                TrendingTopic.city_id == city_id,
                db.func.lower(TrendingTopic.keyword) == kw.lower(),
                TrendingTopic.created_at >= dedup_cutoff,
            ).first()
            if already:
                skipped += 1
                continue
            topic = TrendingTopic(
                city_id=city_id, keyword=kw,
                description=t.get('description', ''),
                trend_score=max(0, min(100, int(t.get('trend_score', 50)))),
                source='rss', fetched_at=datetime.utcnow()
            )
            db.session.add(topic)
            added += 1
        db.session.commit()
        return jsonify({'added': added, 'skipped': skipped, 'city': city.name,
                        'headlines_used': len(headlines)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/trending', methods=['POST'])
@login_required
def api_trending_create():
    d = request.json or {}
    keyword = (d.get('keyword') or '').strip()
    if not keyword:
        return jsonify({'error': 'keyword fehlt'}), 400
    city_id = d.get('city_id') or None
    dedup_cutoff = datetime.utcnow() - timedelta(hours=48)
    already = db.session.query(TrendingTopic).filter(
        TrendingTopic.city_id == city_id,
        db.func.lower(TrendingTopic.keyword) == keyword.lower(),
        TrendingTopic.created_at >= dedup_cutoff,
    ).first()
    if already:
        return jsonify({'error': f'"{keyword}" existiert bereits (letzte 48h)', 'existing': already.to_dict()}), 409
    topic = TrendingTopic(
        city_id=city_id,
        keyword=keyword[:200],
        description=d.get('description', ''),
        trend_score=max(0, min(100, int(d.get('trend_score', 60)))),
        source='manual', fetched_at=datetime.utcnow()
    )
    db.session.add(topic)
    db.session.commit()
    return jsonify(topic.to_dict()), 201


@app.route('/api/trending/<int:tid>/idea', methods=['POST'])
@login_required
def api_trending_idea(tid):
    topic = TrendingTopic.query.get_or_404(tid)
    city_name = topic.city.name if topic.city else 'der Stadt'
    try:
        client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY', ''))
        prompt = f"""Generiere 3 kreative Meme-Ideen für das Thema "{topic.keyword}" aus {city_name}.

Kontext: {topic.description or 'Lokales Trending-Thema'}

Format: kurze, prägnante Instagram-Meme-Konzepte (z.B. "POV: ..." oder "Wenn ..." oder direkte Aussage).
Antworte NUR mit JSON: {{"ideas":["Idee 1","Idee 2","Idee 3"]}}"""

        resp = client.messages.create(
            model='claude-haiku-4-5-20251001', max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = resp.content[0].text.strip()
        start, end = raw.find('{'), raw.rfind('}') + 1
        data = json.loads(raw[start:end])
        topic.meme_idea = '\n'.join(data.get('ideas', []))
        db.session.commit()
        return jsonify({'ideas': data.get('ideas', []), 'topic': topic.to_dict()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/trending/<int:tid>/ignore', methods=['POST'])
@login_required
def api_trending_ignore(tid):
    topic = TrendingTopic.query.get_or_404(tid)
    topic.ignored = not topic.ignored
    db.session.commit()
    return jsonify({'ignored': topic.ignored})


@app.route('/api/trending/<int:tid>/use', methods=['POST'])
@login_required
def api_trending_use(tid):
    topic = TrendingTopic.query.get_or_404(tid)
    topic.used_in_post_id = (request.json or {}).get('post_id')
    db.session.commit()
    return jsonify(topic.to_dict())


@app.route('/api/trending/<int:tid>', methods=['DELETE'])
@login_required
def api_trending_delete(tid):
    topic = TrendingTopic.query.get_or_404(tid)
    db.session.delete(topic)
    db.session.commit()
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════════════════════════
# CONTENT RECYCLING
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/recycle/candidates')
@login_required
def api_recycle_candidates():
    """Kandidaten mit angewandten Einstellungen. Antwort bleibt eine Liste (jeder Eintrag hat
    zusätzlich monthly_slots_left + follower_growth); die wirksamen Einstellungen stehen
    im Header X-Recycle-Settings (JSON)."""
    city_id  = request.args.get('city_id', type=int)
    min_days = request.args.get('min_days', type=int)
    result, settings, eff_days = _recycle_candidates(city_id=city_id, min_days=min_days)
    resp = jsonify(result)
    resp.headers['X-Recycle-Settings'] = json.dumps({**settings, 'effective_min_days': eff_days})
    return resp


@app.route('/api/recycle/jobs')
@login_required
def api_recycle_jobs_list():
    status   = request.args.get('status', 'vorschlag')
    city_id  = request.args.get('city_id', type=int)
    q = RecycleJob.query
    if status != 'alle':
        q = q.filter_by(status=status)
    if city_id:
        q = q.filter_by(city_id=city_id)
    jobs = q.order_by(RecycleJob.created_at.desc()).all()
    return jsonify([j.to_dict() for j in jobs])


@app.route('/api/recycle/jobs', methods=['POST'])
@login_required
def api_recycle_jobs_create():
    d = request.json or {}
    source_id = d.get('source_post_id')
    if not source_id:
        return jsonify({'error': 'source_post_id fehlt'}), 400
    source = MemePost.query.get_or_404(source_id)
    job = RecycleJob(
        source_post_id=source_id,
        city_id=d.get('city_id') or source.city_id,
        new_caption=d.get('new_caption') or source.caption or '',
        scheduled_for=datetime.fromisoformat(d['scheduled_for']) if d.get('scheduled_for') else None,
        recycle_score=_recycle_score(source),
        notes=d.get('notes', ''),
        status='vorschlag'
    )
    db.session.add(job)
    db.session.commit()
    return jsonify(job.to_dict()), 201


@app.route('/api/recycle/jobs/<int:jid>/approve', methods=['POST'])
@login_required
def api_recycle_approve(jid):
    job = RecycleJob.query.get_or_404(jid)
    d   = request.json or {}
    if d.get('scheduled_for'):
        job.scheduled_for = datetime.fromisoformat(d['scheduled_for'])
    if d.get('new_caption'):
        job.new_caption = d['new_caption']
    source = job.source_post
    new_post = MemePost(
        city_id=job.city_id,
        render_job_id=source.render_job_id,
        template_id=source.template_id,
        title=source.title,
        image_path=source.image_path,
        image_url=source.image_url,
        caption=job.new_caption or source.caption,
        hashtags=source.hashtags,
        post_type=source.post_type,
        status='geplant',
        scheduled_at=job.scheduled_for,
        notes=f'Recycelt aus Post #{source.id}'
    )
    db.session.add(new_post)
    db.session.flush()
    job.target_post_id = new_post.id
    job.status = 'geplant'
    source.recycle_count = (source.recycle_count or 0) + 1
    source.last_recycled_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'job': job.to_dict(), 'new_post': new_post.to_dict()})


@app.route('/api/recycle/jobs/<int:jid>/reject', methods=['POST'])
@login_required
def api_recycle_reject(jid):
    job = RecycleJob.query.get_or_404(jid)
    job.status = 'abgelehnt'
    job.rejection_reason = (request.json or {}).get('reason', '')
    db.session.commit()
    return jsonify(job.to_dict())


@app.route('/api/recycle/jobs/<int:jid>', methods=['PUT'])
@login_required
def api_recycle_job_update(jid):
    job = RecycleJob.query.get_or_404(jid)
    d   = request.json or {}
    for field in ('new_caption', 'notes'):
        if field in d:
            setattr(job, field, d[field])
    if 'scheduled_for' in d:
        job.scheduled_for = datetime.fromisoformat(d['scheduled_for']) if d['scheduled_for'] else None
    db.session.commit()
    return jsonify(job.to_dict())


@app.route('/api/recycle/jobs/<int:jid>', methods=['DELETE'])
@login_required
def api_recycle_job_delete(jid):
    job = RecycleJob.query.get_or_404(jid)
    db.session.delete(job)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/recycle/history')
@login_required
def api_recycle_history():
    jobs = RecycleJob.query.filter(
        RecycleJob.status.in_(['geplant', 'veroeffentlicht'])
    ).order_by(RecycleJob.updated_at.desc()).limit(100).all()
    result = []
    for j in jobs:
        d = j.to_dict()
        sp = j.source_post
        tp = j.target_post
        if sp and sp.engagement_rate and tp and tp.engagement_rate:
            d['perf_delta'] = round(tp.engagement_rate - sp.engagement_rate, 2)
            d['perf_delta_pct'] = round((tp.engagement_rate - sp.engagement_rate) / sp.engagement_rate * 100, 1) if sp.engagement_rate else None
        else:
            d['perf_delta'] = None
            d['perf_delta_pct'] = None
        result.append(d)
    return jsonify(result)


@app.route('/api/recycle/caption/<int:jid>', methods=['POST'])
@login_required
def api_recycle_caption(jid):
    job    = RecycleJob.query.get_or_404(jid)
    source = job.source_post
    city_name = job.city.name if job.city else 'der Stadt'
    try:
        client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY', ''))
        prompt = f"""Generiere 3 neue Instagram-Captions für einen recycelten Meme-Post aus {city_name}.

Original-Caption: "{source.caption or ''}"
Post-Typ: {source.post_type or 'feed'}

Die neue Caption soll frisch klingen, nicht identisch mit dem Original sein, aber zum selben Bild passen.
Antworte NUR mit JSON: {{"captions":["Caption 1","Caption 2","Caption 3"]}}"""

        resp = client.messages.create(
            model='claude-haiku-4-5-20251001', max_tokens=500,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = resp.content[0].text.strip()
        start, end = raw.find('{'), raw.rfind('}') + 1
        data = json.loads(raw[start:end])
        return jsonify({'captions': data.get('captions', [])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════════════════

def _seed_market():
    if CityMarketEntry.query.count() > 0:
        return
    cities_100 = [
        (1,'Berlin','Berlin',3755251),(2,'Hamburg','Hamburg',1906411),
        (3,'München','Bayern',1512491),(4,'Köln','Nordrhein-Westfalen',1084394),
        (5,'Frankfurt am Main','Hessen',773068),(6,'Stuttgart','Baden-Württemberg',634830),
        (7,'Düsseldorf','Nordrhein-Westfalen',645000),(8,'Leipzig','Sachsen',620523),
        (9,'Dortmund','Nordrhein-Westfalen',588462),(10,'Essen','Nordrhein-Westfalen',578087),
        (11,'Bremen','Bremen',571403),(12,'Dresden','Sachsen',561922),
        (13,'Hannover','Niedersachsen',535932),(14,'Nürnberg','Bayern',518365),
        (15,'Duisburg','Nordrhein-Westfalen',495885),(16,'Bochum','Nordrhein-Westfalen',364920),
        (17,'Wuppertal','Nordrhein-Westfalen',356293),(18,'Bielefeld','Nordrhein-Westfalen',341755),
        (19,'Bonn','Nordrhein-Westfalen',335988),(20,'Münster','Nordrhein-Westfalen',317763),
        (21,'Karlsruhe','Baden-Württemberg',313092),(22,'Mannheim','Baden-Württemberg',309370),
        (23,'Augsburg','Bayern',295135),(24,'Wiesbaden','Hessen',284665),
        (25,'Gelsenkirchen','Nordrhein-Westfalen',259645),(26,'Mönchengladbach','Nordrhein-Westfalen',259536),
        (27,'Braunschweig','Niedersachsen',249406),(28,'Chemnitz','Sachsen',244517),
        (29,'Aachen','Nordrhein-Westfalen',245885),(30,'Kiel','Schleswig-Holstein',246243),
        (31,'Halle (Saale)','Sachsen-Anhalt',237865),(32,'Magdeburg','Sachsen-Anhalt',237475),
        (33,'Freiburg im Breisgau','Baden-Württemberg',232198),(34,'Krefeld','Nordrhein-Westfalen',225144),
        (35,'Mainz','Rheinland-Pfalz',217556),(36,'Lübeck','Schleswig-Holstein',216277),
        (37,'Erfurt','Thüringen',214966),(38,'Rostock','Mecklenburg-Vorpommern',208886),
        (39,'Oberhausen','Nordrhein-Westfalen',206465),(40,'Kassel','Hessen',201048),
        (41,'Hagen','Nordrhein-Westfalen',188814),(42,'Hamm','Nordrhein-Westfalen',179634),
        (43,'Saarbrücken','Saarland',179349),(44,'Potsdam','Brandenburg',183391),
        (45,'Mülheim an der Ruhr','Nordrhein-Westfalen',170632),(46,'Osnabrück','Niedersachsen',165109),
        (47,'Heidelberg','Baden-Württemberg',161485),(48,'Darmstadt','Hessen',160279),
        (49,'Ludwigshafen am Rhein','Rheinland-Pfalz',163196),(50,'Oldenburg','Niedersachsen',169077),
        (51,'Solingen','Nordrhein-Westfalen',158726),(52,'Leverkusen','Nordrhein-Westfalen',163478),
        (53,'Herne','Nordrhein-Westfalen',155875),(54,'Neuss','Nordrhein-Westfalen',151924),
        (55,'Paderborn','Nordrhein-Westfalen',151877),(56,'Regensburg','Bayern',155519),
        (57,'Ingolstadt','Bayern',140140),(58,'Offenbach am Main','Hessen',132448),
        (59,'Fürth','Bayern',130305),(60,'Ulm','Baden-Württemberg',126790),
        (61,'Würzburg','Bayern',127966),(62,'Heilbronn','Baden-Württemberg',126592),
        (63,'Pforzheim','Baden-Württemberg',125542),(64,'Wolfsburg','Niedersachsen',124371),
        (65,'Göttingen','Niedersachsen',119529),(66,'Bottrop','Nordrhein-Westfalen',115677),
        (67,'Reutlingen','Baden-Württemberg',115818),(68,'Erlangen','Bayern',113758),
        (69,'Bremerhaven','Bremen',113557),(70,'Koblenz','Rheinland-Pfalz',113961),
        (71,'Bergisch Gladbach','Nordrhein-Westfalen',111965),(72,'Remscheid','Nordrhein-Westfalen',110994),
        (73,'Jena','Thüringen',111443),(74,'Trier','Rheinland-Pfalz',111631),
        (75,'Moers','Nordrhein-Westfalen',104637),(76,'Siegen','Nordrhein-Westfalen',102583),
        (77,'Hildesheim','Niedersachsen',98073),(78,'Kaiserslautern','Rheinland-Pfalz',97232),
        (79,'Gütersloh','Nordrhein-Westfalen',101070),(80,'Cottbus','Brandenburg',99700),
        (81,'Salzgitter','Niedersachsen',101767),(82,'Hamm','Nordrhein-Westfalen',179634),
        (83,'Hanau','Hessen',98041),(84,'Witten','Nordrhein-Westfalen',96787),
        (85,'Schwerin','Mecklenburg-Vorpommern',95941),(86,'Gera','Thüringen',93125),
        (87,'Zwickau','Sachsen',91175),(88,'Esslingen am Neckar','Baden-Württemberg',91808),
        (89,'Ludwigsburg','Baden-Württemberg',93000),(90,'Iserlohn','Nordrhein-Westfalen',93000),
        (91,'Marl','Nordrhein-Westfalen',84606),(92,'Heidenheim an der Brenz','Baden-Württemberg',50000),
        (93,'Flensburg','Schleswig-Holstein',90164),(94,'Tübingen','Baden-Württemberg',91788),
        (95,'Villingen-Schwenningen','Baden-Württemberg',84000),(96,'Ratingen','Nordrhein-Westfalen',89000),
        (97,'Lünen','Nordrhein-Westfalen',86000),(98,'Velbert','Nordrhein-Westfalen',82000),
        (99,'Minden','Nordrhein-Westfalen',82000),(100,'Konstanz','Baden-Württemberg',84000),
    ]
    seen_names = set()
    for rank, name, state, pop in cities_100:
        if name in seen_names:
            continue
        seen_names.add(name)
        db.session.add(CityMarketEntry(rank=rank, name=name, state=state, population=pop))
    db.session.commit()


with app.app_context():
    _seed_market()


# ── ERWEITERUNGEN (Phase B) ────────────────────────
# register_extensions(app) hängt die Phase-B-Module ein. Jedes Modul ist eine eigene Datei mit
# init_app(flask_app); ein defektes Modul darf die App nicht killen (try/except + log.error).
# Threads (Render-Worker, Scheduler) starten nur, wenn TESTING falsch ist und MEMEOS_WORKERS bzw.
# MEMEOS_SCHEDULER nicht '0' sind – das entscheiden die Module selbst.
# Reihenfolge: memeos_render (nur Import, oben), studio, pptx_import, vision_import,
# inspiration_fetch, render_queue, scheduler, selftest (als letztes: sieht alle Routen).
_EXTENSION_MODULES = (
    'studio_bp',              # B2 Template-Studio (/studio/<id>, /api/studio/…)
    'pptx_import_bp',         # B3 Template-Import aus Canva/PPTX
    'vision_import_bp',       # B6 Bild-Import mit KI-Erkennung
    'inspiration_fetch_bp',   # B7 Inspiration-Abholung über RapidAPI
    'render_queue',           # B4 persistente Render-Queue → Vorrat
    'scheduler',              # B5 Automatik (RSS/Events/Wetter/Digest/Telegram-Poll)
    'selftest_bp',            # B8 Selbsttest
)
_loaded_extensions = []

def register_extensions(flask_app):
    import importlib
    if memeos_render is None:
        log.error('memeos_render nicht importierbar – Rendern läuft mit der alten Pillow-Implementierung')
    for mod_name in _EXTENSION_MODULES:
        try:
            mod = importlib.import_module(mod_name)
            mod.init_app(flask_app)
            _loaded_extensions.append(mod_name)
        except Exception as ex:
            log.error(f'Erweiterung {mod_name} nicht geladen: {ex}', exc_info=True)
    log.info('Erweiterungen geladen: ' + (', '.join(_loaded_extensions) or 'keine'))

register_extensions(app)


if __name__ == '__main__':
    app.run(debug=True, port=5200)
