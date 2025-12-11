import os
import json
from datetime import datetime
from flask import Flask, render_template_string
import googleapiclient.discovery
import requests
import isodate

app = Flask(__name__)

# === CONFIG ===
API_KEY = os.environ.get("YOUTUBE_API_KEY")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GIST_ID = os.environ.get("GIST_ID")

SAVE_LOCAL_CACHE = False  # Render no permite escritura fuera de /tmp

CHANNEL_IDENTIFIER = "UCg_z7bjf2hQwMwafHhxMmNQ"

GIST_API_URL = f"https://api.github.com/gists/{GIST_ID}" if GIST_ID else None
HEADERS = {
    'Authorization': f'token {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github.v3+json',
    'User-Agent': 'YouTubeMirrorApp'
} if GITHUB_TOKEN else {}


def get_youtube_service():
    if not API_KEY:
        raise ValueError("Falta YOUTUBE_API_KEY")
    return googleapiclient.discovery.build("youtube", "v3", developerKey=API_KEY)


def gist_get():
    if not (GIST_ID and GITHUB_TOKEN):        # ← aquí estaba el error (faltaba :)
        return None

    try:
        r = requests.get(GIST_API_URL, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            content = r.json()["files"]["cache.json"]["content"]
            print("Cache cargado desde Gist")
            return json.loads(content)
    except Exception as e:
        print(f"Error leyendo Gist: {e}")
    return None


def gist_update(cache_data):
    if not (GIST_ID and GITHUB_TOKEN):
        return False

    payload = {
        "description": "YouTube mirror cache",
        "files": {"cache.json": {"content": json.dumps(cache_data, indent=2, default=str)}}
    }
    try:
        r = requests.patch(GIST_API_URL, headers=HEADERS, json=payload, timeout=15)
        if r.status_code == 200:
            print("Gist actualizado")
            return True
    except Exception as e:
        print(f"Error actualizando Gist: {e}")
    return False


def get_channel_details(youtube):
    res = youtube.channels().list(part="snippet,contentDetails", id=CHANNEL_IDENTIFIER).execute()
    if not res.get("items"):
        raise ValueError("Canal no encontrado")
    ch = res["items"][0]
    return ch["snippet"]["title"], ch["snippet"].get("description", ""), ch["contentDetails"]["relatedPlaylists"]["uploads"]


def get_all_videos(youtube, playlist_id):
    videos = []
    page_token = None
    while True:
        req = youtube.playlistItems().list(part="snippet,contentDetails", playlistId=playlist_id, maxResults=50, pageToken=page_token)
        res = req.execute()
        for item in res.get("items", []):
            sn = item["snippet"]
            if sn["title"] in ["Private video", "Deleted video"]:
                continue
            videos.append({
                "title": sn["title"],
                "description": sn.get("description", ""),
                "video_id": item["contentDetails"]["videoId"],
                "thumbnail": sn["thumbnails"].get("high", sn["thumbnails"].get("default", {})()).get("url", ""),
                "published_at": sn["publishedAt"]
            })
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    videos.sort(key=lambda x: x["published_at"], reverse=True)
    return videos


@app.route('/')
def channel_mirror():
    youtube = get_youtube_service()

    cache = gist_get()

    if cache and cache.get("videos"):
        channel_title = cache["channel_title"]
        channel_description = cache["channel_description"]
        regular_videos = cache.get("regular_videos", cache["videos"])
        shorts = cache.get("shorts", [])
        print(f"Caché usado: {len(cache['videos'])} vídeos ({len(shorts)} shorts)")
    else:
        print("Descargando canal completo...")
        channel_title, channel_description, uploads_id = get_channel_details(youtube)
        all_videos = get_all_videos(youtube, uploads_id)

        shorts = []
        regular_videos = []
        ids = [v["video_id"] for v in all_videos]

        for i in range(0, len(ids), 50):
            batch = ids[i:i+50]
            try:
                dur_res = youtube.videos().list(part="contentDetails", id=",".join(batch)).execute()
                for item in dur_res.get("items", []):
                    secs = isodate.parse_duration(item["contentDetails"]["duration"]).total_seconds()
                    vid = next(v for v in all_videos if v["video_id"] == item["id"])
                    (shorts if secs <= 65 else regular_videos).append(vid)
            except:
                for v in all_videos[i:i+50]:
                    if any(k in v["title"].lower() for k in ["#shorts", "#short", "short:"]):
                        shorts.append(v)
                    else:
                        regular_videos.append(v)

        new_cache = {
            "channel_title": channel_title,
            "channel_description": channel_description,
            "videos": all_videos,
            "regular_videos": regular_videos,
            "shorts": shorts,
            "last_update": datetime.utcnow().isoformat() + "Z"
        }
        gist_update(new_cache)

    HTML = """<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{{ title }} - Mirror</title>
<style>
    body{font-family:system-ui,sans-serif;margin:0;background:#000;color:#fff}
    header{background:#c00;padding:20px;text-align:center;position:sticky;top:0}
    h1{margin:0;font-size:2em}
    .tabs{display:flex;justify-content:center;gap:20px;margin:20px;flex-wrap:wrap}
    .tab-btn{padding:12px 30px;background:#333;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:1.1em}
    .tab-btn.active,.tab-btn:hover{background:#c00}
    .desc{background:rgba(255,255,255,.1);margin:20px;padding:20px;border-radius:12px;max-width:900px;margin:20px auto}
    .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:20px;padding:20px;max-width:1400px;margin:0 auto}
    .card{background:#111;border-radius:12px;overflow:hidden;position:relative;transition:.2s}
    .card:hover{transform:scale(1.03)}
    .card img{width:100%;height:180px;object-fit:cover}
    .info{padding:12px}
    .info h3{margin:0 0 8px;font-size:1.1em;line-height:1.3}
    .info p{color:#ccc;font-size:.9em;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
    .meta{font-size:.8em;color:#888;margin-top:8px}
    .short-badge{position:absolute;top:10px;left:10px;background:#c00;padding:4px 8px;border-radius:4px;font-weight:bold}
    @media(max-width:600px){.grid{grid-template-columns:1fr}}
</style></head><body>
<header><h1>{{ title }}</h1></header>
<div class="tabs">
    <button class="tab-btn active" onclick="show('videos')">Vídeos</button>
    <button class="tab-btn" onclick="show('shorts')">Shorts ({{ shorts|length }})</button>
</div>
<div class="desc">{{ desc }}</div>

<div id="videos" class="grid">
    {% for v in videos %}
    <div class="card"><a href="https://youtube.com/watch?v={{ v.video_id }}" target="_blank" rel="noopener">
        <img src="{{ v.thumbnail }}" alt="{{ v.title }}">
        <div class="info">
            <h3>{{ v.title }}</h3>
            <p>{{ v.description[:120] }}{% if v.description|length > 120 %}...{% endif %}</p>
            <div class="meta">{{ v.published_at[:10] }}</div>
        </div>
    </a></div>
    {% endfor %}
</div>

<div id="shorts" class="grid" style="display:none">
    {% for v in shorts %}
    <div class="card"><span class="short-badge">SHORT</span><a href="https://youtube.com/shorts/{{ v.video_id }}" target="_blank" rel="noopener">
        <img src="{{ v.thumbnail }}" alt="{{ v.title }}">
        <div class="info">
            <h3>{{ v.title }}</h3>
            <div class="meta">{{ v.published_at[:10] }}</div>
        </div>
    </a></div>
    {% endfor %}
</div>

<script>
function show(id){
    document.querySelectorAll('.grid').forEach(e=>e.style.display='none');
    document.getElementById(id).style.display='grid';
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
    event.target.classList.add('active');
}
</script>
</body></html>"""

    return render_template_string(HTML,
        title=channel_title or "YouTube Mirror",
        desc=channel_description or "Sin descripción",
        videos=regular_videos,
        shorts=shorts
    )


if __name__ == '__main__':
    app.run(debug=True)
