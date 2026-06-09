#!/usr/bin/env python3
"""
build_diagrams.py — emit self-contained vis-network diagrams (one per subsystem)
for the paper_processor.py runtime. Each spec → a single HTML file with inline
data (no fetch), so it renders identically from file:// or over HTTP.

Usage:  python3 build_diagrams.py        # writes sub_*.html next to this script
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))

# Shared neon/black palette, keyed by node "kind".
PALETTE = {
    "source": "#9affae", "probe": "#ffd45f", "pipe": "#5fffcf", "store": "#7ab8ff",
    "data": "#ff7ad9", "web": "#ffa45f", "client": "#ff6b6b", "backend": "#c0a0ff",
    "stage": "#ff6b6b", "section": "#7aa0c0", "model": "#ff7ad9", "io": "#ffd45f",
}

TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
 html,body{{margin:0;height:100%;background:#06090f;color:#cfe;font-family:ui-monospace,Menlo,monospace}}
 #hdr{{padding:10px 16px;border-bottom:1px solid #163;background:#0a1018}}
 #hdr h1{{margin:0;font-size:16px;color:#5fffcf;letter-spacing:.5px}}
 #hdr p{{margin:4px 0 0;font-size:12px;color:#7aa}}
 #net{{width:100%;height:calc(100vh - 64px)}}
</style></head><body>
<div id="hdr"><h1>🦞 {title}</h1><p>{subtitle}</p></div>
<div id="net"></div>
<script>
const PAL={palette};
const N={nodes};
const E={edges};
const DIR={direction!r};
const nodes=new vis.DataSet(N.map(n=>({{
  id:n.id,label:n.name+(n.detail?'\\n'+n.detail:''),shape:'box',
  color:{{background:'#0d141d',border:PAL[n.kind]||'#888',highlight:{{background:'#152233',border:PAL[n.kind]||'#888'}}}},
  font:{{color:PAL[n.kind]||'#cfe',size:n.big?18:14,multi:true,face:'monospace'}},
  borderWidth:n.big?3:1.5
}})));
const edges=new vis.DataSet(E.map(e=>({{
  from:e.from,to:e.to,label:e.label||'',arrows:'to',dashes:!!e.dashes,
  color:{{color:'#2c4a5a',highlight:'#5fffcf'}},
  font:{{color:'#6a8',size:10,strokeWidth:0}},smooth:{{type:'cubicBezier'}}
}})));
const net=new vis.Network(document.getElementById('net'),{{nodes,edges}},{{
  layout:{{hierarchical:{{enabled:true,direction:DIR,sortMethod:'directed',levelSeparation:230,nodeSpacing:115}}}},
  physics:false,interaction:{{hover:true}}
}});
function fitPad(){{net.fit({{animation:false}});net.moveTo({{scale:net.getScale()*0.85,animation:false}});}}
net.once('afterDrawing',fitPad);net.on('resize',fitPad);
</script></body></html>
"""

