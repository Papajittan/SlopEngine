"""Durable, conservative duplicate history for automatic topics.

The history lives beside the persistent jobs so reserving a topic and its job
can share one SQLite transaction.  Matching is intentionally local and
deterministic: normalized text, token reordering, and a small curated concept
map.  It is a safety filter, not a claim of general semantic understanding.
"""
from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path


STOP_WORDS = {
    "a", "an", "and", "are", "around", "as", "at", "be", "become", "by", "can", "could",
    "do", "does", "for", "from", "get", "how", "in", "into", "is", "it", "its", "of", "on",
    "or", "the", "their", "this", "to", "under", "what", "when", "where", "which", "why",
    "will", "with", "your", "you", "near", "nearby",
}
SYNONYMS = {
    "bends": "bend", "bent": "bend", "bending": "bend", "curves": "bend", "curved": "bend",
    "curving": "bend", "distort": "bend", "distorts": "bend", "distorted": "bend",
    "distorting": "bend", "warp": "bend", "warps": "bend", "warped": "bend", "warping": "bend",
    "refract": "bend", "refracts": "bend", "refracted": "bend",
    "refraction": "bend", "deflect": "bend", "deflects": "bend", "warps": "bend", "warped": "bend",
    "starlight": "light", "rays": "light", "ray": "light",
    "formed": "form", "forms": "form", "forming": "form", "formation": "form", "create": "form",
    "creates": "form", "created": "form", "creation": "form",
    "merged": "merge", "merges": "merge", "merging": "merge", "combine": "merge", "combines": "merge",
    "combined": "merge", "collide": "merge", "collides": "merge", "collision": "merge",
    "holes": "hole", "stars": "star", "galaxies": "galaxy", "planets": "planet",
    "boost": "improve", "boosts": "improve", "improved": "improve", "improves": "improve",
    "enhance": "improve", "enhances": "improve", "enhanced": "improve", "helps": "improve",
    "benefit": "improve", "benefits": "improve", "supports": "improve", "support": "improve",
    "recall": "memory", "remember": "memory", "remembers": "memory", "remembering": "memory",
    "retention": "memory", "memories": "memory", "sleeping": "sleep", "slumber": "sleep", "rest": "sleep",
    "oxidize": "rust", "oxidizes": "rust", "oxidized": "rust", "oxidation": "rust",
    "corrode": "rust", "corrodes": "rust", "corroded": "rust", "corrosion": "rust",
    "iron": "metal", "steel": "metal", "metals": "metal",
    "roughage": "fiber", "fibres": "fiber", "fibre": "fiber",
    "intestine": "digestion", "intestines": "digestion", "digestive": "digestion", "gut": "digestion",
    "glucose": "sugar", "sugars": "sugar", "sunlight": "sunlight", "solar": "sunlight",
    "salty": "sea", "saline": "sea", "salt": "sea", "ocean": "sea", "marine": "sea", "coastal": "sea",
    "air": "environment", "atmosphere": "environment", "ambient": "environment",
    "produce": "make", "produces": "make", "produced": "make",
}
PHRASE_SYNONYMS = {
    "turn sunlight into sugar": "photosynthesis",
    "make food from sunlight": "photosynthesis",
    "convert sunlight into sugar": "photosynthesis",
    "how plants make food": "photosynthesis",
}
INTENT_GROUPS = ({"form", "merge", "evaporate", "destroy", "grow", "shrink"},)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(re.findall(r"[\w]+", value, flags=re.UNICODE))


def tokens(value: str) -> list[str]:
    return [token for token in normalize_text(value).split() if token not in STOP_WORDS]


def concepts(value: str) -> list[str]:
    normalized = normalize_text(value)
    for phrase, replacement in PHRASE_SYNONYMS.items():
        normalized = normalized.replace(phrase, replacement)
    return [SYNONYMS.get(token, token) for token in tokens(normalized)]


