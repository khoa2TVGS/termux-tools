#!/usr/bin/env python3
"""
Termux Tailscale Media Server
-----------------------------
A high-performance, lightweight Python media streaming server designed for Android Termux.
- Indexes images, videos, gifs, and audio from /storage/emulated/0
- Supports RFC 7233 Byte-Range requests (HTTP 206 Partial Content) for smooth video scrubbing
- Auto-detects Tailscale IP and Local LAN IP
- Optional Pillow support for ultra-fast cached thumbnails
- Zero external pip requirements for core functionality
"""

import os
import sys
import time
import json
import socket
import mimetypes
import argparse
import threading
import subprocess
import urllib.parse
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Ensure UTF-8 output on platforms where default encoding is restricted
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Try importing Pillow for fast thumbnail generation (optional)
try:
    from PIL import Image, ImageOps
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

def safe_print(msg, **kwargs):
    """Print with fallback to ASCII if terminal encoding does not support Unicode."""
    try:
        print(msg, **kwargs)
    except Exception:
        try:
            safe_msg = str(msg).encode("ascii", errors="replace").decode("ascii")
            print(safe_msg, **kwargs)
        except Exception:
            pass

# Constants & Configuration
SUPPORTED_EXTENSIONS = {
    # Images
    ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".webp": "image", ".heic": "image", ".heif": "image",
    ".bmp": "image", ".svg": "image",
    # GIFs
    ".gif": "gif",
    # Videos
    ".mp4": "video", ".mkv": "video", ".webm": "video",
    ".mov": "video", ".avi": "video", ".3gp": "video",
    ".m4v": "video", ".ts": "video",
    # Audio
    ".mp3": "audio", ".m4a": "audio", ".aac": "audio",
    ".flac": "audio", ".wav": "audio", ".ogg": "audio",
    ".opus": "audio"
}

# Directories to ignore to prevent scanner hangs or permission issues in Android
IGNORE_DIR_NAMES = {
    "Android",          # Android/data and Android/obb are restricted in modern Android
    ".thumbnails",      # Cache junk
    ".trash",           # Deleted items
    ".Trash",
    ".cache",
    ".git",
    "node_modules",
    "cache"
}

DEFAULT_PORT = 8080
DEFAULT_ROOT = "/storage/emulated/0"
CHUNK_SIZE = 128 * 1024  # 128 KB chunk for smooth video streaming


def format_bytes(size: int) -> str:
    """Format bytes into human-readable string."""
    power = 1024
    n = 0
    units = ["B", "KB", "MB", "GB", "TB"]
    while size >= power and n < len(units) - 1:
        size /= power
        n += 1
    return f"{size:.1f} {units[n]}"


def get_network_ips():
    """Detect Tailscale IP, LAN IP, and localhost."""
    tailscale_ip = None
    lan_ip = None

    # 1. Check Tailscale via CLI
    try:
        res = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=2
        )
        if res.returncode == 0:
            ip = res.stdout.strip().splitlines()[0]
            if ip.startswith("100."):
                tailscale_ip = ip
    except Exception:
        pass

    # 2. Check Tailscale via socket interface scan if not found
    if not tailscale_ip:
        try:
            # Check hostnames / interfaces
            for info in socket.getaddrinfo(socket.gethostname(), None):
                ip = info[4][0]
                if ip.startswith("100.") and not tailscale_ip:
                    tailscale_ip = ip
        except Exception:
            pass

    # 3. Detect LAN IP via UDP connection probe
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        # Does not actually send data over the wire
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("127."):
            lan_ip = ip
    except Exception:
        lan_ip = "127.0.0.1"

    return {
        "tailscale": tailscale_ip,
        "lan": lan_ip or "127.0.0.1",
        "localhost": "127.0.0.1"
    }