SPECS = {
 # ── 1 · per-paper processing pipeline (paper_processor.Processor.process) ──
 "sub_pipeline": {
   "title": "Subsystem · per-paper processing pipeline",
   "subtitle": "paper_processor.py Processor.process() — one PDF → a _processed/ knowledge bundle",
   "direction": "LR",
   "nodes": [
     {"id":"pdf","kind":"source","name":"PDF","detail":"papers_dir.rglob(*.pdf)","big":True},
     {"id":"skip","kind":"io","name":"skip?","detail":"all sections complete\n+ no --reprocess"},
     {"id":"extract","kind":"stage","name":"extract_pages_with_ocr","detail":"PyMuPDF text + OCR fallback"},
     {"id":"chunks","kind":"pipe","name":"build_chunks","detail":"sliding window 12 / overlap 2"},
     {"id":"mapreduce","kind":"pipe","name":"map_reduce_chunks","detail":">1 chunk → context"},
     {"id":"model","kind":"model","name":"select_model","detail":"by page_count / --model","big":True},
     {"id":"sum","kind":"section","name":"summary","detail":"01_summary.md"},
     {"id":"logic","kind":"section","name":"logic","detail":"02_symbolic_logic.md"},
     {"id":"cpp","kind":"section","name":"cpp","detail":"03_cpp_examples.md\n(code_model)"},
     {"id":"diag","kind":"section","name":"diagrams","detail":"parse_diagrams + render_dot\n6× DOT→SVG"},
     {"id":"extras","kind":"section","name":"extras","detail":"04_extras.md"},
     {"id":"meta","kind":"store","name":"metadata.json","detail":"_save_meta · hash · sections_completed","big":True},
   ],
   "edges": [
     {"from":"pdf","to":"skip"},{"from":"skip","to":"extract","label":"no"},
     {"from":"extract","to":"chunks"},{"from":"chunks","to":"mapreduce"},
     {"from":"mapreduce","to":"model","label":"context"},
     {"from":"model","to":"sum","label":"backend.call"},{"from":"model","to":"logic"},
     {"from":"model","to":"cpp"},{"from":"model","to":"diag"},{"from":"model","to":"extras"},
     {"from":"sum","to":"meta"},{"from":"logic","to":"meta"},{"from":"cpp","to":"meta"},
     {"from":"diag","to":"meta"},{"from":"extras","to":"meta"},
   ],
 },
 # ── 2 · OCR fallback (ocr_fallback.py) ──
 "sub_ocr": {
   "title": "Subsystem · OCR fallback",
   "subtitle": "ocr_fallback.extract_pages_with_ocr() — local-first Tesseract for scanned/image PDFs",
   "direction": "LR",
   "nodes": [
     {"id":"page","kind":"source","name":"fitz page","detail":"page.get_text()","big":True},
     {"id":"need","kind":"io","name":"page_needs_ocr?","detail":"stripped < min_chars (100)"},
     {"id":"avail","kind":"io","name":"ocr_available?","detail":"tesseract bin + tessdata lang"},
     {"id":"mode","kind":"io","name":"mode gate","detail":"auto / always / never"},
     {"id":"cache","kind":"store","name":".ocr_cache/","detail":"{hash}_p{idx:04d}.txt","big":True},
     {"id":"ocr","kind":"stage","name":"ocr_page","detail":"get_textpage_ocr @300dpi"},
     {"id":"text","kind":"pipe","name":"page text","detail":"native or OCR'd"},
     {"id":"stats","kind":"data","name":"OcrStats","detail":"ocr_pages · cached_pages"},
   ],
   "edges": [
     {"from":"page","to":"need"},
     {"from":"need","to":"text","label":"no → native"},
     {"from":"need","to":"avail","label":"yes"},
     {"from":"avail","to":"mode","label":"ok"},
     {"from":"avail","to":"text","label":"unavailable → native","dashes":True},
     {"from":"mode","to":"cache","label":"allowed"},
     {"from":"cache","to":"ocr","label":"miss"},
     {"from":"cache","to":"text","label":"hit"},
     {"from":"ocr","to":"cache","label":"write"},
     {"from":"ocr","to":"text"},
     {"from":"text","to":"stats"},
   ],
 },
 # ── 3 · Ollama / model backend (paper_processor Backend + provision_ollama) ──
 "sub_backend": {
   "title": "Subsystem · Ollama model backend",
   "subtitle": "Backend._call_ollama + provision_ollama — inference, VRAM & recovery",
   "direction": "LR",
   "nodes": [
     {"id":"call","kind":"pipe","name":"Backend.call","detail":"prompt + model + ctx","big":True},
     {"id":"prov","kind":"stage","name":"provision_ollama","detail":"evict → wait clean → restart"},
     {"id":"evict","kind":"io","name":"_ollama_evict","detail":"keep_alive=0 generate"},
     {"id":"restart","kind":"io","name":"_ollama_restart_service","detail":"systemctl restart"},
     {"id":"api","kind":"backend","name":"POST /api/generate","detail":"127.0.0.1:11434","big":True},
     {"id":"model","kind":"model","name":"nemotron-3-nano-30b","detail":"31.6B MoE · Q4_K_M"},
     {"id":"gpu","kind":"backend","name":"GPU0+GPU1","detail":"RTX 5080 + RTX 3080\n~23.8GB VRAM"},
     {"id":"out","kind":"data","name":"completion","detail":"section text / DOT"},
   ],
   "edges": [
     {"from":"call","to":"prov","label":"--override / stuck","dashes":True},
     {"from":"prov","to":"evict"},{"from":"prov","to":"restart","label":"if stuck"},
     {"from":"call","to":"api"},{"from":"api","to":"model","label":"load/keep-alive"},
     {"from":"model","to":"gpu","label":"resident"},
     {"from":"api","to":"out"},
     {"from":"evict","to":"api","dashes":True},{"from":"restart","to":"api","dashes":True},
   ],
 },
 # ── 4 · neo4j_viz dashboard (server.py + importer + compose) ──
 "sub_dashboard": {
   "title": "Subsystem · neo4j_viz dashboard",
   "subtitle": "server.py on 0.0.0.0:8585 — serve assets, sync to Neo4j, export",
   "direction": "LR",
   "nodes": [
     {"id":"browser","kind":"client","name":"LAN browser","detail":"index.html dashboard","big":True},
     {"id":"server","kind":"web","name":"server.py :8585","detail":"ThreadingHTTPServer","big":True},
     {"id":"assets","kind":"store","name":"/_processed/*","detail":"translate_path → SSD"},
     {"id":"active","kind":"io","name":"/api/active_datasets","detail":"scan ps for processors"},
     {"id":"setds","kind":"io","name":"/api/set_dataset","detail":"switch corpus root"},
     {"id":"sync","kind":"pipe","name":"/api/sync","detail":"spawn neo4j_importer.py"},
     {"id":"export","kind":"data","name":"/api/export","detail":"nodes/edges snapshot"},
     {"id":"importer","kind":"stage","name":"neo4j_importer.py","detail":"walk _processed/ → MERGE"},
     {"id":"neo4j","kind":"store","name":"Neo4j","detail":"paper-processor-neo4j\nbolt 7687 / http 7474","big":True},
   ],
   "edges": [
     {"from":"browser","to":"server","label":"HTTP"},
     {"from":"server","to":"assets","label":"GET"},
     {"from":"server","to":"active"},{"from":"server","to":"setds","label":"POST"},
     {"from":"server","to":"sync","label":"POST"},{"from":"server","to":"export","label":"POST"},
     {"from":"sync","to":"importer","label":"subprocess"},
     {"from":"importer","to":"neo4j","label":"MERGE papers/sections"},
     {"from":"export","to":"neo4j","label":"query"},
   ],
 },
}

def render_html(spec):
    return TEMPLATE.format(
        title=spec["title"], subtitle=spec["subtitle"],
        palette=json.dumps(PALETTE), nodes=json.dumps(spec["nodes"]),
        edges=json.dumps(spec["edges"]), direction=spec["direction"],
    )

if __name__ == "__main__":
    for name, spec in SPECS.items():
        path = os.path.join(HERE, f"{name}.html")
        with open(path, "w") as f:
            f.write(render_html(spec))
        print("wrote", os.path.relpath(path, HERE))
