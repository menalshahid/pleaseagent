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
import logging

from rag_kb_loader import build_kb_index, bm25_score as _bm25_loader

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 1. KB index (rebuilt by reload_kb() after ist_kb_sync updates all_kb.txt)
# ─────────────────────────────────────────────────────────────────────────────

_RAW: str = ""
_faq_chunks: list[str] = []
_data_chunks: list[str] = []
_body_chunks: list[str] = []
chunks: list[str] = []
_n_faq: int = 0
_n_short: int = 0
_idx_toks: list[list[str]] = []
_chunk_len: list[int] = []
_N: int = 0
_avgdl: float = 0.0
_df = None  # Counter


def _apply_kb_index(idx) -> None:
    global _RAW, _faq_chunks, _data_chunks, _body_chunks, chunks, _n_faq, _n_short
    global _idx_toks, _chunk_len, _N, _avgdl, _df
    _RAW = idx.raw
    _faq_chunks = idx.faq_chunks
    _data_chunks = idx.data_chunks
    _body_chunks = idx.body_chunks
    chunks = idx.chunks
    _n_faq = idx.n_faq
    _n_short = idx.n_short
    _idx_toks = idx.idx_toks
    _chunk_len = idx.chunk_len
    _N = idx.n
    _avgdl = idx.avgdl
    _df = idx.df


def reload_kb(path: str = "all_kb.txt") -> None:
    """Re-read all_kb.txt and rebuild BM25 index (after ist_kb_sync)."""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    _apply_kb_index(build_kb_index(raw))
    logger.info("KB reloaded from %s", path)


_apply_kb_index(build_kb_index(open("all_kb.txt", encoding="utf-8").read()))

# ─────────────────────────────────────────────────────────────────────────────
# 2. Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean_markers(text: str) -> str:
    """Strip [TOPIC:…] and PAGE/TOPIC header lines — used for both display and indexing."""
    t = re.sub(r"\[TOPIC:[^\]]+\]\s*", "", text)
    t = re.sub(r"(PAGE|TOPIC)\s*:\s*[^\n]*\n?", "", t)
    return t.strip()

def _tok(text: str) -> list[str]:
    return re.findall(r"\b[a-z0-9]{2,}\b", text.lower())


def _bm25(q_toks: list[str], i: int) -> float:
    return _bm25_loader(q_toks, i, _idx_toks, _chunk_len, _N, _avgdl, _df, _n_faq, _n_short)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Query expansion
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
    "karachi":     ["karachi", "kicsit", "kahuta", "campus", "director", "krl"],
    "kicsit":      ["kicsit", "kahuta", "karachi", "director", "campus", "incharge"],
}

_QUERY_EXPANSION_HINTS: tuple[tuple[str, list[str]], ...] = (
    # Fee/charges (English + Urdu / Roman Urdu)
    ("fee structure", ["fee", "structure", "semester", "tuition", "charges"]),
    ("fees", ["fee", "semester", "tuition", "charges"]),
    ("fee", ["fee", "semester", "tuition", "charges"]),
    ("semester", ["semester", "fee", "total"]),
    ("tuition", ["tuition", "fee", "semester"]),
    ("فیس", ["fee", "fees", "semester", "tuition", "charges"]),
    ("چارج", ["charges", "fee", "one-time"]),
    ("سمسٹر", ["semester", "fee", "total"]),
    ("سیمیستر", ["semester", "fee", "total"]),
    ("الیکٹریکل", ["electrical", "engineering", "fee", "semester"]),
    ("انجینئر", ["engineering", "program", "fee"]),
    ("کمپیوٹر", ["computer", "computing", "program", "fee"]),
)

def _expand(query: str) -> list[str]:
    base  = _tok(query)
    extra: list[str] = []
    ql = query.lower()
    for t in base:
        for syns in _SYN.values():
            if t in syns:
                extra.extend(syns)
    for needle, hinted in _QUERY_EXPANSION_HINTS:
        if needle in ql:
            extra.extend(hinted)
    # Keep order stable while deduplicating.
    seen: set[str] = set()
    out: list[str] = []
    for tok in (base + extra):
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out

