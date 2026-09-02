import json
import hashlib
import shutil
import sqlite3
import tarfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "lifecycle.db")
    monkeypatch.setattr(main, "REFERENCE_ROOT", tmp_path / "references")
    main.REFERENCE_ROOT.mkdir()
    main.init_database()
    stamp=main.now_iso()
    with main.db() as con:
        con.execute("INSERT INTO nodes(node_id,name,mode,status,created_at,updated_at) VALUES('uxnode','UXNODE','remote','online',?,?)",(stamp,stamp))
    def image(image_id="plex-one", name="Plex One", versions=("v1",), active="v1"):
        stamp=main.now_iso()
        with main.db() as con:
            con.execute("INSERT INTO reference_images(image_id,name,media_type,description,status,current_version_id,created_at,updated_at,source_node_id) VALUES(?,?,'plex','Test UX','published',NULL,?,?, 'uxnode')",(image_id,name,stamp,stamp))
        ids=[]
        for index,label in enumerate(versions,1):
            source=main.REFERENCE_ROOT/image_id/label; source.mkdir(parents=True)
            snapshot=f"{image_id}-{label}-snapshot"; version_id=f"{image_id}-{label}"; ids.append(version_id)
            with main.db() as con:
                con.execute("INSERT INTO catalog_snapshots(snapshot_id,name,media_type,version,source_path,status,created_at,updated_at) VALUES(?,?,'plex',?,?,'ready',?,?)",(snapshot,label,label,str(source),stamp,stamp))
                con.execute("INSERT INTO reference_image_versions(version_id,image_id,version,snapshot_id,application_version,size_bytes,state,created_at,published_at) VALUES(?,?,?,?, '1.40',1024,'published',?,?)",(version_id,image_id,label,snapshot,stamp,stamp))
        current=ids[list(versions).index(active)] if active in versions else None
        with main.db() as con:
            con.execute("UPDATE reference_images SET current_version_id=? WHERE image_id=?",(current,image_id))
        return ids
    return TestClient(main.app), image


def test_library_multiple_images_has_only_manage_and_deploy(lifecycle):
    client,make=lifecycle; make(); make('plex-two','Plex Two',('r1','r2'),'r2')
    html=client.get('/reference-images').text
    assert html.count('reference-library-card')==2
    assert 'Gérer' in html and 'Déployer' in html and 'Supprimer la référence complète' not in html
    assert '2 référence(s)' in html


def test_detail_distinguishes_active_and_historical_versions(lifecycle):
    client,make=lifecycle; versions=make(versions=('2026-08-27-114','2026-08-30-001'),active='2026-08-30-001')
    html=client.get('/reference-images/plex-one').text
    assert 'ACTIVE' in html and 'HISTORIQUE' in html
    assert 'Créer une nouvelle version' in html
    assert 'Créez ou activez une autre version avant de supprimer celle-ci.' in html
    assert f'/versions/{versions[0]}/delete' in html
    assert 'Zone de danger' in html and 'Supprimer la référence complète' in html


def test_active_blocker_links_to_new_version_workflow(lifecycle):
    client,make=lifecycle; versions=make()
    detail=client.get('/reference-images/plex-one').text
    assert '1 version(s)' in detail and 'ACTIVE' in detail
    html=client.get(f'/reference-images/plex-one/versions/{versions[0]}/delete').text
    assert 'Version active/default' in html
    assert 'href="/reference-images/plex-one/new-version"' in html


def test_job_blocker_links_to_job_detail(lifecycle):
    client,make=lifecycle; versions=make(versions=('old','active'),active='active')
    stamp=main.now_iso()
    with main.db() as con:
        con.execute("INSERT INTO jobs(job_id,node_id,action,title,status,progress,detail,created_at,updated_at,options_json) VALUES('job-ref','uxnode','deploy','Reference use','running',10,'',?,?,?)",
                    (stamp,stamp,json.dumps({'reference_version_id':versions[0]})))
    html=client.get(f'/reference-images/plex-one/versions/{versions[0]}/delete').text
    assert 'Job actif' in html and 'href="/jobs/job-ref"' in html


