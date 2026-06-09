#!/usr/bin/env -S ./.venv/bin/python
"""
paper_processor.py — OpenClaw / Ollama AI-ML Paper Processing Pipeline
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For each PDF in the target directory, produces:
  01_summary.md          — Comprehensive paper summary
  02_symbolic_logic.md   — Core insights in formal symbolic logic
  03_cpp_examples.md     — C++20/23 implementations of key algorithms
  diagrams/              — 6+ Graphviz DOT + rendered SVG (neon/black)
  04_extras.md           — Open questions, connections, critical assessment
  metadata.json          — Audit trail (model, hash, timestamps, strategy)

Usage:
  python paper_processor.py                               # all papers, ollama backend
  python paper_processor.py --backend openclaw            # use OpenClaw agent CLI
  python paper_processor.py --model deepseek-r1:14b      # force a model
  python paper_processor.py --paper "attention.pdf"      # single paper
  python paper_processor.py --list                        # show status table
  python paper_processor.py --reprocess diagrams         # redo one section
  python paper_processor.py --workers 2                  # parallel (VRAM permitting)
  python paper_processor.py --override                   # evict loaded models, restart if stuck
"""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# ── Third-party imports ────────────────────────────────────────────────────
try:
    import fitz  # noqa: F401  (availability probe; OCR/extraction uses it via ocr_fallback)
except ImportError:
    sys.exit("❌  pymupdf not installed.\n    Fix: pip install pymupdf --break-system-packages")

try:
    import requests
except ImportError:
    sys.exit("❌  requests not installed.\n    Fix: pip install requests --break-system-packages")

# Local-first OCR fallback for scanned / image-only PDFs (optional at runtime).
from ocr_fallback import (
    DEFAULT_DPI,
    DEFAULT_LANG,
    DEFAULT_MIN_CHARS,
    extract_pages_with_ocr,
)


# ══════════════════════════════════════════════════════════════════════════════
# GRACEFUL SHUTDOWN
# ══════════════════════════════════════════════════════════════════════════════
_shutdown = threading.Event()
_sync_lock = threading.Lock()



def _install_signal_handlers() -> None:
    def _handler(signum, frame):
        if _shutdown.is_set():
            print("\n  🚨  Forced exit requested — stopping immediately!")
            os._exit(1)
        print("\n\n  ⚡  Shutdown requested — finishing current section then stopping …")
        print("      (Press Ctrl+C again to force exit immediately)")
        _shutdown.set()
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


class _ShutdownRequested(Exception):
    """Raised when _shutdown fires during a blocking LLM call or map-reduce loop."""


# ══════════════════════════════════════════════════════════════════════════════
# HARDWARE CONTEXT
# RTX 3080  → 10 240 MiB VRAM   (GPU 0, display attached)
# RTX 3060  → 12 288 MiB VRAM   (GPU 1)
# Total     →  ~22 GB  (Ollama auto-spans with CUDA_VISIBLE_DEVICES or its own scheduler)
# RAM       → 128 GB   (no OOM concern for CPU offload)
# ══════════════════════════════════════════════════════════════════════════════

# Models in approximate VRAM-fit order (dual-GPU first, then single-GPU)
MODEL_TIERS = {
    # ── Dual-GPU tier  (~18–20 GB) ──────────────────────────────────────
    "xl_quality":   "nemotron-3-nano-30b-small:latest",  # text-only; was gemma4 (multimodal, vision unused)
    "xl_reason":    "deepseek-r1:32b",              # Strong chain-of-thought
    "xl_code":      "qwen3-coder:30b",              # Best for C++ sections
    # ── Mid tier  (~14–17 GB) ───────────────────────────────────────────
    "mid_code":     "devstral:24b",                 # Good code, fits dual-GPU
    "mid_reason":   "deepseek-r1:14b-qwen-distill-q8_0",  # Q8 fidelity at 14B
    # ── Single-GPU tier  (≤12 GB, fits on 3060) ─────────────────────────
    "single":       "deepseek-r1:14b",              # Reliable, 9 GB
    "single_code":  "qwen2.5-coder:14b",            # Code tasks, 9 GB
    # ── Fast fallback  (≤6 GB) ──────────────────────────────────────────
    "fast":         "deepseek-r1:8b",               # 5 GB, very quick
}

# Page-count → primary model mapping
TIER_BY_PAGES: List[Tuple[int, str]] = [
    (8,   "fast"),          # tiny paper / abstract
    (18,  "single"),        # short paper
    (35,  "xl_quality"),    # standard conference paper
    (999, "xl_quality"),    # long paper — chunking handles context overflow
]

# Separate model for C++ section (code-specialised)
CODE_MODEL = MODEL_TIERS["xl_code"]

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Curated list of known-good models for the interactive selector (-s flag).
# Ordered by preference: best/largest first, fast fallback last.
# (model_name, vram_label, role_label, is_default)
KNOWN_GOOD_MODELS: List[tuple] = [
    ("nemotron-3-nano-30b-small:latest",   "~24 GB", "xl_quality — current default",    True),
    ("deepseek-r1:32b",                    "~19 GB", "xl_reason — chain-of-thought",    False),
    ("deepseek-r1:14b-qwen-distill-q8_0", "~15 GB", "mid_reason — Q8 fidelity",        False),
    ("devstral:24b",                       "~14 GB", "mid_code — code + text",          False),
    ("qwen3.6:35b",                        "~23 GB", "xl — strong general reasoning",   False),
    ("deepseek-r1:14b",                    "~9 GB",  "single — reliable, 9 GB",         False),
    ("gpt-oss:20b",                        "~13 GB", "text-only alternative",           False),
    ("deepseek-r1:8b",                     "~5 GB",  "fast — quick fallback",           False),
]

# Curated code-specialist models for the C++ section selector (-c flag).
KNOWN_GOOD_CODE_MODELS: List[tuple] = [
    ("qwen3-coder:30b",              "~18 GB", "xl_code — current default, best C++",  True),
    ("devstral:24b",                 "~14 GB", "mid_code — strong code, lower VRAM",   False),
    ("qwen2.5-coder:14b-base-q6_K", "~12 GB", "single — Q6 fidelity",                False),
    ("qwen2.5-coder:14b",           "~9 GB",  "single — reliable, single-GPU",        False),
    ("deepseek-coder-v2:16b",       "~9 GB",  "single — fast alternative",            False),
    ("qwen2.5-coder:7b",            "~5 GB",  "fast — quick fallback",                False),
]