def fingerprint(title: str, angle: str = "") -> str:
    text = normalize_text(title) + "\n" + normalize_text(angle)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compare_topics(title: str, angle: str, other_title: str, other_angle: str = "", *, threshold: float = 0.9) -> tuple[bool, float, str]:
    """Return duplicate decision, score, and matching layer."""
    left = normalize_text(title)
    right = normalize_text(other_title)
    if left and left == right:
        return True, 1.0, "exact"
    if not left or not right:
        return False, 0.0, "none"
    left_concepts, right_concepts = concepts(f"{title} {angle}"), concepts(f"{other_title} {other_angle}")
    a, b = set(left_concepts), set(right_concepts)
    # Scientific actions are meaningful distinctions; protect them before
    # lexical similarity can classify their shared nouns as a duplicate.
    for group in INTENT_GROUPS:
        left_intents, right_intents = a & group, b & group
        if left_intents and right_intents and left_intents != right_intents:
            return False, 0.0, "distinct-intent"
    sequence = SequenceMatcher(None, left, right, autojunk=False).ratio()
    left_tokens, right_tokens = tokens(title), tokens(other_title)
    sort_ratio = SequenceMatcher(None, " ".join(sorted(left_tokens)), " ".join(sorted(right_tokens)), autojunk=False).ratio()
    score = max(sequence, sort_ratio)
    if score >= threshold:
        return True, score, "similarity"

    if not a or not b:
        return False, score, "none"
    # Distinct scientific actions such as black-hole formation and merger must
    # remain distinct even when the rest of their vocabulary overlaps.
    for group in INTENT_GROUPS:
        left_intents, right_intents = a & group, b & group
        if left_intents and right_intents and left_intents != right_intents:
            return False, score, "distinct-intent"
    title_a, title_b = set(concepts(title)), set(concepts(other_title))
    overlaps = [len(left & right) / max(1, len(left | right))
                for left, right in ((a, b), (title_a, title_b))]
    overlap = max(overlaps)
    score = max(score, overlap)
    minimum_shared = 2 if min(len(a), len(b), len(title_a), len(title_b)) < 5 else 3
    shared = max(len(a & b), len(title_a & title_b))
    if overlap >= 0.75 and shared >= minimum_shared:
        return True, overlap, "semantic"
    return False, score, "none"