def test_contextual_wizard_keeps_target_image_id(lifecycle,monkeypatch):
    client,make=lifecycle; make()
    wizard=client.get('/reference-images/plex-one/new-version').text
    assert 'value="plex-one"' in wizard and 'La capture créera une version de cette référence' in wizard
    launched=[]
    monkeypatch.setattr(main,'launch_reference_discovery',lambda build_id,source: launched.append((build_id,source)))
    response=client.post('/reference-images/wizard',data={
        'target_image_id':'plex-one','source_node_id':'uxnode','source_instance':'plex-appb-34ah',
        'source_type':'appbox','application':'plex','name':'Ignored','description':'Ignored'},follow_redirects=False)
    assert response.status_code==303 and response.headers['location'].startswith('/reference-builds/')
    with main.db() as con:
        build=con.execute("SELECT * FROM reference_builds ORDER BY created_at DESC LIMIT 1").fetchone()
    assert build['image_id']=='plex-one' and build['display_name']=='Plex One'
    assert launched==[(build['build_id'],'plex-appb-34ah')]


def test_new_reference_wizard_creates_an_unbound_build(lifecycle,monkeypatch):
    client,_=lifecycle; launched=[]
    monkeypatch.setattr(main,'launch_reference_discovery',lambda build_id,source: launched.append(build_id))
    response=client.post('/reference-images/wizard',data={
        'target_image_id':'','source_node_id':'uxnode','source_instance':'plex-source',
        'source_type':'server','application':'plex','name':'My New Reference','description':'Guided'},
        follow_redirects=False)
    assert response.status_code==303
    with main.db() as con:
        build=con.execute("SELECT * FROM reference_builds ORDER BY created_at DESC LIMIT 1").fetchone()
    assert build['image_id'] is None and build['display_name']=='My New Reference'
    assert launched==[build['build_id']]


def test_new_reference_never_silently_becomes_a_version(lifecycle,monkeypatch):
    client,make=lifecycle; make(image_id='plex-plex-one',name='Plex One')
    monkeypatch.setattr(main,'launch_reference_discovery',lambda *_: None)
    response=client.post('/reference-images/wizard',data={
        'target_image_id':'','source_node_id':'uxnode','source_instance':'plex-source',
        'source_type':'server','application':'plex','name':'Plex One','description':'Duplicate'})
    assert response.status_code==409
    assert 'Créer une nouvelle version' in response.text
    with main.db() as con:
        assert con.execute("SELECT COUNT(*) FROM reference_builds").fetchone()[0]==0


def test_historical_version_can_be_reactivated_with_confirmation(lifecycle):
    client,make=lifecycle; versions=make(versions=('old','active'),active='active')
    assert client.post(f'/reference-images/plex-one/publish/{versions[0]}',data={}).status_code==400
    response=client.post(f'/reference-images/plex-one/publish/{versions[0]}',data={'confirmed':'true'},follow_redirects=False)
    assert response.status_code==303
    with main.db() as con:
        assert con.execute("SELECT current_version_id FROM reference_images WHERE image_id='plex-one'").fetchone()[0]==versions[0]


def test_metadata_and_deploy_preselection(lifecycle):
    client,make=lifecycle; versions=make()
    response=client.post('/reference-images/plex-one/metadata',data={'name':'Plex Renamed','description':'Updated'},follow_redirects=False)
    assert response.status_code==303
    assert 'Plex Renamed' in client.get('/reference-images/plex-one').text
    html=client.get(f'/appboxes?deployment_image_id=reference:{versions[0]}').text
    assert f'value="reference:{versions[0]}"' in html
    assert f'value="reference:{versions[0]}" data-type="plex" selected' in html


def test_reference_routes_are_unique():
    signatures=[]
    for route in main.app.routes:
        if getattr(route,'path','').startswith('/reference-images') or getattr(route,'path','').startswith('/reference-builds'):
            for method in getattr(route,'methods',set()):
                signatures.append((method,route.path))
    assert len(signatures)==len(set(signatures))


