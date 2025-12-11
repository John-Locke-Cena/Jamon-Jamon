import os
import json
from datetime import datetime
from flask import Flask, render_template_string
import googleapiclient.discovery
from googleapiclient.discovery import build
import requests
import isodate

app = Flask(__name__)

# === CONFIGURACIÓN ===
API_KEY = os.environ.get("YOUTUBE_API_KEY")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GIST_ID = os.environ.get("GIST_ID")

# En Render el sistema de archivos es read-only → desactivamos cache local
SAVE_LOCAL_CACHE = False
LOCAL_CACHE_FILE = "/tmp/cache_local.json"  # si algún día quieres usarlo

CHANNEL_IDENTIFIER = "UCg_z7bjf2hQwMwafHhxMmNQ"  # ID del canal
USE_USERNAME = False

# URLs del Gist
GIST_API_URL = f"https://api.github.com/gists/{GIST_ID}" if GIST_ID else None
GIST_RAW_URL = f"https://gist.githubusercontent.com/anonymous/{GIST_ID}/raw/cache.json" if GIST_ID else None

HEADERS = {
    'Authorization': f'token {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github.v3+json',
    'User-Agent': 'YouTubeMirrorApp'
} if GITHUB_TOKEN else {}

def get_youtube_service():
    """Construye el cliente de YouTube SIN usar Application Default Credentials"""
    return build(
        "youtube", "v3",
        developerKey=API_KEY,
        cache_discovery=False,      # ← esencial
        static_discovery=False,     # ← esencial también
    )

def gist_get():
    """Lee el caché desde Gist. Si falla, devuelve None (no rompe nada)"""
    if not (GIST_ID and GITHUB_TOKEN):
        print("Gist no configurado (falta GIST_ID o GITHUB_TOKEN)")
        return None

    try:
        response = requests.get(GIST_API_URL, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            content = response.json()['files']['cache.json']['content']
            cache = json.loads(content)
            print("Cache cargado desde GitHub Gist")
            return cache
        else:
            print(f"Gist devolvió {response.status_code}: {response.text}")
    except Exception as e:
        print(f"Error leyendo Gist: {e}")

    return None


def gist_update(cache_data):
    """Actualiza el Gist. Si falla, NO rompe la web (solo avisa)"""
    if not (GIST_ID and GITHUB_TOKEN):
        print("No se puede actualizar Gist: faltan credenciales")
        return False

    try:
        gist_data = {
            "description": "Caché de canal YouTube (mirror) – actualizado automáticamente",
            "files": {
                "cache.json": {
                    "content": json.dumps(cache_data, default=str, indent=2)
                }
            }
        }
        response = requests.patch(GIST_API_URL, headers=HEADERS, json=gist_data, timeout=15)
        if response.status_code == 200:
            print("Gist actualizado correctamente")
            return True
        else:
            print(f"Error actualizando Gist ({response.status_code}): {response.text}")
    except Exception as e:
        print(f"Excepción al actualizar Gist: {e}")

    return False


def get_channel_details(youtube):
    request = youtube.channels().list(
        part="snippet,contentDetails",
        id=CHANNEL_IDENTIFIER
    )
    response = request.execute()
    items = response.get('items', [])
    if not items:
        raise ValueError("Canal no encontrado")
    channel = items[0]
    return (
        channel['snippet']['title'],
        channel['snippet'].get('description', ''),
        channel['contentDetails']['relatedPlaylists']['uploads']
    )


def get_all_videos(youtube, playlist_id):
    videos = []
    next_page_token = None

    while True:
        request = youtube.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=next_page_token
        )
        response = request.execute()

        for item in response.get("items", []):
            snippet = item["snippet"]
            if snippet.get("title") == "Private video" or snippet.get("title") == "Deleted video":
                continue

            videos.append({
                'title': snippet['title'],
                'description': snippet.get('description', ''),
                'video_id': item["contentDetails"]["videoId"],
                'thumbnail': snippet['thumbnails'].get('high', snippet['thumbnails'].get('default', {})).get('url', ''),
                'published_at': snippet['publishedAt']
            })

        next_page_token = response.get('nextPageToken')
        if not next_page_token:
            break

    videos.sort(key=lambda x: x['published_at'], reverse=True)
    return videos


