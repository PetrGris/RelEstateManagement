import json
import os
import sqlite3
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Consistent SQLite backup plus private documents. Pause writes/uploads while backing up."

    def add_arguments(self, parser):
        parser.add_argument("destination", help="New ZIP file path outside the checkout")

    def handle(self, *args, **options):
        target = Path(options["destination"]).resolve()
        if target.is_relative_to(settings.BASE_DIR):
            raise CommandError("Store backups outside the Git checkout.")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as output, tempfile.TemporaryDirectory() as tmp:
                database = Path(tmp) / "db.sqlite3"
                with (
                    sqlite3.connect(settings.DATABASES["default"]["NAME"]) as source,
                    sqlite3.connect(database) as dest,
                ):
                    source.backup(dest)
                    if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise CommandError("Database integrity check failed.")
                with ZipFile(output, "w", ZIP_DEFLATED) as archive:
                    archive.write(database, "db.sqlite3")
                    media = Path(settings.MEDIA_ROOT)
                    if media.exists():
                        for file in media.rglob("*"):
                            if file.is_file() and not file.is_symlink():
                                archive.write(file, "media/" + file.relative_to(media).as_posix())
                    archive.writestr(
                        "manifest.json",
                        json.dumps(
                            {
                                "created": timezone.now().isoformat(),
                                "database": "SQLite",
                                "documents": "media/",
                            }
                        ),
                    )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        self.stdout.write(self.style.SUCCESS("Backup saved: " + str(target)))
