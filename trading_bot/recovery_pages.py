"""Offline-only immutable snapshot pages; integrity is not exchange authentication.

The simulator commits an ordered full list into a manifest before any page is
served. This contract is synthetic and cannot be inferred from a real API.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
from .fill_ledger import Fill


def _canonical(items):
    return json.dumps([asdict(f) for f in items], sort_keys=True,
                      separators=(',', ':'), ensure_ascii=True).encode('ascii')


def _digest(items):
    return hashlib.sha256(_canonical(items)).hexdigest()


@dataclass(frozen=True)
class Manifest:
    snapshot_id: str
    page_hashes: tuple
    total: int
    history_hash: str


@dataclass(frozen=True)
class Page:
    snapshot_id: str
    index: int
    fills: tuple


class SnapshotSimulator:
    """Frozen in-memory fixture, with explicit pages and full-history commitment."""
    is_simulator = True

    def __init__(self, snapshot_id, fills, page_size=2):
        if not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 100:
            raise ValueError('Invalid snapshot ID')
        if type(page_size) is not int or page_size < 1: raise ValueError('Invalid page size')
        fills = tuple(fills)
        if not all(isinstance(f, Fill) for f in fills): raise ValueError('Invalid fill')
        if any(type(f.sequence) is not int for f in fills): raise ValueError('Invalid sequence')
        if any(a.sequence >= b.sequence for a,b in zip(fills,fills[1:])):
            raise ValueError('Snapshot history must be strictly ordered')
        if len({f.trade_id for f in fills}) != len(fills): raise ValueError('Duplicate trade ID')
        self.pages = tuple(Page(snapshot_id, i, fills[start:start+page_size])
                           for i,start in enumerate(range(0,len(fills),page_size)))
        self.manifest = Manifest(snapshot_id, tuple(_digest(p.fills) for p in self.pages),
                                 len(fills), _digest(fills))

    def get_manifest(self): return self.manifest
    def get_page(self, index): return self.pages[index]


class PageCollector:
    """Collect in any page arrival order; conflicting duplicates are rejected."""
    def __init__(self, manifest):
        if not isinstance(manifest, Manifest) or type(manifest.total) is not int or manifest.total < 0:
            raise ValueError('Invalid manifest')
        if len(manifest.page_hashes) > 10000: raise ValueError('Too many pages')
        self.manifest, self.pages = manifest, {}

    def add(self, page):
        if not isinstance(page,Page) or page.snapshot_id != self.manifest.snapshot_id:
            raise ValueError('Snapshot mismatch')
        if type(page.index) is not int or not 0 <= page.index < len(self.manifest.page_hashes):
            raise ValueError('Invalid page index')
        if not isinstance(page.fills, tuple) or not all(isinstance(f, Fill) for f in page.fills):
            raise ValueError('Invalid page')
        if _digest(page.fills) != self.manifest.page_hashes[page.index]:
            raise ValueError('Page differs from commitment')
        existing=self.pages.get(page.index)
        if existing is not None and existing != page: raise ValueError('Contradictory duplicate')
        self.pages[page.index] = page

    def finish(self):
        if len(self.pages) != len(self.manifest.page_hashes): return None
        fills=tuple(f for i in range(len(self.manifest.page_hashes)) for f in self.pages[i].fills)
        if len(fills) != self.manifest.total or _digest(fills) != self.manifest.history_hash:
            raise ValueError('Incomplete or inconsistent history')
        if any(type(f.sequence) is not int for f in fills): raise ValueError('Invalid sequence')
        if any(a.sequence >= b.sequence for a,b in zip(fills,fills[1:])):
            raise ValueError('History out of order')
        if len({f.trade_id for f in fills}) != len(fills): raise ValueError('Duplicate trade ID')
        return fills
