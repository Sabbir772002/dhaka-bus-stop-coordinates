#!/usr/bin/env python3
"""
Live Bus Route Tracker Server
- Watches final_buss.json and final_safe.json for changes
- Pushes reload signal to browser via WebSocket
- Handles save_file messages from browser to write final_safe.json and final_buss.json
- Accepts new stop creation, route modifications, and stop position updates
- Run: python server.py
- Open: http://localhost:8000
"""

import http.server
import socketserver
import os
import json
import time
import hashlib
import shutil
import threading
import webbrowser
from pathlib import Path

# ── Try to import websockets ──────────────────────────────────────────────────
try:
    import websockets
    import asyncio
    HAS_WS = True
except ImportError:
    HAS_WS = False
    print("⚠️  websockets not installed. Run: pip install websockets\n")

# ── Config ────────────────────────────────────────────────────────────────────
PORT    = 8000
WS_PORT = 8001
WATCH   = ['final_buss.json', 'final_safe.json']  # Allow browser to save these files
SAVE_WHITELIST = {'final_safe.json', 'final_buss.json'}

os.chdir(Path(__file__).parent)

# ── Find the HTML entry point ─────────────────────────────────────────────────
def find_html():
    for name in ('index.html', 'bus_route_map.html', 'bus_route_tracker.html'):
        if Path(name).exists():
            return name
    for f in Path('.').glob('*.html'):
        return f.name
    return 'index.html'

# ── File watcher ──────────────────────────────────────────────────────────────
file_hashes = {}
change_log  = []
ws_clients  = set()

def file_hash(path):
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None

def file_size(path):
    try:
        return Path(path).stat().st_size
    except FileNotFoundError:
        return 0

def init_hashes():
    for f in WATCH:
        file_hashes[f] = file_hash(f)
    print(f"👁️  Watching: {', '.join(WATCH)}\n")

def check_files():
    changed = []
    for f in WATCH:
        old = file_hashes.get(f)
        new = file_hash(f)
        if old != new:
            size  = file_size(f)
            entry = {
                'time': time.strftime('%H:%M:%S'),
                'file': f,
                'size': size,
                'hash': new or 'DELETED'
            }
            change_log.append(entry)
            file_hashes[f] = new
            changed.append(entry)
            status = '🗑️  DELETED' if new is None else f'✏️  CHANGED ({size:,} bytes)'
            print(f"  [{entry['time']}] {status} → {f}")
    return changed

# ── Save file handler ─────────────────────────────────────────────────────────
def save_file(filename, content):
    """
    Validate and write content to filename (final_safe.json or final_buss.json).
    Returns (True, size_bytes) on success or (False, error_message) on failure.
    """
    # Security: only whitelisted filenames, no path traversal
    if filename not in SAVE_WHITELIST:
        return False, f'Filename "{filename}" is not allowed.'
    if '/' in filename or '\\' in filename or '..' in filename:
        return False, 'Path traversal is not allowed.'

    # Validate content is proper JSON
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return False, f'Invalid JSON: {e}'

    # Validate structure based on file type
    if filename == 'final_safe.json':
        # Expect array of stop objects: {id, names, coordinates}
        if not isinstance(data, list):
            return False, 'final_safe.json must be an array of stops'
        
        total_coords = 0  # Track total coordinates across all stops
        for i, stop in enumerate(data):
            if not isinstance(stop, dict):
                return False, f'Stop #{i} is not an object'
            if 'id' not in stop:
                return False, f'Stop #{i} missing "id" field'
            # names can be list or dict
            if 'names' not in stop:
                return False, f'Stop #{i} missing "names" field'
            # coordinates must be array of [lat, lng] pairs
            if 'coordinates' not in stop or not isinstance(stop['coordinates'], list):
                return False, f'Stop #{i} "coordinates" must be an array'
            
            if len(stop['coordinates']) == 0:
                return False, f'Stop #{i} must have at least one coordinate'
            
            # Validate each coordinate
            for j, coord in enumerate(stop['coordinates']):
                if not isinstance(coord, (list, tuple)) or len(coord) < 2:
                    return False, f'Stop #{i} coordinate #{j} invalid format'
                try:
                    lat, lng = float(coord[0]), float(coord[1])
                    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
                        return False, f'Stop #{i} coordinate #{j} out of valid range: [{lat}, {lng}]'
                except (ValueError, TypeError):
                    return False, f'Stop #{i} coordinate #{j} must be numeric'
            
            total_coords += len(stop['coordinates'])
        
        print(f"  ✓ final_safe.json validated: {len(data)} stops, {total_coords} total coordinates")
        
    elif filename == 'final_buss.json':
        # Expect array of bus route objects with 'data' key containing routes
        if isinstance(data, dict) and 'data' in data:
            routes = data['data']
        else:
            routes = data if isinstance(data, list) else []
        
        if not isinstance(routes, list):
            return False, 'final_buss.json must have array of routes or {"data": [...]}'
        
        for i, route in enumerate(routes):
            if not isinstance(route, dict):
                return False, f'Route #{i} is not an object'
            # Routes may have 'english', 'bangla', 'routes' fields
            if 'english' not in route:
                return False, f'Route #{i} missing "english" field'
            if 'routes' not in route or not isinstance(route['routes'], list):
                return False, f'Route #{i} "routes" must be an array'
        
        print(f"  ✓ final_buss.json validated: {len(routes)} routes")

    file_path = Path(filename)

    # Backup existing file before overwriting
    if file_path.exists():
        backup_path = Path(filename + '.bak')
        try:
            shutil.copy2(file_path, backup_path)
            print(f"  💾 Backed up → {backup_path}")
        except Exception as e:
            print(f"  ⚠️  Backup failed (non-fatal): {e}")

    # Write new content
    try:
        file_path.write_text(content, encoding='utf-8')
        size = file_path.stat().st_size
        ts   = time.strftime('%H:%M:%S')
        print(f"  [{ts}] ✅ SAVED ({size:,} bytes) → {filename}")
        return True, size
    except Exception as e:
        print(f"  ❌ Save failed: {e}")
        return False, str(e)

