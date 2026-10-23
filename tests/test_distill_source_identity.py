"""Changes to executed local package code invalidate a prepared evaluator."""
from pathlib import Path
import shutil

import pytest

from distill_eval.identity import evaluator_identity

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("relative", [
    "scripts/acceptance_lib/__init__.py",
    "scripts/answer_eval/__init__.py",
    "scripts/answer_eval/statistics.py",
    "scripts/answer_eval/workflow.py",
    "scripts/product_eval/__init__.py",
    "scripts/product_eval/bundle.py",
    "scripts/product_eval/alignment.py",
    "scripts/product_eval/scoring.py",
    "scripts/product_eval/schema.py",
])
def test_mutating_eager_local_dependency_invalidates_identity(tmp_path, relative):
    original = evaluator_identity(ROOT)
    assert relative in original
    fixture = tmp_path / "source-copy"
    for name in original:
        target = fixture / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    before = evaluator_identity(fixture)
    assert before == original
    target = fixture / relative
    target.write_bytes(target.read_bytes() + b"\n_IDENTITY_MUTATION = True\n")
    after = evaluator_identity(fixture)
    assert after[relative] != before[relative]
    assert {k: v for k, v in after.items() if k != relative} == {k: v for k, v in before.items() if k != relative}
