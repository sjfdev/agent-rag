"""Input validation and Milvus expression safety."""

import pytest

from src.validation import InvalidDepartment, validate_dept


@pytest.mark.parametrize("dept", ["Tech", "HR", "Demo", "QA_1", "team-a", "研发部"])
def test_valid_departments(dept):
    assert validate_dept(dept) == dept


@pytest.mark.parametrize(
    "dept",
    [
        "",
        "Tech' or dept != '",
        'Tech" or 1==1',
        "team a",
        "a" * 65,
        "Tech\\",
        None,
        123,
    ],
)
def test_invalid_departments_rejected(dept):
    with pytest.raises(InvalidDepartment):
        validate_dept(dept)


def test_department_escape_keeps_filter_intact():
    # The regex rejects quotes, so no escaping can widen the filter.
    with pytest.raises(InvalidDepartment):
        validate_dept("Tech' or dept != '")
