"""Replay current documentation and actual installed command evidence."""
from pathlib import Path

from .evidence import InvalidEvidence, external_ref, read_json


def check(inputs, targets):
    from documentation_lib import verify

    path = inputs.file("evidence.docs", manifest=True)
    data = read_json(path)
    own_distribution = external_ref(path.parent, data["distribution"], manifest=True)
    if own_distribution != inputs.file("evidence.distribution", manifest=True):
        raise InvalidEvidence("documentation and local_rc must pin the same distribution bundle")
    return "PASS", verify(Path(__file__).resolve().parents[2], path)
