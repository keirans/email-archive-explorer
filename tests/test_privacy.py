"""Spec 11 §2.4: the repository stays free of personal data (generic rules)."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("privacy_check", ROOT / "scripts" / "privacy_check.py")
privacy_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy_check)


def test_generic_rules_flag_personal_data():
    # Built at runtime so this file itself stays clean for the repository scan
    text = "\n".join([
        "path /home/" + "jsmith/Mail/old.pst",
        "contact jane.doe" + "@" + "realcorp.com",
        "fine: /home/you/Mail and alex@larkspur.example and sam@example.com",
    ])
    hits = list(privacy_check.check_text("x.md", text, []))
    assert [h.split(": ")[1].split("]")[0] + "]" for h in hits] == ["[home-path]", "[email]"]
    assert all(":3:" not in h for h in hits)


def test_denylist_rules():
    deny = [("Acme", __import__("re").compile("Acme", 2)), ("re:\\bXY\\b", __import__("re").compile(r"\bXY\b", 2))]
    hits = list(privacy_check.check_text("x.md", "worked at ACME\nthe xy team\nxylophone", deny))
    assert len(hits) == 2


def test_repository_passes_generic_rules():
    assert privacy_check.main(["--no-denylist"]) == 0
