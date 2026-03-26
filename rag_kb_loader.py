"""Build BM25 chunk index from all_kb.txt raw string. Used by rag.py and reload_kb()."""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_FAQ_END = "## PROGRAMS AND ADMISSIONS DATA"
_K1 = 1.5
_B = 0.75
_FAQ_BOOST = 2.2
_DATA_BOOST = 1.6


def _clean_markers(text: str) -> str:
    t = re.sub(r"\[TOPIC:[^\]]+\]\s*", "", text)
    t = re.sub(r"(PAGE|TOPIC)\s*:\s*[^\n]*\n?", "", t)
    return t.strip()


def _is_nav_block(text: str) -> bool:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return True
    nav_like = sum(1 for l in lines if len(l) < 55 and not any(c in l for c in ".?:,()@+"))
    return (nav_like / len(lines)) > 0.55


def _dedup(lst: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for c in lst:
        key = c[:120].strip()
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


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


def _tok(text: str) -> list[str]:
    return re.findall(r"\b[a-z0-9]{2,}\b", text.lower())


@dataclass
class KBIndex:
    raw: str
    faq_chunks: list[str]
    data_chunks: list[str]
    body_chunks: list[str]
    chunks: list[str]
    n_faq: int
    n_short: int
    idx_toks: list[list[str]]
    chunk_len: list[int]
    n: int
    avgdl: float
    df: Counter


def build_kb_index(raw: str) -> KBIndex:
    faq_chunks: list[str] = []
    data_chunks: list[str] = []
    _faq_raw = raw.split(_FAQ_END)[0]

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
                faq_chunks.append(s)

    _in_data_section = False
    _current_data_lines: list[str] = []
    current_dept_label = ""

    def flush_data_para(lines: list[str]) -> None:
        nonlocal current_dept_label
        para = "\n".join(lines).strip()
        if not para or len(para) < 50 or _is_nav_block(para):
            return
        dept_blocks = re.split(r"(?=^DEPARTMENT:)", para, flags=re.MULTILINE)
        for block in dept_blocks:
            b = block.strip()
            if not b or len(b) < 50 or _is_nav_block(b):
                continue
            dept_match = re.match(r"DEPARTMENT: (.+)", b)
            if dept_match:
                current_dept_label = b
            elif current_dept_label:
                b = f"{current_dept_label}\n---\n{b}"
            data_chunks.append(b)

    for _line in _faq_raw.splitlines():
        s = _line.strip()
        if re.match(r"^=== .+ ===$", s):
            if _current_data_lines:
                flush_data_para(_current_data_lines)
                _current_data_lines = []
            _in_data_section = False
            current_dept_label = ""
            continue
        if s.startswith("## "):
            if _current_data_lines:
                flush_data_para(_current_data_lines)
                _current_data_lines = []
            _in_data_section = True
            current_dept_label = ""
            continue
        if s.startswith("==="):
            if _in_data_section and _current_data_lines:
                flush_data_para(_current_data_lines)
                _current_data_lines = []
            current_dept_label = ""
            continue
        if _in_data_section:
            if s:
                _current_data_lines.append(s)
            else:
                if _current_data_lines:
                    flush_data_para(_current_data_lines)
                    _current_data_lines = []

    if _current_data_lines:
        flush_data_para(_current_data_lines)

    idx = raw.find(_FAQ_END)
    scraped_raw = raw[idx:] if idx >= 0 else ""
    body_chunks = _split_scraped(scraped_raw)

    faq_chunks = _dedup(faq_chunks)
    data_chunks = _dedup(data_chunks)
    body_chunks = _dedup(body_chunks)
    chunks = faq_chunks + data_chunks + body_chunks
    n_faq = len(faq_chunks)
    n_short = len(faq_chunks) + len(data_chunks)

    idx_toks = [_tok(_clean_markers(c)) for c in chunks]
    chunk_len = [len(t) for t in idx_toks]
    n = len(chunks)
    avgdl = sum(chunk_len) / max(n, 1)
    df: Counter = Counter()
    for tl in idx_toks:
        for t in set(tl):
            df[t] += 1

    logger.info(
        "RAG: %d FAQ + %d data + %d body = %d total",
        len(faq_chunks),
        len(data_chunks),
        len(body_chunks),
        len(chunks),
    )
    return KBIndex(
        raw=raw,
        faq_chunks=faq_chunks,
        data_chunks=data_chunks,
        body_chunks=body_chunks,
        chunks=chunks,
        n_faq=n_faq,
        n_short=n_short,
        idx_toks=idx_toks,
        chunk_len=chunk_len,
        n=n,
        avgdl=avgdl,
        df=df,
    )


def idf(term: str, n: int, df: Counter) -> float:
    dfc = df.get(term, 0)
    return math.log((n - dfc + 0.5) / (dfc + 0.5) + 1)


def bm25_score(
    q_toks: list[str],
    i: int,
    idx_toks: list[list[str]],
    chunk_len: list[int],
    n: int,
    avgdl: float,
    df: Counter,
    n_faq: int,
    n_short: int,
) -> float:
    tf_map = Counter(idx_toks[i])
    dl = chunk_len[i]
    score = 0.0
    for t in q_toks:
        tf = tf_map.get(t, 0)
        if tf == 0:
            continue
        idfv = idf(t, n, df)
        score += idfv * (tf * (_K1 + 1)) / (tf + _K1 * (1 - _B + _B * dl / avgdl))
    if i < n_faq:
        score *= _FAQ_BOOST
    elif i < n_short:
        score *= _DATA_BOOST
    return score
