"""Two agents on one journal would both trade the same account: the second must refuse to start."""
import pytest

from aeon.journal import Journal, JournalLocked


def test_a_second_agent_on_the_same_journal_is_refused(tmp_path):
    path = str(tmp_path / "aeon-demo.db")
    first = Journal(path)
    with pytest.raises(JournalLocked, match="already using"):
        Journal(path)
    first.close()


def test_the_lock_is_released_on_close_so_a_restart_can_take_it(tmp_path):
    path = str(tmp_path / "aeon-demo.db")
    Journal(path).close()
    second = Journal(path)
    second.kv_set("k", 1)
    assert second.kv_get("k") == 1
    second.close()


def test_different_journals_do_not_block_each_other(tmp_path):
    a, b = Journal(str(tmp_path / "aeon-paper.db")), Journal(str(tmp_path / "aeon-demo.db"))
    a.close()
    b.close()


def test_in_memory_journals_are_not_locked():
    one, two = Journal(":memory:"), Journal(":memory:")
    one.close()
    two.close()