# ─────────────────────────────────────────────────────────────────────────────
# 6. Retrieve
# ─────────────────────────────────────────────────────────────────────────────

TOP_K = 10

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
    r"no more questions|that will be all|khuda hafiz|allah hafiz|"
    r"خدا حافظ|اللہ حافظ|شکریہ بائے)\b",
    re.I,
)

def _is_end_call(txt: str) -> bool:
    t = txt.strip()
    return bool(t) and bool(_END_CALL_RE.search(t))

def _is_thank_you(txt: str) -> bool:
    t = txt.lower().strip()
    return len(t) < 60 and any(x in t for x in [
        "thank you", "thanks", "thankyou",
        "shukriya", "shukria", "شکریہ", "بہت شکریہ",
    ])

# ─────────────────────────────────────────────────────────────────────────────
# 8. System prompts  (English + Urdu)
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_EN = """You are the IST (Institute of Space Technology) admissions helpline assistant on a live phone call.

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
   - For yes/no questions: answer yes or no using your general knowledge about IST.
   - For factual questions you know: answer briefly and naturally.
   - Only say "I don't have that detail" for very specific data you genuinely don't know. Then add: "You can contact IST admissions at 051-9075100 or email admissions@ist.edu.pk."
9. Speak naturally as if on a phone call. Do not start with "Based on the context".
10. Never begin with meta phrases like "The answer to your question is" or "The answer is that" — start directly with the information.
11. MERIT SCHOLARSHIPS — use ONLY figures and rules from context. Awards are rank-based (e.g. top three per BS discipline, top two per MS program in context): explain that eligible students must meet published minimum SGPA/CGPA AND compete for limited positions. If the caller gives a GPA and context gives a minimum threshold, compare correctly: e.g. 4.0 is above 3.75 — do NOT say they fail eligibility. Never invent GPA cutoffs not in context. Never tell someone they "cannot" get a merit scholarship just because you mis-compare numbers; say they meet the stated minimum if they do, and that final awards depend on semester ranking among eligible students.
12. KICSIT — context says KICSIT is at Kahuta (Rawalpindi area), not Karachi city. Director Incharge named in context is Engr. Masood Khalid. If the user says "Karachi campus" for KICSIT, politely clarify location and give the director from context."""

_SYSTEM_UR = """آپ IST (Institute of Space Technology) کے admissions helpline assistant ہیں اور ایک live phone call پر ہیں۔

اہم ہدایات:
1. جواب دینے کے لیے فراہم کردہ context کو بنیاد بنائیں۔
2. ہمیشہ صاف اور قدرتی اردو میں جواب دیں — یہ voice call ہے، اس لیے 2 سے 4 جملے کافی ہیں۔
3. Bullet points، numbered lists، یا markdown استعمال نہ کریں۔
4. "[TOPIC:]" یا "PAGE:" جیسے internal labels کبھی نہ بولیں۔
5. Technical terms جیسے BS، MS، NAT، ECAT، fee structure، merit list، GPA وغیرہ انگریزی میں رکھیں — باقی سب اردو میں بولیں۔
6. اگر context میں contact details موجود ہوں تو ضرور بتائیں:
   - Transport سوالات → 03000544707 نمبر بتائیں۔
   - Fee سوالات → کل per-semester رقم اور one-time charges بتائیں۔
   - Personnel سوالات → phone اور email بتائیں۔
7. اگر context میں جواب نہ ہو تو کہیں: "مجھے ابھی یہ تفصیل نہیں ملی۔ براہ کرم IST admissions سے 051-9075100 پر رابطہ کریں یا admissions@ist.edu.pk پر email کریں۔"
8. قدرتی انداز میں بات کریں جیسے phone پر کرتے ہیں۔ "Based on the context" جیسے جملوں سے گریز کریں۔
9. جواب براہ راست شروع کریں — کبھی بھی "آپ کی پوچھ گئی بات کا جواب یہ ہے کہ"، "جواب یہ ہے کہ"، "آپ کے سوال کا جواب یہ ہے کہ" جیسے filler جملے نہ بولیں۔
10. MERIT SCHOLARSHIP — context میں جو minimum SGPA/CGPA لکھا ہے اسی کو استعمال کریں۔ اگر caller کا GPA minimum سے زیادہ یا برابر ہے تو کہیں کہ وہ minimum پورا کرتا ہے؛ غلطی سے یہ نہ کہیں کہ وہ اہل نہیں۔ merit scholarship محدود اوپر کی positions پر rank/position کی بنیاد پر ملتی ہے (فی discipline اوپر کے طلباء) — ہر eligible شخص کو خودکار طور پر نہیں ملتی۔
11. KICSIT — context کے مطابق KICSIT کا campus Kahuta پر ہے، Karachi شہر میں نہیں۔ Director Incharge: Engr. Masood Khalid (نام context سے)۔ اگر user "Karachi campus" کہے تو نرمی سے وضاحت کریں اور director بتائیں۔"""

