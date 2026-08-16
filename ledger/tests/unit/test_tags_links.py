"""Tag and link name validation (design §4, M6).

The charset is Beancount's, verified against 3.2.3: letters, digits,
dash, underscore, slash, dot. Anything else could never be exported, so
record() refuses it up front.
"""

import pytest

from ledger.domain.errors import InvalidLinkError, InvalidTagError
from ledger.domain.transaction import validate_links, validate_tags

VALID_NAMES = ["trip-nyc-2024", "a", "UPPER", "123", "-lead", "a.b/c-d_e"]
INVALID_NAMES = ["", "with space", "né", "trip!", "#tagged", "^linked", "a:b"]


@pytest.mark.parametrize("name", VALID_NAMES)
def test_valid_names_are_accepted(name):
    assert validate_tags([name]) == frozenset({name})
    assert validate_links([name]) == frozenset({name})


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_invalid_names_are_refused(name):
    with pytest.raises(InvalidTagError):
        validate_tags([name])
    with pytest.raises(InvalidLinkError):
        validate_links([name])


def test_validation_normalizes_to_a_frozenset():
    assert validate_tags(["b", "a", "b"]) == frozenset({"a", "b"})
    assert validate_links(iter(["x"])) == frozenset({"x"})


def test_empty_input_is_fine():
    assert validate_tags([]) == frozenset()
    assert validate_links(set()) == frozenset()
