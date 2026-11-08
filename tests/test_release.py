"""Release metadata and dependency regression checks."""
import re
import unittest
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def test_version_and_changelog(self):
        manifest = (ROOT / 'plugin.yaml').read_text()
        match = re.search(r'^version: (\S+)$', manifest, re.M)
        assert match is not None
        version = match.group(1)
        self.assertEqual(version, '1.1.1')
        self.assertRegex((ROOT / 'CHANGELOG.md').read_text(), rf'(?m)^## {re.escape(version)}\b')

    def test_substrate_requirement_rejects_broken_releases(self):
        manifest = (ROOT / 'plugin.yaml').read_text()
        match = re.search(r'^\s*- "(weft-memory[^"]+)"$', manifest, re.M)
        assert match is not None
        dependency = match.group(1)
        requirement = Requirement(dependency)
        self.assertFalse(requirement.specifier.contains(Version('1.0.0')))
        self.assertFalse(requirement.specifier.contains(Version('1.1.0')))
        self.assertTrue(requirement.specifier.contains(Version('1.1.1')))
        self.assertFalse(requirement.specifier.contains(Version('2.0.0')))

    def test_media_setup_does_not_claim_provider_support(self):
        readme = (ROOT / 'README.md').read_text()
        self.assertIn('not an image or audio ingestion setup', readme)
        for dependency in ('mlx-vlm', 'pywhispercpp', 'ffmpeg'):
            self.assertIn(dependency, readme)


if __name__ == '__main__':
    unittest.main()