# Remove LLM filler intros if they still appear (Roman Urdu / spelling variants)
_UR_META_PREFIX = re.compile(
    r"^\s*(آپ کی پوچھ[ئیے]\s*گئ[ئیے]\s*بات کا جواب یہ ہے کہ\s*|"
    r"آپ کے سوال کا جواب یہ ہے کہ\s*|"
    r"آپ کے سوال کا مختصر جواب یہ ہے کہ\s*|"
    r"جواب یہ ہے کہ\s*)",
)
_EN_META_PREFIX = re.compile(
    r"^\s*(The answer to your question is that\s*|The answer to your question is\s*|The answer is that\s*)",
    re.I,
)


def _strip_voice_meta_filler(text: str, language: str) -> str:
    t = (text or "").strip()
    if not t:
        return t
    if language == "ur":
        t = _UR_META_PREFIX.sub("", t).strip()
    else:
        t = _EN_META_PREFIX.sub("", t).strip()
    return t


# ─────────────────────────────────────────────────────────────────────────────
# 9. Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def answer_question(
    question: str,
    history:  list[dict] | None = None,
    language: str = "en",
) -> tuple[str, str]:
    """
    Returns (kind, reply_text).
    kind     = "__REPLY__" | "__END_CALL__"
    history  = list of {"role": "user"|"assistant", "content": "..."} for this call.
    language = "en" or "ur"
    """
    q = question.strip()

    if _is_end_call(q):
        msg = (
            "شکریہ! IST admissions helpline استعمال کرنے کا۔ خدا حافظ!"
            if language == "ur"
            else "Thank you for calling Institute of Space Technology. Goodbye!"
        )
        return ("__END_CALL__", msg)

    if _is_thank_you(q):
        msg = (
            "آپ کا شکریہ! کیا کوئی اور سوال ہے؟"
            if language == "ur"
            else "You're welcome. Is there anything else I can help you with?"
        )
        return ("__REPLY__", msg)

    context = retrieve(q)
    system  = _SYSTEM_UR if language == "ur" else _SYSTEM_EN

    try:
        from groq_utils import get_client
        client = get_client()

        messages: list[dict] = [{"role": "system", "content": system}]
        if history:
            messages.extend(history[-6:])
        messages.append({
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {question}",
        })

        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            max_tokens=220,
            temperature=0.1,
        )
        reply = resp.choices[0].message.content.strip()
        reply = re.sub(r"\[TOPIC:[^\]]+\]\s*", "", reply).strip()
        reply = re.sub(r"(PAGE|TOPIC)\s*:\s*[^\n]*", "", reply).strip()
        reply = _strip_voice_meta_filler(reply, language)
        if not reply:
            raise ValueError("empty reply")
        return ("__REPLY__", reply)

    except Exception as exc:
        logger.exception("LLM call failed: %s", exc)
        fallback = next(
            (l.strip() for l in context.splitlines() if len(l.strip()) > 60),
            (
                "معاف کیجیے، معلومات نہیں مل سکی۔ براہ کرم 051-9075100 پر رابطہ کریں۔"
                if language == "ur"
                else "I'm sorry, I couldn't retrieve that. Please contact IST at 051-9075100."
            ),
        )
        return ("__REPLY__", fallback)
