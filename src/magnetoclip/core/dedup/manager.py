"""DedupManager: indexes downloaded files and detects duplicates.

The manager owns the ``file_hashes`` index. At add-time we run a cheap
size+filename check (``check_by_attrs``); once a file is on disk we hash it
(``check_file`` / ``scan_directory``) to confirm exact duplicates, and we
register completed downloads in the index (``index_download``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from ...core.events.bus import Events
from .hasher import digest_file_async, ensure_supported
from .types import DedupStats, DuplicateResult


class DedupManager:
    def __init__(self, context, session_factory: sessionmaker) -> None:
        self.context = context
        self.session_factory = session_factory
        self.events = context.events
        self._algo = ""

    # ----- configuration -----

    def _algo_name(self) -> str:
        if not self._algo:
            configured = str(
                self.context.settings.get("dedup.hash_algo", "xxh3_128")
            )
            self._algo = ensure_supported(configured)
        return self._algo

    def _enabled(self) -> bool:
        return bool(self.context.settings.get("dedup.enabled", True))

    def _only_same_filename(self) -> bool:
        return bool(self.context.settings.get("dedup.only_same_filename", True))

    # ----- queries -----

    def _repo(self, session):
        from ...database.repositories import FileHashRepository

        return FileHashRepository(session)

    def count(self) -> int:
        with self.session_factory() as session:
            return self._repo(session).count()

    def stats(self) -> DedupStats:
        with self.session_factory() as session:
            repo = self._repo(session)
            stats = DedupStats(indexed_files=repo.count())
            rows = repo.list(limit=100000)
            size_groups: set[int] = set()
            for row in rows:
                stats.indexed_bytes += row.size or 0
                size_groups.add(row.size or 0)
            stats.size_groups = len(size_groups)
            return stats

    def names_with_size(self, size: int) -> list[str]:
        """Names of indexed files that are exactly *size* bytes."""
        with self.session_factory() as session:
            repo = self._repo(session)
            return [row.filename for row in repo.by_size(size)]

    def check_by_attrs(
        self,
        filename: str,
        size: int | None,
    ) -> DuplicateResult:
        """Cheap pre-download check: any same-size (and, by default, same-name)
        file already present in the index."""
        result = DuplicateResult()
        if not self._enabled() or not size:
            return result
        with self.session_factory() as session:
            matches = self._repo(session).by_size(int(size))
        if not matches:
            return result
        result.size_match = True
        # Prefer an exact same-name match; otherwise use a same-size match.
        for row in matches:
            if self._only_same_filename() and row.filename != filename:
                continue
            result.is_duplicate = True
            result.existing_path = row.file_path
            result.existing_filename = row.filename
            result.existing_size = row.size
            result.existing_categories = self._category_names(row.category_id)
            break
        if not result.is_duplicate and not self._only_same_filename() and matches:
            row = matches[0]
            result.existing_path = row.file_path
            result.existing_filename = row.filename
            result.existing_size = row.size
            result.existing_categories = self._category_names(row.category_id)
        return result

    def check_file(
        self,
        path: Path | str,
        *,
        filename: str | None = None,
    ) -> DuplicateResult:
        """Hash an on-disk *path* and look up exact content duplicates."""
        result = DuplicateResult()
        if not self._enabled():
            return result
        path = Path(path)
        if not path.is_file():
            return result
        digest = self._blocking_digest(path)
        if digest is None:
            return result
        with self.session_factory() as session:
            matches = self._repo(session).by_hash_all(
                self._algo_name(), digest
            )
        if not matches:
            return result
        result.is_duplicate = True
        result.size_match = True
        result.existing_path = matches[0].file_path
        result.existing_filename = matches[0].filename
        result.existing_size = matches[0].size
        result.existing_categories = self._category_names(matches[0].category_id)
        return result

    def check_bytes(
        self,
        data: bytes,
        *,
        filename: str | None = None,
    ) -> DuplicateResult:
        """Hash in-memory *data* (e.g. a browser capture) and look up exact
        content duplicates in the index."""
        result = DuplicateResult()
        if not self._enabled() or not data:
            return result
        from .hasher import digest_bytes

        digest = digest_bytes(data, self._algo_name())
        with self.session_factory() as session:
            matches = self._repo(session).by_hash_all(
                self._algo_name(), digest
            )
        if not matches:
            return result
        result.is_duplicate = True
        result.size_match = True
        result.existing_path = matches[0].file_path
        result.existing_filename = matches[0].filename
        result.existing_size = matches[0].size
        result.existing_categories = self._category_names(matches[0].category_id)
        return result

    # ----- indexing -----

    def index_file(
        self,
        path: Path | str,
        *,
        filename: str | None = None,
        category_id: int | None = None,
    ) -> bool:
        """Hash *path* and add it to the index. Returns True on success."""
        if not self._enabled():
            return False
        path = Path(path)
        if not path.is_file():
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size <= 0:
            return False
        digest = self._blocking_digest(path)
        if digest is None:
            return False
        name = filename or path.name
        with self.session_factory() as session:
            repo = self._repo(session)
            if repo.by_hash(self._algo_name(), digest) is None:
                repo.add(
                    str(path),
                    name,
                    size,
                    self._algo_name(),
                    digest,
                    category_id=category_id,
                )
        self.events.post(Events.DEDUP_INDEXED, {"path": str(path), "size": size})
        return True

    def index_download(self, download: Any) -> bool:
        """Index a completed download's on-disk file."""
        if not self._enabled() or self.context.settings.get(
            "dedup.index_after_download", True
        ) is False:
            return False
        path = getattr(download, "save_path", None)
        if not path:
            return False
        filename = getattr(download, "filename", None) or Path(path).name
        return self.index_file(
            path,
            filename=filename,
            category_id=getattr(download, "category_id", None),
        )

    def index_file_and_check(
        self,
        path: Path | str,
        filename: str | None = None,
        category_id: int | None = None,
    ) -> DuplicateResult:
        """Index *path* and return a ``DuplicateResult`` describing whether the
        exact content was already present under a different location."""
        result = DuplicateResult()
        if not self._enabled():
            return result
        path = Path(path)
        if not path.is_file():
            return result
        try:
            size = path.stat().st_size
        except OSError:
            return result
        if size <= 0:
            return result
        digest = self._blocking_digest(path)
        if digest is None:
            return result
        name = filename or path.name
        with self.session_factory() as session:
            repo = self._repo(session)
            existing = repo.by_hash(self._algo_name(), digest)
            if existing is not None and existing.file_path != str(path):
                result.is_duplicate = True
                result.size_match = True
                result.existing_path = existing.file_path
                result.existing_filename = existing.filename
                result.existing_size = existing.size
                result.existing_categories = self._category_names(
                    existing.category_id
                )
            if existing is None:
                repo.add(
                    str(path),
                    name,
                    size,
                    self._algo_name(),
                    digest,
                    category_id=category_id,
                )
        self.events.post(Events.DEDUP_INDEXED, {"path": str(path), "size": size})
        return result

    def forget(self, file_path: str) -> int:
        """Remove *file_path* from the index (called when a download is deleted)."""
        with self.session_factory() as session:
            return self._repo(session).remove_by_file_path(file_path)

    def prune(self) -> int:
        """Drop index entries whose file no longer exists. Returns removed count."""
        removed = 0
        with self.session_factory() as session:
            repo = self._repo(session)
            for row in repo.list(limit=100000):
                if not Path(row.file_path).is_file():
                    repo.remove(row)
                    removed += 1
        return removed

    async def scan_directory(self, path: Path | str) -> int:
        """Index every regular file under *path* not already indexed.

        Returns the number of newly indexed files.
        """
        root = Path(path)
        if not root.is_dir():
            return 0
        known: set[str] = set()
        with self.session_factory() as session:
            repo = self._repo(session)
            for row in repo.list(limit=100000):
                known.add(str(Path(row.file_path)))
        added = 0
        for candidate in root.rglob("*"):
            if not candidate.is_file() or str(candidate) in known:
                continue
            try:
                if candidate.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            if await asyncio.to_thread(self.index_file, candidate):
                added += 1
        return added

    # ----- helpers -----

    def _blocking_digest(self, path: Path) -> str | None:
        """Run the blocking digest, deferring to a thread pool if the event
        loop is active so we never stall the UI thread."""
        from .hasher import digest_file

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            return asyncio.run_coroutine_threadsafe(
                digest_file_async(path, self._algo_name()), loop
            ).result()
        try:
            return digest_file(path, self._algo_name())
        except OSError:
            return None

    def _category_names(self, category_id: int | None) -> list[str]:
        if category_id is None:
            return []
        categories = getattr(self.context, "categories", None)
        if categories is None:
            return []
        try:
            cat = categories.get(category_id)
        except Exception:  # noqa: BLE001 - category lookup is best-effort
            return []
        return [cat.name] if cat is not None else []