class MediaCatalog:
    """In-memory thread-safe media catalog and background indexer."""

    def __init__(self, root_dir: str, cache_dir: str):
        self.root_dir = os.path.abspath(root_dir)
        self.cache_dir = os.path.abspath(cache_dir)
        self.items = []          # List of item dicts
        self.item_map = {}       # id -> item
        self.folders = {}        # folder_name -> count
        self.stats = {
            "total_items": 0,
            "total_size": 0,
            "image_count": 0,
            "video_count": 0,
            "gif_count": 0,
            "audio_count": 0,
            "folder_count": 0,
            "is_scanning": False,
            "last_scanned": None,
            "has_pillow": HAS_PILLOW
        }
        self.lock = threading.Lock()
        os.makedirs(self.cache_dir, exist_ok=True)

    def scan(self):
        """Recursively scan root_dir for media files in a background thread."""
        with self.lock:
            if self.stats["is_scanning"]:
                return
            self.stats["is_scanning"] = True

        def _worker():
            safe_print(f"🔍 Starting background scan of: {self.root_dir}")
            start_time = time.time()
            discovered = []
            folder_counts = {}
            total_size = 0
            img_c = vid_c = gif_c = aud_c = 0
            file_id = 1

            try:
                for root, dirs, files in os.walk(self.root_dir, topdown=True, followlinks=False):
                    # Filter out restricted or heavy directories
                    dirs[:] = [
                        d for d in dirs
                        if d not in IGNORE_DIR_NAMES and not d.startswith(".")
                    ]

                    rel_dir = os.path.relpath(root, self.root_dir)
                    folder_label = "Root" if rel_dir == "." else rel_dir.replace("\\", "/")

                    for file in files:
                        if file.startswith("."):
                            continue

                        _, ext = os.path.splitext(file)
                        ext_lower = ext.lower()
                        media_type = SUPPORTED_EXTENSIONS.get(ext_lower)

                        if media_type:
                            full_path = os.path.join(root, file)
                            try:
                                stat = os.stat(full_path)
                                size = stat.st_size
                                mtime = int(stat.st_mtime)
                            except OSError:
                                continue

                            rel_path = os.path.relpath(full_path, self.root_dir).replace("\\", "/")
                            item = {
                                "id": file_id,
                                "name": file,
                                "rel_path": rel_path,
                                "full_path": full_path,
                                "folder": folder_label,
                                "ext": ext_lower,
                                "type": media_type,
                                "size": size,
                                "size_fmt": format_bytes(size),
                                "mtime": mtime,
                                "date_fmt": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
                            }
                            discovered.append(item)
                            file_id += 1
                            total_size += size

                            # Stats
                            if media_type == "image":
                                img_c += 1
                            elif media_type == "video":
                                vid_c += 1
                            elif media_type == "gif":
                                gif_c += 1
                            elif media_type == "audio":
                                aud_c += 1

                            folder_counts[folder_label] = folder_counts.get(folder_label, 0) + 1

            except Exception as e:
                safe_print(f"⚠️ Scan warning: {e}", file=sys.stderr)

            # Sort items by date modified (newest first) by default
            discovered.sort(key=lambda x: x["mtime"], reverse=True)
            id_map = {item["id"]: item for item in discovered}

            with self.lock:
                self.items = discovered
                self.item_map = id_map
                self.folders = folder_counts
                self.stats.update({
                    "total_items": len(discovered),
                    "total_size": total_size,
                    "total_size_fmt": format_bytes(total_size),
                    "image_count": img_c,
                    "video_count": vid_c,
                    "gif_count": gif_c,
                    "audio_count": aud_c,
                    "folder_count": len(folder_counts),
                    "is_scanning": False,
                    "last_scanned": time.strftime("%Y-%m-%d %H:%M:%S")
                })

            duration = time.time() - start_time
            safe_print(f"✅ Scan complete in {duration:.2f}s! Found {len(discovered)} media items ({format_bytes(total_size)}).")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def query(self, media_type="all", folder=None, search=None, sort="date_desc", page=1, limit=60):
        """Query and paginate catalog items."""
        with self.lock:
            results = self.items.copy()

        # Filter by media type
        if media_type and media_type != "all":
            results = [item for item in results if item["type"] == media_type]

        # Filter by folder
        if folder:
            results = [item for item in results if item["folder"] == folder]

        # Filter by search string
        if search:
            q = search.lower()
            results = [item for item in results if q in item["name"].lower() or q in item["folder"].lower()]

        # Sorting
        if sort == "date_asc":
            results.sort(key=lambda x: x["mtime"])
        elif sort == "size_desc":
            results.sort(key=lambda x: x["size"], reverse=True)
        elif sort == "size_asc":
            results.sort(key=lambda x: x["size"])
        elif sort == "name_asc":
            results.sort(key=lambda x: x["name"].lower())
        elif sort == "name_desc":
            results.sort(key=lambda x: x["name"].lower(), reverse=True)
        else:  # date_desc
            results.sort(key=lambda x: x["mtime"], reverse=True)

        total_matches = len(results)
        total_pages = max(1, (total_matches + limit - 1) // limit)
        start_idx = (page - 1) * limit
        end_idx = start_idx + limit
        paged_items = results[start_idx:end_idx]

        return {
            "items": paged_items,
            "total": total_matches,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1
        }

    def get_by_id(self, file_id: int):
        """Retrieve single item by id."""
        with self.lock:
            return self.item_map.get(file_id)

    def get_thumbnail_path(self, item: dict, size=(360, 360)) -> str:
        """Return path to cached thumbnail, generating it with Pillow if missing."""
        if not HAS_PILLOW or item["type"] not in ("image", "gif"):
            return None

        thumb_name = f"thumb_{item['id']}_{item['mtime']}_{size[0]}x{size[1]}.webp"
        thumb_file = os.path.join(self.cache_dir, thumb_name)

        if os.path.exists(thumb_file):
            return thumb_file

        try:
            with Image.open(item["full_path"]) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail(size, Image.Resampling.LANCZOS)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGBA")
                else:
                    img = img.convert("RGB")
                img.save(thumb_file, "WEBP", quality=80)
                return thumb_file
        except Exception:
            return None


class MediaRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler with RFC 7233 Range request streaming, REST API, and static assets."""

    server_version = "TermuxTailMedia/1.0"

    @property
    def catalog(self) -> MediaCatalog:
        return self.server.catalog

    @property
    def web_dir(self) -> str:
        return self.server.web_dir

    def log_message(self, format, *args):
        """Clean minimal log output."""
        status_code = args[1] if len(args) > 1 else ""
        if status_code in ("200", "206", "304") and ("/thumb/" in self.path or "/stream/" in self.path):
            return
        super().log_message(format, *args)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 1. API Endpoints
        if path == "/api/media":
            self.handle_api_media(query)
            return
        elif path == "/api/folders":
            self.handle_api_folders()
            return
        elif path == "/api/stats":
            self.handle_api_stats()
            return
        elif path == "/api/network":
            self.handle_api_network()
            return

        # 2. Media Streaming with RFC 7233 Byte Range Requests
        elif path.startswith("/stream/"):
            file_id_str = path[len("/stream/"):]
            self.handle_media_stream(file_id_str, query)
            return

        # 3. Media Thumbnail
        elif path.startswith("/thumb/"):
            file_id_str = path[len("/thumb/"):]
            self.handle_media_thumb(file_id_str)
            return

        # 4. Frontend Web UI & Static files
        elif path == "/" or path == "/index.html":
            self.serve_static("index.html", "text/html; charset=utf-8")
            return
        elif path.startswith("/static/"):
            rel_file = path[len("/static/"):]
            self.serve_static(rel_file)
            return

        # Fallback 404
        self.send_error(HTTPStatus.NOT_FOUND, "Resource not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/rescan":
            self.catalog.scan()
            self.send_json({"status": "scanning", "message": "Background scan started"})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "API not found")

    def send_json(self, data, status=HTTPStatus.OK):
        """Send JSON response."""
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def handle_api_media(self, query):
        media_type = query.get("type", ["all"])[0]
        folder = query.get("folder", [None])[0]
        search = query.get("search", [None])[0]
        sort = query.get("sort", ["date_desc"])[0]
        try:
            page = max(1, int(query.get("page", [1])[0]))
            limit = min(200, max(1, int(query.get("limit", [60])[0])))
        except ValueError:
            page, limit = 1, 60

        data = self.catalog.query(
            media_type=media_type,
            folder=folder,
            search=search,
            sort=sort,
            page=page,
            limit=limit
        )
        self.send_json(data)

    def handle_api_folders(self):
        with self.catalog.lock:
            sorted_folders = sorted(self.catalog.folders.items(), key=lambda x: x[1], reverse=True)
            folder_list = [{"name": name, "count": count} for name, count in sorted_folders]
        self.send_json({"folders": folder_list})

    def handle_api_stats(self):
        with self.catalog.lock:
            stats_copy = dict(self.catalog.stats)
        self.send_json(stats_copy)

    def handle_api_network(self):
        ips = get_network_ips()
        ips["port"] = self.server.server_address[1]
        self.send_json(ips)

    def handle_media_stream(self, file_id_str: str, query):
        try:
            file_id = int(file_id_str)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid file ID")
            return

        item = self.catalog.get_by_id(file_id)
        if not item or not os.path.exists(item["full_path"]):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        full_path = item["full_path"]
        try:
            stat = os.stat(full_path)
            file_size = stat.st_size
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File inaccessible")
            return

        mime_type, _ = mimetypes.guess_type(full_path)
        if not mime_type:
            if item["type"] == "video":
                mime_type = "video/mp4"
            elif item["type"] == "image":
                mime_type = "image/jpeg"
            elif item["type"] == "audio":
                mime_type = "audio/mpeg"
            else:
                mime_type = "application/octet-stream"

        is_download = query.get("download", ["0"])[0] in ("1", "true")
        disposition = "attachment" if is_download else "inline"
        filename_escaped = urllib.parse.quote(item["name"])

        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            self.serve_range_stream(full_path, file_size, mime_type, range_header, disposition, filename_escaped)
        else:
            self.serve_full_stream(full_path, file_size, mime_type, disposition, filename_escaped)

    def serve_full_stream(self, file_path: str, file_size: int, mime_type: str, disposition: str, filename_escaped: str):
        """Serve entire file (HTTP 200)."""
        try:
            f = open(file_path, "rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "Cannot open file")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{filename_escaped}")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            f.close()

    def serve_range_stream(self, file_path: str, file_size: int, mime_type: str, range_header: str, disposition: str, filename_escaped: str):
        """Serve byte range slice (HTTP 206 Partial Content) for smooth video scrub."""
        try:
            ranges = range_header.replace("bytes=", "").split("-")
            start = int(ranges[0]) if ranges[0] else 0
            end = int(ranges[1]) if len(ranges) > 1 and ranges[1] else file_size - 1

            if start >= file_size or end >= file_size or start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.end_headers()
                return

            chunk_len = end - start + 1
            f = open(file_path, "rb")
            f.seek(start)

            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Length", str(chunk_len))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{filename_escaped}")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            bytes_remaining = chunk_len
            while bytes_remaining > 0:
                to_read = min(CHUNK_SIZE, bytes_remaining)
                chunk = f.read(to_read)
                if not chunk:
                    break
                self.wfile.write(chunk)
                bytes_remaining -= len(chunk)
            f.close()

        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))

    def handle_media_thumb(self, file_id_str: str):
        try:
            file_id = int(file_id_str)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid file ID")
            return

        item = self.catalog.get_by_id(file_id)
        if not item or not os.path.exists(item["full_path"]):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        # Generate thumbnail with Pillow if available
        if HAS_PILLOW and item["type"] in ("image", "gif"):
            thumb_path = self.catalog.get_thumbnail_path(item)
            if thumb_path and os.path.exists(thumb_path):
                self.serve_static_file(thumb_path, "image/webp")
                return

        # Fallback: Redirect or stream the original media directly
        self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
        self.send_header("Location", f"/stream/{file_id}")
        self.end_headers()

    def serve_static(self, rel_path: str, content_type: str = None):
        """Serve files from web_dir."""
        target_path = os.path.normpath(os.path.join(self.web_dir, rel_path))
        if not target_path.startswith(self.web_dir) or not os.path.exists(target_path):
            self.send_error(HTTPStatus.NOT_FOUND, "Static file not found")
            return

        if not content_type:
            content_type, _ = mimetypes.guess_type(target_path)
            content_type = content_type or "application/octet-stream"

        self.serve_static_file(target_path, content_type)

    def serve_static_file(self, full_path: str, content_type: str):
        try:
            stat = os.stat(full_path)
            f = open(full_path, "rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not readable")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            while True:
                buf = f.read(CHUNK_SIZE)
                if not buf:
                    break
                self.wfile.write(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            f.close()


def print_banner(host: str, port: int, root_dir: str):
    """Print a clean, informative startup banner with Tailscale & LAN addresses."""
    ips = get_network_ips()
    tailscale_url = f"http://{ips['tailscale']}:{port}" if ips["tailscale"] else "Not connected / Not running"
    lan_url = f"http://{ips['lan']}:{port}"
    local_url = f"http://localhost:{port}"

    banner = f"""
\033[1;36m╔══════════════════════════════════════════════════════════════════════════╗
║               🚀  TERMUX TAILSCALE MEDIA SERVER & GALLERY                ║
╚══════════════════════════════════════════════════════════════════════════╝\033[0m

  \033[1;32m📁 Serving Folder:\033[0m   {root_dir}
  \033[1;32m⚡ Pillow Engine:\033[0m   {'Available (Fast WebP Thumbnails)' if HAS_PILLOW else 'Not Installed (Original Image Fallback)'}

  \033[1;33m📡 Tailscale Network URL:\033[0m
     \033[1;37m{tailscale_url}\033[0m
     \033[0;90m(Accessible from any device linked to your Tailscale mesh)\033[0m

  \033[1;34m🏠 Local Wi-Fi Network URL:\033[0m
     \033[1;37m{lan_url}\033[0m
     \033[0;90m(Accessible from devices connected to the same Wi-Fi router)\033[0m

  \033[1;35m📱 On This Android Device:\033[0m
     \033[1;37m{local_url}\033[0m

\033[0;90m──────────────────────────────────────────────────────────────────────────
  Press Ctrl+C to stop server.
──────────────────────────────────────────────────────────────────────────\033[0m
"""
    try:
        print(banner)
    except Exception:
        safe_banner = f"""
==========================================================================
              TERMUX TAILSCALE MEDIA SERVER & GALLERY
==========================================================================

  Serving Folder:   {root_dir}
  Pillow Engine:    {'Available' if HAS_PILLOW else 'Not Installed'}

  Tailscale Network URL:
     {tailscale_url}

  Local Wi-Fi Network URL:
     {lan_url}

  On This Android Device:
     {local_url}

==========================================================================
  Press Ctrl+C to stop server.
==========================================================================
"""
        print(safe_banner)


def main():
    parser = argparse.ArgumentParser(description="Termux Tailscale Media Server")
    parser.add_argument(
        "--root",
        default=DEFAULT_ROOT,
        help=f"Root directory to scan (default: {DEFAULT_ROOT})"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to bind to (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host interface to bind to (default: 0.0.0.0 for Tailscale/LAN access)"
    )
    parser.add_argument(
        "--cache",
        default=os.path.expanduser("~/.cache/termux_media_server/thumbs"),
        help="Directory to cache thumbnails"
    )
    args = parser.parse_args()

    target_root = args.root
    if not os.path.exists(target_root):
        safe_print(f"⚠️ Notice: Root directory '{target_root}' not found. Falling back to current directory: {os.getcwd()}")
        target_root = os.getcwd()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    web_dir = os.path.join(script_dir, "web")
    os.makedirs(web_dir, exist_ok=True)

    catalog = MediaCatalog(root_dir=target_root, cache_dir=args.cache)

    server_address = (args.host, args.port)
    server = ThreadingHTTPServer(server_address, MediaRequestHandler)
    server.catalog = catalog
    server.web_dir = web_dir
    server.daemon_threads = True

    print_banner(args.host, args.port, target_root)
    catalog.scan()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        safe_print("\n🛑 Stopping server...")
    finally:
        server.server_close()
        safe_print("👋 Termux Tailscale Media Server stopped.")


if __name__ == "__main__":
    main()
