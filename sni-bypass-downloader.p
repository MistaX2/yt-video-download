import os
import sys
import time
import requests
import urllib3
import io
import argparse
from tqdm import tqdm
from urllib3.util import connection

# SSL WARNINGS OFF
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


#part-d-443.py -Host support.zoom.us -Path 140.245.62.35/Movie/Reacher.S04E02.1080p.HEVC.x265-MeGusta\[EZTVx.to\].mkv -output Reacher.S04E02.1080p.HEVC.x265-MeGusta\[EZTVx.to\].mkv

# COMMAND LINE ARGUMENTS
parser = argparse.ArgumentParser(description="YouTube Data Downloader")
parser.add_argument("-Host", required=True, help="Target Host")
parser.add_argument("-Path", required=True, help="IP/Path")
parser.add_argument("-output", required=True, help="Output filename")
args = parser.parse_args()

# CONFIGURATION
HOST = args.Host
IP, PATH = args.Path.split('/', 1)
PATH = '/' + PATH
OUTPUT = args.output
TEMP_FILE = f"{OUTPUT}.size"
BUFFER_LIMIT = 100 * 1024 * 1024  # 100MB
CHUNK_SIZE = 1024 * 32

# SNI PATCH
_orig_create_connection = connection.create_connection
def patched_create_connection(address, *args, **kwargs):
    if address[0] == HOST:
        return _orig_create_connection((IP, address[1]), *args, **kwargs)
    return _orig_create_connection(address, *args, **kwargs)
connection.create_connection = patched_create_connection

def get_downloaded():
    total = 0
    index = 1
    while os.path.exists(f"{OUTPUT}.part{index}"):
        total += os.path.getsize(f"{OUTPUT}.part{index}")
        index += 1
    return total

def download_controller():
    
    while True:
        try:
            downloaded = get_downloaded()
            total_size = int(open(TEMP_FILE, "r").read()) if os.path.exists(TEMP_FILE) else None

            headers = {"Host": HOST, "Range": f"bytes={downloaded}-", "User-Agent": "Mozilla/5.0"}
            response = requests.get(f"http://{HOST}{PATH}", headers=headers, stream=True, verify=False, timeout=(10, 300))

            if not total_size and 'Content-Length' in response.headers:
                total_size = int(response.headers['Content-Length']) + downloaded
                with open(TEMP_FILE, "w") as f: f.write(str(total_size))

            print(f"\n🚀 Resuming Download: {OUTPUT}")
            print(f"📦 Total: {total_size/(1024**3):.2f} GB | 📥 Downloaded: {downloaded/(1024**3):.2f} GB")

            ram_buffer = io.BytesIO()
            buffer_size = 0
            part_index = 1
           
            while os.path.exists(f"{OUTPUT}.part{part_index}") and os.path.getsize(f"{OUTPUT}.part{part_index}") >= 5*1024*1024*1024:
                part_index += 1

            bar = tqdm(total=total_size, initial=downloaded, unit='B', unit_scale=True, bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]')

            for chunk in response.iter_content(CHUNK_SIZE):
                if not chunk: continue
                ram_buffer.write(chunk)
                buffer_size += len(chunk)

                bar.set_postfix_str(f"Buffer: {buffer_size/1024/1024:.1f}MB/100MB")

                if buffer_size >= BUFFER_LIMIT:
                    with open(f"{OUTPUT}.part{part_index}", "ab") as f:
                        f.write(ram_buffer.getvalue())
                    ram_buffer = io.BytesIO()
                    buffer_size = 0

                bar.update(len(chunk))

           
            if buffer_size > 0:
                with open(f"{OUTPUT}.part{part_index}", "ab") as f:
                    f.write(ram_buffer.getvalue())

            bar.close()
            if os.path.exists(TEMP_FILE): os.remove(TEMP_FILE)
            print("\n✅ Download Finished Successfully!")
            break # 

        except KeyboardInterrupt:
            print("\n🛑 Stopped! Saving RAM buffer...")
            if 'ram_buffer' in locals() and buffer_size > 0:
                with open(f"{OUTPUT}.part{part_index}", "ab") as f:
                    f.write(ram_buffer.getvalue())
            sys.exit()

        except Exception as e:
            print(f"\n⚠️ Error: {e} | Retrying in 5 seconds...")
            time.sleep(5)

if __name__ == "__main__":
    download_controller()
