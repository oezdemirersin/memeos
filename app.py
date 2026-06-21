import os
import json
import time
import secrets
import hashlib
import threading
import urllib.parse
import uuid
import requests
import feedparser
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, session, flash, send_from_directory, abort)
from models import (db, User, City, CityKnowledge, MemeTemplate, RenderJob,
                    NewsItem, ResidentSurvey, AppSettings, AppTodo, AiUsageLog,
                    CityMarketEntry, BuyablePage,
                    MemoInspirationSource, MemoInspirationPost,
                    MemePost, TrendingTopic, RecycleJob, CityFollowerSnapshot,
                    KNOWLEDGE_CATEGORIES, CATEGORY_MAP)
import anthropic
import logging

# ── Setup ──────────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'memeos-dev-secret-change-in-prod')

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH  = os.path.join(_BASE_DIR, 'instance', 'memeos.db')
os.makedirs(os.path.join(_BASE_DIR, 'instance'), exist_ok=True)
os.makedirs(os.path.join(_BASE_DIR, 'static', 'renders'), exist_ok=True)
os.makedirs(os.path.join(_BASE_DIR, 'static', 'uploads'), exist_ok=True)
os.makedirs(os.path.join(_BASE_DIR, 'static', 'exports'), exist_ok=True)

# In-memory ZIP job store: job_id -> {status, path, filename, post_count, error}
_zip_jobs: dict = {}

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


def _build_zip_async(job_id: str, post_ids: list, status_filter: str, city_id_filter):
    import zipfile, io, csv as csv_mod
    with app.app_context():
        try:
            _zip_jobs[job_id]['status'] = 'building'
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
                _zip_jobs[job_id].update({'status': 'error', 'error': 'Keine Posts gefunden'})
                return

            fname = f'memeos_export_{datetime.utcnow().strftime("%Y%m%d_%H%M")}_{job_id[:6]}.zip'
            zip_path = os.path.join(_BASE_DIR, 'static', 'exports', fname)

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
                    elif p.image_path:
                        base = os.path.basename(p.image_path)
                        for folder in ('static/uploads', 'static/renders'):
                            candidate = os.path.join(_BASE_DIR, folder, base)
                            if os.path.exists(candidate):
                                with open(candidate, 'rb') as fh:
                                    img_bytes = fh.read()
                                ext = base.rsplit('.', 1)[-1].lower() if '.' in base else 'jpg'
                                img_filename = f'post_{p.id}_{city_slug}.{ext}'
                                break
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

            _zip_jobs[job_id].update({
                'status': 'ready', 'path': zip_path,
                'filename': fname, 'post_count': len(posts),
            })
        except Exception as e:
            log.error(f'ZIP build failed for job {job_id}: {e}')
            _zip_jobs[job_id].update({'status': 'error', 'error': str(e)})


app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

db.init_app(app)

# ── Env ────────────────────────────────────────────────────────────────────────
ADMIN_USERNAME    = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD    = os.getenv('ADMIN_PASSWORD', 'memeos2025')
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

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '')
        # DB-User
        user = User.query.filter_by(username=u, active=True).first()
        if user and user.check_password(p):
            session['logged_in'] = True
            session['username']  = u
            user.last_login = datetime.utcnow()
            db.session.commit()
            return redirect(url_for('dashboard'))
        # Env-Fallback
        elif u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
            session['logged_in'] = True
            session['username']  = u
            return redirect(url_for('dashboard'))
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
        market_summary=market_summary,
        inspo_counts=inspo_counts,
        vorrat_counts=vorrat_counts,
        now=datetime.utcnow(),
    )

# ── Static renders ─────────────────────────────────────────────────────────────
@app.route('/renders/<path:filename>')
@login_required
def serve_render(filename):
    return send_from_directory(os.path.join(_BASE_DIR, 'static', 'renders'), filename)

@app.route('/uploads/<path:filename>')
@login_required
def serve_upload(filename):
    return send_from_directory(os.path.join(_BASE_DIR, 'static', 'uploads'), filename)

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

    # Avg engagement_rate per city (last 30 days, published posts with reach data)
    cutoff = datetime.utcnow() - timedelta(days=30)
    er_rows = db.session.query(
        MemePost.city_id, sqlfunc.avg(MemePost.engagement_rate)
    ).filter(
        MemePost.city_id.in_(city_ids),
        MemePost.status == 'veroeffentlicht',
        MemePost.published_at >= cutoff,
        MemePost.perf_reach.isnot(None),
    ).group_by(MemePost.city_id).all()
    er_map = {cid: round(float(er), 2) if er else None for cid, er in er_rows}

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
                  'accent_color','rss_url','notes','active']:
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
        category=d['category'],
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
            setattr(e, field, d[field])
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

