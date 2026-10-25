"""v2 item 21 (F7): `goldmine help` — every topic renders; unknown topics
exit 2 with the topic list; the scope invariant is stated in getting-started."""

from __future__ import annotations

from goldmine import help as help_mod
from goldmine.cli import main


def test_every_topic_renders_nonempty(capsys):
    for topic in help_mod.TOPICS:
        rc = main(["help", topic])
        assert rc == 0, topic
        out = capsys.readouterr().out
        assert out.strip(), f"topic {topic} rendered nothing"


def test_help_without_topic_lists_topics(capsys):
    rc = main(["help"])
    assert rc == 0
    out = capsys.readouterr().out
    for topic in help_mod.TOPICS:
        assert topic in out


def test_unknown_topic_exits_2_with_list(capsys):
    rc = main(["help", "no-such-topic"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "unknown help topic" in captured.err
    for topic in help_mod.TOPICS:
        assert topic in captured.err  # the list follows the error


def test_required_topics_present():
    assert set(help_mod.TOPICS) >= {
        "getting-started",
        "ingest",
        "search-syntax",
        "models",
        "media",
        "maintenance",
        "troubleshoot",
    }


def test_scope_invariant_in_getting_started():
    assert "never your whole Mac" in help_mod.TOPICS["getting-started"]


def test_troubleshoot_mirrors_the_error_table():
    text = help_mod.TOPICS["troubleshoot"]
    for needle in (
        "goldmine model check",
        "fingerprint",
        "TCC",
        "goldmine reset --reindex",
        "nothing found",
    ):
        assert needle in text, needle


def test_distill_help_describes_fixed_v3_threshold(capsys):
    assert main(["distill", "--help"]) == 0
    text = capsys.readouterr().out
    assert "0.45" in text and "fixed" in text and "co-association" in text
    assert "Otsu" not in text and "CALIBRATED" not in text
