from flask import Flask, request, jsonify
from flask_cors import CORS
from pathlib import Path
from urllib.parse import urlparse
import os
import tempfile
import imageio_ffmpeg
import yt_dlp

app = Flask(__name__)
CORS(app, resources={r"/download": {"origins": "chrome-extension://*"}})
DOWNLOAD_DIRECTORY = Path(__file__).resolve().parent / 'downloads'


def is_youtube_url(video_url):
    try:
        hostname = (urlparse(video_url).hostname or "").lower()
        return hostname == "youtu.be" or hostname == "youtube.com" or hostname.endswith(".youtube.com")
    except ValueError:
        return False


def write_cookie_file(cookies):
    cookie_file = tempfile.NamedTemporaryFile(
        mode='w',
        encoding='utf-8',
        newline='\n',
        prefix='youtube-',
        suffix='.txt',
        delete=False,
    )
    with cookie_file:
        cookie_file.write('# Netscape HTTP Cookie File\n')
        for cookie in cookies:
            domain = str(cookie.get('domain', ''))
            name = str(cookie.get('name', ''))
            value = str(cookie.get('value', ''))
            if not domain.endswith('.youtube.com') or not name or '\t' in name or '\n' in value or '\r' in value:
                continue
            include_subdomains = 'TRUE' if domain.startswith('.') else 'FALSE'
            path = str(cookie.get('path', '/'))
            secure = 'TRUE' if cookie.get('secure') else 'FALSE'
            expires = int(cookie.get('expirationDate') or 0)
            cookie_file.write(
                f'{domain}\t{include_subdomains}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n'
            )
    return cookie_file.name


@app.route('/download', methods=['POST'])
def download_video():
    data = request.get_json(silent=True) or {}
    video_url = str(data.get('url', '')).strip()
    download_type = str(data.get('type', 'mp4')).lower()
    cookies = data.get('cookies', [])

    if not is_youtube_url(video_url):
        return jsonify({"status": "error", "message": "A valid YouTube URL is required."}), 400
    if download_type not in {'mp3', 'mp4'}:
        return jsonify({"status": "error", "message": "Download type must be MP3 or MP4."}), 400
    if not isinstance(cookies, list) or not cookies:
        return jsonify({"status": "error", "message": "YouTube cookies are required."}), 400

    cookie_file = write_cookie_file(cookies)
    ffmpeg_path = r"C:\Users\Janeesha\Desktop\Downloader-My\yt-downloader\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe"
    ydl_opts = {
        'cookiefile': cookie_file,
        'js_runtimes': {'node': {}},
        'remote_components': {'ejs:github'},
        'ffmpeg_location': ffmpeg_path,
        'outtmpl': str(DOWNLOAD_DIRECTORY / '%(title)s [%(id)s].%(ext)s'),
        'quiet': False,
        'no_warnings': False,
    }
    if download_type == 'mp3':
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
    else:
        ydl_opts.update({
            'format': 'bestvideo*+bestaudio/best',
            'merge_output_format': 'mp4',
        })

    try:
        DOWNLOAD_DIRECTORY.mkdir(exist_ok=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
        return jsonify({"status": "success", "message": f"{download_type.upper()} downloaded."})
    except Exception as error:
        return jsonify({"status": "error", "message": str(error)}), 500
    finally:
        if os.path.exists(cookie_file):
            os.remove(cookie_file)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=200)
