"""Unit tests for Phase 4: CLI commands (demo archive)."""

import argparse

from email_archive_explorer.cli import cmd_demo, cmd_index, cmd_stats


def test_cli_stats(demo_eml, tmp_path, capsys):
    args = argparse.Namespace(archive_path=str(demo_eml), db_path=str(tmp_path / "stats_test.db"))
    assert cmd_stats(args) == 0
    out = capsys.readouterr().out
    assert "larkspur-mail" in out and "Total Messages" in out


def test_cli_index(demo_eml, tmp_path):
    test_db = tmp_path / "index_test.db"
    args = argparse.Namespace(archive_path=str(demo_eml), db_path=str(test_db), reindex=False)
    assert cmd_index(args) == 0
    assert test_db.exists()


def test_cli_missing_archive(tmp_path, capsys):
    args = argparse.Namespace(archive_path=str(tmp_path / "nope.pst"), db_path=None)
    assert cmd_stats(args) == 1
    assert "not found" in capsys.readouterr().err


def test_cli_demo(tmp_path, capsys):
    assert cmd_demo(argparse.Namespace(out=str(tmp_path / "d"), seed=7)) == 0
    assert (tmp_path / "d" / "larkspur-mail" / "Inbox").is_dir()
    assert (tmp_path / "d" / "larkspur-newsletters.mbox").is_file()
    assert "fictional" in capsys.readouterr().out
