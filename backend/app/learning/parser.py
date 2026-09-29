"""Feedback parser: free-text user feedback -> rating + normalized lesson candidates.

Uses the fast model through OmniRoute when available, with a deterministic rule-based parser as
fallback (and as a sanity check)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.enums import FeedbackRating, LearningCategory
from app.integrations.omniroute.client import ModelClient

_POS = re.compile(r"\b(worked|works|great|correct|perfect|good job|helpful|solved|fixed|thanks|thank you|"
                  r"exactly|nice|well done|accurate|useful)\b", re.I)
_NEG = re.compile(r"\b(incorrect|wrong|failed|fail|didn'?t work|does not work|doesn'?t work|not work|bad|useless|"
                  r"broke|broken|mistake|inaccurate|missed|should have|should not have|shouldn'?t have)\b", re.I)
_IMPERATIVE = re.compile(r"^(before|always|never|do not|don'?t|next time|first|make sure|remember|prefer|use|check|"
                         r"avoid|instead|verify|ensure|when|if|only|stop|start|run|look|confirm|in future|going forward)\b",
                         re.I)
_GUIDANCE_ANYWHERE = re.compile(r"\b(next time|in the future|in future|going forward|from now on|always|never|"
                                r"before (restarting|deleting|changing|deploying|doing|running|you))\b", re.I)
_ANTI = re.compile(r"^(never|do not|don'?t|avoid|stop)\b|\bdo not use\b|\bdon'?t use\b", re.I)
_PROC = re.compile(r"\b(before|first|then|after|check|verify|run|confirm|look at|inspect|review|ensure|step)\b", re.I)
_PREF = re.compile(r"\b(prefer|i like|i want|use .+ instead|format|language|style|shorter|longer|tone)\b", re.I)
_FACT = re.compile(r"\b(is|are|runs on|lives in|located|our|we use|the .+ (?:is|are))\b", re.I)
_HEDGE = re.compile(r"\b(maybe|perhaps|might|possibly|not sure|i guess)\b", re.I)
_LEAD = re.compile(r"^(next time|in the future|in future|going forward|from now on|please|also|and)[,:]?\s*", re.I)
_DOMAIN = re.compile(r"\b(aws|ec2|rds|s3|eks|cloudwatch|lambda|alarm|github|pr|pull request|test|tests|deploy|"
                     r"restart|database|logs|metrics|cost|kubernetes|docker|python|api|report|email)\b", re.I)
_SENSITIVE = re.compile(r"\b(source code|codebase|security|approval|approvals|permission|credential|password|secret|"
                        r"instructions|system prompt|schema|migration|drop table|admin|disable|bypass|skip review)\b",
                        re.I)


@dataclass
class ParsedLesson:
    text: str
    category: LearningCategory
    confidence: float
    domain_tags: list[str] = field(default_factory=list)
    sensitive: bool = False


@dataclass
class ParsedFeedback:
    rating: FeedbackRating
    lessons: list[ParsedLesson]
    parser: str


def detect_rating(text: str) -> FeedbackRating:
    pos, neg = len(_POS.findall(text)), len(_NEG.findall(text))
    if neg > pos:
        return FeedbackRating.NEGATIVE
    if pos > neg:
        return FeedbackRating.POSITIVE
    return FeedbackRating.NEUTRAL


def _normalize(sentence: str) -> str:
    s = _LEAD.sub("", sentence.strip()).strip(" -–—")
    s = s[0].upper() + s[1:] if s else s
    return s if s.endswith((".", "!", "?")) else s + "."


def _categorize(s: str) -> LearningCategory:
    if _ANTI.search(s):
        return LearningCategory.ANTI_PATTERN
    if _PROC.search(s):
        return LearningCategory.PROCEDURE
    if _PREF.search(s):
        return LearningCategory.PREFERENCE
    if _FACT.search(s):
        return LearningCategory.FACT
    return LearningCategory.PROCEDURE


def rule_based_parse(text: str, rating: FeedbackRating | None = None) -> ParsedFeedback:
    rating = rating or detect_rating(text)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?;])\s+|\n+", text) if s.strip()]
    lessons: list[ParsedLesson] = []
    for s in sentences:
        core = _LEAD.sub("", s)
        if not (_IMPERATIVE.search(core) or _GUIDANCE_ANYWHERE.search(s)):
            continue
        if len(core) < 12:
            continue
        cat = _categorize(core)
        conf = 0.6
        if _IMPERATIVE.search(core):
            conf += 0.12
        domains = sorted({d.lower() for d in _DOMAIN.findall(s)})
        if domains:
            conf += 0.08
        if _HEDGE.search(s):
            conf -= 0.2
        lessons.append(ParsedLesson(text=_normalize(s), category=cat, confidence=round(max(0.1, min(conf, 0.95)), 2),
                                    domain_tags=domains, sensitive=bool(_SENSITIVE.search(s))))
    return ParsedFeedback(rating=rating, lessons=lessons, parser="rule-based")


LLM_PROMPT = """Extract reusable lessons from user feedback about an AI agent's completed task.
Only extract guidance that should change FUTURE behaviour (procedures, anti-patterns, preferences, facts).
Pure praise/complaints produce no lessons. Reply ONLY JSON:
{"rating": "POSITIVE"|"NEGATIVE"|"NEUTRAL", "lessons": [{"text": "imperative, self-contained lesson",
"category": "PROCEDURE"|"ANTI_PATTERN"|"PREFERENCE"|"FACT", "confidence": 0..1, "domain_tags": [str]}]}"""


async def parse_feedback(text: str, rating: FeedbackRating | None, task_request: str,
                         model: ModelClient | None) -> ParsedFeedback:
    base = rule_based_parse(text, rating)
    if model is None or not model.configured:
        return base
    data, resp = await model.chat_json([
        {"role": "system", "content": LLM_PROMPT},
        {"role": "user", "content": f"Task request: {task_request[:1500]}\nFeedback: {text[:3000]}"}], tier="fast")
    if not isinstance(data, dict) or resp.offline:
        return base
    lessons = []
    for item in data.get("lessons") or []:
        try:
            cat = LearningCategory(str(item.get("category", "PROCEDURE")).upper())
            t = str(item["text"]).strip()
        except (KeyError, ValueError):
            continue
        if len(t) < 12:
            continue
        lessons.append(ParsedLesson(text=_normalize(t), category=cat,
                                    confidence=round(max(0.1, min(float(item.get("confidence", 0.7)), 0.95)), 2),
                                    domain_tags=[str(x).lower() for x in item.get("domain_tags") or []][:8],
                                    sensitive=bool(_SENSITIVE.search(t))))
    try:
        llm_rating = FeedbackRating(str(data.get("rating", "")).upper())
    except ValueError:
        llm_rating = base.rating
    return ParsedFeedback(rating=rating or llm_rating, lessons=lessons or base.lessons, parser="llm")
