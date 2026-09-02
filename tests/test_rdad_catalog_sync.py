import json
import os
import shutil
from pathlib import Path

import pytest

from agent import rdad_catalog_sync as sync, rdad_refresh as refresh


def config(tmp_path, **overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    identity = tmp_path / 'id_sync'; identity.write_text('fake-private-key', encoding='utf-8')
    destination = tmp_path / 'catalog'; destination.mkdir()
    value = {
        'node_id': 'future-node',
        'rdad_catalog_sync_enabled': True,
        'rdad_catalog_sync_host': 'catalog.internal',
        'rdad_catalog_sync_user': 'root',
        'rdad_catalog_sync_identity_file': str(identity),
        'rdad_catalog_sync_source_root': '/mnt/media/decypharr',
        'rdad_catalog_sync_destination_root': str(destination),
        'rdad_catalog_sync_state_file': str(tmp_path / 'state' / 'sync.json'),
        'rdad_catalog_sync_interval': 300,
        'rdad_catalog_sync_timeout': 45,
    }
    value.update(overrides)
    return value


def test_four_catalogs_use_safe_rsync_links_delete_and_no_mnt(tmp_path):
    calls = []
    engine = sync.CatalogSyncEngine(config(tmp_path), runner=lambda argv, timeout: calls.append((argv, timeout)) or 0,
                                    legacy_probe=lambda: False, clock=lambda: 100)
    result = engine.run_cycle()
    assert result == {'enabled':True, 'attempted':4, 'succeeded':4, 'failed':0}
    assert [call[0][-2].rsplit('/', 2)[-2] for call in calls] == list(sync.LIBRARIES)
    for argv, timeout in calls:
        assert argv[:4] == ['rsync', '-a', '--delete', '--links']
        assert '--' in argv and timeout == 45
        assert '.mnt' not in ' '.join(argv)
        assert argv[-2].startswith('root@catalog.internal:/mnt/media/decypharr/')
    assert not hasattr(sync, 'PlexHTTP')


def test_one_catalog_failure_and_timeout_do_not_block_others(tmp_path):
    calls = []
    def runner(argv, _timeout):
        library = argv[-2].rsplit('/', 2)[-2]; calls.append(library)
        return {'radarr':23, 'sonarr':124}.get(library, 0)
    logs = []
    engine = sync.CatalogSyncEngine(config(tmp_path), runner=runner, legacy_probe=lambda: False,
                                    logger=lambda event, **fields: logs.append((event, fields)), clock=lambda: 100)
    result = engine.run_cycle()
    assert calls == list(sync.LIBRARIES)
    assert result == {'enabled':True, 'attempted':4, 'succeeded':2, 'failed':2}
    failures = {fields['library']:fields['result'] for event, fields in logs if event == 'catalog_sync_failed'}
    assert failures == {'radarr':'rsync_failed', 'sonarr':'timeout'}


def test_cadence_is_persistent_across_agent_restart(tmp_path):
    calls = []; now = [100.0]
    cfg = config(tmp_path)
    make = lambda: sync.CatalogSyncEngine(cfg, runner=lambda argv, timeout: calls.append(argv) or 0,
                                          legacy_probe=lambda: False, clock=lambda: now[0])
    assert make().run_cycle()['attempted'] == 4
    now[0] = 200
    assert make().run_cycle()['reason'] == 'not_due' and len(calls) == 4
    now[0] = 400
    assert make().run_cycle()['attempted'] == 4 and len(calls) == 8
    state = json.loads(Path(cfg['rdad_catalog_sync_state_file']).read_text())
    assert all(state['libraries'][library]['last_success_epoch'] == 400 for library in sync.LIBRARIES)


@pytest.mark.parametrize('override,reason', [
    ({'rdad_catalog_sync_enabled':False}, 'disabled'),
    ({'rdad_catalog_sync_host':'bad;host'}, 'configuration_invalid'),
    ({'rdad_catalog_sync_user':'root -oProxyCommand=x'}, 'configuration_invalid'),
    ({'rdad_catalog_sync_source_root':'/mnt/media/.mnt'}, 'configuration_invalid'),
    ({'rdad_catalog_sync_source_root':'/mnt/media/../etc'}, 'configuration_invalid'),
    ({'rdad_catalog_sync_destination_root':'relative'}, 'configuration_invalid'),
])
def test_disabled_or_unsafe_configuration_never_runs_rsync(tmp_path, override, reason):
    calls = []
    engine = sync.CatalogSyncEngine(config(tmp_path, **override),
        runner=lambda argv, timeout: calls.append(argv) or 0, legacy_probe=lambda: False)
    assert engine.run_cycle()['reason'] == reason
    assert calls == []


def test_missing_identity_destination_and_active_legacy_are_safe(tmp_path):
    calls = []
    cfg = config(tmp_path); Path(cfg['rdad_catalog_sync_identity_file']).unlink()
    engine = sync.CatalogSyncEngine(cfg, runner=lambda argv, timeout: calls.append(argv) or 0,
                                    legacy_probe=lambda: False)
    assert engine.run_cycle()['reason'] == 'identity_unavailable'
    cfg = config(tmp_path / 'second')
    shutil.rmtree(cfg['rdad_catalog_sync_destination_root'])
    engine = sync.CatalogSyncEngine(cfg, runner=lambda argv, timeout: calls.append(argv) or 0,
                                    legacy_probe=lambda: False)
    assert engine.run_cycle()['reason'] == 'destination_root_unavailable'
    cfg = config(tmp_path / 'third')
    engine = sync.CatalogSyncEngine(cfg, runner=lambda argv, timeout: calls.append(argv) or 0,
                                    legacy_probe=lambda: True)
    assert engine.run_cycle()['reason'] == 'legacy_timer_active'
    assert calls == []


def test_unknown_legacy_systemd_state_fails_closed(tmp_path, monkeypatch):
    engine = sync.CatalogSyncEngine(config(tmp_path))
    monkeypatch.setattr(sync.subprocess, 'run', lambda *args, **kwargs: (_ for _ in ()).throw(
        sync.subprocess.TimeoutExpired('systemctl', 10)))
    assert engine.run_cycle()['reason'] == 'legacy_timer_active'


def test_simulated_rsync_preserves_symlinks_deletes_and_handles_empty_library(tmp_path):
    source = tmp_path / 'remote'; source.mkdir()
    cfg = config(tmp_path)
    destination = Path(cfg['rdad_catalog_sync_destination_root'])
    for library in sync.LIBRARIES:
        (source / library).mkdir()
        local = destination / library; local.mkdir()
        (local / 'obsolete.strm').write_text('obsolete')
    movie = source / 'radarr' / 'Iron Man (2008)'; movie.mkdir()
    (movie / 'catalog.strm').write_text('source-entry', encoding='utf-8')
    source_link = movie / 'Iron Man (2008).mkv'
    try:
        os.symlink('/data/.mnt/__all__/Iron.Man.mkv', source_link)
    except OSError:
        source_link = None  # Windows without Developer Mode; --links argv remains asserted separately.

    def simulated_rsync(argv, _timeout):
        library = argv[-2].rsplit('/', 2)[-2]
        src, dst = source / library, Path(argv[-1])
        for child in list(dst.iterdir()):
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
        return 0

    before = refresh.scan_catalog_state(destination)
    result = sync.CatalogSyncEngine(cfg, runner=simulated_rsync,
        legacy_probe=lambda: False, clock=lambda: 100).run_cycle()
    after_add = refresh.scan_catalog_state(destination)
    link = destination / 'radarr' / 'Iron Man (2008)' / 'Iron Man (2008).mkv'
    assert result['succeeded'] == 4
    if source_link is not None:
        assert link.is_symlink() and os.readlink(link) == '/data/.mnt/__all__/Iron.Man.mkv'
    assert not list(destination.rglob('obsolete.strm'))
    assert list((destination / 'sonarr').iterdir()) == []
    added_changes = refresh.extract_changed_top_paths(before, after_add)
    assert {'library':'radarr', 'path':'/data/radarr/Iron Man (2008)'} in added_changes
    shutil.rmtree(movie)
    sync.CatalogSyncEngine(cfg, runner=simulated_rsync,
        legacy_probe=lambda: False, clock=lambda: 400).run_cycle()
    after_delete = refresh.scan_catalog_state(destination)
    assert refresh.extract_changed_top_paths(after_add, after_delete) == [
        {'library':'radarr', 'path':'/data/radarr/Iron Man (2008)'},
    ]


def test_logs_never_expose_host_identity_or_remote_path(tmp_path, capsys):
    cfg = config(tmp_path, rdad_catalog_sync_host='secret-host.internal',
                 rdad_catalog_sync_identity_file=str(tmp_path / 'secret-key-name'))
    Path(cfg['rdad_catalog_sync_identity_file']).write_text('PRIVATE SECRET')
    sync.CatalogSyncEngine(cfg, runner=lambda argv, timeout: 23,
                           legacy_probe=lambda: False, clock=lambda: 100).run_cycle()
    output = capsys.readouterr().out
    assert 'secret-host' not in output and 'secret-key' not in output
    assert '/mnt/media' not in output and 'PRIVATE SECRET' not in output
