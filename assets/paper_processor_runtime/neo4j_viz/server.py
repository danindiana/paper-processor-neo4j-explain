#!/usr/bin/env python3
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# server.py
# Serves the premium dashboard and streams processed paper assets on Port 8585.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import os
import sys
import socket
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = 8585
PROCESSED_PATH = os.path.join(os.path.expanduser("~"), "Documents", "AI-ML_Papers", "_processed")
if len(sys.argv) > 1:
    PROCESSED_PATH = sys.argv[1]
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))

class DashboardHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        global PROCESSED_PATH
        # Strip query parameters and hash fragments
        path = path.split('?', 1)[0]
        path = path.split('#', 1)[0]
        
        # Intercept and map /_processed/ assets to the external processed SSD folder
        if path.startswith("/_processed/"):
            rel_path = path[len("/_processed/"):].lstrip("/")
            root_real = os.path.realpath(PROCESSED_PATH)
            target_path = os.path.realpath(os.path.join(root_real, rel_path))
            try:
                if os.path.commonpath([root_real, target_path]) != root_real:
                    return None
            except ValueError:
                return None
            return target_path
            
        if path == "/api/active_datasets":
            import subprocess, json
            try:
                ps = subprocess.check_output(["ps", "aux"]).decode()
                datasets = set()
                for line in ps.splitlines():
                    if "vram_resident_processor.py" in line and "grep" not in line:
                        parts = line.split("vram_resident_processor.py")
                        if len(parts) > 1:
                            args = parts[1].strip().split()
                            if args:
                                dataset_path = args[0]
                                datasets.add(os.path.join(dataset_path, "_processed"))
                datasets.add(PROCESSED_PATH)
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"datasets": list(datasets)}).encode())
            except Exception as e:
                self.send_response(500)
                self.end_headers()
            return None
            
        if path == "/webgl":
            path = "/webgl.html"
            
        # Intercept /api/sync to trigger Neo4j import
        if path == "/api/sync":
            import subprocess, json
            try:
                subprocess.run([sys.executable, "neo4j_importer.py", PROCESSED_PATH], cwd=DASHBOARD_DIR)
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f'{{"error":"{e}"}}'.encode())
            return None
            
        # Default behavior: serve from dashboard directory
        return os.path.join(DASHBOARD_DIR, path.lstrip("/"))

    def do_GET(self):
        # Override do_GET to handle our API which returns None from translate_path
        path = self.translate_path(self.path)
        if path is None:
            return
        super().do_GET()

    def do_POST(self):
        global PROCESSED_PATH
        if self.path == "/api/set_dataset":
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                body = self.rfile.read(content_length)
                import json
                try:
                    data = json.loads(body.decode('utf-8'))
                    if 'path' in data:
                        PROCESSED_PATH = data['path']
                        self.send_response(200)
                        self.send_header('Content-type', 'application/json')
                        self.end_headers()
                        self.wfile.write(b'{"status":"ok"}')
                        return
                except:
                    pass
            self.send_response(400)
            self.end_headers()
            return

        if self.path == "/api/export":
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                body = self.rfile.read(content_length)
                import json, time, os, zipfile
                try:
                    import pandas as pd
                    data = json.loads(body.decode('utf-8'))
                    ts = int(time.time())
                    
                    nodes_list = []
                    for n in data.get('nodes', []):
                        flat = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k,v in n.items()}
                        nodes_list.append(flat)
                    edges_list = []
                    for e in data.get('edges', []):
                        flat = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k,v in e.items()}
                        edges_list.append(flat)
                        
                    nodes_df = pd.DataFrame(nodes_list)
                    edges_df = pd.DataFrame(edges_list)
                    
                    # Convert to string to prevent mixed-type inference errors in pyarrow
                    nodes_df = nodes_df.astype(str)
                    edges_df = edges_df.astype(str)
                    
                    nodes_df.to_parquet(f"lobster_nodes_{ts}.parquet")
                    edges_df.to_parquet(f"lobster_edges_{ts}.parquet")
                    nodes_df.to_json(f"lobster_nodes_{ts}.jsonl", orient="records", lines=True)
                    edges_df.to_json(f"lobster_edges_{ts}.jsonl", orient="records", lines=True)
                    
                    zip_path = f"lobster_snapshot_{ts}.zip"
                    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                        zipf.write(f"lobster_nodes_{ts}.parquet")
                        zipf.write(f"lobster_edges_{ts}.parquet")
                        zipf.write(f"lobster_nodes_{ts}.jsonl")
                        zipf.write(f"lobster_edges_{ts}.jsonl")
                        
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"url": f"/{zip_path}"}).encode())
                    return
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode())
                    return
        
        self.send_response(404)
        self.end_headers()

def get_lan_ip():
    """Helper to query the local hostname to get the actual LAN IP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't need to be reachable, just triggers OS interface lookup
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = 'localhost'
    finally:
        s.close()
    return ip

def run():
    os.chdir(DASHBOARD_DIR)
    lan_ip = get_lan_ip()
    
    server_address = ('0.0.0.0', PORT)
    httpd = ThreadingHTTPServer(server_address, DashboardHandler)
    
    print(f"\n🦞 LOBSTER DASHBOARD SERVER")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"🟢 Local: http://localhost:{PORT}")
    print(f"🟢 LAN:   http://{lan_ip}:{PORT}")
    print(f"📂 Assets: {PROCESSED_PATH}")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("Press Ctrl+C to terminate the web server.\n")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Stopping dashboard web server...")
        sys.exit(0)

if __name__ == '__main__':
    run()
