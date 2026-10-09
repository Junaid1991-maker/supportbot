"""NODE 3 - RAG Retrieval (keyword search, no ChromaDB - same approach as TradeBot/DPPBot fix).

Reads knowledge_base/*.txt (Q: / K: / A: entries), scores every entry against the buyer's
message and returns the top 3 with a relevance score between 0 and 1.

Score = 0.50 x phrase score   (how many of the entry's keyword phrases appear in the message;
                               multi-word phrases count more than single words)
      + 0.30 x overlap        (share of the message explained by the entry, or of the FAQ question found in the message)
      + 0.20 x category match (entry file matches the Node 1 query type)

Entries from the matching category file get searched first-class; others still compete,
so a mis-classified message can still find the right answer (with a lower score).
"""
import re
from functools import lru_cache
from pathlib import Path

from nodes.state import SupportState

KB_DIR = Path(__file__).resolve().parent.parent / "knowledge_base"

# Node 1 query type -> knowledge base file
CATEGORY_FILE = {
    "SHIPMENT": "shipment",
    "INVOICE": "invoice",
    "CERTIFICATE": "certificates",
    "COMPLAINT": "quality",
    "CHANGE": "general",
    "GENERAL": "general",
}

W_PHRASE, W_COVERAGE, W_CATEGORY = 0.50, 0.30, 0.20
SINGLE_WORD_WEIGHT, MULTI_WORD_WEIGHT = 0.6, 0.8

STOPWORDS = set("""
a an the is are was were be been am i me my we our you your he she it its they them their this that these
those of to in on at for from by with and or but if so as do does did done have has had can could would
should will shall may might must please pls plz kindly hi hello dear sir madam bhai sahab ji thanks thank
yet still just also any some what when where which who why how there here about need want get send know
hai hain ka ki ke ko se mein mai main ap aap kya kab kar karo kr de dein do yeh ye wo woh tha thi
""".split())


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", (text or "").lower())
    return {_stem(w) for w in words if w not in STOPWORDS and len(w) > 1}


def _norm(text: str) -> str:
    """Lowercase, unify punctuation/spaces so phrase matching is forgiving ('oeko-tex' == 'oeko tex')."""
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip() + " "


@lru_cache(maxsize=1)
def load_kb() -> tuple[dict, ...]:
    entries = []
    for path in sorted(KB_DIR.glob("*.txt")):
        category = path.stem
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.startswith("#")]
        for block in re.split(r"\n\s*\n", "\n".join(lines)):
            q = re.search(r"^Q:\s*(.+)$", block, re.M)
            a = re.search(r"^A:\s*(.+(?:\n(?![QK]:).+)*)", block, re.M)
            if not (q and a):
                continue
            k = re.search(r"^K:\s*(.+)$", block, re.M)
            question = q.group(1).strip()
            # the FAQ question itself also counts as a keyword phrase
            keywords = [p.strip() for p in (k.group(1).split(",") if k else []) if p.strip()] + [question]
            entries.append({
                "category": category,
                "question": question,
                "answer": " ".join(a.group(1).split()),
                "phrases": [_norm(p).strip() for p in keywords],
                "phrase_tokens": [tokens(p) for p in keywords],
                "q_tokens": tokens(question),
                "vocab": tokens(question + " " + " ".join(keywords)),
            })
    return tuple(entries)


def score_entry(entry: dict, text: str, query_type: str) -> float:
    return _score(entry, text, query_type)[0]


def _score(entry: dict, text: str, query_type: str) -> tuple[float, float]:
    """Returns (score 0-1, tie-break strength). Strength = uncapped keyword weight + overlap,
    so when two entries both hit the 1.0 cap, the one matching MORE keywords wins."""
    norm_text = _norm(text)
    q_tokens = tokens(text)

    # A keyword phrase matches if it appears verbatim, OR all its meaningful words appear
    # anywhere in the message ("change order" matches "change my order").
    phrase = 0.0
    for p, p_tokens in zip(entry["phrases"], entry["phrase_tokens"]):
        if not p:
            continue
        if f" {p} " in norm_text or (p_tokens and p_tokens <= q_tokens):
            phrase += MULTI_WORD_WEIGHT if len(p_tokens) > 1 else SINGLE_WORD_WEIGHT
    raw_phrase = phrase
    phrase = min(phrase, 1.0)

    # Overlap: best of "how much of the message the entry explains" and
    # "how much of the FAQ question the message contains".
    coverage = len(q_tokens & entry["vocab"]) / len(q_tokens) if q_tokens else 0.0
    q_sim = len(q_tokens & entry["q_tokens"]) / len(entry["q_tokens"]) if entry["q_tokens"] else 0.0
    overlap = max(coverage, q_sim)

    category = 1.0 if CATEGORY_FILE.get(query_type) == entry["category"] else 0.0

    score = round(W_PHRASE * phrase + W_COVERAGE * overlap + W_CATEGORY * category, 3)
    return score, raw_phrase + overlap


def retrieve(text: str, query_type: str, k: int = 3) -> list[dict]:
    scored = []
    for e in load_kb():
        score, strength = _score(e, text, query_type)
        scored.append(({"category": e["category"], "question": e["question"],
                        "answer": e["answer"], "score": score}, strength))
    scored.sort(key=lambda ms: (ms[0]["score"], ms[1]), reverse=True)
    return [m for m, _ in scored[:k] if m["score"] > 0]


# ---------- LangGraph node ----------
def rag_retrieval(state: SupportState) -> SupportState:
    matches = retrieve(state.get("text", ""), state.get("query_type", "GENERAL"))
    top = matches[0] if matches else None
    return {
        "matches": matches,
        "top_match": top["answer"] if top else "",
        "relevance_score": top["score"] if top else 0.0,
    }
