"""Input validation helpers shared by the API and the vector store drivers."""

import re

# Department names come from directory names: letters, digits, underscore, dash,
# and CJK characters. Keep it strict so the value is safe to embed in a Milvus
# filter expression.
_DEPT_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fa5]{1,64}$")


class InvalidDepartment(ValueError):
    pass


def validate_dept(dept: str) -> str:
    """Validate a department name and return it escaped for Milvus `expr`."""
    if not isinstance(dept, str) or not _DEPT_RE.fullmatch(dept):
        raise InvalidDepartment("invalid department name")
    return dept.replace("\\", "\\\\").replace("'", "\\'")
