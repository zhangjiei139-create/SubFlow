"""Shared subtitle purpose labels; metadata is evidence, never proof of sync."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class SubtitleRole:
    code: str
    reason: str = ""

    @property
    def main_dialogue(self):
        return self.code in {"main", "sdh"}


def classify(value, *, titles=(), forced=False, hearing_impaired=False):
    from subtitle_identity_guard import release_metadata
    text = release_metadata(str(value or ""), titles).replace("_", " ")
    text = re.sub(r"[.\-]+", " ", text)
    rules = (
        ("commentary", r"\bcommentar(?:y|ies)\b|评论|評論|讲评|講評", "评论音轨字幕，不是正片对白"),
        ("extra", r"\b(?:featurettes?|bonus|special features|behind the scenes|making of|deleted scenes|development and production|building the predator homeworld)\b|花絮|制作特辑|製作特輯|幕后|幕後|删减片段|刪減片段", "花絮或制作特辑字幕，不是正片对白"),
        ("trailer", r"\b(?:trailer|teaser|sample)\b|预告片|預告片", "预告片或样片字幕，不是正片字幕"),
        ("partial", r"\bforced(?:\s+only)?\b|\bforeign\s+(?:parts?\s+)?only\b|\baliens?\s+only\b|\bsigns?\s*(?:&|and|/)\s*songs?\b|强制|強制|片段字幕", "片段或强制字幕，不是完整字幕"),
        ("volume", r"(?<![A-Za-z0-9])(?:cd|disc|disk)\s*[1-9](?![A-Za-z0-9])", "CD/Disc 分卷字幕；单卷不覆盖整片"),
    )
    for code, pattern, reason in rules:
        if re.search(pattern, text, re.I):
            return SubtitleRole(code, reason)
    # Explicit complete/bilingual labels can override a container's forced bit.
    if forced and not re.search(r"\b(?:full|complete|bilingual|dual language)\b|完整|全字幕|双语|雙語", text, re.I):
        return SubtitleRole("partial", "片段或强制字幕，不是完整字幕")
    if hearing_impaired or re.search(r"\b(?:sdh|cc|hearing impaired)\b|听障|聽障", text, re.I):
        return SubtitleRole("sdh")
    return SubtitleRole("main")


def track_role(track):
    return classify(getattr(track, "name", ""), forced=getattr(track, "forced", False))


def candidate_role(candidate, identity=None):
    titles = tuple(str(getattr(identity, key, "") or "") for key in ("title", "original_title"))
    roles = [classify(getattr(candidate, key, ""), titles=titles,
                      hearing_impaired=getattr(candidate, "hearing_impaired", False))
             for key in ("release", "file_name")]
    return next((role for role in roles if not role.main_dialogue),
                next((role for role in roles if role.code == "sdh"), roles[0]))