@app.route('/')
def channel_mirror():
    # Si no hay API key → error claro
    if not API_KEY:
        return "Falta YOUTUBE_API_KEY en variables de entorno", 500

    youtube = get_youtube_service()

    cache = gist_get()
    now = datetime.utcnow()

    # Si hay caché válido, usarlo
    if cache and cache.get('videos'):
        channel_title = cache['channel_title']
        channel_description = cache['channel_description']
        all_videos = cache['videos']
        shorts = cache.get('shorts', [])
        regular_videos = cache.get('regular_videos', all_videos)
        print(f"Usando caché con {len(all_videos)} vídeos ({len(shorts)} shorts)")
    else:
        # Sin caché → descargar todo
        print("Sin caché o vacío → descargando canal completo...")
        try:
            channel_title, channel_description, uploads_playlist_id = get_channel_details(youtube)
        except Exception as e:
            return f"Error obteniendo datos del canal: {e}", 500

        all_videos = get_all_videos(youtube, uploads_playlist_id)

        # Clasificar Shorts por duración (lotes de 50)
        shorts = []
        regular_videos = []
        video_ids = [v['video_id'] for v in all_videos]

        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i+50]
            try:
                resp = youtube.videos().list(part="contentDetails", id=",".join(batch)).execute()
                for item in resp.get('items', []):
                    dur = isodate.parse_duration(item['contentDetails']['duration']).total_seconds()
                    video_id = item['id']
                    video = next(v for v in all_videos if v['video_id'] == video_id)
                    if dur <= 65:
                        shorts.append(video)
                    else:
                        regular_videos.append(video)
            except:
                # Si falla la duración → fallback por título
                for v in all_videos[i:i+50]:
                    if any(k in v['title'].lower() for k in ['#shorts', '#short', 'short:']):
                        shorts.append(v)
                    else:
                        regular_videos.append(v)

        # Guardar en Gist (pero no romper si falla)
        cache = {
            'channel_title': channel_title,
            'channel_description': channel_description,
            'videos': all_videos,
            'shorts': shorts,
            'regular_videos': regular_videos,
            'last_update': now.isoformat() + 'Z'
        }
        gist_update(cache)  # fuego y olvido

    # === Render HTML ===
    html = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ channel_title }} - Mirror</title>
    <style>
        body {font-family:system-ui,sans-serif;margin:0;background:#000;color:#fff}
        header {background:#c00;padding:20px;text-align:center;position:sticky;top:0;z-index:100}
        h1 {margin:0;font-size:2em}
        .tabs {display:flex;justify-content:center;gap:20px;margin:20px 0;flex-wrap:wrap}
        .tab-btn {padding:12px 30px;font-size:1.1em;background:#333;border:none;color:white;cursor:pointer;border-radius:8px;transition:.3s}
        .tab-btn.active {background:#c00;font-weight:bold}
        .tab-btn:hover {background:#a00}
        .description {background:rgba(255,255,255,0.1);padding:20px;margin:20px;border-radius:12px;max-width:900px;margin:20px auto}
        .video-grid {display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:20px;padding:20px;max-width:1400px;margin:0 auto}
        .video-card {background:#111;border-radius:12px;overflow:hidden;transition:.2s;position:relative}
        .video-card:hover {transform:scale(1.03)}
        .video-card img {width:100%;height:180px;object-fit:cover}
        .video-info {padding:12px}
        .video-info h3 {margin:0 0 8px;font-size:1.1em;line-height:1.3}
        .video-info p {margin:0;color:#ccc;font-size:0.9em;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}
        .meta {font-size:0.8em;color:#888;margin-top:8px}
        .short-badge {position:absolute;top:10px;left:10px;background:#c00;padding:4px 8px;border-radius:4px;font-size:0.8em;font-weight:bold}
        @media(max-width:600px){.video-grid{grid-template-columns:1fr}.tabs{flex-direction:column;align-items:center}}
    </style>
</head>
<body>
    <header><h1>{{ channel_title }}</h1></header>
    <div class="tabs">
        <button class="tab-btn active" onclick="show('videos')">Vídeos</button>
        <button class="tab-btn" onclick="show('shorts')">Shorts ({{ shorts|length }})</button>
    </div>
    <div class="description">{{ channel_description }}</div>

    <div id="videos" class="tab-content">
        <div class="video-grid">
            {% for v in regular_videos %}
            <div class="video-card">
                <a href="https://www.youtube.com/watch?v={{ v.video_id }}" target="_blank" rel="noopener">
                    <img src="{{ v.thumbnail }}" alt="{{ v.title }}">
                    <div class="video-info">
                        <h3>{{ v.title }}</h3>
                        <p>{{ v.description[:120] }}{% if v.description|length > 120 %}...{% endif %}</p>
                        <div class="meta">{{ v.published_at[:10] }}</div>
                    </div>
                </a>
            </div>
            {% endfor %}
        </div>
    </div>

    <div id="shorts" class="tab-content" style="display:none">
        <div class="video-grid">
            {% for v in shorts %}
            <div class="video-card">
                <span class="short-badge">SHORT</span>
                <a href="https://www.youtube.com/shorts/{{ v.video_id }}" target="_blank" rel="noopener">
                    <img src="{{ v.thumbnail }}" alt="{{ v.title }}">
                    <div class="video-info">
                        <h3>{{ v.title }}</h3>
                        <div class="meta">{{ v.published_at[:10] }}</div>
                    </div>
                </a>
            </div>
            {% endfor %}
        </div>
    </div>

    <script>
        function show(id) {
            document.querySelectorAll('.tab-content').forEach(e=>e.style.display='none');
            document.getElementById(id).style.display='block';
            document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
            event.target.classList.add('active');
        }
    </script>
</body>
</html>"""

    return render_template_string(html,
        channel_title=channel_title or "Canal YouTube",
        channel_description=channel_description or "Sin descripción",
        regular_videos=regular_videos,
        shorts=shorts
    )


if __name__ == '__main__':
    app.run(debug=True)