def test_reference_layout_has_mobile_breakpoint():
    css=(Path(__file__).parents[1]/'app/static/app.css').read_text(encoding='utf-8')
    js=(Path(__file__).parents[1]/'app/static/app.js').read_text(encoding='utf-8')
    assert '@media(max-width:620px)' in css
    assert 'data-reference-wizard' in js and 'reportValidity' in js


def test_reference_retire_hides_catalogue_preserves_dependencies_and_republishes(lifecycle):
    client, make = lifecycle
    version_id = make()[0]
    with main.db() as con:
        checksum_before = con.execute("SELECT checksum FROM reference_image_versions WHERE version_id=?", (version_id,)).fetchone()[0]
        stamp = main.now_iso()
        con.execute("""INSERT INTO appboxes(client_id,node_id,path,status,containers_json,reference_image_id,
            reference_version_id,created_at,updated_at) VALUES('existing-box','uxnode','/tmp/existing','running','[]',
            'plex-one',?,?,?)""", (version_id, stamp, stamp))
    assert client.post('/reference-images/plex-one/retire', data={}).status_code == 400
    response = client.post('/reference-images/plex-one/retire', data={'confirmed':'true'}, follow_redirects=False)
    assert response.status_code == 303
    assert all(item.get('reference_version_id') != version_id for item in main.deployment_images('plex'))
    assert client.post('/reference-images/plex-one/retire', data={'confirmed':'true'}, follow_redirects=False).status_code == 303
    with pytest.raises(HTTPException) as exc:
        main.parse_deployment_image(f'reference:{version_id}', 'plex')
    assert exc.value.status_code == 409
    with main.db() as con:
        image = con.execute("SELECT status,current_version_id FROM reference_images WHERE image_id='plex-one'").fetchone()
        box = con.execute("SELECT status,reference_version_id FROM appboxes WHERE client_id='existing-box'").fetchone()
    assert tuple(image) == ('retired', version_id)
    assert tuple(box) == ('running', version_id)
    assert client.post('/reference-images/plex-one/republish', data={'confirmed':'true'}, follow_redirects=False).status_code == 303
    with main.db() as con:
        image = con.execute("SELECT status,current_version_id FROM reference_images WHERE image_id='plex-one'").fetchone()
        checksum_after = con.execute("SELECT checksum FROM reference_image_versions WHERE version_id=?", (version_id,)).fetchone()[0]
    assert tuple(image) == ('published', version_id) and checksum_after == checksum_before


def test_retired_delete_preflight_and_ui_actions(lifecycle):
    client, make = lifecycle
    make()
    client.post('/reference-images/plex-one/retire', data={'confirmed':'true'})
    preview = client.get('/api/reference-images/plex-one/deletion').json()
    assert not any('active/publiée' in item for item in preview['blockers'])
    html = client.get('/reference-images/plex-one').text
    assert 'RETIRÉE' in html and 'Republier' in html and 'Supprimer définitivement' in html


def test_retire_is_catalogue_only_and_preserves_archive_cache_and_version_state(lifecycle):
    client, make = lifecycle
    version_id = make()[0]
    archive = main.REFERENCE_ROOT / 'preserved.tar.gz'
    archive.write_bytes(b'preserved bytes')
    stamp = main.now_iso()
    with main.db() as con:
        con.execute("UPDATE reference_image_versions SET archive_path=?,checksum=? WHERE version_id=?",
                    (str(archive), hashlib.sha256(archive.read_bytes()).hexdigest(), version_id))
        con.execute("""INSERT INTO node_reference_cache(node_id,version_id,local_path,checksum,status,size_bytes,updated_at)
            VALUES('uxnode',?,'/agent/cache.tar.gz',?,'ready',10,?)""", (version_id, 'a'*64, stamp))
    assert client.post('/reference-images/plex-one/retire', data={'confirmed':'true'}, follow_redirects=False).status_code == 303
    assert archive.read_bytes() == b'preserved bytes'
    with main.db() as con:
        assert con.execute("SELECT state FROM reference_image_versions WHERE version_id=?", (version_id,)).fetchone()[0] == 'published'
        assert con.execute("SELECT 1 FROM node_reference_cache WHERE node_id='uxnode' AND version_id=?", (version_id,)).fetchone()