# ── WebSocket ─────────────────────────────────────────────────────────────────
async def ws_handler(ws):
    ws_clients.add(ws)
    addr = ws.remote_address
    print(f"🔌 Browser connected  {addr[0]}:{addr[1]}  [{len(ws_clients)} total]")
    try:
        # Send history to newly connected client
        await ws.send(json.dumps({'type': 'history', 'log': change_log}))

        # Listen for messages from the browser
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps({'type': 'save_err', 'reason': 'Invalid JSON message'}))
                continue

            if msg.get('type') == 'save_file':
                filename = msg.get('filename', '')
                content  = msg.get('content', '')
                ok, result = save_file(filename, content)
                if ok:
                    size = result
                    h    = hashlib.md5(content.encode()).hexdigest()
                    await ws.send(json.dumps({
                        'type': 'save_ok',
                        'filename': filename,
                        'size': size,
                        'hash': h
                    }))
                    # Also update watcher hash so it doesn't fire a spurious change event
                    file_hashes[filename] = h
                else:
                    await ws.send(json.dumps({'type': 'save_err', 'reason': result}))

    except websockets.exceptions.ConnectionClosedError:
        pass
    finally:
        ws_clients.discard(ws)
        print(f"🔌 Browser disconnected {addr[0]}:{addr[1]}  [{len(ws_clients)} remaining]")

async def broadcast(msg):
    if ws_clients:
        await asyncio.gather(
            *[c.send(msg) for c in list(ws_clients)],
            return_exceptions=True
        )

async def watch_loop():
    while True:
        await asyncio.sleep(1)
        changed = await asyncio.get_event_loop().run_in_executor(None, check_files)
        for entry in changed:
            await broadcast(json.dumps({'type': 'change', 'entry': entry}))

async def run_ws_server():
    async with websockets.serve(ws_handler, 'localhost', WS_PORT):
        print(f"🔗 WebSocket →  ws://localhost:{WS_PORT}")
        await watch_loop()

def start_ws_thread():
    asyncio.run(run_ws_server())

# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(http.server.SimpleHTTPRequestHandler):

    def log_message(self, fmt, *args):
        if not args:
            return
        first = args[0]
        if not isinstance(first, str):
            super().log_message(fmt, *args)
            return
        try:
            path = first.split()[1]
        except (IndexError, AttributeError):
            path = str(first)
        skip = ('.png', '.jpg', '.ico', '.css', '.woff', '.woff2', '.ttf')
        if not any(path.endswith(s) for s in skip):
            ts   = time.strftime('%H:%M:%S')
            code = args[1] if len(args) > 1 else '-'
            print(f"  [{ts}] HTTP {code}  {path}")

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        if self.path.endswith('.json'):
            self.send_header('Content-Type', 'application/json; charset=utf-8')
        super().end_headers()

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_hashes()

    html_file = find_html()

    print("=" * 60)
    print("🚌  BUS ROUTE TRACKER  —  LIVE SERVER")
    print("=" * 60)
    print(f"\n✅  HTTP  →  http://localhost:{PORT}/{html_file}")
    if HAS_WS:
        print(f"🔗  WS    →  ws://localhost:{WS_PORT}  (live reload + save)")
    print(f"💾  Save  →  {', '.join(SAVE_WHITELIST)}")
    print(f"📂  Dir   →  {os.getcwd()}")
    print("\n⚠️   Press Ctrl+C to stop\n")
    print("=" * 60 + "\n")

    if HAS_WS:
        t = threading.Thread(target=start_ws_thread, daemon=True)
        t.start()
        time.sleep(0.5)

    webbrowser.open(f'http://localhost:{PORT}/{html_file}')

    try:
        with socketserver.TCPServer(('', PORT), Handler) as httpd:
            httpd.allow_reuse_address = True
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n\n🛑 Server stopped.")
    except OSError as e:
        if 'Address already in use' in str(e) or '10048' in str(e):
            print(f"❌ Port {PORT} already in use — kill the old process first.")
        else:
            raise