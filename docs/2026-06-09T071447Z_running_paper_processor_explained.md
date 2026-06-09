# Session — Running `paper_processor.py` Instance Explained

**Timestamp:** 2026-06-09T071447Z
**Host:** worlock (192.168.1.85)
**Author:** Claude Code session

---

## 1. What is running

| Field | Value |
|-------|-------|
| Process | `python paper_processor.py` |
| PID | 104025 |
| Started | 2026-06-08 (~13:53), elapsed ~17h20m at inspection |
| CWD | `/home/jeb/programs/python_programs/paper_processor` |
| TTY | `pts/6` (foreground, interactive) |
| CPU time | ~8m53s total (I/O- and GPU-bound, not CPU-bound) |
| RSS | ~490 MB |
| Backend socket | `127.0.0.1:49042 → 127.0.0.1:11434` (Ollama, ESTABLISHED) |

## 2. What it's doing

`paper_processor.py` is the **OpenClaw / Ollama AI-ML paper-processing pipeline**. For
each PDF under the target directory it produces a structured knowledge bundle:

- `01_summary.md` — comprehensive summary
- `02_symbolic_logic.md` — core insights in formal symbolic logic
- `03_cpp_examples.md` — C++20/23 implementations of key algorithms
- `diagrams/` — 6+ Graphviz DOT + rendered SVG (neon/black theme)
- `04_extras.md` — open questions, connections, critical assessment
- `metadata.json` — audit trail (model, hash, timestamps, strategy)

**Backend:** Ollama (default), single worker (`--workers 1`).
**Model loaded in VRAM:** `nemotron-3-nano-30b-small:latest` (31.6B params, MoE
`nemotron_h_moe`, Q4_K_M, 16384 ctx) — ~23.8 GB resident in VRAM, keep-alive 60m.

**Target corpus:** `~/Documents/AI-ML_Papers`
- Total PDFs queued: **5219**
- Output bundles created so far (`_processed/` dirs): **~2501** (~48% through)
- Output root: `~/Documents/AI-ML_Papers/_processed/`

## 3. Current activity at inspection (07:14Z / 06:23 local last write)

- Just **completed**: `_processed/info_theory/projstatus_tuan/` — all sections
  (`03_cpp_examples.md`, `04_extras.md`, 6 diagrams DOT+SVG, `metadata.json`) written 06:23.
- Now **in progress**: `_processed/info_theory/putnam-and-beyond/` — a large (~333-page)
  math problem book. OCR fallback ran (`.ocr_cache/` populated, scanned/image pages),
  and the pipeline is in the LLM generation stage (Ollama model still warm within
  keep-alive window → actively generating summary/logic/code sections).
- GPU: GPU0 ~15.6 GB used / 8% util, GPU1 ~8.4 GB / 16% util — typical token-generation
  load for this MoE (low SM util, VRAM-resident weights).

**Diagnosis:** healthy and progressing. Sparse, low-CPU, GPU token-generation bound,
serially walking the PDF tree and skipping already-`_processed` papers.

## 4. Hosting the explanation on the LAN

The explanation is materialized as a graph in the running Neo4j instance
(`paper-processor-neo4j`, neo4j/password123, bolt 7687 / http 7474, localhost-bound)
under label `:PPExplain`, and rendered as an interactive vis-network page served on the
LAN.

- Graph loader: `neo4j_viz/pp_explain_graph.cypher`
- LAN page: `neo4j_viz/explanation.html` (vis-network, reads `pp_explain_graph.json`)
- Served at: **http://192.168.1.85:8686/explanation.html**
