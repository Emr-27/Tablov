"""无点 do 的绝对音高锚定和可反解的数字简谱。"""
import re

from .errors import ContractError
from .rationals import integer

OFFSETS = (0, 2, 4, 5, 7, 9, 11)
LETTERS = "CDEFGAB"
NATURAL_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
MAJOR_SIG = {
    "Cb": -7, "Gb": -6, "Db": -5, "Ab": -4, "Eb": -3, "Bb": -2, "F": -1,
    "C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5, "F#": 6, "C#": 7,
}
MINOR_SIG = {
    "Ab": -7, "Eb": -6, "Bb": -5, "F": -4, "C": -3, "G": -2, "D": -1,
    "A": 0, "E": 1, "B": 2, "F#": 3, "C#": 4, "G#": 5, "D#": 6, "A#": 7,
}
PITCH_RE = re.compile(r"^([A-G])((?:#{1,2}|b{1,2})?)(-?\d+)$")
KEY_RE = re.compile(r"^([A-G](?:#|b)?)\s+(major|minor)$", re.IGNORECASE)


def spelled_pc(name: str) -> int:
    match = re.fullmatch(r"([A-G])([#b]?)", name)
    if not match:
        raise ContractError("tonic_spelling", "调名应如 G、F#、Bb")
    return (NATURAL_PC[match[1]] + (1 if match[2] == "#" else -1 if match[2] == "b" else 0)) % 12


def midi_spelling(name: str) -> int:
    match = PITCH_RE.fullmatch(name)
    if not match:
        raise ContractError("spelling_hint", "音名应如 C#4、Db4、B#3")
    letter, marks, octave_text = match.groups()
    accidental = len(marks) * (1 if marks.startswith("#") else -1)
    result = 12 * (int(octave_text) + 1) + NATURAL_PC[letter] + accidental
    if not 0 <= result <= 127:
        raise ContractError("spelling_hint", "音名超出 MIDI 0–127")
    return result


def signature_accidentals(spelling: str, mode: str) -> dict[str, int]:
    count = (MAJOR_SIG if mode == "major" else MINOR_SIG)[spelling]
    order = "FCGDAEB" if count > 0 else "BEADGCF"
    result = {letter: 0 for letter in LETTERS}
    for letter in order[:abs(count)]:
        result[letter] = 1 if count > 0 else -1
    return result


def default_do(spelling: str, mode: str) -> int:
    letter = spelling[0]
    if mode == "minor":
        letter = LETTERS[(LETTERS.index(letter) + 2) % 7]
    accidental = signature_accidentals(spelling, mode)[letter]
    return 60 + NATURAL_PC[letter] + accidental


def key_from_text(text: str, do_midi: int | None = None, source: str = "user") -> dict:
    match = KEY_RE.fullmatch(text.strip())
    if not match:
        raise ContractError("--key", "调性应如 'G major' 或 'A minor'")
    spelling = match[1][0].upper() + match[1][1:]
    mode = match[2].lower()
    table = MAJOR_SIG if mode == "major" else MINOR_SIG
    if spelling not in table:
        raise ContractError("--key", "只支持传统 -7 至 +7 调号")
    tonic = spelled_pc(spelling)
    result = {"at_q": "0", "tonic_pc": tonic, "tonic_spelling": spelling, "mode": mode,
              "do_midi": default_do(spelling, mode) if do_midi is None else do_midi,
              "minor_system": "la", "source": source}
    validate_key(result, "key_map[0]")
    return result


def validate_key(key: dict, field: str) -> None:
    if not isinstance(key, dict):
        raise ContractError(field, "应为调性对象")
    mode = key.get("mode")
    if mode not in ("major", "minor"):
        raise ContractError(field + ".mode", "仅支持 major/minor")
    spelling = key.get("tonic_spelling")
    table = MAJOR_SIG if mode == "major" else MINOR_SIG
    if spelling not in table:
        raise ContractError(field + ".tonic_spelling", "只支持传统 -7 至 +7 调号")
    tonic = integer(key.get("tonic_pc"), field + ".tonic_pc", 0, 11)
    if spelled_pc(spelling) != tonic:
        raise ContractError(field + ".tonic_spelling", "调名与 tonic_pc 不一致")
    do = integer(key.get("do_midi"), field + ".do_midi", 0, 127)
    if do % 12 != (tonic + (3 if mode == "minor" else 0)) % 12:
        raise ContractError(field + ".do_midi", "无点 do 与调性不一致")
    if key.get("minor_system", "la") != "la":
        raise ContractError(field + ".minor_system", "本阶段仅支持 la-based minor")
    if key.get("source") not in ("user", "midi", "estimated", "assumed"):
        raise ContractError(field + ".source", "来源必须明确")


