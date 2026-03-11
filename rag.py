"""
RAG for IST admissions voice agent.

Chunking strategy:
  1. FAQ Q&A lines  — individual lines from === sections (precise, one answer per line)
  2. ## data blocks  — paragraph chunks from ## sections (keeps label + value together)
  3. Scraped TOPIC   — topic blocks from scraped web content

Scoring: BM25 on CLEANED text (TOPIC labels stripped before indexing).
         FAQ lines get a boost to counteract length-normalisation penalty.
"""
import re
import math
import logging
from collections import Counter

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 1. Load KB
# ─────────────────────────────────────────────────────────────────────────────

with open("all_kb.txt", "r", encoding="utf-8") as _f:
    _RAW = _f.read()

# ─────────────────────────────────────────────────────────────────────────────
# 2. Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean_markers(text: str) -> str:
    """Strip [TOPIC:…] and PAGE/TOPIC header lines — used for both display and indexing."""
    t = re.sub(r"\[TOPIC:[^\]]+\]\s*", "", text)
    t = re.sub(r"(PAGE|TOPIC)\s*:\s*[^\n]*\n?", "", t)
    return t.strip()

def _is_nav_block(text: str) -> bool:
    """True when >55 % of non-empty lines are bare nav link labels (no punctuation)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return True
    nav_like = sum(1 for l in lines if len(l) < 55 and not any(c in l for c in ".?:,()@+"))
    return (nav_like / len(lines)) > 0.55

# ─────────────────────────────────────────────────────────────────────────────
# 3. Build chunk pool
# ─────────────────────────────────────────────────────────────────────────────

_faq_chunks:  list[str] = []   # individual Q&A lines from === sections
_data_chunks: list[str] = []   # paragraph blocks from ## sections
_body_chunks: list[str] = []   # scraped TOPIC blocks

_FAQ_END = "## PROGRAMS AND ADMISSIONS DATA"
_faq_raw  = _RAW.split(_FAQ_END)[0]

# ── 3a. === FAQ sections: one chunk per Q&A line ─────────────────────────────
_in_faq_section = False
for _line in _faq_raw.splitlines():
    s = _line.strip()
    if re.match(r"^=== .+ ===$", s):
        _in_faq_section = True
        continue
    if s.startswith("## "):
        _in_faq_section = False

    if _in_faq_section:
        if len(s) > 40 and any(c in s for c in ".?:()"):
            _faq_chunks.append(s)

# ── 3b. ## data sections: paragraph chunks (keeps label + data together) ─────
_in_data_section = False
_current_data_lines: list[str] = []

_current_dept_label = ""   # tracks "DEPARTMENT: Computing" for labelling sub-paragraphs

def _flush_data_para(lines: list[str]) -> None:
    global _current_dept_label
    para = "\n".join(lines).strip()
    if not para or len(para) < 50 or _is_nav_block(para):
        return
    # Sub-split on DEPARTMENT: so each dept is its own chunk
    dept_blocks = re.split(r"(?=^DEPARTMENT:)", para, flags=re.MULTILINE)
    for block in dept_blocks:
        b = block.strip()
        if not b or len(b) < 50 or _is_nav_block(b):
            continue
        # Update current dept label when we hit a DEPARTMENT: block
        dept_match = re.match(r"DEPARTMENT: (.+)", b)
        if dept_match:
            _current_dept_label = b   # the whole dept header block
        elif _current_dept_label:
            # Prepend department context so "Benish Amin" chunk knows it belongs to Computing
            b = f"{_current_dept_label}\n---\n{b}"
        _data_chunks.append(b)

for _line in _faq_raw.splitlines():
    s = _line.strip()
    if re.match(r"^=== .+ ===$", s):
        if _current_data_lines:
            _flush_data_para(_current_data_lines)
            _current_data_lines = []
        _in_data_section = False
        _current_dept_label = ""
        continue
    if s.startswith("## "):
        if _current_data_lines:
            _flush_data_para(_current_data_lines)
            _current_data_lines = []
        _in_data_section = True
        _current_dept_label = ""
        continue
    # === separators within data sections = paragraph boundary
    if s.startswith("==="):
        if _in_data_section and _current_data_lines:
            _flush_data_para(_current_data_lines)
            _current_data_lines = []
        _current_dept_label = ""
        continue

    if _in_data_section:
        if s:
            _current_data_lines.append(s)
        else:
            if _current_data_lines:
                _flush_data_para(_current_data_lines)
                _current_data_lines = []

if _current_data_lines:
    _flush_data_para(_current_data_lines)

# ── 3c. Scraped body — split on all page-break patterns ──────────────────────
_scraped_raw = _RAW[_RAW.find(_FAQ_END):]

def _split_scraped(text: str) -> list[str]:
    blocks: list[str] = []
    for piece in re.split(r"={10,}", text):
        p = re.sub(r"^(PAGE\s*:\s*[^\n]*\n|TOPIC\s*:\s*[^\n]*\n)+", "", piece.strip()).strip()
        if not p or len(p) < 60:
            continue
        for sp in re.split(r"(?=\[TOPIC:)", p):
            sp = sp.strip()
            if not sp or len(sp) < 60:
                continue
            for ssp in re.split(r"\n---[^-\n]{1,60}---\n", sp):
                ssp = ssp.strip()
                if len(ssp) >= 60 and not _is_nav_block(ssp):
                    blocks.append(ssp)
    return blocks

_body_chunks = _split_scraped(_scraped_raw)

# ── 3d. Deduplicate ───────────────────────────────────────────────────────────
def _dedup(lst: list[str]) -> list[str]:
    seen: set[str] = set()
    out:  list[str] = []
    for c in lst:
        key = c[:120].strip()
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out

_faq_chunks  = _dedup(_faq_chunks)
_data_chunks = _dedup(_data_chunks)
_body_chunks = _dedup(_body_chunks)

# Final pool: FAQ lines first, then data blocks, then scraped body
chunks:  list[str] = _faq_chunks + _data_chunks + _body_chunks
_n_faq   = len(_faq_chunks)
_n_short = len(_faq_chunks) + len(_data_chunks)   # FAQ + data share higher boost

logger.info("RAG: %d FAQ + %d data + %d body = %d total",
            len(_faq_chunks), len(_data_chunks), len(_body_chunks), len(chunks))

# ─────────────────────────────────────────────────────────────────────────────
# 4. BM25 index  — built on CLEANED text so [TOPIC:] metadata doesn't pollute
# ─────────────────────────────────────────────────────────────────────────────

_K1 = 1.5
_B  = 0.75
_FAQ_BOOST  = 2.2   # Short precise FAQ lines penalised by length-norm; restore balance
_DATA_BOOST = 1.6   # Structured ## data blocks (contact cards, fee tables)

def _tok(text: str) -> list[str]:
    return re.findall(r"\b[a-z0-9]{2,}\b", text.lower())

# Index on cleaned text (no [TOPIC:] noise)
_idx_toks: list[list[str]] = [_tok(_clean_markers(c)) for c in chunks]
_chunk_len: list[int]       = [len(t) for t in _idx_toks]
_N         = len(chunks)
_avgdl     = sum(_chunk_len) / max(_N, 1)

_df: dict[str, int] = Counter()
for _tl in _idx_toks:
    for _t in set(_tl):
        _df[_t] += 1

def _idf(term: str) -> float:
    df = _df.get(term, 0)
    return math.log((_N - df + 0.5) / (df + 0.5) + 1)

def _bm25(q_toks: list[str], i: int) -> float:
    tf_map = Counter(_idx_toks[i])
    dl     = _chunk_len[i]
    score  = 0.0
    for t in q_toks:
        tf = tf_map.get(t, 0)
        if tf == 0:
            continue
        score += _idf(t) * (tf * (_K1 + 1)) / (tf + _K1 * (1 - _B + _B * dl / _avgdl))
    if i < _n_faq:
        score *= _FAQ_BOOST
    elif i < _n_short:
        score *= _DATA_BOOST
    return score

# ─────────────────────────────────────────────────────────────────────────────
# 5. Query expansion
# ─────────────────────────────────────────────────────────────────────────────

_SYN: dict[str, list[str]] = {
    "fee":         ["fee", "fees", "charges", "cost", "tuition", "payment"],
    "hostel":      ["hostel", "dormitory", "boarding", "accommodation", "room"],
    "transport":   ["transport", "transportation", "bus", "pick", "drop", "route"],
    "contact":     ["contact", "phone", "email", "reach", "address", "number"],
    "apply":       ["apply", "application", "portal", "register", "admission"],
    "merit":       ["merit", "criteria", "calculation", "weightage", "aggregate"],
    "test":        ["test", "nat", "ecat", "nts", "hat", "entry", "exam"],
    "scholarship": ["scholarship", "financial", "aid", "waiver", "stipend", "fund"],
    "eligible":    ["eligible", "eligibility", "requirement", "qualify"],
    "deadline":    ["deadline", "last", "date", "closing", "schedule"],
    "program":     ["program", "programmes", "department", "course", "degree", "bs", "ms"],
    "structure":   ["structure", "breakdown", "detail", "total", "semester"],
    # People / roles
    "vc":          ["vc", "vice", "chancellor"],
    "dean":        ["dean", "head", "director"],
    "faculty":     ["faculty", "professor", "lecturer", "staff", "member", "teacher",
                    "assistant", "associate", "head", "department"],
    "hod":         ["hod", "head", "department", "chair"],
    "document":    ["document", "documents", "cnic", "certificate", "attested",
                    "required", "form", "domicile", "photo", "character"],
    "good":        ["good", "accredited", "recognized", "quality", "ranking",
                    "reputation", "hec", "pec", "washington", "nceac"],
    "university":  ["university", "institute", "ist", "accredited", "chartered"],
}

def _expand(query: str) -> list[str]:
    base  = _tok(query)
    extra: list[str] = []
    for t in base:
        for syns in _SYN.values():
            if t in syns:
                extra.extend(syns)
    return base + extra

# ─────────────────────────────────────────────────────────────────────────────
# 6. Retrieve
# ─────────────────────────────────────────────────────────────────────────────

TOP_K = 12

def retrieve(query: str) -> str:
    q_toks = _expand(query)
    if not q_toks:
        return "\n\n".join(chunks[:5])

    ranked = sorted(range(_N), key=lambda i: _bm25(q_toks, i), reverse=True)

    clean: list[str] = []
    seen_keys: set[str] = set()
    for i in ranked:
        if len(clean) >= TOP_K:
            break
        t = _clean_markers(chunks[i])
        if len(t) < 40:
            continue
        # Deduplicate: skip if same opening content already in results
        key = t[:80].strip()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        clean.append(t)

    return "\n\n".join(clean) if clean else "\n\n".join(chunks[:5])

# ─────────────────────────────────────────────────────────────────────────────
# 7. Intent helpers
# ─────────────────────────────────────────────────────────────────────────────

_END_CALL_RE = re.compile(
    r"\b(bye|goodbye|end call|end the call|that'?s all|nothing else|"
    r"no more questions|that will be all|khuda hafiz|allah hafiz)\b",
    re.I,
)

def _is_end_call(txt: str) -> bool:
    t = txt.strip()
    return bool(t) and bool(_END_CALL_RE.search(t))

def _is_thank_you(txt: str) -> bool:
    t = txt.lower().strip()
    return len(t) < 60 and any(x in t for x in ["thank you", "thanks", "thankyou", "shukriya", "shukria"])

# ─────────────────────────────────────────────────────────────────────────────
# 8. System prompt
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM = """You are the IST (Institute of Space Technology) admissions helpline assistant on a live phone call.

