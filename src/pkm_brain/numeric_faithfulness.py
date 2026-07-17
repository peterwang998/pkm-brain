from __future__ import annotations

import re
from typing import Any


NUMBER_TOKEN_RE = re.compile(r"\$?\d+(?:,\d{3})*(?:\.\d+)?(?:[%kKmMbB])?|[A-Za-z]+")
NUMBER_WORD_VALUES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
NUMBER_SCALE_WORDS = {
    "hundred": 100,
    "thousand": 1_000,
    "k": 1_000,
    "million": 1_000_000,
    "m": 1_000_000,
    "billion": 1_000_000_000,
    "bn": 1_000_000_000,
    "b": 1_000_000_000,
}
SPEAKER_IDENTIFIER_LABELS = {
    "attendee",
    "candidate",
    "guest",
    "interviewer",
    "moderator",
    "participant",
    "person",
    "speaker",
}
EXISTENTIAL_ROLE_NOUNS = SPEAKER_IDENTIFIER_LABELS | {
    "customer",
    "employee",
    "engineer",
    "founder",
    "member",
    "respondent",
    "user",
}


def unsupported_statement_numbers(statement: str, evidence_text: str) -> list[str]:
    statement_numbers = extract_numeric_mentions(statement)
    if not statement_numbers:
        return []
    evidence_numbers = extract_numeric_mentions(evidence_text)
    unsupported: list[str] = []
    for statement_number in statement_numbers:
        if not any(
            number_values_match(statement_number, evidence_number)
            for evidence_number in evidence_numbers
        ):
            unsupported.append(statement_number["surface"])
    return stable_unique_strings(unsupported)


def extract_numeric_mentions(text: str) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    tokens = [
        (match.group(0), match.start(), match.end())
        for match in NUMBER_TOKEN_RE.finditer(text)
    ]
    consumed_word_indexes: set[int] = set()
    for index, (raw, start, end) in enumerate(tokens):
        if re.search(r"\d", raw) and digit_token_is_identifierish(text, start, end):
            continue
        parsed = parse_digit_number_token(
            raw, tokens[index + 1][0] if index + 1 < len(tokens) else ""
        )
        if parsed is not None:
            value, kind = parsed
            mentions.append({"surface": raw, "value": value, "kind": kind})
            continue
        if index in consumed_word_indexes:
            continue
        parsed_words = parse_number_word_sequence(tokens, index)
        if parsed_words is None:
            continue
        value, kind, end_index = parsed_words
        if number_word_sequence_is_idiom(tokens, index, end_index):
            continue
        consumed_word_indexes.update(range(index, end_index + 1))
        surface = text[start : tokens[end_index][2]]
        mentions.append({"surface": surface, "value": value, "kind": kind})
    return mentions


def digit_token_is_identifierish(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if before.isalnum() or after.isalnum():
        return True
    if before == "/" or after == "/":
        return True
    if before == "-" and any(
        char.isalpha() for char in text[max(0, start - 12) : start]
    ):
        return True
    if after == "-" and any(
        char.isalpha() for char in text[end : min(len(text), end + 12)]
    ):
        return True
    label_match = re.search(r"([A-Za-z]+)\s*$", text[max(0, start - 32) : start])
    if label_match and label_match.group(1).casefold() in SPEAKER_IDENTIFIER_LABELS:
        return True
    return False


def parse_digit_number_token(raw: str, next_token: str) -> tuple[float, str] | None:
    token = raw.strip()
    has_currency = token.startswith("$")
    token = token.lstrip("$").replace(",", "")
    suffix = ""
    if token and token[-1] in "%kKmMbB":
        suffix = token[-1].lower()
        token = token[:-1]
    if not re.fullmatch(r"\d+(?:\.\d+)?", token):
        return None
    value = float(token)
    kind = "number"
    if suffix == "%":
        kind = "percent"
    elif suffix in {"k", "m", "b"}:
        value *= {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix]
    else:
        scale = NUMBER_SCALE_WORDS.get(next_token.casefold())
        if scale and scale >= 1_000:
            value *= scale
    if has_currency:
        kind = "number"
    return value, kind


def parse_number_word_sequence(
    tokens: list[tuple[str, int, int]],
    start_index: int,
) -> tuple[float, str, int] | None:
    total = 0.0
    current = 0.0
    index = start_index
    consumed = False
    kind = "number"
    while index < len(tokens):
        word = tokens[index][0].casefold().replace("-", " ")
        if word in {"and", "a"} and consumed:
            index += 1
            continue
        if word == "point" and consumed:
            decimal_digits: list[str] = []
            index += 1
            while index < len(tokens):
                digit_word = tokens[index][0].casefold()
                if (
                    digit_word not in NUMBER_WORD_VALUES
                    or NUMBER_WORD_VALUES[digit_word] > 9
                ):
                    break
                decimal_digits.append(str(NUMBER_WORD_VALUES[digit_word]))
                index += 1
            if decimal_digits:
                current += float("0." + "".join(decimal_digits))
                continue
            break
        if word in NUMBER_WORD_VALUES:
            current += NUMBER_WORD_VALUES[word]
            consumed = True
            index += 1
            continue
        if word == "half" and consumed:
            current += 0.5
            index += 1
            continue
        if word in NUMBER_SCALE_WORDS and consumed:
            scale = NUMBER_SCALE_WORDS[word]
            if scale == 100:
                current = max(1.0, current) * scale
            else:
                total += max(1.0, current) * scale
                current = 0.0
            consumed = True
            index += 1
            continue
        if word in {"percent", "percentage"} and consumed:
            kind = "percent"
            index += 1
            break
        break
    if not consumed:
        return None
    return total + current, kind, index - 1


def number_word_sequence_is_idiom(
    tokens: list[tuple[str, int, int]],
    start_index: int,
    end_index: int,
) -> bool:
    words = [tokens[index][0].casefold() for index in range(start_index, end_index + 1)]
    previous_words = [
        tokens[index][0].casefold()
        for index in range(max(0, start_index - 2), start_index)
    ]
    next_words = [
        tokens[index][0].casefold()
        for index in range(end_index + 1, min(len(tokens), end_index + 3))
    ]
    if words == ["zero"] and next_words[:2] == ["to", "one"]:
        return True
    if previous_words[-2:] == ["zero", "to"] and words == ["one"]:
        return True
    if words == ["one"] and next_words[:2] == ["on", "one"]:
        return True
    if previous_words[-2:] == ["one", "on"] and words == ["one"]:
        return True
    if previous_words[-1:] == ["day"] and words == ["one"]:
        return True
    exact_count_cues = {"exactly", "just", "only", "single"}
    if (
        words == ["one"]
        and next_words[:1]
        and next_words[0] in EXISTENTIAL_ROLE_NOUNS
        and not any(word in exact_count_cues for word in previous_words)
    ):
        # "One participant said ..." commonly normalizes an indefinite
        # source reference, while "exactly one" remains a count assertion.
        return True
    return False


def number_values_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("kind") != right.get("kind"):
        return False
    left_value = float(left.get("value") or 0.0)
    right_value = float(right.get("value") or 0.0)
    tolerance = max(1.0, abs(left_value) * 0.05)
    return abs(left_value - right_value) <= tolerance


def stable_unique_strings(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
    return output
