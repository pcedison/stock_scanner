from backend.adapters._utils import to_float


def test_to_float_preserves_numeric_zero():
    assert to_float(0) == 0.0
    assert to_float("0") == 0.0


def test_to_float_handles_null_tokens_and_parenthesized_negatives():
    assert to_float("--") is None
    assert to_float(None) is None
    assert to_float("(1,234.5)", parenthesized_negative=True) == -1234.5


def test_to_float_returns_none_for_unparseable_text():
    assert to_float("abc") is None
    assert to_float("1.2.3") is None

