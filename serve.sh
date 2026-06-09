#!/usr/bin/env bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# serve.sh — load the explanation graph into Neo4j and host the viz on the LAN.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -euo pipefail

PORT="${PORT:-8686}"
NEO4J_URL="${NEO4J_URL:-http://127.0.0.1:7474}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASS="${NEO4J_PASS:-password123}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "▶ Loading graph/pp_explain_graph.cypher into Neo4j ($NEO4J_URL) ..."
python3 - "$HERE/graph/pp_explain_graph.cypher" <<'PY'
import sys, json, base64, urllib.request, os
cy = open(sys.argv[1]).read()
stmts = []
for chunk in cy.split(';'):
    lines = [l for l in chunk.splitlines() if not l.strip().startswith('//')]
    s = "\n".join(lines).strip()
    if s: stmts.append(s)
body = json.dumps({"statements":[{"statement":s} for s in stmts]}).encode()
url  = os.environ.get("NEO4J_URL","http://127.0.0.1:7474") + "/db/neo4j/tx/commit"
auth = base64.b64encode(f'{os.environ.get("NEO4J_USER","neo4j")}:{os.environ.get("NEO4J_PASS","password123")}'.encode()).decode()
req  = urllib.request.Request(url, data=body, headers={"Content-Type":"application/json","Authorization":"Basic "+auth})
r = json.load(urllib.request.urlopen(req))
print("  errors:", r.get("errors") or "none")
PY

LAN_IP="$(hostname -I | awk '{print $1}')"
echo "▶ Serving web/ on 0.0.0.0:$PORT"
echo "  Local: http://localhost:$PORT/explanation.html"
echo "  LAN:   http://$LAN_IP:$PORT/explanation.html"
cd "$HERE/web"
exec python3 -m http.server "$PORT" --bind 0.0.0.0