@app.route('/api/templates', methods=['GET'])
@login_required
def api_templates_list():
    templates = MemeTemplate.query.order_by(MemeTemplate.name).all()
    return jsonify([{
        'id': t.id, 'name': t.name, 'description': t.description,
        'canva_template_id': t.canva_template_id,
        'required_vars': t.get_required_vars(),
        'tags': t.get_tags(),
        'category': t.category,
        'preview_image': t.preview_image,
        'example_text': t.example_text,
        'has_canva': t.has_canva(),
        'use_count': t.use_count,
        'active': t.active,
        'seasonal_from': t.seasonal_from,
        'seasonal_to': t.seasonal_to,
        'min_population': t.min_population,
    } for t in templates])

@app.route('/api/templates', methods=['POST'])
@login_required
def api_template_create():
    d = request.json or {}
    if not d.get('name'):
        return jsonify({'error': 'Name fehlt'}), 400
    t = MemeTemplate(
        name=d['name'].strip(),
        description=d.get('description', ''),
        canva_template_id=d.get('canva_template_id', ''),
        required_vars=json.dumps(d.get('required_vars', [])),
        canva_field_map=json.dumps(d.get('canva_field_map', {})),
        tags=json.dumps(d.get('tags', [])),
        category=d.get('category', 'allgemein'),
        example_text=d.get('example_text', ''),
        seasonal_from=d.get('seasonal_from', ''),
        seasonal_to=d.get('seasonal_to', ''),
        min_population=int(d.get('min_population', 0)),
    )
    db.session.add(t)
    db.session.commit()
    return jsonify({'id': t.id}), 201

@app.route('/api/templates/<int:tmpl_id>', methods=['PUT'])
@login_required
def api_template_update(tmpl_id):
    t = MemeTemplate.query.get_or_404(tmpl_id)
    d = request.json or {}
    for field in ['name','description','canva_template_id','category',
                  'example_text','seasonal_from','seasonal_to','min_population','active']:
        if field in d:
            setattr(t, field, d[field])
    if 'required_vars' in d:
        t.required_vars = json.dumps(d['required_vars'])
    if 'canva_field_map' in d:
        t.canva_field_map = json.dumps(d['canva_field_map'])
    if 'tags' in d:
        t.tags = json.dumps(d['tags'])
    db.session.commit()
    return jsonify({'ok': True})

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
    f.save(os.path.join(_BASE_DIR, 'static', 'uploads', filename))
    t.preview_image = filename
    db.session.commit()
    return jsonify({'filename': filename})

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
            os.remove(os.path.join(_BASE_DIR, 'static', 'renders', job.image_filename))
        except Exception:
            pass
    db.session.delete(job)
    db.session.commit()
    return jsonify({'ok': True})

