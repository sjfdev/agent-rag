"""Application version, kept in sync with git tags and CHANGELOG.md.

Bump this together with a tag: `scripts/check_release.py` compares the three
(``__version__``, the tag on HEAD, and ``CHANGELOG.md``) so a release cannot
ship with a mismatched or unrecorded version.
"""

__version__ = "1.0.0"
