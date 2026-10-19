"""Bounded, structured recovery for adjacent Chinese subtitle fragments.

The caller supplies the existing model request function. No model, provider or
global inference settings are changed by this module.
"""
from __future__ import annotations

import json
import re


_SPLIT_GUIDANCE = (
    '按原字幕的含义分条，不是按中文逗号分条。若原条包含主语及动作，译条不能只留开头的副词，'
    '也不能把该条绝大部分意思全部塞进下一条；中文语序需要时可以在相邻条之间少量借位。'
    '分条示例：英语s1=Whenever I got home,；s2=she had already taken...；s3=the children to school.。'
    '完整中文=每当我回到家时，她早已把孩子们送到学校了。；'
    '正确分条s1=每当我回到家时，；s2=她早已把孩子们；s3=送到学校了。'
    '不要把s2只写成“她早已”，然后把整个动作及所有对象挤入s3。'
)


def continuation(left: str, right: str) -> bool:
    left, right = left.strip(), right.strip()
    if not left or not right:
        return False
    return (not re.search(r'[.!?。！？]["\u201d\u2019\']?$', left)
            or left.endswith(('...', '…'))) and bool(re.match(r'[a-z]', right))


def window_indices(index: int, available: set[int], sources: dict[int, str]) -> list[int]:
    """Expand only across a visible sentence continuation, at most three cues."""
    indices = [index]
    if index - 1 in available and continuation(sources[index - 1], sources[index]):
        indices.insert(0, index - 1)
    if index + 1 in available and continuation(sources[index], sources[index + 1]):
        indices.append(index + 1)
    return indices


def request_spec(lines: list[str], target_name: str) -> tuple[str, dict]:
    if not 1 <= len(lines) <= 3 or any(not isinstance(s, str) or not s.strip() for s in lines):
        raise ValueError('恢复窗口必须包含1至3条非空字幕。')
    if sum(map(len, lines)) > 600:
        raise ValueError('恢复窗口超过600字符，保留单条修复。')
    joined = re.sub(r'\.{3,}\s+(?=\w)', ' ', ' '.join(lines))
    keys = [f's{i+1}' for i in range(len(lines))]
    # A mandatory initial Chinese character avoids punctuation-only attractors;
    # finite repetition prevents an unbounded constrained-generation response.
    string = {'type': 'string', 'pattern': '^[一-鿿][^A-Za-z]{0,160}$'}
    properties = {'whole': string, **{key: string for key in keys}}
    schema = {'type': 'object', 'properties': properties,
              'required': list(properties), 'additionalProperties': False}
    prompt = (
        f'将整段英语电影对白先完整翻译到whole字段，然后按原字幕依次在{"、".join(keys)}字段中分别写对应译文。'
        '先完成whole的翻译；各s字段只能从whole原样、连续、按顺序切分，不能再次翻译或换用同义词。'
        '各s字段拼接必须逐字还原whole（仅标点可不同）。切分时对应原字幕，保留人物、动作及对象的关系，不增删意思，不交换各条的含义。'
        + _SPLIT_GUIDANCE
        + f'输出JSON对象。译文使用{target_name}。\n整段：' + joined
        + '\n原字幕：' + json.dumps(dict(zip(keys, lines)), ensure_ascii=False)
    )
    return prompt, schema


def _response_data(response: str, count: int) -> dict:
    data = json.loads(response)
    keys = [f's{i+1}' for i in range(count)]
    if not isinstance(data, dict) or set(data) != {'whole', *keys}:
        raise ValueError('恢复响应字段不完整或包含多余字段。')
    for value in data.values():
        if not isinstance(value, str) or not re.fullmatch(r'[一-鿿][^A-Za-z]{0,160}', value):
            raise ValueError('恢复响应不满足有界中文输出约束。')
        if '\n' in value or '\r' in value:
            raise ValueError('恢复响应包含段内换行。')
    return data


def parse_response(response: str, count: int) -> list[str]:
    data = _response_data(response, count)
    keys = [f's{i+1}' for i in range(count)]
    normalized = lambda value: ''.join(char for char in value if char.isalnum())
    segments = [data[key] for key in keys]
    if normalized(''.join(segments)) != normalized(data['whole']):
        raise ValueError('分条译文与完整译文不一致，拒绝漏字、增字或改写。')
    return segments



def recover(lines: list[str], target_name: str, request, diagnostics: list, log) -> list[str]:
    """One bounded context request; retain rejected evidence without weakening checks."""
    prompt, schema = request_spec(lines, target_name)
    response = request(prompt, schema)
    record = {'phase': 'translate_and_split', 'response': response[:8192]}
    diagnostics.append(record)
    try:
        return parse_response(response, len(lines))
    except ValueError as exc:
        record['error'] = str(exc)
        raise
