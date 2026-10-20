"""The built-in Yuanbao sticker catalogue and its search (P16-21, P16-22).

A Yuanbao sticker travels as a ``TIMFaceElem`` whose data names the sticker by its id in the
vendor's sticker pack (package 1003, 128×128 PNG); the receiving app draws it. The ids and names
below are the vendor's (interop facts); the English keywords are ours, so the model can find a
sticker in either language.

Search ranks each sticker by its best signal: the exact name or id, the name starting with or
containing the query (or the query containing the name), a keyword equal to or containing the
query, then the share of the query's characters found in the name and keywords. Weak matches are
left out when a strong one exists.
"""

from __future__ import annotations

import random
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "PACKAGE_ID",
    "STICKERS",
    "Sticker",
    "find_sticker",
    "random_sticker",
    "search_stickers",
]

PACKAGE_ID: Final = "1003"


@dataclass(frozen=True)
class Sticker:
    sticker_id: str
    name: str
    keywords: tuple[str, ...]

    def face_data(self) -> dict[str, Any]:
        """The ``TIMFaceElem`` data for this sticker."""
        return {
            "sticker_id": self.sticker_id,
            "package_id": PACKAGE_ID,
            "width": 128,
            "height": 128,
            "formats": "png",
            "name": self.name,
        }

    def summary(self) -> dict[str, str]:
        return {
            "sticker_id": self.sticker_id,
            "name": self.name,
            "keywords": ", ".join(self.keywords),
        }


# id | name | our keywords
_TABLE = """
113|略略略|tease, tongue out, cheeky, nyah
119|凋谢|wilted flower, heartbreak, sad ending
125|委屈|wronged, pouting, about to cry
130|害羞|shy, blushing, embarrassed
131|酷|cool, sunglasses
132|吐了|vomit, disgusted, gross
134|怒|angry, furious, mad
138|爱心|heart, love
143|惊讶|surprised, shocked, wow
144|疑问|question, confused, what
145|睡|sleep, asleep, zzz
146|亲亲|kiss, smooch
151|奋斗|work hard, determined, keep going
152|发呆|dazed, blank stare, spacing out
157|可怜|pitiful, puppy eyes, please
159|点赞|thumbs up, like, good job
163|抱拳|thanks, respect, fist and palm
164|握手|handshake, deal, agreement
165|玫瑰|rose, romance, flower
169|ok|ok, okay, fine, got it
170|晚安|good night, moon, sleep well
174|拳头|fist, fight, power
176|太阳|sun, good morning, sunny
180|困|sleepy, tired, yawn
181|折磨|tormented, suffering, cannot stand it
182|抠鼻|nose picking, whatever, bored
183|鼓掌|applause, clap, bravo
184|撅嘴|pout, sulking
185|哼|hmph, annoyed, sulky
191|鞭炮|firecrackers, festival, new year
199|泪奔|crying, tears, sobbing
200|摊手|shrug, no idea, oh well
203|吐血|speechless, coughing blood, exasperated
204|斜眼笑|smirk, sly grin, side eye
213|头大|headache, overwhelmed, stressed
216|辣眼睛|eye sore, cringe, cannot unsee
217|哦哟|ooh, oh my, teasing
218|头秃|bald, overworked, programmer life
220|嘿嘿|hehe, grin, mischievous
221|暗中观察|watching quietly, lurking, peeking
222|吃瓜|popcorn, watching the drama, gossip
224|我酸了|jealous, envious, sour
225|狗头|doge, just kidding, sarcasm
227|敬礼|salute, respect, yes sir
231|哦|oh, i see, noted
236|拿到红包|red envelope, lucky money, thanks boss
239|牛吖|awesome, impressive, so good
246|打call|cheering, support, go go go
248|仔细分析|thinking, analysing, let me think
251|庆祝|celebrate, party, hooray
252|比心|finger heart, love you, heart hands
256|吓|scared, frightened, startled
258|烟花|fireworks, celebration, festival
262|我想开了|enlightened, at peace, letting go
266|柠檬|lemon, jealous, sour
267|大冤种|sucker, unlucky, scapegoat
272|贴贴|cuddle, snuggle, hug
276|尊嘟假嘟|really, for real, no way
278|六六六|666, awesome, nailed it
"""


def _load() -> tuple[Sticker, ...]:
    found: list[Sticker] = []
    for line in _TABLE.strip().splitlines():
        sticker_id, name, words = line.split("|")
        found.append(Sticker(sticker_id, name, tuple(w.strip() for w in words.split(","))))
    return tuple(found)


STICKERS: Final[tuple[Sticker, ...]] = _load()
_BY_ID: Final = {s.sticker_id: s for s in STICKERS}
_BY_NAME: Final = {s.name: s for s in STICKERS}

_STRONG = 60.0
_FLOOR = 15.0


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _overlap(query: str, target: str) -> float:
    """The share of the query's characters (with repeats) found in ``target``."""
    if not query:
        return 0.0
    shared = Counter(query) & Counter(target)
    return sum(shared.values()) / len(query)


def _score(sticker: Sticker, query: str) -> float:
    q = _norm(query)
    if not q:
        return 0.0
    name = _norm(sticker.name)
    if q in (name, sticker.sticker_id):
        return 100.0
    best = 0.0
    if name.startswith(q):
        best = 92.0
    elif q in name or (name and name in q):
        best = 88.0
    words = [_norm(w) for w in sticker.keywords]
    if q in words:
        best = max(best, 80.0)
    elif any(q in w for w in words):
        best = max(best, 70.0)
    elif any(w and w in q for w in words):
        best = max(best, 62.0)
    blob = name + "".join(words)
    return max(best, 40.0 * _overlap(q, blob))


def search_stickers(query: str, limit: int = 10) -> list[Sticker]:
    """Stickers matching ``query``, best first; an empty query lists the catalogue."""
    limit = max(1, min(int(limit), len(STICKERS)))
    if not _norm(query):
        return list(STICKERS[:limit])
    scored = sorted(
        ((s, _score(s, query)) for s in STICKERS), key=lambda pair: pair[1], reverse=True
    )
    top = scored[0][1] if scored else 0.0
    floor = _STRONG if top >= _STRONG else _FLOOR
    return [s for s, score in scored if score >= floor][:limit]


def find_sticker(text: str) -> Sticker | None:
    """A sticker by id or exact name, else the best search hit (None when nothing matches)."""
    key = text.strip()
    if not key:
        return None
    found = _BY_ID.get(key) or _BY_NAME.get(key)
    if found is not None:
        return found
    hits = search_stickers(key, 1)
    return hits[0] if hits and _score(hits[0], key) >= _STRONG else None


def random_sticker(rng: random.Random | None = None) -> Sticker:
    return (rng or random.Random()).choice(STICKERS)
