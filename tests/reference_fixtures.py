"""Synthetic references only; never reads a real Plex installation."""
import sqlite3
import tarfile
from pathlib import Path
from agent.reference_contract import PLEX_ROOT


def reference_archive(root):
    config = Path(root) / 'fixture-config'
    plex = config / PLEX_ROOT
    for name in ('Metadata', 'Media', 'Plug-in Support/Databases'):
        (plex / name).mkdir(parents=True, exist_ok=True)
    (plex / 'Metadata/poster').write_bytes(b'poster')
    (plex / 'Media/index').write_bytes(b'index')
    (plex / 'Preferences.xml').write_text('<Preferences Language="fr"/>', encoding='utf-8')
    database = plex / 'Plug-in Support/Databases/com.plexapp.plugins.library.db'
    con = sqlite3.connect(database)
    try:
        con.execute('CREATE TABLE IF NOT EXISTS library_sections(id INTEGER, name TEXT)')
        con.execute("INSERT INTO library_sections VALUES(1, 'Films')")
        con.commit()
    finally:
        con.close()
    archive = Path(root) / 'fixture.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(config / 'Library', arcname='Library')
    return archive
