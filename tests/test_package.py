from __future__ import annotations

import unittest

import archive_index


class PackageTests(unittest.TestCase):
    def test_version_is_defined(self) -> None:
        self.assertRegex(archive_index.__version__, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
