#!/usr/bin/env python3
"""
gen_live_state.py — re-introspect the running paper_processor.py instance and
emit a fresh :PPExplain graph as JSON (for live.html) and, optionally, load it
into Neo4j. Run it on a timer (cron / watch / loop) to drive a live dashboard.

  python3 gen_live_state.py                 # write web/pp_explain_live.json
  python3 gen_live_state.py --neo4j         # also MERGE into Neo4j :PPExplain
  watch -n 15 python3 gen_live_state.py     # refresh every 15s

stdlib only.
"""
import argparse, base64, json, os, re, subprocess, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT  = HERE / "pp_explain_live.json"
PAPERS = Path.home() / "Documents" / "AI-ML_Papers"
PROCESSED = PAPERS / "_processed"
OLLAMA = "http://127.0.0.1:11434"
NEO4J  = os.environ.get("NEO4J_URL", "http://127.0.0.1:7474")
NEO4J_AUTH = (os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASS", "password123"))


def sh(cmd):
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""


def find_pid():
    for line in sh(["ps", "-eo", "pid,etime,comm,args"]).splitlines():
        if "paper_processor.py" in line and "grep" not in line and "gen_live_state" not in line:
            m = re.match(r"\s*(\d+)\s+(\S+)", line)
            if m:
                return int(m.group(1)), m.group(2)
    return None, None


def ollama_model():
    try:
        r = json.load(urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=3))
        if r.get("models"):
            m = r["models"][0]
            vram = m.get("size_vram", 0) / 1e9
            return m["name"], f"{m['details'].get('parameter_size','?')} · {vram:.1f}GB VRAM"
    except Exception:
        pass
    return None, ""


def gpu_lines():
    out = sh(["nvidia-smi", "--query-gpu=index,memory.used,utilization.gpu",
              "--format=csv,noheader,nounits"])
    rows = [r.split(", ") for r in out.strip().splitlines() if r.strip()]
    return rows  # [[idx, mem_used_MiB, util_pct], ...]


def corpus_progress():
    total = len(list(PAPERS.rglob("*.pdf"))) if PAPERS.exists() else 0
    # cheaper: count metadata.json under _processed
    done = len(list(PROCESSED.rglob("metadata.json"))) if PROCESSED.exists() else 0
    return total, done


def current_paper():
    if not PROCESSED.exists():
        return None, None
    newest, newest_t = None, 0.0
    for p in PROCESSED.rglob("*"):
        if p.is_file():
            try:
                t = p.stat().st_mtime
            except OSError:
                continue
            if t > newest_t:
                newest_t, newest = t, p
    if newest is None:
        return None, None
    # paper dir holds the section files; OCR/diagram artifacts live one level deeper.
    paper_dir = newest if newest.is_dir() else newest.parent
    if paper_dir.name in (".ocr_cache", "diagrams"):
        paper_dir = paper_dir.parent
    name = paper_dir.name if paper_dir != PROCESSED else newest.stem
    age = time.time() - newest_t
    return name, age


def build_graph():
    pid, etime = find_pid()
    model, model_detail = ollama_model()
    gpus = gpu_lines()
    total, done = corpus_progress()
    paper, age = current_paper()

    pct = f"{100*done/total:.0f}%" if total else "?"
    alive = pid is not None
    state = "RUNNING" if alive else "NOT RUNNING"
    age_str = (f"{int(age)}s ago" if age and age < 90
               else f"{int(age/60)}m ago" if age else "—")

    nodes = [
        {"id": "proc", "kind": "source", "big": True,
         "name": "paper_processor.py",
         "detail": f"PID {pid or '—'} · {state}\nuptime {etime or '—'}"},
        {"id": "ollama", "kind": "backend", "name": "Ollama", "detail": "127.0.0.1:11434"},
        {"id": "model", "kind": "model", "name": model or "(no model loaded)",
         "detail": model_detail},
        {"id": "corpus", "kind": "store", "big": True, "name": "AI-ML_Papers",
         "detail": f"{done}/{total} processed ({pct})"},
        {"id": "cur", "kind": "section", "big": True,
         "name": paper or "(idle)",
         "detail": f"last write {age_str}"},
    ]
    for idx, mem, util in gpus:
        nodes.append({"id": f"gpu{idx}", "kind": "backend",
                      "name": f"GPU{idx}", "detail": f"{mem} MiB · {util}% util"})

    edges = [
        {"from": "proc", "to": "ollama", "label": "USES"},
        {"from": "ollama", "to": "model", "label": "LOADS"},
        {"from": "proc", "to": "corpus", "label": "WALKS"},
        {"from": "corpus", "to": "cur", "label": "CURRENT"},
        {"from": "proc", "to": "cur", "label": "PROCESSING"},
    ]
    for idx, _, _ in gpus:
        edges.append({"from": "model", "to": f"gpu{idx}", "label": "VRAM"})

    return {
        "title": "LIVE · paper_processor.py state",
        "subtitle": f"{state} · {done}/{total} ({pct}) · current: {paper or 'idle'} · refreshed every 15s",
        "generated_epoch": int(time.time()),
        "alive": alive,
        "nodes": nodes,
        "edges": edges,
    }


def load_neo4j(graph):
    stmts = ["MATCH (n:PPLive) DETACH DELETE n"]
    for n in graph["nodes"]:
        stmts.append(
            "CREATE (:PPLive {id:$id, kind:$kind, name:$name, detail:$detail})"
            .replace("$id", json.dumps(n["id"]))
            .replace("$kind", json.dumps(n["kind"]))
            .replace("$name", json.dumps(n["name"]))
            .replace("$detail", json.dumps(n.get("detail", "")))
        )
    for e in graph["edges"]:
        stmts.append(
            f"MATCH (a:PPLive {{id:{json.dumps(e['from'])}}}),(b:PPLive {{id:{json.dumps(e['to'])}}}) "
            f"CREATE (a)-[:REL {{label:{json.dumps(e['label'])}}}]->(b)"
        )
    body = json.dumps({"statements": [{"statement": s} for s in stmts]}).encode()
    auth = base64.b64encode(f"{NEO4J_AUTH[0]}:{NEO4J_AUTH[1]}".encode()).decode()
    req = urllib.request.Request(f"{NEO4J}/db/neo4j/tx/commit", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})
    r = json.load(urllib.request.urlopen(req))
    return r.get("errors") or "ok"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--neo4j", action="store_true", help="also load into Neo4j :PPLive")
    a = ap.parse_args()
    g = build_graph()
    OUT.write_text(json.dumps(g, indent=2))
    print(f"wrote {OUT.name} · {g['subtitle']}")
    if a.neo4j:
        print("neo4j:", load_neo4j(g))
