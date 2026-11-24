"""A filesystem gallery for one QQ group, with atomic publication of each batch."""
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing, contextmanager


class Gallery:
    def __init__(self, root, scope):
        self.path = Path(root) / hashlib.sha256(scope.encode()).hexdigest()

    @contextmanager
    def locked(self):
        self.path.mkdir(parents=True, exist_ok=True)
        # A SQLite write lock works across plugin reloads and processes, and
        # is automatically released by the OS if the process exits.
        with closing(sqlite3.connect(self.path / "lock.sqlite3", timeout=120)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield

    def save(self, key, images):
        batch = hashlib.sha256(key.encode()).hexdigest()
        with self.locked():
            live = self.path / "images"
            live.mkdir(exist_ok=True)
            if (live / batch).exists():
                return
            staging = self.path / ".pending"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir()
            try:
                for index, data in enumerate(images, 1):
                    with (staging / f"{index:03d}.png").open("xb") as output:
                        output.write(data)
                os.rename(staging, live / batch)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

    def pack(self, output):
        with self.locked():
            live = self.path / "images"
            files = sorted(live.glob("*/*.png"))
            if not files:
                raise ValueError("本群图库是空的。")
            # PNG is already compressed. Allow room for ZIP headers as well.
            required = sum(p.stat().st_size + 512 for p in files) + 128 * 1024 * 1024
            if shutil.disk_usage(self.path).free < required:
                raise ValueError("VPS 临时空间不足，无法打包本群图库。原图仍在图库中。")
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
                for entry in files:
                    archive.write(entry, f"{entry.parent.name}-{entry.name}")
            output.seek(0)
            return len(files)

    def archive(self):
        output = tempfile.TemporaryFile(dir=self.path.parent)
        try:
            return output, self.pack(output)
        except BaseException:
            output.close()
            raise

    def clear(self):
        with self.locked():
            live = self.path / "images"
            trash = self.path / ".deleting"
            staging = self.path / ".pending"
            if staging.exists():
                shutil.rmtree(staging)
            if trash.exists():
                shutil.rmtree(trash)
            if not live.exists():
                return 0
            count = sum(1 for _ in live.glob("*/*.png"))
            os.rename(live, trash)
            shutil.rmtree(trash)
            return count
