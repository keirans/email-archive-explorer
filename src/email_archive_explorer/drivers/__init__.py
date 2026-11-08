"""Pluggable archive drivers (Spec 07).

Importing this package registers the built-in drivers. Registration order is
probe order: the most specific magic-byte checks come first.
"""

from email_archive_explorer.drivers.base import BaseArchiveDriver
from email_archive_explorer.drivers.eml_dir_driver import EmlDirDriver
from email_archive_explorer.drivers.mbox_driver import MboxDriver
from email_archive_explorer.drivers.pst_driver import PstDriver
from email_archive_explorer.drivers.registry import ArchiveDriverRegistry, detect_driver

for _driver in (PstDriver, MboxDriver, EmlDirDriver):
    ArchiveDriverRegistry.register(_driver)

__all__ = [
    "ArchiveDriverRegistry",
    "BaseArchiveDriver",
    "EmlDirDriver",
    "MboxDriver",
    "PstDriver",
    "detect_driver",
]
