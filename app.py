import os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template_string
import googleapiclient.discovery
import requests
import isodate

app = Flask(__name__)

API_KEY = os.environ.get("YOUTUBE_API_KEY")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GIST_ID = os.environ.get("GIST_ID")

SAVE_LOCAL_CACHE = True          
LOCAL_CACHE_FILE = "cache_local.json"

CHANNEL_IDENTIFIER = "UCg_z7bjf2hQwMwafHhxMmNQ"  # ID del canal
USE_USERNAME = False

GIST_API_URL = f"https://api.github.com/gists/{GIST_ID}"
GIST_RAW_URL = f"https://gist.githubusercontent.com/anonymous/{GIST_ID}/raw/cache.json"
HEADERS = {
    'Authorization': f'token {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github.v3+json',
    'User-Agent': 'YouTubeMirrorApp'
}

def gist_get():
    """Intenta leer primero del Gist, si falla → intenta del archivo local"""
    # 1. Intenta desde GitHub Gist 
    try:
        response = requests.get(GIST_API_URL, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            data = response.json()
            content = data['files']['cache.json']['content']
            cache = json.loads(content)
            print("Cache cargado desde GitHub Gist")
            # Guardamos también localmente como backup
            if SAVE_LOCAL_CACHE:
                with open(LOCAL_CACHE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(cache, f, indent=2, default=str)
            return cache
    except Exception as e:
        print(f"Error leyendo Gist: {e}")

    # 2. Fallback: intenta desde archivo local
    if SAVE_LOCAL_CACHE and os.path.exists(LOCAL_CACHE_FILE):
        try:
            with open(LOCAL_CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
                print("Cache cargado desde archivo local (fallback)")
                return cache
        except Exception as e:
            print(f"Error leyendo archivo local: {e}")

    return None


def gist_update(cache_data):
    """Actualiza Gist + archivo local (si está activado)"""
    success = False

    # 1. Actualizar Gist
    try:
        gist_data = {
            "description": "Caché de canal YouTube (mirror)",
            "files": {
                "cache.json": {
                    "content": json.dumps(cache_data, default=str, indent=2)
                }
            }
        }
        response = requests.patch(GIST_API_URL, headers=HEADERS, json=gist_data, timeout=15)
        if response.status_code == 200:
            success = True
            print("Gist actualizado correctamente")
    except Exception as e:
        print(f"Error actualizando Gist: {e}")

    # 2. Guardar también localmente
    if SAVE_LOCAL_CACHE:
        try:
            with open(LOCAL_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, indent=2, default=str)
            print("Cache guardado localmente")
        except Exception as e:
            print(f"Error guardando cache local: {e}")

    if not success:
        raise RuntimeError("No se pudo actualizar el caché en ningún sitio")

def get_channel_details(youtube):
    request = youtube.channels().list(
        part="snippet,contentDetails",
        id=CHANNEL_IDENTIFIER if not USE_USERNAME else None,
        forUsername=CHANNEL_IDENTIFIER if USE_USERNAME else None
    )
    response = request.execute()
    if not response.get('items'):
        raise ValueError("Canal no encontrado.")
    channel = response['items'][0]
    return (
        channel['snippet']['title'],
        channel['snippet']['description'],
        channel['contentDetails']['relatedPlaylists']['uploads']
    )

def get_all_videos(youtube, playlist_id, published_after=None):
    """
    Trae todos los vídeos de una playlist de uploads.
    Si se pasa published_after (ISO 8601 con Z), solo devuelve vídeos más nuevos que esa fecha.
    """
    videos = []
    next_page_token = None
    cutoff = None
    if published_after:
        try:
            cutoff = isodate.parse_datetime(published_after)
        except:
            cutoff = None

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
            published_at_str = snippet["publishedAt"]
            published_at = isodate.parse_datetime(published_at_str)

            # Si hay filtro de fecha y el vídeo es antiguo → saltar
            if cutoff and published_at <= cutoff:
                continue

            video = {
                'title': snippet['title'],
                'description': snippet['description'],
                'video_id': item["contentDetails"]["videoId"],
                'thumbnail': snippet['thumbnails']['high']['url'] if 'high' in snippet['thumbnails'] else snippet['thumbnails']['default']['url'],
                'published_at': published_at_str
            }
            videos.append(video)

        next_page_token = response.get('nextPageToken')
        if not next_page_token:
            break

    # Ordenar de más nuevo a más antiguo
    videos.sort(key=lambda x: x['published_at'], reverse=True)
    return videos

@app.route('/')
def channel_mirror():
    youtube = googleapiclient.discovery.build(
    "youtube",
    "v3",
    developerKey=API_KEY,
    cache_discovery=False,
    static_discovery=False
    )

    cache = gist_get()
    channel_title = channel_description = None
    all_videos = []          # <-- todos los vídeos + shorts
    shorts = []              # <-- solo shorts (duration < 60 segundos o título con #shorts, etc.)

    if cache and 'videos' in cache and cache['videos']:
        channel_title = cache['channel_title']
        channel_description = cache['channel_description']
        all_videos = cache['videos']

        # Separar Shorts (método fiable: duración < 65 segundos)
        shorts = []
        regular_videos = []
        for v in all_videos:
            # Intentamos obtener duración real
            try:
                video_response = youtube.videos().list(
                    part="contentDetails",
                    id=v['video_id']
                ).execute()
                duration = video_response['items'][0]['contentDetails']['duration']
                dur_seconds = isodate.parse_duration(duration).total_seconds()
                if dur_seconds <= 65:  # YouTube considera Short hasta ~60-65s
                    shorts.append(v)
                else:
                    regular_videos.append(v)
            except:
                # Fallback: si falla la API, usamos heurística simple
                if any(x in v['title'].lower() for x in ['#shorts', '#short', 'shorts', 'short:']):
                    shorts.append(v)
                else:
                    regular_videos.append(v)

        # Guardar en caché para próximas visitas (evita volver a calcular)
        cache['shorts'] = shorts
        cache['regular_videos'] = regular_videos
        gist_update(cache)

    else:
        # Primera vez: descarga todo
        print("Descargando canal completo...")
        channel_title, channel_description, uploads_playlist_id = get_channel_details(youtube)
        all_videos = get_all_videos(youtube, uploads_playlist_id)

        # Separar Shorts igual que arriba
        shorts = []
        regular_videos = []
        video_ids_batch = [v['video_id'] for v in all_videos]

        # Llamada en lote para obtener duraciones (más eficiente)
        for i in range(0, len(video_ids_batch), 50):
            batch = video_ids_batch[i:i+50]
            response = youtube.videos().list(part="contentDetails", id=",".join(batch)).execute()
            duration_map = {
                item['id']: isodate.parse_duration(item['contentDetails']['duration']).total_seconds()
                for item in response.get('items', [])
            }
            for v in all_videos[i:i+50]:
                dur = duration_map.get(v['video_id'], 999)
                if dur <= 65:
                    shorts.append(v)
                else:
                    regular_videos.append(v)

        all_videos = regular_videos  # por defecto mostramos vídeos normales
        cache = {
            'channel_title': channel_title,
            'channel_description': channel_description,
            'videos': regular_videos + shorts,  # guardamos todos
            'shorts': shorts,
            'regular_videos': regular_videos,
            'last_update': datetime.utcnow().isoformat() + 'Z'
        }
        gist_update(cache)

    # HTML con pestañas Vídeos / Shorts
    html = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{{ channel_title }} - Mirror</title>
        <style>
            body { font-family: system-ui, sans-serif; margin: 0; background: #000; color: #fff; }
            header { background: #c00; padding: 15px; text-align: center; position: sticky; top: 0; z-index: 100; }
            h1 { margin: 0; font-size: 1.8em; }
            .tabs { display: flex; justify-content: center; gap: 20px; margin: 20px 0; }
            .tab-btn { padding: 12px 30px; font-size: 1.1em; background: #333; border: none; color: white; cursor: pointer; border-radius: 8px; transition: 0.3s; }
            .tab-btn.active { background: #c00; font-weight: bold; }
            .tab-btn:hover { background: #a00; }
            .description { background: rgba(255,255,255,0.1); padding: 15px; margin: 20px; border-radius: 10px; }
            .video-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 20px; padding: 20px; }
            .video-card { background: #111; border-radius: 12px; overflow: hidden; transition: transform 0.2s; }
            .video-card:hover { transform: scale(1.03); }
            .video-card img { width: 100%; height: 180px; object-fit: cover; }
            .video-info { padding: 12px; }
            .video-info h3 { margin: 0 0 8px; font-size: 1.1em; }
            .video-info p { margin: 0; color: #ccc; font-size: 0.9em; }
            .short-badge { position: absolute; top: 10px; left: 10px; background: #c00; padding: 4px 8px; border-radius: 4px; font-size: 0.8em; }
            .meta { font-size: 0.8em; color: #888; margin-top: 8px; }
            @media (max-width: 600px) {
                .video-grid { grid-template-columns: 1fr; }
                .tab-btn { padding: 10px 20px; font-size: 1em; }
            }
        </style>
    </head>
    <body>
        <header>
            <h1>{{ channel_title }}</h1>
        </header>

        <div class="tabs">
            <button class="tab-btn active" onclick="showTab('videos')">Vídeos</button>
            <button class="tab-btn" onclick="showTab('shorts')">Shorts ({{ shorts|length }})</button>
        </div>

        <div class="description">{{ channel_description }}</div>

        <!-- Vídeos normales -->
        <div id="videos" class="tab-content">
            <div class="video-grid">
                {% for video in regular_videos %}
                <div class="video-card">
                    <a href="https://www.youtube.com/watch?v={{ video.video_id }}" target="_blank" rel="noopener">
                        <img src="{{ video.thumbnail }}" alt="{{ video.title }}">
                        <div class="video-info">
                            <h3>{{ video.title }}</h3>
                            <p>{{ video.description[:120] }}{% if video.description|length > 120 %}...{% endif %}</p>
                            <div class="meta">{{ video.published_at[:10] }}</div>
                        </div>
                    </a>
                </div>
                {% endfor %}
            </div>
        </div>

        <!-- Shorts -->
        <div id="shorts" class="tab-content" style="display: none;">
            <div class="video-grid">
                {% for video in shorts %}
                <div class="video-card" style="position: relative;">
                    <span class="short-badge">SHORT</span>
                    <a href="https://www.youtube.com/shorts/{{ video.video_id }}" target="_blank" rel="noopener">
                        <img src="{{ video.thumbnail }}" alt="{{ video.title }}">
                        <div class="video-info">
                            <h3>{{ video.title }}</h3>
                            <div class="meta">{{ video.published_at[:10] }}</div>
                        </div>
                    </a>
                </div>
                {% endfor %}
            </div>
        </div>

        <script>
            function showTab(tabName) {
                document.querySelectorAll('.tab-content').forEach(el => el.style.display = 'none');
                document.getElementById(tabName).style.display = 'block';
                
                document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
                event.target.classList.add('active');
            }
        </script>
    </body>
    </html>
    """

    # Pasamos las listas separadas al template
    context = {
        'channel_title': channel_title,
        'channel_description': channel_description,
        'regular_videos': cache.get('regular_videos', all_videos),
        'shorts': cache.get('shorts', shorts)
    }

    return render_template_string(html, **context)

if __name__ == '__main__':
    app.run(debug=True)
