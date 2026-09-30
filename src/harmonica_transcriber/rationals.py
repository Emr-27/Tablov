from fractions import Fraction
import re

from .errors import ContractError

_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:/[1-9][0-9]*)?$")


def read_q(value: object, field: str) -> Fraction:
    if not isinstance(value, str) or not _PATTERN.fullmatch(value):
        raise ContractError(field, "应为规范有理数字符串 n 或 n/d")
    q = Fraction(value)
    if str_q(q) != value:
        raise ContractError(field, f"应约分为 {str_q(q)}")
    return q


def str_q(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def number(value: object, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(field, "应为有限数值")
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")) or (positive and result <= 0):
        raise ContractError(field, "应为有限正数" if positive else "应为有限数值")
    return result


def integer(value: object, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ContractError(field, f"应为 {low}–{high} 的整数")
    return value