def transpose_key(key: dict, semitones: int) -> dict:
    if semitones == 0:
        return dict(key)
    tonic = (key["tonic_pc"] + semitones) % 12
    mode = key["mode"]
    table = MAJOR_SIG if mode == "major" else MINOR_SIG
    old_sign = table[key["tonic_spelling"]]
    candidates = [name for name in table if spelled_pc(name) == tonic]
    name = min(candidates, key=lambda s: (abs(table[s]), 0 if (table[s] >= 0) == (old_sign >= 0) else 1,
                                          0 if table[s] >= 0 else 1, s))
    result = dict(key)
    result.update(tonic_pc=tonic, tonic_spelling=name, do_midi=key["do_midi"] + semitones,
                  source="user")
    validate_key(result, "transposed_key")
    return result


def active_key(key_map: list[dict], q) -> dict:
    current = key_map[0]
    from .rationals import read_q
    for key in key_map:
        if read_q(key["at_q"], "key_map.at_q") <= q:
            current = key
        else:
            break
    return current


def spell_pitch(pitch: int, key: dict, hint: str | None = None) -> tuple[str, str]:
    if hint is not None and midi_spelling(hint) != pitch:
        raise ContractError("spelling_hint", f"{hint} 与 pitch_midi={pitch} 不一致")
    do = key["do_midi"]
    key_orientation = (MAJOR_SIG if key["mode"] == "major" else MINOR_SIG)[key["tonic_spelling"]]
    do_letter = key["tonic_spelling"][0]
    if key["mode"] == "minor":
        # relative major is two letter steps after the minor tonic: A -> C.
        do_letter = LETTERS[(LETTERS.index(do_letter) + 2) % 7]
    options = []
    signature = signature_accidentals(key["tonic_spelling"], key["mode"])
    for degree, offset in enumerate(OFFSETS, 1):
        for octave in range(-11, 11):
            accidental = pitch - do - offset - 12 * octave
            if not -2 <= accidental <= 2:
                continue
            letter = LETTERS[(LETTERS.index(do_letter) + degree - 1) % 7]
            spelled_accidental = signature[letter] + accidental
            if not -2 <= spelled_accidental <= 2:
                continue
            natural_midi = pitch - spelled_accidental
            if (natural_midi - NATURAL_PC[letter]) % 12:
                continue
            scientific_octave = (natural_midi - NATURAL_PC[letter]) // 12 - 1
            name = letter + ("#" * spelled_accidental if spelled_accidental > 0 else "b" * -spelled_accidental) + str(scientific_octave)
            suffix = "'" * octave if octave > 0 else "," * -octave
            token = ("#" * accidental if accidental > 0 else "b" * -accidental) + str(degree) + suffix
            hint_rank = 0 if hint == name else 1
            orient_rank = 0 if accidental == 0 or (accidental > 0) == (key_orientation >= 0) else 1
            options.append(((hint_rank if hint else 0, abs(accidental), orient_rank, degree, abs(octave)), token, name))
    if not options:
        raise ContractError("pitch_midi", f"{pitch} 无可用简谱拼写")
    _, token, name = min(options, key=lambda item: item[0])
    return token, name


def parse_jianpu(token: str, key: dict) -> int:
    match = re.fullmatch(r"(##|bb|#|b)?([1-7])([',]*)", token)
    if not match:
        raise ContractError("jianpu", "无效数字音")
    marks, degree, dots = match.groups()
    if "'" in dots and "," in dots:
        raise ContractError("jianpu", "高低八度点不能混用")
    accidental = (len(marks) if marks and marks.startswith("#") else -len(marks) if marks else 0)
    octave = len(dots) if dots.startswith("'") else -len(dots)
    result = key["do_midi"] + OFFSETS[int(degree) - 1] + accidental + 12 * octave
    integer(result, "jianpu pitch", 0, 127)
    return result


def pitch_name(pitch: int) -> str:
    names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
    return names[pitch % 12] + str(pitch // 12 - 1)
