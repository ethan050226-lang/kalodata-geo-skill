"""Only scan the explicit public source set; never read private configs or snapshots."""
from pathlib import Path
import re
import unittest


class PublicSourcePrivacyTest(unittest.TestCase):
    def test_no_live_credentials_resource_ids_or_internal_document_links(self):
        root=Path(__file__).resolve().parent
        files=list(root.glob('*.py'))+[root/'README.md',root/'.gitignore',root/'requirements.txt']
        files += [p for folder in ('skill','examples') for p in (root/folder).rglob('*')
                  if p.is_file() and p.suffix in ('.md','.json','.yaml','.html')]
        patterns=[r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{20,}',
                  r'\b(?:ou_|oc_|cli_)[A-Za-z0-9]{16,}', r'\b\d{18,20}\b',
                  r'https://[^/]+\.feishu\.cn/(?:wiki|docx|sheets|drive/folder|file)/[A-Za-z0-9]{15,}']
        for path in files:
            text=path.read_text()
            for pattern in patterns:
                self.assertIsNone(re.search(pattern,text),f'{path.relative_to(root)} contains a potential private value; value withheld')