def test_republish_rejects_missing_current_archive_and_source(lifecycle):
    client, make = lifecycle
    version_id = make()[0]
    client.post('/reference-images/plex-one/retire', data={'confirmed':'true'})
    with main.db() as con:
        source = Path(con.execute("""SELECT s.source_path FROM reference_image_versions v
            JOIN catalog_snapshots s ON s.snapshot_id=v.snapshot_id WHERE v.version_id=?""", (version_id,)).fetchone()[0])
    source.rmdir()
    response = client.post('/reference-images/plex-one/republish', data={'confirmed':'true'})
    assert response.status_code == 409


def test_republish_accepts_immutable_archive_without_source(lifecycle):
    client, make = lifecycle
    version_id = make()[0]
    with main.db() as con:
        source = Path(con.execute("""SELECT s.source_path FROM reference_image_versions v
            JOIN catalog_snapshots s ON s.snapshot_id=v.snapshot_id WHERE v.version_id=?""", (version_id,)).fetchone()[0])
    plex = source / 'Library' / 'Application Support' / 'Plex Media Server'
    (plex / 'Metadata').mkdir(parents=True)
    (plex / 'Media').mkdir()
    database = plex / 'Plug-in Support' / 'Databases' / 'com.plexapp.plugins.library.db'
    database.parent.mkdir(parents=True)
    database_connection = sqlite3.connect(database)
    try:
        con = database_connection
        con.execute('CREATE TABLE metadata_items(id INTEGER PRIMARY KEY)')
        con.commit()
    finally:
        database_connection.close()
    (plex / 'Preferences.xml').write_text('<Preferences/>', encoding='utf-8')
    archive = main.REFERENCE_ROOT / 'archive-only.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        output.add(source / 'Library', arcname='Library')
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    with main.db() as con:
        con.execute("UPDATE reference_image_versions SET archive_path=?,checksum=? WHERE version_id=?", (str(archive), checksum, version_id))
    shutil.rmtree(source)
    client.post('/reference-images/plex-one/retire', data={'confirmed':'true'})
    assert client.post('/reference-images/plex-one/republish', data={'confirmed':'true'}, follow_redirects=False).status_code == 303
    assert main.parse_deployment_image(f'reference:{version_id}', 'plex') == ('plex-one', version_id)


def test_republish_rejects_missing_current_and_corrupt_archive(lifecycle):
    client, make = lifecycle
    version_id = make()[0]
    client.post('/reference-images/plex-one/retire', data={'confirmed':'true'})
    with main.db() as con:
        con.execute("UPDATE reference_images SET current_version_id=NULL WHERE image_id='plex-one'")
    assert client.post('/reference-images/plex-one/republish', data={'confirmed':'true'}).status_code == 409
    with main.db() as con:
        con.execute("UPDATE reference_images SET current_version_id=? WHERE image_id='plex-one'", (version_id,))
        source = Path(con.execute("""SELECT s.source_path FROM reference_image_versions v JOIN catalog_snapshots s
            ON s.snapshot_id=v.snapshot_id WHERE v.version_id=?""", (version_id,)).fetchone()[0])
    archive = main.REFERENCE_ROOT / 'corrupt.tar.gz'
    archive.write_bytes(b'not a tar archive')
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    shutil.rmtree(source)
    with main.db() as con:
        con.execute("UPDATE reference_image_versions SET archive_path=?,checksum=? WHERE version_id=?", (str(archive), checksum, version_id))
    assert client.post('/reference-images/plex-one/republish', data={'confirmed':'true'}).status_code == 409


def test_republish_already_published_is_idempotent(lifecycle):
    client, make = lifecycle
    version_id = make()[0]
    response = client.post('/reference-images/plex-one/republish', data={'confirmed':'true'}, follow_redirects=False)
    assert response.status_code == 303
    with main.db() as con:
        assert tuple(con.execute("SELECT status,current_version_id FROM reference_images WHERE image_id='plex-one'").fetchone()) == ('published', version_id)