# ══════════════════════════════════════════════════════════════════════════════
# OLLAMA GPU PROVISIONER  (--override mode)
# ══════════════════════════════════════════════════════════════════════════════

def _ollama_get_loaded() -> List[str]:
    """Return names of models currently resident in VRAM via /api/ps."""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/ps", timeout=5)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def _ollama_evict(model: str) -> bool:
    """Ask Ollama to unload a model immediately (keep_alive=0)."""
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model, "keep_alive": 0},
            timeout=20,
        )
        return r.status_code == 200
    except Exception:
        return False


def _ollama_wait_clean(timeout: int = 30, interval: float = 2.0) -> bool:
    """Poll /api/ps until no models are loaded or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _ollama_get_loaded():
            return True
        time.sleep(interval)
    return False


def _ollama_restart_service() -> bool:
    """Restart the ollama systemd service and wait for it to come back online."""
    print("  🔄  Restarting ollama service …")
    try:
        r = subprocess.run(
            ["sudo", "systemctl", "restart", "ollama"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            print(f"  ⚠️   systemctl restart failed: {r.stderr.strip()}")
            return False
    except Exception as exc:
        print(f"  ❌  Could not restart ollama: {exc}")
        return False

    print("  ⏳  Waiting for Ollama to come back up …", end="", flush=True)
    deadline = time.time() + 45
    while time.time() < deadline:
        time.sleep(2)
        try:
            r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
            if r.status_code == 200:
                print(" up!")
                return True
        except Exception:
            pass
        print(".", end="", flush=True)
    print(" timed out")
    return False


def provision_ollama(verbose: bool = False) -> bool:
    """
    Aggressively free GPU VRAM before processing starts.

    Escalation ladder:
      1. Evict every loaded model via keep_alive=0 generate requests.
      2. If any model is still loaded after 20 s, restart the ollama service.
      3. Verify clean state; warn but do not abort if it cannot be confirmed.

    Returns True when Ollama is up and VRAM appears free (or after best effort).
    """
    print("\n  🎯  Override: provisioning Ollama for exclusive GPU use …")

    loaded = _ollama_get_loaded()
    if not loaded:
        print("  ✓   No models currently loaded — GPU is free")
        return True

    # Level 1: graceful eviction via keep_alive=0
    print(f"  ⚡  Evicting {len(loaded)} loaded model(s): {', '.join(loaded)}")
    for model in loaded:
        print(f"       → {model} …", end="", flush=True)
        ok = _ollama_evict(model)
        print(" ✓" if ok else " ✗")

    if _ollama_wait_clean(timeout=20):
        print("  ✅  All models evicted — GPU is free\n")
        return True

    # Level 2: force a service restart
    remaining = _ollama_get_loaded()
    print(f"  ⚠️   {len(remaining)} model(s) still loaded — escalating to service restart")
    if not _ollama_restart_service():
        print("  ⚠️   Service restart failed — proceeding anyway\n")
        return False

    if _ollama_wait_clean(timeout=15):
        print("  ✅  Ollama restarted clean\n")
        return True

    print("  ⚠️   Could not confirm clean VRAM — proceeding anyway\n")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# PROMPTS
# ══════════════════════════════════════════════════════════════════════════════
PROMPTS = {
    "summary": textwrap.dedent("""\
        Write a comprehensive, well-structured summary of this AI/ML research paper.
        Your summary must cover ALL of the following:
          1. Motivation & Problem Statement — what gap does this address?
          2. Core Methodology — how does the approach work at a high level?
          3. Key Contributions — what is genuinely novel?
          4. Experimental Setup & Results — benchmarks, datasets, metrics, numbers
          5. Limitations & Failure Modes — where does the approach break down?
          6. Significance — how does this advance the field?
        Be thorough and precise. Use section headers."""),

    "logic": textwrap.dedent("""\
        Refactor the paper's core insights using rigorous symbolic logic and formal notation.
        Structure your response as:

        ## 1. Core Definitions & Notation
        Define all entities, sets, and functions with formal notation (∈, ⊂, →, ℝⁿ, etc.)

        ## 2. Key Theorems & Propositions
        State the paper's central claims as formal propositions with ∀, ∃, →, ↔ quantifiers.

        ## 3. Algorithm Formalisation
        Express each major algorithm using pseudocode with mathematical notation,
        loop invariants, and complexity bounds (O, Ω, Θ).

        ## 4. Optimality & Convergence Conditions
        State any convergence theorems, loss landscape properties, or PAC-learning bounds.

        ## 5. Information-Theoretic View
        Express the core learning objective using entropy H(·), KL divergence, mutual information I(·;·) where applicable.
        
        At the very end of your response, output a fenced JSON block containing all parsed concepts, theorems, and algorithms.
        Use this exact schema:
        ```json
        {
          "concepts": [{"name": "Concept Name", "description": "Concept Description"}],
          "theorems": [{"name": "Theorem Name", "statement": "Theorem Statement"}],
          "algorithms": [{"name": "Algorithm Name", "pseudocode": "Brief pseudocode text", "invariant": "Invariant condition"}]
        }
        ```"""),

    "cpp": textwrap.dedent("""\
        Refactor the paper's core insights using well-crafted C++ code examples.
        Requirements:
          - Use modern C++20 / C++23 (concepts, ranges, coroutines, std::expected, etc.)
          - Implement the key algorithms and data structures described in the paper
          - Each code block must open with a comment block citing the relevant paper section/equation
          - Provide at least 3 self-contained, compilable examples
          - Include a main() that exercises each implementation with sample data
          - Prefer STL containers and algorithms; avoid raw owning pointers
          - Show template metaprogramming or concept constraints where they model the paper's abstractions
          - Add inline comments explaining the mapping from math → code
          
        At the very end of your response, output a fenced JSON block containing all C++ examples.
        Use this exact schema:
        ```json
        {
          "examples": [{"name": "Example Name", "code": "The full C++ code string", "complexity": "Big O string if applicable"}]
        }
        ```"""),

    "extras": textwrap.dedent("""\
        Provide deep additional analysis beyond what the paper itself claims:

        ## 1. Open Questions
        What does this paper leave unresolved? What follow-up experiments are obviously needed?

        ## 2. Related Work & Connections
        How does this relate to other landmark AI/ML papers? What does it supersede?
        What does it complement? Are there surprising connections to other subfields?

        ## 3. Practical Deployment Considerations
        Real-world tradeoffs: latency, memory, data requirements, failure modes in production.

        ## 4. Critical Assessment
        Evaluate the paper's claims critically:
          - Is the experimental setup fair and reproducible?
          - Are there cherry-picked baselines?
          - Do the ablations actually support the claimed conclusions?

        ## 5. Surprising or Underappreciated Insights
        What does the paper imply but not say explicitly? What would a careful reader notice
        that a casual reader would miss?

        ## 6. One-Paragraph Pitch & One-Paragraph Critique
        Steelman the paper in one paragraph. Then write the strongest possible critique in one paragraph."""),
}

# Diagram generation prompt — model must return delimited DOT blocks
DIAGRAM_PROMPT = textwrap.dedent("""\
    Generate exactly 6 Graphviz DOT diagrams that illuminate this AI/ML paper from 6 different angles:

      Diagram 1 — High-Level Architecture / System Overview
      Diagram 2 — Data Flow & Processing Pipeline
      Diagram 3 — Core Algorithm as a Flowchart
      Diagram 4 — Concept Taxonomy / Knowledge Hierarchy
      Diagram 5 — Training Loop / Optimisation Dynamics
      Diagram 6 — Comparison vs Prior Art (or Ablation Structure)

    ══ MANDATORY VISUAL STYLE (apply to EVERY diagram) ══
      graph-level:  bgcolor="black"
      node default: style=filled, fillcolor="#0a0a0a", fontname="Courier New", fontsize=11
      Use NEON accent colours for borders, labels, and edges. Pick from:
        Electric Green  #00FF41    Hot Magenta  #FF00FF    Cyan      #00FFFF
        Neon Orange     #FF6600    Volt Yellow  #FFFF00    Hot Pink  #FF0055
        Chartreuse      #7FFF00    Electric Blue #0080FF   Lavender  #DA70FF
      Edges: penwidth=2.0, use neon colours (vary per diagram)
      Graph titles: use label= and labelloc=t with a bright fontcolor
      Mix rankdir=LR and rankdir=TB between diagrams for variety.

    ══ OUTPUT FORMAT — strictly follow this delimiter pattern ══
    ===DIAGRAM_START: <Descriptive Title for Diagram N>===
    digraph G {
      // ... full valid DOT source ...
    }
    ===DIAGRAM_END===

    Output ONLY the 6 delimited DOT blocks. No prose before, between, or after.""")


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Metadata:
    paper_name: str
    pdf_path: str
    page_count: int
    chunk_strategy: str
    model_used: str
    code_model: str
    processed_at: str
    paper_hash: str
    sections_completed: List[str] = field(default_factory=list)

    def save(self, path: Path):
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Optional["Metadata"]:
        try:
            data = json.loads(path.read_text())
            return cls(**data)
        except Exception:
            return None


ALL_SECTIONS = {"summary", "logic", "cpp", "diagrams", "extras"}


# ══════════════════════════════════════════════════════════════════════════════
# BACKEND  (Ollama direct API  or  OpenClaw agent CLI)
# ══════════════════════════════════════════════════════════════════════════════
class Backend:
    """
    Thin abstraction over LLM backends.
    Ollama  → POST /api/generate  (reliable, full model control)
    OpenClaw → `openclaw agent --message "..."` subprocess
               (model selection via OPENCLAW_MODEL env var or pre-configured gateway)
    """

    def __init__(self, name: str, default_model: str):
        self.name          = name
        self.default_model = default_model

    def call(
        self,
        prompt: str,
        model: Optional[str] = None,
        ctx_tokens: int = 32768,
    ) -> str:
        m = model or self.default_model
        if self.name == "ollama":
            return self._call_ollama(prompt, m, ctx_tokens)
        return self._call_openclaw(prompt, m)

    # ── Ollama ────────────────────────────────────────────────────────────
    def _call_ollama(self, prompt: str, model: str, ctx: int) -> str:
        url = f"{OLLAMA_URL}/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": True,  # Enable streaming for interruptibility
            "options": {
                "num_ctx":        ctx,
                "temperature":    0.20,
                "top_p":          0.90,
                "repeat_penalty": 1.10,
            },
        }
        for attempt in range(1, 3):
            if _shutdown.is_set():
                return ""

            try:
                r = requests.post(url, json=payload, timeout=1200, stream=True)
                if not r.ok:
                    try:
                        body = r.json().get("error", r.text[:200])
                    except Exception:
                        body = r.text[:200]
                    raise RuntimeError(f"Ollama HTTP {r.status_code} (model={model}): {body}")

                full_response = []
                for line in r.iter_lines():
                    if _shutdown.is_set():
                        r.close()
                        raise _ShutdownRequested()
                    if line:
                        data = json.loads(line)
                        if "response" in data:
                            full_response.append(data["response"])
                        if data.get("done"):
                            break
                return "".join(full_response).strip()

            except requests.exceptions.ConnectionError:
                sys.exit(
                    f"❌  Cannot reach Ollama at {OLLAMA_URL}\n"
                    "    Is `ollama serve` running?"
                )
            except requests.exceptions.Timeout as exc:
                if attempt == 1 and not _shutdown.is_set():
                    print(f"     ⏱  Ollama timed out — restarting and retrying (model={model}) …")
                    _ollama_restart_service()
                    continue
                raise RuntimeError(f"Ollama error (model={model}): {exc}") from exc
            except (_ShutdownRequested, RuntimeError):
                raise
            except Exception as exc:
                raise RuntimeError(f"Ollama error (model={model}): {exc}") from exc

    # ── OpenClaw ──────────────────────────────────────────────────────────
    def _call_openclaw(self, prompt: str, model: str) -> str:
        """
        Calls:  openclaw agent --agent main --message "<prompt>"
        Routes via the gateway and uses the agent's configured default model
        (set in ~/.openclaw/openclaw.json). OPENCLAW_MODEL is exported for
        builds that honour it, but the gateway path ignores per-call overrides
        unless the caller is authorised — so per-page-count routing is a
        no-op here. To restore per-call model selection, switch to
        `openclaw agent --local --agent main --model <model> --message <p>`.
        """
        env = os.environ.copy()
        env["OPENCLAW_MODEL"] = model  # no-op against the gateway path

        cmd = ["openclaw", "agent", "--agent", "main", "--message", prompt]

        try:
            # Using Popen + poll for interruptibility
            with subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            ) as proc:
                deadline = time.time() + 1200
                while proc.poll() is None:
                    if _shutdown.is_set():
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        return ""
                    if time.time() > deadline:
                        proc.kill()
                        raise RuntimeError("OpenClaw agent timed out (>20 min)")
                    time.sleep(1.0)

                stdout, stderr = proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"openclaw agent exited {proc.returncode}: {stderr.strip() or '(no stderr)'}"
                    )

                output = stdout.strip()
                if not output:
                    raise RuntimeError("openclaw agent returned empty response")
                return output

        except FileNotFoundError:
            sys.exit("❌  `openclaw` not found in PATH.")
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"OpenClaw error: {exc}") from exc


# ══════════════════════════════════════════════════════════════════════════════
# PDF  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════
def extract_pages(pdf_path: Path) -> List[str]:
    """Return list of non-empty page texts (back-compat: no OCR, native only)."""
    pages, _ = extract_pages_with_ocr(pdf_path, mode="never")
    return pages


def select_model(page_count: int, user_override: Optional[str]) -> str:
    if user_override:
        return user_override
    for threshold, tier_key in TIER_BY_PAGES:
        if page_count <= threshold:
            return MODEL_TIERS[tier_key]
    return MODEL_TIERS["xl_quality"]


def prompt_model_selection(models: List[tuple] = None) -> str:
    """Numbered menu of known-good models. Returns the chosen model string."""
    if models is None:
        models = KNOWN_GOOD_MODELS
    print("\n  Known-good models — select for this run:")
    print(f"  {'#':<4} {'Model':<47} {'VRAM':<9} Role")
    print(f"  {'─'*80}")
    for i, (name, vram, role, is_default) in enumerate(models, 1):
        marker = "  ◀" if is_default else ""
        print(f"  {i:<4} {name:<47} {vram:<9} {role}{marker}")
    print()
    default_idx = next(
        i for i, (_, _, _, d) in enumerate(models, 1) if d
    )
    while True:
        try:
            raw = input(f"  Enter number [{default_idx}]: ").strip()
            idx = int(raw) if raw else default_idx
            if 1 <= idx <= len(models):
                chosen = models[idx - 1][0]
                print(f"  → {chosen}\n")
                return chosen
        except (ValueError, EOFError):
            pass
        print(f"  Please enter 1–{len(models)}")


def build_chunks(pages: List[str], window: int = 12, overlap: int = 2) -> List[str]:
    """Sliding-window chunking with overlap to preserve cross-page context."""
    if len(pages) <= window:
        return ["\n\n".join(pages)]
    chunks, i = [], 0
    while i < len(pages):
        end = min(i + window, len(pages))
        chunks.append("\n\n".join(pages[i:end]))
        if end == len(pages):
            break
        i += window - overlap
    return chunks


def map_reduce_chunks(
    chunks: List[str],
    backend: Backend,
    model: str,
) -> str:
    """Summarise each chunk, then return joined summaries as condensed context."""
    if len(chunks) == 1:
        return chunks[0]

    print(f"      ↳ Map-reduce: {len(chunks)} chunks …")
    partial: List[str] = []
    for idx, chunk in enumerate(chunks, 1):
        if _shutdown.is_set():
            raise _ShutdownRequested()
        prompt = (
            f"You are reading chunk {idx} of {len(chunks)} of an AI/ML research paper. "
            "Summarise this chunk concisely, preserving all technical details, "
            "equations, and experimental results:\n\n" + chunk[:18000]
        )
        summary = backend.call(prompt, model, ctx_tokens=16384)
        partial.append(f"### Chunk {idx}/{len(chunks)}\n{summary}")
        print(f"        chunk {idx}/{len(chunks)} ✓")

    return "\n\n---\n\n".join(partial)


# ══════════════════════════════════════════════════════════════════════════════
# DIAGRAM  PARSING  +  RENDERING
# ══════════════════════════════════════════════════════════════════════════════
_DELIM_RE = re.compile(
    r"===DIAGRAM_START:\s*(.+?)===\s*(.*?)===DIAGRAM_END===",
    re.DOTALL | re.IGNORECASE,
)
# Fallback: bare ``` or ```dot fences
_FENCE_RE = re.compile(
    r"```(?:dot|graphviz)?\s*\n?((?:digraph|graph)\b.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def parse_diagrams(raw: str) -> List[Tuple[str, str]]:
    results: List[Tuple[str, str]] = []

    for m in _DELIM_RE.finditer(raw):
        title = m.group(1).strip()
        dot   = m.group(2).strip()
        if dot and ("digraph" in dot.lower() or "graph" in dot.lower()):
            results.append((title, dot))

    if not results:  # fallback — try fenced blocks
        for idx, m in enumerate(_FENCE_RE.finditer(raw), 1):
            results.append((f"diagram_{idx:02d}", m.group(1).strip()))

    return results


def ensure_neon_black(dot_src: str) -> str:
    """
    Inject bgcolor=black and a default neon node style if the LLM forgot.
    Non-destructive: skips injection if already present.
    """
    if "bgcolor" not in dot_src:
        dot_src = re.sub(
            r"((?:di)?graph\s+\w*\s*\{)",
            r'\1\n  graph [bgcolor="black" fontcolor="#00FF41" fontname="Courier New"];'
            r'\n  node  [style=filled fillcolor="#0a0a0a" color="#00FF41" fontcolor="#00FF41" fontname="Courier New"];'
            r'\n  edge  [color="#FF00FF" penwidth=2.0];',
            dot_src,
            count=1,
        )
    return dot_src


def render_dot(dot_src: str, out_svg: Path) -> bool:
    try:
        r = subprocess.run(
            ["dot", "-Tsvg", "-o", str(out_svg)],
            input=dot_src,
            text=True,
            capture_output=True,
            timeout=30,
        )
        return r.returncode == 0
    except FileNotFoundError:
        print("      ⚠️  graphviz `dot` not found — SVGs will not be rendered")
        print("          Fix: sudo apt install graphviz")
        return False
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# PAPER  PROCESSOR
# ══════════════════════════════════════════════════════════════════════════════
class PaperProcessor:
    def __init__(
        self,
        papers_dir: Path,
        backend: Backend,
        forced_model: Optional[str] = None,
        forced_code_model: Optional[str] = None,
        reprocess: Optional[str] = None,
        verbose: bool = False,
        ocr_mode: str = "auto",
        ocr_min_chars: int = DEFAULT_MIN_CHARS,
        ocr_dpi: int = DEFAULT_DPI,
        ocr_lang: str = DEFAULT_LANG,
    ):
        self.papers_dir        = papers_dir
        self.out_root          = papers_dir / "_processed"
        self.backend           = backend
        self.forced_model      = forced_model
        self.forced_code_model = forced_code_model
        self.reprocess    = reprocess  # section name or "all"
        self.verbose      = verbose
        self.ocr_mode      = ocr_mode
        self.ocr_min_chars = ocr_min_chars
        self.ocr_dpi       = ocr_dpi
        self.ocr_lang      = ocr_lang
        self.out_root.mkdir(exist_ok=True)

    # ── Utilities ─────────────────────────────────────────────────────────
    def _paper_dir(self, pdf: Path) -> Path:
        # Mirror the input tree under _processed/ so subfolder structure is preserved
        # and slug collisions across subdirs are impossible.
        try:
            rel_parent = pdf.parent.relative_to(self.papers_dir)
        except ValueError:
            rel_parent = Path("")
        parent_slug = Path(*[
            re.sub(r"[^\w\-]", "_", part).lower().strip("_")
            for part in rel_parent.parts
        ]) if rel_parent.parts else Path("")
        stem_slug = re.sub(r"[^\w\-]", "_", pdf.stem)[:64].lower().strip("_")
        d = self.out_root / parent_slug / stem_slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "diagrams").mkdir(exist_ok=True)
        return d

    def _should_run(self, section: str, completed: List[str]) -> bool:
        if self.reprocess in (section, "all"):
            return True
        return section not in completed

    def _write_md(self, path: Path, heading: str, body: str):
        path.write_text(f"# {heading}\n\n{body}\n", encoding="utf-8")

    def _tag_prompt(self, task_prompt: str, context: str) -> str:
        """Wrap paper context in XML tags for cleaner prompt structure."""
        return f"{task_prompt}\n\n<paper>\n{context}\n</paper>"

    def _save_meta(
        self,
        meta_path: Path,
        pdf_path: Path,
        page_count: int,
        strategy: str,
        model: str,
        code_model: str,
        paper_hash: str,
        completed: List[str],
    ) -> None:
        Metadata(
            paper_name        = pdf_path.name,
            pdf_path          = str(pdf_path),
            page_count        = page_count,
            chunk_strategy    = strategy,
            model_used        = model,
            code_model        = code_model,
            processed_at      = time.strftime("%Y-%m-%dT%H:%M:%S"),
            paper_hash        = paper_hash,
            sections_completed= completed,
        ).save(meta_path)

    # ── Main entry ────────────────────────────────────────────────────────
    def process(self, pdf_path: Path) -> None:
        paper_dir = self._paper_dir(pdf_path)
        meta_path = paper_dir / "metadata.json"
        meta      = Metadata.load(meta_path)

        # Skip if fully complete and no reprocess requested
        if (
            meta is not None
            and ALL_SECTIONS.issubset(set(meta.sections_completed))
            and self.reprocess is None
        ):
            print(f"  ⏭   {pdf_path.name}  (all sections complete)")
            return

        print(f"\n{'─'*64}")
        print(f"  📄  {pdf_path.name}")

        # ── Extract & chunk ───────────────────────────────────────────────
        # Hash first so the OCR cache key is stable across re-runs.
        paper_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()[:16]
        pages, ocr_stats = extract_pages_with_ocr(
            pdf_path,
            mode=self.ocr_mode,
            min_chars=self.ocr_min_chars,
            dpi=self.ocr_dpi,
            lang=self.ocr_lang,
            paper_hash=paper_hash,
            cache_dir=paper_dir / ".ocr_cache",
        )
        page_count = len(pages)
        model      = select_model(page_count, self.forced_model)
        code_model = self.forced_code_model or self.forced_model or CODE_MODEL

        chunks     = build_chunks(pages)
        strategy   = (
            f"single-pass ({page_count} pages)"
            if len(chunks) == 1
            else f"sliding-window ({len(chunks)} chunks, {page_count} pages)"
        )

        print(f"     pages={page_count}  chunks={len(chunks)}")
        if ocr_stats.ocr_used or ocr_stats.cached_pages:
            print(f"     ocr={ocr_stats.summary()}")
        print(f"     model={model}")
        print(f"     code_model={code_model}")
        print(f"     strategy={strategy}")

        completed: List[str] = list(meta.sections_completed) if meta else []

        def _checkpoint():
            self._save_meta(meta_path, pdf_path, page_count, strategy,
                            model, code_model, paper_hash, completed)

        def _shutting_down() -> bool:
            if _shutdown.is_set():
                _checkpoint()
                print(f"     ⚡  Stopped — {len(completed)} section(s) saved")
                return True
            return False

        # Condense multi-chunk papers to a manageable context string
        try:
            context = map_reduce_chunks(chunks, self.backend, model)
        except _ShutdownRequested:
            _checkpoint()
            print(f"     ⚡  Stopped during map-reduce — {len(completed)} section(s) saved")
            return

        # Cap to ~90k chars (~22k tokens) — safe for 32k-ctx models
        capped = context[:90_000]

        # ── 1. Summary ────────────────────────────────────────────────────
        if self._should_run("summary", completed):
            if _shutting_down():
                return
            print("     📝  Summary …")
            try:
                out = self.backend.call(self._tag_prompt(PROMPTS["summary"], capped), model)
            except _ShutdownRequested:
                _checkpoint()
                print(f"     ⚡  Interrupted mid-section — {len(completed)} section(s) saved")
                return
            self._write_md(paper_dir / "01_summary.md", "Summary", out)
            if "summary" not in completed:
                completed.append("summary")
            _checkpoint()
            print("         ✓")

        # ── 2. Symbolic Logic ─────────────────────────────────────────────
        if self._should_run("logic", completed):
            if _shutting_down():
                return
            print("     🔣  Symbolic logic …")
            try:
                out = self.backend.call(self._tag_prompt(PROMPTS["logic"], capped), model)
            except _ShutdownRequested:
                _checkpoint()
                print(f"     ⚡  Interrupted mid-section — {len(completed)} section(s) saved")
                return
            self._write_md(paper_dir / "02_symbolic_logic.md", "Symbolic Logic Formulation", out)
            if "logic" not in completed:
                completed.append("logic")
            _checkpoint()
            print("         ✓")

        # ── 3. C++ Examples ───────────────────────────────────────────────
        if self._should_run("cpp", completed):
            if _shutting_down():
                return
            print(f"     💻  C++ examples  (model: {code_model}) …")
            try:
                out = self.backend.call(self._tag_prompt(PROMPTS["cpp"], capped), code_model)
            except _ShutdownRequested:
                _checkpoint()
                print(f"     ⚡  Interrupted mid-section — {len(completed)} section(s) saved")
                return
            self._write_md(paper_dir / "03_cpp_examples.md", "C++ Implementation Examples", out)
            if "cpp" not in completed:
                completed.append("cpp")
            _checkpoint()
            print("         ✓")

        # ── 4. Graphviz Diagrams ──────────────────────────────────────────
        if self._should_run("diagrams", completed):
            if _shutting_down():
                return
            print("     📊  Graphviz diagrams …")
            try:
                raw = self.backend.call(
                    self._tag_prompt(DIAGRAM_PROMPT, capped[:60_000]),
                    model,
                    ctx_tokens=32768,
                )
            except _ShutdownRequested:
                _checkpoint()
                print(f"     ⚡  Interrupted mid-section — {len(completed)} section(s) saved")
                return
            diagrams = parse_diagrams(raw)

            if not diagrams:
                # Save raw output so user can inspect / manually extract DOT
                raw_out = paper_dir / "diagrams" / "_raw_llm_output.txt"
                raw_out.write_text(raw, encoding="utf-8")
                print(
                    f"     ⚠️   No diagrams parsed from LLM output.\n"
                    f"          Raw output saved → {raw_out}\n"
                    f"          Tip: re-run with --reprocess diagrams after inspecting output."
                )
            else:
                # Purge stale slugs so reprocessing doesn't leave orphaned files
                # from a prior run that used different diagram titles.
                diag_dir = paper_dir / "diagrams"
                for _stale in list(diag_dir.glob("*.dot")) + list(diag_dir.glob("*.svg")):
                    _stale.unlink()
                for idx, (title, dot_src) in enumerate(diagrams, 1):
                    dot_src  = ensure_neon_black(dot_src)
                    safe     = re.sub(r"[^\w\-]", "_", title)[:40].lower().strip("_")
                    dot_path = diag_dir / f"{idx:02d}_{safe}.dot"
                    svg_path = diag_dir / f"{idx:02d}_{safe}.svg"
                    dot_path.write_text(dot_src, encoding="utf-8")
                    ok     = render_dot(dot_src, svg_path)
                    status = "✓" if ok else "✗ (dot saved, SVG render failed)"
                    print(f"       {idx}. {title:<45} {status}")

            if "diagrams" not in completed:
                completed.append("diagrams")
            _checkpoint()

        # ── 5. Extras ─────────────────────────────────────────────────────
        if self._should_run("extras", completed):
            if _shutting_down():
                return
            print("     💡  Extras / critical analysis …")
            try:
                out = self.backend.call(self._tag_prompt(PROMPTS["extras"], capped), model)
            except _ShutdownRequested:
                _checkpoint()
                print(f"     ⚡  Interrupted mid-section — {len(completed)} section(s) saved")
                return
            self._write_md(paper_dir / "04_extras.md", "Additional Insights", out)
            if "extras" not in completed:
                completed.append("extras")
            _checkpoint()
            print("         ✓")

        # ── Write / update metadata ────────────────────────────────────────
        _checkpoint()
        print(f"     ✅  Output → {paper_dir}")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════
def list_status(papers_dir: Path):
    processed_dir = papers_dir / "_processed"
    pdfs = sorted(p for p in papers_dir.rglob("*.pdf") if "_processed" not in p.parts)
    if not pdfs:
        print(f"  No PDFs found in {papers_dir}")
        return

    print(f"\n  {'Paper':<60}  {'Status'}")
    print(f"  {'─'*60}  {'─'*30}")
    for pdf in pdfs:
        try:
            rel_parent = pdf.parent.relative_to(papers_dir)
        except ValueError:
            rel_parent = Path("")
        parent_slug = Path(*[
            re.sub(r"[^\w\-]", "_", part).lower().strip("_")
            for part in rel_parent.parts
        ]) if rel_parent.parts else Path("")
        stem_slug = re.sub(r"[^\w\-]", "_", pdf.stem)[:64].lower().strip("_")
        meta_path = processed_dir / parent_slug / stem_slug / "metadata.json"
        meta      = Metadata.load(meta_path) if meta_path.exists() else None

        if meta is None:
            status = "⬜  not started"
        elif ALL_SECTIONS.issubset(set(meta.sections_completed)):
            status = f"✅  complete  [{meta.model_used}]"
        else:
            missing = ALL_SECTIONS - set(meta.sections_completed)
            status  = f"⚠️   partial — missing: {', '.join(sorted(missing))}"

        rel = pdf.relative_to(papers_dir).as_posix()
        name = rel[:58] + "…" if len(rel) > 58 else rel
        print(f"  {name:<60}  {status}")
    print()


def health_check_ollama():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        print(f"  🟢  Ollama reachable at {OLLAMA_URL}")
        print(f"       {len(models)} models available")
        return True
    except Exception as exc:
        print(f"  ❌  Cannot reach Ollama at {OLLAMA_URL}: {exc}")
        return False


def check_required_models(models: List[str]) -> None:
    """Exit with pull instructions if any model is absent from Ollama's local library.

    Prevents auto-download hangs: missing models trigger a silent pull on first
    generate request, which can exceed the 1200s timeout and cascade to all
    subsequent papers (OLLAMA_NUM_PARALLEL=1 queues them behind the stuck request).
    """
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        r.raise_for_status()
        available = {m["name"] for m in r.json().get("models", [])}
    except Exception:
        return  # health_check_ollama already reported unreachable Ollama
    missing = [m for m in models if m not in available]
    if missing:
        sys.exit(
            "❌  Required models not found locally — pull them first to avoid "
            "auto-download timeouts:\n"
            + "\n".join(f"    ollama pull {m}" for m in missing)
        )


def health_check_openclaw():
    try:
        r = subprocess.run(
            ["openclaw", "health"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            print("  🟢  OpenClaw gateway healthy")
            return True
        print(f"  ⚠️   openclaw health returned {r.returncode}: {r.stderr.strip()}")
        return False
    except FileNotFoundError:
        print("  ❌  `openclaw` not found in PATH")
        return False


def _sync_to_neo4j(processed_dir: Optional[str] = None):
    """Checks if Neo4j is listening on Port 7687 and runs the importer script to auto-sync."""
    if not _sync_lock.acquire(blocking=False):
        return
    try:
        import socket
        import subprocess
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(('localhost', 7687)) == 0:
                print("\n  🔄 Syncing processed results to Neo4j graph database...")
                cmd = [sys.executable, "neo4j_viz/neo4j_importer.py"]
                if processed_dir:
                    cmd.append(processed_dir)
                subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                print("  ✅ Neo4j database sync complete!")
    except Exception:
        pass
    finally:
        _sync_lock.release()


def _periodic_sync_worker(processed_dir: Optional[str] = None, interval: int = 300):
    """Background thread worker to periodically trigger database synchronization."""
    # Sleep 15 seconds initially to let the first paper get some processing headstart
    for _ in range(15):
        if _shutdown.is_set():
            return
        time.sleep(1)
        
    while not _shutdown.is_set():
        _sync_to_neo4j(processed_dir)
        
        # Non-blocking sleep responsive to shutdown event
        for _ in range(interval):
            if _shutdown.is_set():
                break
            time.sleep(1)


def main():
    ap = argparse.ArgumentParser(
        prog="paper_processor",
        description="🦞  OpenClaw / Ollama  AI-ML Paper Processing Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Output tree per paper:
              _processed/<slug>/
                metadata.json
                01_summary.md
                02_symbolic_logic.md
                03_cpp_examples.md
                04_extras.md
                diagrams/
                  01_<title>.dot
                  01_<title>.svg
                  …  (6+ diagrams)

            Model auto-selection by page count (use -s to pick interactively):
              ≤ 8  pages  →  deepseek-r1:8b      (~5 GB)
              ≤ 18 pages  →  deepseek-r1:14b     (~9 GB)
              > 18 pages  →  nemotron-3-nano-30b-small (~24 GB, dual-GPU)
              C++ section →  qwen3-coder:30b      (~17 GB, dual-GPU)
              -s / --select-model  →  numbered menu of known-good models
        """),
    )
    ap.add_argument(
        "papers_dir", nargs="?",
        default=os.path.join(os.path.expanduser("~"), "Documents", "AI-ML_Papers"),
        help="Directory containing PDF papers (default: ~/Documents/AI-ML_Papers)",
    )
    ap.add_argument(
        "--backend", choices=["ollama", "openclaw"], default="ollama",
        help="LLM backend: 'ollama' (direct API) or 'openclaw' (agent CLI) [default: ollama]",
    )
    ap.add_argument(
        "--model", default=None, metavar="MODEL",
        help="Force a specific model for all sections (overrides auto-selection)",
    )
    ap.add_argument(
        "--paper", default=None, metavar="FILENAME",
        help="Process a single paper by filename (e.g. 'attention.pdf')",
    )
    ap.add_argument(
        "--workers", type=int, default=1, metavar="N",
        help="Parallel paper workers — keep at 1 unless papers are small (VRAM limit)",
    )
    ap.add_argument(
        "--list", action="store_true",
        help="Show processing status for all papers and exit",
    )
    ap.add_argument(
        "--reprocess", default=None,
        choices=["summary", "logic", "cpp", "diagrams", "extras", "all"],
        metavar="SECTION",
        help="Re-run a specific section: summary|logic|cpp|diagrams|extras|all",
    )
    ap.add_argument(
        "--override", action="store_true",
        help=(
            "Aggressively provision GPU before processing: evict all loaded "
            "Ollama models (keep_alive=0), and restart the ollama service if "
            "any model is still resident after eviction."
        ),
    )
    ap.add_argument(
        "--verbose", "-v", action="store_true",
        help="Extra debug output",
    )
    ap.add_argument(
        "--select-model", "-s", action="store_true",
        help="Interactively choose the model before processing (overrides auto-selection by page count)",
    )
    ap.add_argument(
        "--code-model", default=None, metavar="MODEL",
        help="Force a specific model for C++ sections only (overrides CODE_MODEL default)",
    )
    ap.add_argument(
        "--select-code-model", "-c", action="store_true",
        help="Interactively choose the C++ section model before processing",
    )
    ap.add_argument(
        "--ocr", choices=["auto", "always", "never"], default="auto", metavar="MODE",
        help="OCR fallback for scanned PDFs: auto (OCR low-text pages), "
             "always (OCR every page), never (disable) [default: auto]",
    )
    ap.add_argument(
        "--ocr-min-chars", type=int, default=DEFAULT_MIN_CHARS, metavar="N",
        help=f"Pages with fewer than N native chars are OCR'd in auto mode "
             f"[default: {DEFAULT_MIN_CHARS}]",
    )
    ap.add_argument(
        "--ocr-dpi", type=int, default=DEFAULT_DPI, metavar="DPI",
        help=f"Rasterisation DPI passed to Tesseract [default: {DEFAULT_DPI}]",
    )
    ap.add_argument(
        "--ocr-lang", default=DEFAULT_LANG, metavar="LANG",
        help=f"Tesseract language pack(s), e.g. 'eng' or 'eng+deu' [default: {DEFAULT_LANG}]",
    )
    args = ap.parse_args()

    if args.workers < 1:
        ap.error("--workers must be >= 1")

    papers_dir = Path(args.papers_dir)
    if not papers_dir.exists():
        sys.exit(f"❌  Directory not found: {papers_dir}")

    # ── List mode ──────────────────────────────────────────────────────────
    if args.list:
        list_status(papers_dir)
        return

    _install_signal_handlers()

    # ── Health check ───────────────────────────────────────────────────────
    print(f"\n  🦞  OpenClaw Paper Processor")
    print(f"  {'─'*40}")
    ok = health_check_ollama() if args.backend == "ollama" else health_check_openclaw()
    if not ok:
        sys.exit(1)

    if args.backend == "ollama":
        models_to_check = (
            [args.model]
            if args.model
            else list(dict.fromkeys(
                [MODEL_TIERS[k] for _, k in TIER_BY_PAGES] + [CODE_MODEL]
            ))
        )
        check_required_models(models_to_check)

    if args.override and args.backend == "ollama":
        provision_ollama(verbose=args.verbose)

    if args.select_model and not args.model:
        if sys.stdin.isatty():
            args.model = prompt_model_selection()
        else:
            print("[warn] --select-model ignored (stdin is not a TTY)", file=sys.stderr)

    if args.select_code_model and not args.code_model:
        if sys.stdin.isatty():
            print("  C++ / code section model:")
            args.code_model = prompt_model_selection(KNOWN_GOOD_CODE_MODELS)
        else:
            print("[warn] --select-code-model ignored (stdin is not a TTY)", file=sys.stderr)

    default_model = args.model or MODEL_TIERS["xl_quality"]
    backend       = Backend(args.backend, default_model)

    print(f"  Backend   : {args.backend}")
    print(f"  Default   : {default_model}")
    if args.reprocess:
        print(f"  Reprocess : {args.reprocess}")

    # ── Build file list ────────────────────────────────────────────────────
    if args.paper:
        target = papers_dir / args.paper
        if not target.exists():
            # Fallback: search recursively by basename
            matches = [p for p in papers_dir.rglob(args.paper) if p.is_file()]
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                sys.exit(
                    f"❌  Ambiguous --paper '{args.paper}' — {len(matches)} matches:\n    "
                    + "\n    ".join(str(m.relative_to(papers_dir)) for m in matches[:10])
                )
            else:
                sys.exit(f"❌  File not found: {target}")
        pdfs = [target]
    else:
        pdfs = sorted(
            p for p in papers_dir.rglob("*.pdf") if "_processed" not in p.parts
        )
        if not pdfs:
            sys.exit(f"❌  No PDF files found in {papers_dir}")

    print(f"  Papers    : {len(pdfs)}")
    print(f"  Output    : {papers_dir / '_processed'}")
    print()

    processor = PaperProcessor(
        papers_dir   = papers_dir,
        backend      = backend,
        forced_model      = args.model,
        forced_code_model = args.code_model,
        reprocess         = args.reprocess,
        verbose      = args.verbose,
        ocr_mode      = args.ocr,
        ocr_min_chars = args.ocr_min_chars,
        ocr_dpi       = args.ocr_dpi,
        ocr_lang      = args.ocr_lang,
    )

    # ── Spawn periodic database sync background thread ───────────────────────
    sync_thread = threading.Thread(
        target=_periodic_sync_worker,
        args=(str(papers_dir / "_processed"), 300),  # sync every 5 minutes / 300 seconds
        daemon=True
    )
    sync_thread.start()

    # ── Process ────────────────────────────────────────────────────────────
    errors: List[str] = []

    if args.workers > 1:
        print(f"  ⚡ Parallel mode: {args.workers} workers")
        print(f"  ⚠️   Ensure models fit in VRAM when running concurrently!\n")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(processor.process, pdf): pdf for pdf in pdfs}
            remaining = set(futures)
            while remaining:
                done, remaining = wait(remaining, timeout=2.0,
                                       return_when=FIRST_COMPLETED)
                for fut in done:
                    pdf = futures[fut]
                    try:
                        fut.result()
                    except _ShutdownRequested:
                        pass
                    except Exception as exc:
                        msg = f"{pdf.name}: {exc}"
                        errors.append(msg)
                        print(f"  ❌  {msg}")
                        if "timed out" in str(exc).lower():
                            print("  🔄  Timeout detected — restarting Ollama to unblock next paper …")
                            _ollama_restart_service()
                if _shutdown.is_set():
                    print(f"  ⚡  Cancelling {len(remaining)} pending paper(s) …")
                    for f in remaining:
                        f.cancel()
                    break
    else:
        for pdf in pdfs:
            if _shutdown.is_set():
                print(f"  ⚡  Shutdown — skipping remaining {len(pdfs) - pdfs.index(pdf)} paper(s)")
                break
            try:
                processor.process(pdf)
            except _ShutdownRequested:
                break
            except Exception as exc:
                msg = f"{pdf.name}: {exc}"
                errors.append(msg)
                print(f"  ❌  {msg}")
                if "timed out" in str(exc).lower():
                    print("  🔄  Timeout detected — restarting Ollama to unblock next paper …")
                    _ollama_restart_service()

    # Auto-sync results to Neo4j Graph DB if it is running
    _sync_to_neo4j(str(papers_dir / "_processed"))

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'═'*64}")
    succeeded = len(pdfs) - len(errors)
    print(f"  ✅  {succeeded}/{len(pdfs)} papers processed successfully")
    if errors:
        print(f"  ❌  {len(errors)} error(s):")
        for e in errors:
            print(f"       • {e}")
    print(f"{'═'*64}\n")


if __name__ == "__main__":
    main()
