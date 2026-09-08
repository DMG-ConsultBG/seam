# -*- coding: utf-8 -*-
"""Open an encrypted Seam backup.

`SEAM_BACKUP_PASSPHRASE` makes `make_backup()` write `seam-backup-*.zip.enc`
instead of a plain archive. Until this file existed there was no way to open
one again: the format lives in app.py, the instructions live inside the
archive you cannot read yet, and the operator finds all of this out on the one
day it matters. An encryption with no supported way back is not a safeguard,
it is a second failure waiting behind the first.

    python restore_backup.py backups/seam-backup-20260903T111445Z.zip.enc

Writes the plain .zip next to it. The passphrase is read from
SEAM_BACKUP_PASSPHRASE, or from a file given with --passphrase-file, or asked
for at the terminal. It is never taken as a command-line argument: arguments
end up in shell history and in the process list, where other users can read
them.

Nothing here needs the app to be running, and nothing here touches the live
database. It reads one file and writes another.
"""
import argparse
import getpass
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cryptobox                                             # noqa: E402

MAGIC = b"SEAMBK01"
SALT_LEN = 16


def read_passphrase(from_file=None):
    if from_file:
        with open(from_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    env = os.environ.get("SEAM_BACKUP_PASSPHRASE", "").strip()
    if env:
        return env
    if not sys.stdin.isatty():
        sys.exit("No passphrase. Set SEAM_BACKUP_PASSPHRASE or pass "
                 "--passphrase-file, or run this where a terminal can ask.")
    return getpass.getpass("Backup passphrase: ")


def decrypt(blob, passphrase):
    """The archive bytes, or None when the passphrase is wrong.

    A wrong passphrase and a corrupted file give the same answer on purpose:
    the tag check cannot tell them apart, and pretending otherwise would be
    guessing.
    """
    if not blob.startswith(MAGIC):
        return None
    salt = blob[len(MAGIC):len(MAGIC) + SALT_LEN]
    body = blob[len(MAGIC) + SALT_LEN:]
    key = cryptobox.derive(passphrase, salt)
    return cryptobox.open_box(key, body, MAGIC)


def main():
    ap = argparse.ArgumentParser(description="Open an encrypted Seam backup.")
    ap.add_argument("archive", help="the .zip.enc file")
    ap.add_argument("-o", "--out", help="where to write the plain .zip "
                                        "(default: next to the input)")
    ap.add_argument("--passphrase-file", help="file holding the passphrase")
    ap.add_argument("--check", action="store_true",
                    help="verify it opens and list what is inside, write nothing")
    args = ap.parse_args()

    if not os.path.exists(args.archive):
        sys.exit("No such file: %s" % args.archive)
    with open(args.archive, "rb") as f:
        blob = f.read()

    if not blob.startswith(MAGIC):
        # A plain archive is a legitimate thing to be handed; say so rather
        # than failing at it.
        try:
            zipfile.ZipFile(args.archive).close()
            sys.exit("This archive is not encrypted - open it as a normal zip.")
        except zipfile.BadZipFile:
            sys.exit("Not a Seam backup: no %s header and not a zip either."
                     % MAGIC.decode())

    plain = decrypt(blob, read_passphrase(args.passphrase_file))
    if plain is None:
        sys.exit("Wrong passphrase, or the file has been altered. "
                 "(The two cannot be told apart, by design.)")

    out = args.out or args.archive[:-4] if args.archive.endswith(".enc") else \
        (args.out or args.archive + ".zip")
    if args.check:
        import io
        z = zipfile.ZipFile(io.BytesIO(plain))
        names = z.namelist()
        print("Opens correctly. %d entries, %.1f MB." % (len(names), len(plain) / 1048576.0))
        print("  database : %s" % ("seam.db" if "seam.db" in names else "MISSING"))
        print("  uploads  : %d files" % sum(1 for n in names if n.startswith("uploads/")))
        bad = z.testzip()
        print("  integrity: %s" % ("ok" if bad is None else "corrupt entry: %s" % bad))
        return 0 if bad is None and "seam.db" in names else 1

    if os.path.exists(out):
        sys.exit("Refusing to overwrite %s" % out)
    with open(out, "wb") as f:
        f.write(plain)
    print("Written: %s (%.1f MB)" % (out, len(plain) / 1048576.0))
    print("Open it as a normal zip; RESTORE.txt inside says what to do next.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