RULES:
1. Use the provided context as your PRIMARY source. Always prefer context over general knowledge.
2. Be concise and natural — this is a voice call. 2–4 sentences maximum.
3. No bullet points, numbered lists, markdown, or headers.
4. Never say "[TOPIC:]", "PAGE:", or any internal label.
5. Always include relevant contact details when present in context:
   - Transport questions → include the contact number 03000544707.
   - Fee questions → state the TOTAL per-semester figure and one-time charges.
   - Contact/personnel questions → give the phone number and/or email.
6. For fee questions: give the specific program's total per-semester amount; state the actual figure.
7. For faculty questions: list the faculty members found in context by name and designation.
8. INTELLIGENT FALLBACK — if the context does not contain the answer:
   - For yes/no questions (e.g. "Is IST good?", "Is IST better than NUST?"): answer yes or no using your general knowledge about IST, then add one sentence of context.
   - For factual questions you know the answer to: answer briefly and naturally.
   - Only say "I don't have that detail" for very specific data (exact dates, specific figures, names) that you genuinely don't know. In that case add: "You can contact IST admissions at 051-9075100 or email admissions@ist.edu.pk."
9. Speak naturally as if on a phone call. Do not start with "Based on the context" or similar."""

# ─────────────────────────────────────────────────────────────────────────────
# 9. Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def answer_question(question: str, history: list[dict] | None = None):
    """
    Returns (kind, reply_text).
    kind  = "__REPLY__" | "__END_CALL__"
    history = list of {"role": "user"|"assistant", "content": "..."} from this call.
    """
    q = question.strip()

    if _is_end_call(q):
        return ("__END_CALL__", "Thank you for calling Institute of Space Technology. Goodbye!")

    if _is_thank_you(q):
        return ("__REPLY__", "You're welcome. Is there anything else I can help you with?")

    context = retrieve(q)

    try:
        from groq_utils import get_client
        client = get_client()

        messages: list[dict] = [{"role": "system", "content": _SYSTEM}]
        if history:
            messages.extend(history[-6:])
        messages.append({
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {question}",
        })

        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            max_tokens=300,
            temperature=0.1,
        )
        reply = resp.choices[0].message.content.strip()
        reply = re.sub(r"\[TOPIC:[^\]]+\]\s*", "", reply).strip()
        reply = re.sub(r"(PAGE|TOPIC)\s*:\s*[^\n]*", "", reply).strip()
        if not reply:
            raise ValueError("empty reply")
        return ("__REPLY__", reply)

    except Exception as exc:
        logger.exception("LLM call failed: %s", exc)
        fallback = next(
            (l.strip() for l in context.splitlines() if len(l.strip()) > 60),
            "I'm sorry, I couldn't retrieve that. Please contact IST at 051-9075100.",
        )
        return ("__REPLY__", fallback)