class TopicHistory:
    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS auto_topics (
                id TEXT PRIMARY KEY, session_id TEXT, niche TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL, proposed_title TEXT NOT NULL DEFAULT '', angle TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL,
                status TEXT NOT NULL, source TEXT NOT NULL, job_id TEXT, output TEXT,
                matched_id TEXT, similarity REAL, match_layer TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '', accounted INTEGER NOT NULL DEFAULT 0,
                created TEXT NOT NULL, updated TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS auto_topics_fingerprint ON auto_topics(fingerprint);
            CREATE INDEX IF NOT EXISTS auto_topics_session ON auto_topics(session_id,status);
            CREATE TABLE IF NOT EXISTS auto_history_imports (
                output TEXT PRIMARY KEY, imported TEXT NOT NULL
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(auto_topics)")}
        if "proposed_title" not in columns:
            self.db.execute("ALTER TABLE auto_topics ADD COLUMN proposed_title TEXT NOT NULL DEFAULT ''")
            self.db.execute("UPDATE auto_topics SET proposed_title=title WHERE proposed_title=''")
        if "accounted" not in columns:
            self.db.execute("ALTER TABLE auto_topics ADD COLUMN accounted INTEGER NOT NULL DEFAULT 0")

    def find_duplicate(self, title: str, angle: str = "", *, threshold: float = 0.9, exclude_id: str | None = None):
        rows = self.db.execute(
            "SELECT * FROM auto_topics WHERE status != 'REJECTED' OR (source='auto' AND job_id IS NOT NULL) ORDER BY created DESC"
        ).fetchall()
        best = None
        for row in rows:
            if row["id"] == exclude_id:
                continue
            matches = [compare_topics(title, angle, row["title"], row["angle"], threshold=threshold)]
            proposed = row["proposed_title"] if "proposed_title" in row.keys() else row["title"]
            if proposed and proposed != row["title"]:
                matches.append(compare_topics(title, angle, proposed, row["angle"], threshold=threshold))
            duplicate, score, layer = max(matches, key=lambda result: result[1])
            if duplicate and (best is None or score > best["similarity"]):
                best = {**dict(row), "similarity": score, "match_layer": layer}
                if score >= 1:
                    break
        return best

    def prompt_context(self, limit: int = 40) -> dict[str, list[str]]:
        rows = self.db.execute("SELECT title,proposed_title,angle,status,source FROM auto_topics ORDER BY updated DESC LIMIT ?",
                               (limit * 4,)).fetchall()
        accepted, rejected, completed = [], [], []
        for row in rows:
            title = row["title"] or row["proposed_title"]
            item = f"{title} — {row['angle']}" if row["angle"] else title
            if row["status"] == "REJECTED":
                rejected.append(title)
            else:
                accepted.append(item)
                if row["status"] == "COMPLETED":
                    completed.append(item)
        return {"accepted": accepted[:limit], "rejected": rejected[:limit], "completed": completed[:limit]}

    def record_rejection(self, *, topic_id: str, session_id: str, niche: str, title: str,
                         angle: str, matched: dict, timestamp: str | None = None) -> None:
        now = timestamp or datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO auto_topics (id,session_id,niche,title,proposed_title,angle,fingerprint,status,source,matched_id,similarity,match_layer,outcome,created,updated) "
            "VALUES (?,?,?,?,?,?,?,'REJECTED','auto',?,?,?,?,?,?)",
            (topic_id, session_id, niche, title, title, angle, fingerprint(title, angle), matched["id"],
             matched["similarity"], matched["match_layer"], "duplicate", now, now),
        )

    def record_manual(self, topic: str, job_id: str, *, niche: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        text = str(topic).strip()
        if not text:
            return
        self.db.execute(
            "INSERT OR IGNORE INTO auto_topics (id,session_id,niche,title,proposed_title,angle,fingerprint,status,source,job_id,outcome,created,updated) "
            "VALUES (?,NULL,?,?,?,'',?,'QUEUED','manual',?,'observed',?,?)",
            ("manual:" + job_id, niche, text, text, fingerprint(text), job_id, now, now),
        )

    def import_outputs(self, directory: Path | str) -> int:
        """Import old metadata once, without touching any output files."""
        import json as json_module
        directory = Path(directory)
        imported = 0
        has_jobs_table = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone() is not None
        for path in directory.glob("*.metadata.json") if directory.is_dir() else ():
            output = str(path.with_name(path.name.removesuffix(".metadata.json") + ".mp4").resolve())
            if self.db.execute("SELECT 1 FROM auto_history_imports WHERE output=?", (output,)).fetchone():
                continue
            tracked_job = has_jobs_table and self.db.execute("SELECT 1 FROM jobs WHERE output=? LIMIT 1", (output,)).fetchone()
            if self.db.execute("SELECT 1 FROM auto_topics WHERE output=? LIMIT 1", (output,)).fetchone() or tracked_job:
                self.db.execute("INSERT OR IGNORE INTO auto_history_imports VALUES (?,?)",
                                (output, datetime.now(timezone.utc).isoformat()))
                continue
            try:
                metadata = json_module.loads(path.read_text(encoding="utf-8"))
                if not isinstance(metadata, dict):
                    continue
                payload = metadata.get("script_payload") or {}
                if not isinstance(payload, dict):
                    payload = {}
                title = str(payload.get("title") or metadata.get("topic") or "").strip()
                topic = str(metadata.get("topic") or title).strip()
                if not title:
                    continue
                now = datetime.now(timezone.utc).isoformat()
                output_id = "output:" + hashlib.sha256(output.encode()).hexdigest()
                self.db.execute(
                    "INSERT OR IGNORE INTO auto_topics (id,session_id,niche,title,proposed_title,angle,fingerprint,status,source,output,outcome,created,updated) "
                    "VALUES (?,NULL,'',?,?,?,?,'COMPLETED','output',?,'imported',?,?)",
                    (output_id, title, title, topic, fingerprint(title, topic), output, now, now),
                )
                self.db.execute("INSERT OR IGNORE INTO auto_history_imports VALUES (?,?)", (output, now))
                imported += 1
            except (OSError, ValueError, TypeError):
                continue
        return imported

    def update_job(self, job_id: str, status: str, output: str | None = None, *, title: str | None = None,
                   outcome: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        row = self.db.execute("SELECT * FROM auto_topics WHERE job_id=? ORDER BY created DESC LIMIT 1", (job_id,)).fetchone()
        if not row:
            return
        final_title = (title or row["title"]).strip()
        matched = self.find_duplicate(final_title, row["angle"], exclude_id=row["id"])
        outcome_value = row["outcome"] if row["status"] == "REJECTED" else (outcome or row["outcome"])
        if matched:
            outcome_value = (outcome_value + "; " if outcome_value else "") + f"final title overlaps {matched['id']} ({matched['match_layer']})"
        stored_status = "REJECTED" if row["status"] == "REJECTED" else status
        self.db.execute(
            "UPDATE auto_topics SET title=?,fingerprint=?,status=?,output=COALESCE(?,output),outcome=?,updated=? WHERE id=?",
            (final_title, fingerprint(final_title, row["angle"]), stored_status, output, outcome_value, now, row["id"]),
        )

    def accept_final_title(self, topic_id: str, final_title: str) -> None:
        row = self.db.execute("SELECT * FROM auto_topics WHERE id=?", (topic_id,)).fetchone()
        if not row:
            return
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute("UPDATE auto_topics SET title=?,fingerprint=?,outcome='script title checked',updated=? WHERE id=?",
                        (final_title, fingerprint(final_title, row["angle"]), now, topic_id))

    def reject_final_title(self, topic_id: str, final_title: str, matched: dict) -> None:
        row = self.db.execute("SELECT * FROM auto_topics WHERE id=?", (topic_id,)).fetchone()
        if not row:
            return
        now = datetime.now(timezone.utc).isoformat()
        outcome = f"script title duplicate of {matched['id']} ({matched['match_layer']})"
        self.db.execute(
            "UPDATE auto_topics SET title=?,fingerprint=?,status='REJECTED',matched_id=?,similarity=?,match_layer=?,outcome=?,updated=? WHERE id=?",
            (final_title, fingerprint(final_title, row["angle"]), matched["id"], matched["similarity"],
             matched["match_layer"], outcome, now, topic_id),
        )