def _job_to_dict(j):
    return {
        'id': j.id,
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

def _run_generate_job(flask_app, job_id):
    with flask_app.app_context():
        job = RenderJob.query.get(job_id)
        if not job:
            return
        job.status = 'running'
        db.session.commit()
        try:
            template = job.template
            city     = job.city

            # 1) Fit-Score + Variable-Matching via Claude
            result = _claude_fit_and_vars(city, template)
            job.fit_score    = result['fit_score']
            job.fit_reasoning = result['reasoning']
            job.vars_used    = json.dumps(result['vars'])
            job.manual_brief = result['brief']

            if result['fit_score'] < 40:
                job.status = 'done'
                job.error_message = f'Fit-Score zu niedrig ({result["fit_score"]}/100) — übersprungen'
                db.session.commit()
                return

            # 2) Canva Autofill (wenn Template und Verbindung vorhanden)
            if template.has_canva() and _canva_is_connected():
                png_bytes = _canva_autofill(template, result['vars'])
                if png_bytes:
                    filename = f'render_{job.id}_{int(time.time())}.png'
                    path = os.path.join(_BASE_DIR, 'static', 'renders', filename)
                    with open(path, 'wb') as f:
                        f.write(png_bytes)
                    job.image_filename = filename
                    cloud_url = _upload_cloudinary(path, folder='memeos/renders')
                    if cloud_url:
                        job.image_url = cloud_url
                        log.info(f'Job {job_id}: uploaded to Cloudinary → {cloud_url}')
                    job.status = 'done'
                else:
                    job.status = 'done'
                    job.error_message = 'Canva Autofill fehlgeschlagen — nur Brief verfügbar'
            else:
                job.status = 'done'

            job.completed_at = datetime.utcnow()

            # Verwendete Knowledge-Einträge markieren
            _mark_knowledge_used(city.id, result['vars'], template.id)

            # Template use_count erhöhen
            template.use_count = (template.use_count or 0) + 1
            db.session.commit()

        except Exception as ex:
            log.error(f'Generate Job {job_id} Error: {ex}')
            job.status = 'failed'
            job.error_message = str(ex)
            db.session.commit()


def _claude_fit_and_vars(city, template):
    if not ANTHROPIC_API_KEY:
        return {'fit_score': 75, 'reasoning': 'Kein API Key — Standard-Score', 'vars': {}, 'brief': 'Kein API Key konfiguriert'}

    knowledge = CityKnowledge.query.filter_by(city_id=city.id, active=True)\
                    .filter(CityKnowledge.cooldown_until == None)\
                    .order_by(CityKnowledge.confidence.desc()).all()

    knowledge_str = '\n'.join([
        f"- [{e.category}] {e.name} (Konfidenz: {e.confidence}, Quelle: {e.source})"
        + (f": {e.description}" if e.description else '')
        for e in knowledge
    ]) or 'Keine Knowledge-Einträge vorhanden'

    required_vars = template.get_required_vars()
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
        import re
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception as ex:
        log.error(f'Claude Fit-Score Error: {ex}')

    return {'fit_score': 50, 'reasoning': 'Fehler beim KI-Aufruf', 'vars': {}, 'brief': 'Manuell erstellen'}


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


def _canva_autofill(template, vars_dict):
    token = _canva_get_token()
    if not token:
        return None
    field_map = template.get_canva_field_map()
    data = {}
    for var_key, value in vars_dict.items():
        canva_field = field_map.get(var_key, var_key)
        data[canva_field] = {'type': 'text', 'text': str(value)}

    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    try:
        r = requests.post('https://api.canva.com/rest/v1/autofills', headers=headers, json={
            'brand_template_id': template.canva_template_id,
            'data': data,
        }, timeout=20)
        if not r.ok:
            log.warning(f'Canva Autofill Error: {r.status_code} {r.text[:150]}')
            return None
        job_id = r.json().get('job', {}).get('id')
        if not job_id:
            return None
    except Exception as ex:
        log.warning(f'Canva Autofill Request Error: {ex}')
        return None

    design_id = None
    for _ in range(20):
        time.sleep(2)
        try:
            sr = requests.get(f'https://api.canva.com/rest/v1/autofills/{job_id}',
                              headers=headers, timeout=10)
            if sr.ok:
                job_data = sr.json().get('job', {})
                status = job_data.get('status', '')
                if status == 'success':
                    design_id = job_data.get('result', {}).get('design', {}).get('id')
                    break
                elif status == 'failed':
                    return None
        except Exception:
            pass

    if not design_id:
        return None

    try:
        er = requests.post('https://api.canva.com/rest/v1/exports', headers=headers, json={
            'design_id': design_id,
            'format': {'type': 'png', 'lossless': True},
        }, timeout=20)
        if not er.ok:
            return None
        export_job_id = er.json().get('job', {}).get('id')
        if not export_job_id:
            return None
    except Exception:
        return None

    for _ in range(20):
        time.sleep(2)
        try:
            pr = requests.get(f'https://api.canva.com/rest/v1/exports/{export_job_id}',
                              headers=headers, timeout=10)
            if pr.ok:
                ej = pr.json().get('job', {})
                if ej.get('status') == 'success':
                    urls = ej.get('result', {}).get('urls', [])
                    if urls:
                        img_r = requests.get(urls[0], timeout=30)
                        if img_r.ok:
                            return img_r.content
                    break
                elif ej.get('status') == 'failed':
                    return None
        except Exception:
            pass
    return None


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
        'scope':                  'asset:read design:content:read design:content:write brand_template:read',
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
                img_path = os.path.join(_BASE_DIR, 'static', 'renders', job.image_filename)
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
    return jsonify({
        'content_os_url':       CONTENT_OS_URL or AppSettings.get('content_os_url', ''),
        'canva_connected':      _canva_is_connected(),
        'canva_client_id':      bool(CANVA_CLIENT_ID),
        'ai_key_set':           bool(ANTHROPIC_API_KEY),
        'ai_cost_month':        _ai_cost_this_month(),
        'telegram_token':       AppSettings.get('telegram_token', ''),
        'telegram_chat_id':     AppSettings.get('telegram_chat_id', ''),
        'alert_threshold_days': AppSettings.get('alert_threshold_days', '3'),
    })

@app.route('/api/settings', methods=['POST'])
@login_required
def api_settings_save():
    d = request.json or {}
    for key in ('content_os_url', 'telegram_token', 'telegram_chat_id', 'alert_threshold_days'):
        if key in d:
            AppSettings.set(key, d[key])
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

with app.app_context():
    db.create_all()
    _seed_todos()
    _seed_cities()

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

@app.route('/api/inspiration/sources', methods=['GET'])
@login_required
def api_inspo_sources():
    sources = MemoInspirationSource.query.order_by(MemoInspirationSource.username).all()
    return jsonify([{
        'id': s.id, 'username': s.username, 'notes': s.notes,
        'post_count': s.post_count(), 'new_count': s.new_count(),
        'last_fetch': s.last_fetch.isoformat() if s.last_fetch else None,
    } for s in sources])

@app.route('/api/inspiration/sources', methods=['POST'])
@login_required
def api_inspo_source_add():
    d = request.json or {}
    username = d.get('username', '').strip().lstrip('@')
    if not username:
        return jsonify({'error': 'Username fehlt'}), 400
    if MemoInspirationSource.query.filter_by(username=username).first():
        return jsonify({'error': 'Quelle bereits vorhanden'}), 409
    s = MemoInspirationSource(username=username, notes=d.get('notes', ''))
    db.session.add(s)
    db.session.commit()
    return jsonify({'id': s.id, 'username': s.username}), 201

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
        'instagram_code': p.instagram_code,
        'image_url': p.image_url, 'caption': p.caption,
        'like_count': p.like_count, 'media_type': p.media_type,
        'status': p.status, 'is_saved': p.is_saved,
        'meme_idea': p.meme_idea,
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
# HOCHLADEN & EINPLANEN API
# ═══════════════════════════════════════════════════════════════════════════════

ALLOWED_UPLOAD = {'jpg', 'jpeg', 'png', 'webp', 'gif', 'mp4', 'mov'}

@app.route('/api/upload/batch', methods=['POST'])
@login_required
def api_upload_batch():
    files   = request.files.getlist('files')
    city_id = request.form.get('city_id', type=int)
    if not files:
        return jsonify({'error': 'Keine Dateien'}), 400

    created = []
    upload_dir = os.path.join(_BASE_DIR, 'static', 'uploads')

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
        # Try Cloudinary, fall back to local path
        rtype = 'video' if ftype == 'video' else 'image'
        cloud_url = _upload_cloudinary(path, folder='memeos/uploads', resource_type=rtype)
        final_url = cloud_url or f'/uploads/{fname}'
        # Create draft MemePost
        post = MemePost(
            city_id=city_id,
            title=f.filename.rsplit('.', 1)[0],
            image_url=final_url,
            image_path=fname,
            post_type='feed',
            status='entwurf',
        )
        db.session.add(post)
        db.session.flush()
        created.append({
            'id':       post.id,
            'fname':    fname,
            'title':    post.title,
            'url':      post.image_url,
            'ftype':    ftype,
            'cloudinary': bool(cloud_url),
        })
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
        _log_ai_usage('upload_caption', msg.usage.input_tokens, msg.usage.output_tokens)
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
        fname = p.image_url.lstrip('/')
        local = os.path.join(_BASE_DIR, 'static', fname)
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
        local = os.path.join(_BASE_DIR, 'static', 'renders', j.image_filename)
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
        _log_ai_usage('caption_gen', msg.usage.input_tokens, msg.usage.output_tokens)
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
    _zip_jobs[job_id] = {'status': 'pending', 'path': None, 'filename': None,
                          'post_count': 0, 'error': None}
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
    job = _zip_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job nicht gefunden'}), 404
    return jsonify({
        'status': job['status'],
        'ready': job['status'] == 'ready',
        'filename': job.get('filename'),
        'post_count': job.get('post_count', 0),
        'error': job.get('error'),
    })


@app.route('/api/vorrat/export-zip/download/<job_id>')
@login_required
def api_vorrat_export_zip_download(job_id):
    job = _zip_jobs.get(job_id)
    if not job or job['status'] != 'ready':
        return jsonify({'error': 'ZIP nicht bereit'}), 400
    from flask import send_file

    def _cleanup():
        time.sleep(60)
        try:
            os.remove(job['path'])
        except Exception:
            pass
        _zip_jobs.pop(job_id, None)

    threading.Thread(target=_cleanup, daemon=True).start()
    return send_file(job['path'], mimetype='application/zip',
                     as_attachment=True, download_name=job['filename'])


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

    rc_posts = MemePost.query.filter(
        MemePost.city_id == city_id, MemePost.status == 'veroeffentlicht',
        MemePost.published_at <= datetime.utcnow() - timedelta(days=14)
    ).all()
    candidates = sorted([
        {**p.to_dict(), 'recycle_score': _recycle_score(p),
         'days_since_post': (datetime.utcnow()-p.published_at).days if p.published_at else None}
        for p in rc_posts
    ], key=lambda x: x['recycle_score'], reverse=True)[:3]

    return jsonify({
        'city': {
            'id': city.id, 'name': city.name, 'state': city.state or '',
            'accent_color': city.accent_color or '#3b82f6',
            'population': city.population, 'instagram_handle': city.instagram_handle or '',
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

@app.route('/api/kalender', methods=['GET'])
@login_required
def api_kalender():
    from_str = request.args.get('from')
    to_str   = request.args.get('to')
    try:
        from_dt = datetime.fromisoformat(from_str) if from_str else datetime.utcnow().replace(day=1, hour=0, minute=0, second=0)
        to_dt   = datetime.fromisoformat(to_str)   if to_str   else datetime(from_dt.year, from_dt.month % 12 + 1, 1)
    except:
        from_dt = datetime.utcnow()
        to_dt   = from_dt
    posts = MemePost.query.filter(
        MemePost.scheduled_at >= from_dt,
        MemePost.scheduled_at < to_dt
    ).order_by(MemePost.scheduled_at).all()
    return jsonify([p.to_dict() for p in posts])

@app.route('/api/performance', methods=['GET'])
@login_required
def api_performance():
    published = MemePost.query.filter_by(status='veroeffentlicht').all()
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
            threading.Thread(target=_run_generate_job, args=(job.id,), daemon=True).start()
    db.session.commit()
    return jsonify({'created': len(created), 'job_ids': created})


# ═══════════════════════════════════════════════════════════════════════════════
# TRENDING MONITOR
# ═══════════════════════════════════════════════════════════════════════════════

def _recycle_score(post):
    """Recycle-Score 0–100 basierend auf Performance + Zeit seit Veröffentlichung."""
    if not post.published_at:
        return 0
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
    return max(0, min(100, int(base - penalty)))


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
    city_id  = request.args.get('city_id', type=int)
    min_days = request.args.get('min_days', 14, type=int)
    cutoff   = datetime.utcnow() - timedelta(days=min_days)
    q = MemePost.query.filter(
        MemePost.status == 'veroeffentlicht',
        MemePost.published_at <= cutoff
    )
    if city_id:
        q = q.filter_by(city_id=city_id)
    posts = q.order_by(MemePost.published_at.desc()).all()
    result = []
    for p in posts:
        d = p.to_dict()
        d['recycle_score'] = _recycle_score(p)
        d['days_since_post'] = (datetime.utcnow() - p.published_at).days if p.published_at else None
        d['open_recycle_jobs'] = RecycleJob.query.filter(
            RecycleJob.source_post_id == p.id,
            RecycleJob.status.in_(['vorschlag', 'geplant'])
        ).count()
        result.append(d)
    result.sort(key=lambda x: x['recycle_score'], reverse=True)
    return jsonify(result)


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

if __name__ == '__main__':
    app.run(debug=True, port=5200)
