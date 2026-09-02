import json
from pathlib import Path

import pytest

from agent import rdad_refresh as refresh
from agent import upgrade_contract


def container(client, node, port, config_root, *, container_id=None, state='running', labels=True, name=None):
    appbox_name = name or f'plex-appb-{client}'
    metadata = {
        'marinos.appbox.type': 'plex',
        'marinos.appbox.id': client,
        'marinos.appbox.node': node,
    } if labels else {}
    return {
        'Id': container_id or f'id-{client}', 'Name': '/' + appbox_name,
        'Config': {'Labels': metadata}, 'State': {'Status': state},
        'NetworkSettings': {'Ports': {'32400/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(port)}]}},
        'Mounts': [{'Type': 'bind', 'Source': str(config_root), 'Destination': '/config'}],
    }


def preferences(root: Path, token='token-value-never-log'):
    path = root / 'Library/Application Support/Plex Media Server/Preferences.xml'
    path.parent.mkdir(parents=True, exist_ok=True)
    value = f' PlexOnlineToken="{token}"' if token is not None else ''
    path.write_text(f'<Preferences{value}/>', encoding='utf-8')
    return path


def sections_xml(ids=None, titles=None, extra_locations=True):
    ids = ids or {'radarr': '1', 'radarr-4k': '2', 'sonarr': '3', 'sonarr-4k': '4'}
    titles = titles or {}
    directories = []
    for library, path in refresh.LIBRARY_PATHS.items():
        locations = f'<Location path="{path}"/>'
        if extra_locations:
            locations = f'<Location path="/NEMESIS/{library}"/>{locations}<Location path="/ATHENA/{library}"/>'
        directories.append(f'<Directory key="{ids[library]}" title="{titles.get(library, library)}">{locations}</Directory>')
    return ('<MediaContainer>' + ''.join(directories) + '</MediaContainer>').encode()


class FakeDocker:
    def __init__(self, items):
        self.items = items
        self.probes = {}
        self.inspect_calls = 0

    def inspect_all(self):
        self.inspect_calls += 1
        return list(self.items)

    def media_probe(self, target, path, mode='readable'):
        return self.probes.get((target.client_id, path), (True, 'readable'))


class FakePlex:
    def __init__(self):
        self.sections = {}
        self.activities = {}
        self.request_errors = {}
        self.refresh_errors = {}
        self.requests = []
        self.refreshes = []

    def request(self, target, path, token):
        self.requests.append((target.client_id, path, token))
        error = self.request_errors.get((target.client_id, path))
        if error:
            raise RuntimeError(error)
        if path == '/library/sections':
            return self.sections.get(target.client_id, sections_xml())
        if path == '/activities':
            return self.activities.get(target.client_id, b'<MediaContainer size="0"/>')
        raise AssertionError(path)

    def refresh(self, target, section, path, token):
        self.refreshes.append((target.client_id, section, path, token))
        error = self.refresh_errors.get(target.client_id)
        if error:
            raise RuntimeError(error)


def make_engine(tmp_path, docker, plex, catalog, *, catalog_interval=0, clock=None,
                catalog_scanner=None):
    logs = []
    engine = refresh.TargetedRefreshEngine({
        'node_id': 'future-node-42', 'rdad_refresh_enabled': True,
        'rdad_refresh_state_dir': str(tmp_path / 'state'),
        'rdad_refresh_catalog_root': str(tmp_path / 'catalog'),
        'rdad_refresh_catalog_interval': catalog_interval,
    }, docker=docker, plex=plex,
       catalog_scanner=catalog_scanner or (lambda _root: dict(catalog)),
       logger=lambda event, **fields: logs.append((event, fields)), clock=clock)
    engine.legacy_runtime_active = lambda: False
    return engine, logs


def queue_state(engine, target):
    return json.loads(engine.store.path(target.identity).read_text(encoding='utf-8'))


def test_discovery_uses_managed_labels_dynamic_ports_mounts_and_local_node(tmp_path):
    one, two, remote = (tmp_path / name for name in ('one', 'two', 'remote'))
    items = [
        container('one', 'future-node-42', 32435, one),
        container('two', 'future-node-42', 32777, two),
        container('three', 'another-node', 32436, remote),
        container('unmanaged', 'future-node-42', 32400, tmp_path / 'other', labels=False,
                  name='unrelated-plex'),
    ]
    targets = refresh.discover_targets(items, 'future-node-42')
    assert [(item.client_id, item.endpoint, item.config_root) for item in targets] == [
        ('one', 'http://127.0.0.1:32435', one),
        ('two', 'http://127.0.0.1:32777', two),
    ]


@pytest.mark.parametrize('node', ['artemis', 'orion', 'demeter', 'future-node-42'])
def test_same_discovery_code_has_no_hostname_branch(tmp_path, node):
    found = refresh.discover_targets([container('one', node, 40123, tmp_path / node)], node)
    assert len(found) == 1 and found[0].node_id == node


def test_legacy_34ah_compatibility_is_explicit_and_removable(tmp_path):
    legacy = container('ignored', 'wrong-node', 32434, tmp_path / 'legacy', labels=False,
                       name='plex-appb-34ah', container_id='legacy-id')
    assert refresh.discover_targets([legacy], 'any-node')[0].legacy is True
    assert refresh.discover_targets([legacy], 'any-node', allow_legacy=False) == []


@pytest.mark.parametrize('library,expected', [
    ('radarr', '8'), ('radarr-4k', '9'), ('sonarr', '12'), ('sonarr-4k', '18'),
])
def test_sections_are_resolved_by_data_location_not_id_or_title(library, expected):
    ids = {'radarr': '8', 'radarr-4k': '9', 'sonarr': '12', 'sonarr-4k': '18'}
    titles = {'radarr': 'My Movies', 'radarr-4k': 'Cinéma UHD', 'sonarr': 'TV FR', 'sonarr-4k': 'Télévision UHD'}
    assert refresh.plex_section_map(sections_xml(ids, titles, extra_locations=True))[library] == expected


def test_section_without_expected_rdad_location_is_not_mapped():
    payload = b'<MediaContainer><Directory key="8" title="My Movies"><Location path="/NEMESIS/movies"/></Directory></MediaContainer>'
    assert refresh.plex_section_map(payload) == {}


def test_invalid_sections_and_activities_are_rejected():
    with pytest.raises(RuntimeError, match='plex_sections_invalid'):
        refresh.plex_section_map(b'<broken')
    with pytest.raises(RuntimeError, match='plex_activities_invalid'):
        refresh.plex_is_busy(b'<broken')
    assert refresh.plex_is_busy(b'<MediaContainer size="1"><Activity/></MediaContainer>') is True
    assert refresh.plex_is_busy(b'<MediaContainer size="0"/>') is False


def test_token_present_absent_and_preferences_missing(tmp_path):
    root = tmp_path / 'config'
    target = refresh.discover_targets([container('one', 'node', 32435, root)], 'node')[0]
    preferences(root, 'secret-value')
    assert refresh.read_plex_token(target) == ('secret-value', 'available')
    preferences(root, None)
    assert refresh.read_plex_token(target) == (None, 'token_missing')
    (root / 'Library/Application Support/Plex Media Server/Preferences.xml').unlink()
    assert refresh.read_plex_token(target) == (None, 'preferences_missing')


def test_structured_logger_redacts_secret_values(tmp_path, capsys):
    engine = refresh.TargetedRefreshEngine({'node_id': 'node', 'rdad_refresh_state_dir': str(tmp_path)})
    engine._log('refresh_failed', client_id='client', result='PlexOnlineToken=do-not-print')
    output = capsys.readouterr().out
    assert 'do-not-print' not in output and '[REDACTED]' in output


def test_changed_top_paths_deduplicate_and_keep_library_boundaries():
    previous = {'radarr/Movie A/old.strm': '1', 'sonarr/Show/episode.strm': '1'}
    current = {
        'radarr/Movie A/one.strm': '2', 'radarr/Movie A/two.strm': '3',
        'sonarr/Show/episode.strm': '1', 'unrelated/file': '9',
    }
    assert refresh.extract_changed_top_paths(previous, current) == [
        {'library': 'radarr', 'path': '/data/radarr/Movie A'},
    ]


def test_catalog_scan_persists_bounded_top_level_fingerprints(tmp_path):
    movie = tmp_path / 'radarr' / 'Movie A'; movie.mkdir(parents=True)
    (movie / 'one.strm').write_text('one'); (movie / 'two.strm').write_text('two')
    show = tmp_path / 'sonarr' / 'Show A'; show.mkdir(parents=True)
    (show / 'episode.strm').write_text('episode')
    first = refresh.scan_catalog_state(tmp_path)
    assert set(first) == {'radarr/Movie A', 'sonarr/Show A'}
    (movie / 'one.strm').write_text('changed')
    second = refresh.scan_catalog_state(tmp_path)
    assert first['radarr/Movie A'] != second['radarr/Movie A']
    assert first['sonarr/Show A'] == second['sonarr/Show A']


def test_two_target_queues_are_independent_and_one_failure_does_not_block_other(tmp_path):
    roots = [tmp_path / 'one', tmp_path / 'two']
    for root in roots:
        preferences(root)
    docker = FakeDocker([
        container('one', 'future-node-42', 32435, roots[0]),
        container('two', 'future-node-42', 32436, roots[1]),
    ])
    plex = FakePlex(); plex.refresh_errors['two'] = 'plex_unreachable'
    catalog = {}
    engine, logs = make_engine(tmp_path, docker, plex, catalog)
    assert engine.run_cycle()['targets'] == 2
    catalog['radarr/Movie/file.strm'] = '1'
    engine.run_cycle()
    targets = refresh.discover_targets(docker.items, 'future-node-42')
    assert queue_state(engine, targets[0])['entries'] == []
    failed = queue_state(engine, targets[1])
    assert len(failed['entries']) == 1 and failed['entries'][0]['last_result'] == 'plex_unreachable'
    assert failed['catalog_timestamps'] == catalog
    engine.run_cycle()
    assert len(queue_state(engine, targets[1])['entries']) == 1
    assert sum(event == 'queue_add' for event, _ in logs) == 2
    assert any(event == 'refresh_success' and fields['client_id'] == 'one' for event, fields in logs)


def test_zero_target_marks_orphans_without_scanning_catalog(tmp_path):
    docker = FakeDocker([]); plex = FakePlex(); scans = []
    engine, logs = make_engine(
        tmp_path, docker, plex, {}, catalog_interval=300,
        catalog_scanner=lambda _root: scans.append(True),
    )
    orphan = engine.store.root / 'targets' / ('a' * 64) / 'queue.json'
    orphan.parent.mkdir(parents=True)
    orphan.write_text(json.dumps({'identity': 'a' * 64, 'entries': [], 'orphaned_at': None}), encoding='utf-8')

    assert engine.run_cycle() == {'enabled': True, 'targets': 0}
    assert scans == []
    assert json.loads(orphan.read_text(encoding='utf-8'))['orphaned_at']
    assert logs[-1] == ('cycle_idle', {'result': 'no_local_target'})


def test_catalog_scan_interval_baseline_and_elapsed_scan(tmp_path):
    root = tmp_path / 'one'; preferences(root)
    docker = FakeDocker([container('one', 'future-node-42', 32435, root)])
    plex = FakePlex(); catalog = {}; now = [100.0]; scans = []

    def scanner(_root):
        scans.append(now[0])
        return dict(catalog)

    engine, _ = make_engine(tmp_path, docker, plex, catalog, catalog_interval=300,
                            clock=lambda: now[0], catalog_scanner=scanner)
    first = engine.run_cycle()
    assert first['catalog_scanned'] is True and scans == [100.0]
    target = refresh.discover_targets(docker.items, 'future-node-42')[0]
    assert queue_state(engine, target)['baseline_complete'] is True

    now[0] = 200.0
    assert engine.run_cycle()['catalog_scanned'] is False
    assert scans == [100.0]

    now[0] = 400.0
    assert engine.run_cycle()['catalog_scanned'] is True
    assert scans == [100.0, 400.0]


def test_new_target_forces_one_shared_scan_and_preserves_existing_target_change(tmp_path):
    one, two = tmp_path / 'one', tmp_path / 'two'
    preferences(one, None); preferences(two, None)
    docker = FakeDocker([container('one', 'future-node-42', 32435, one)])
    plex = FakePlex(); catalog = {}; now = [100.0]; scans = []

    def scanner(_root):
        scans.append(now[0])
        return dict(catalog)

    engine, _ = make_engine(tmp_path, docker, plex, catalog, catalog_interval=300,
                            clock=lambda: now[0], catalog_scanner=scanner)
    engine.run_cycle()
    catalog['radarr/Movie/file.strm'] = '1'
    docker.items.append(container('two', 'future-node-42', 32436, two))
    now[0] = 110.0
    assert engine.run_cycle()['catalog_scanned'] is True
    targets = refresh.discover_targets(docker.items, 'future-node-42')
    assert scans == [100.0, 110.0]
    assert len(queue_state(engine, targets[0])['entries']) == 1
    assert queue_state(engine, targets[1])['entries'] == []
    assert queue_state(engine, targets[1])['catalog_timestamps'] == catalog


def test_two_new_targets_share_one_catalog_scan(tmp_path):
    one, two = tmp_path / 'one', tmp_path / 'two'
    docker = FakeDocker([
        container('one', 'future-node-42', 32435, one),
        container('two', 'future-node-42', 32436, two),
    ])
    scans = []
    engine, _ = make_engine(tmp_path, docker, FakePlex(), {}, catalog_interval=300,
                            clock=lambda: 100.0,
                            catalog_scanner=lambda _root: scans.append(True) or {})
    assert engine.run_cycle()['targets'] == 2
    assert scans == [True]


def test_existing_queue_is_processed_between_catalog_scans_and_after_restart(tmp_path):
    root = tmp_path / 'one'; preferences(root)
    docker = FakeDocker([container('one', 'future-node-42', 32435, root)])
    plex = FakePlex(); plex.refresh_errors['one'] = 'temporary'
    catalog = {}; now = [100.0]; scans = []

    def scanner(_root):
        scans.append(now[0])
        return dict(catalog)

    engine, _ = make_engine(tmp_path, docker, plex, catalog, catalog_interval=300,
                            clock=lambda: now[0], catalog_scanner=scanner)
    engine.run_cycle()
    catalog['sonarr/Show/file.strm'] = '1'; now[0] = 400.0
    engine.run_cycle()
    target = refresh.discover_targets(docker.items, 'future-node-42')[0]
    assert len(queue_state(engine, target)['entries']) == 1

    plex.refresh_errors.clear(); now[0] = 410.0
    restarted, _ = make_engine(tmp_path, docker, plex, catalog, catalog_interval=300,
                               clock=lambda: now[0], catalog_scanner=scanner)
    result = restarted.run_cycle()
    assert result['catalog_scanned'] is False
    assert scans == [100.0, 400.0]
    assert queue_state(restarted, target)['entries'] == []


def test_unclaimed_target_keeps_queue_without_blocking_claimed_target_or_logging_token(tmp_path):
    claimed, unclaimed = tmp_path / 'claimed', tmp_path / 'unclaimed'
    preferences(claimed, 'highly-secret-token'); preferences(unclaimed, None)
    docker = FakeDocker([
        container('claimed', 'future-node-42', 32435, claimed),
        container('unclaimed', 'future-node-42', 32436, unclaimed),
    ])
    plex = FakePlex(); catalog = {}
    engine, logs = make_engine(tmp_path, docker, plex, catalog)
    engine.run_cycle(); catalog['sonarr/Show/file.strm'] = '1'; engine.run_cycle()
    targets = refresh.discover_targets(docker.items, 'future-node-42')
    assert queue_state(engine, targets[0])['entries'] == []
    assert len(queue_state(engine, targets[1])['entries']) == 1
    assert any(event == 'refresh_target_unavailable' and fields['result'] == 'token_missing' for event, fields in logs)
    assert 'highly-secret-token' not in json.dumps(logs)


def test_failure_of_first_target_does_not_prevent_second_target_refresh(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    preferences(first); preferences(second)
    docker = FakeDocker([
        container('first', 'future-node-42', 32435, first),
        container('second', 'future-node-42', 32436, second),
    ])
    plex = FakePlex(); plex.sections['first'] = b'<broken'; catalog = {}
    engine, _ = make_engine(tmp_path, docker, plex, catalog)
    engine.run_cycle(); catalog['sonarr/Show/file.strm'] = '1'; engine.run_cycle()
    assert any(client == 'second' for client, *_ in plex.refreshes)


@pytest.mark.parametrize('failure,expected', [
    ('sections_unreachable', 'sections_unreachable'),
    ('sections_invalid', 'plex_sections_invalid'),
    ('activities_unavailable', 'activities_unavailable'),
    ('plex_busy', 'plex_busy'),
])
def test_plex_failure_modes_preserve_queue(tmp_path, failure, expected):
    root = tmp_path / 'config'; preferences(root)
    docker = FakeDocker([container('one', 'future-node-42', 32435, root)])
    plex = FakePlex(); catalog = {}
    if failure == 'sections_unreachable': plex.request_errors[('one', '/library/sections')] = expected
    if failure == 'sections_invalid': plex.sections['one'] = b'<broken'
    if failure == 'activities_unavailable': plex.request_errors[('one', '/activities')] = expected
    if failure == 'plex_busy': plex.activities['one'] = b'<MediaContainer size="1"><Activity/></MediaContainer>'
    engine, logs = make_engine(tmp_path, docker, plex, catalog)
    engine.run_cycle(); catalog['radarr/Movie/file.strm'] = '1'; engine.run_cycle()
    target = refresh.discover_targets(docker.items, 'future-node-42')[0]
    assert len(queue_state(engine, target)['entries']) == 1
    assert any(fields.get('result') == expected for _, fields in logs)


def test_media_unreadable_and_container_stopped_are_retryable(tmp_path):
    root = tmp_path / 'config'; preferences(root)
    docker = FakeDocker([container('one', 'future-node-42', 32435, root)])
    plex = FakePlex(); catalog = {}
    engine, _ = make_engine(tmp_path, docker, plex, catalog)
    engine.run_cycle(); catalog['radarr/Movie/file.strm'] = '1'
    docker.probes[('one', '/data/radarr/Movie')] = (False, 'container_not_running')
    engine.run_cycle()
    target = refresh.discover_targets(docker.items, 'future-node-42')[0]
    assert queue_state(engine, target)['entries'][0]['last_result'] == 'container_not_running'
    docker.probes[('one', '/data/radarr/Movie')] = (True, 'readable')
    engine.run_cycle()
    assert queue_state(engine, target)['entries'] == []


def test_target_appears_next_cycle_and_removed_identity_queue_becomes_orphan(tmp_path):
    one, two = tmp_path / 'one', tmp_path / 'two'
    preferences(one); preferences(two)
    docker = FakeDocker([container('one', 'future-node-42', 32435, one)])
    plex = FakePlex(); plex.refresh_errors['one'] = 'keep-queued'; catalog = {}
    engine, _ = make_engine(tmp_path, docker, plex, catalog)
    engine.run_cycle()
    docker.items.append(container('two', 'future-node-42', 32436, two))
    catalog['radarr/Movie/file.strm'] = '1'; assert engine.run_cycle()['targets'] == 2
    targets = refresh.discover_targets(docker.items, 'future-node-42')
    assert engine.store.path(targets[1].identity).exists()
    catalog['sonarr/Show/file.strm'] = '2'; plex.refresh_errors['two'] = 'keep-queued'; engine.run_cycle()
    old_identity = targets[0].identity
    docker.items = [docker.items[1]]
    engine.run_cycle()
    old = json.loads(engine.store.path(old_identity).read_text(encoding='utf-8'))
    assert old['entries'] and old['orphaned_at']
    replacement = container('one', 'future-node-42', 32435, one, container_id='new-container-id')
    docker.items.append(replacement); engine.run_cycle()
    new_target = refresh.discover_targets([replacement], 'future-node-42')[0]
    assert new_target.identity != old_identity
    assert queue_state(engine, new_target)['entries'] == []


def test_node_engine_never_discovers_or_contacts_other_node(tmp_path):
    a1, a2, b1 = (tmp_path / name for name in ('a1', 'a2', 'b1'))
    for root in (a1, a2, b1): preferences(root)
    docker = FakeDocker([
        container('a1', 'future-node-42', 32435, a1),
        container('a2', 'future-node-42', 32436, a2),
        container('b1', 'other-node', 32437, b1),
    ])
    plex = FakePlex(); catalog = {}
    engine, _ = make_engine(tmp_path, docker, plex, catalog)
    engine.run_cycle(); catalog['sonarr/Show/file.strm'] = '1'; engine.run_cycle()
    assert {client for client, *_ in plex.refreshes} == {'a1', 'a2'}
    assert not any(client == 'b1' for client, *_ in plex.requests)


def test_active_legacy_timer_prevents_new_engine_from_touching_docker(tmp_path):
    docker = FakeDocker([]); plex = FakePlex(); scans = []
    engine, logs = make_engine(tmp_path, docker, plex, {},
                               catalog_scanner=lambda _root: scans.append(True))
    engine.legacy_runtime_active = lambda: True
    assert engine.run_cycle() == {'enabled': False, 'reason': 'legacy_timer_active', 'targets': 0}
    assert docker.inspect_calls == 0
    assert scans == []
    assert logs[-1][1]['result'] == 'legacy_timer_active'


def test_runtime_media_probe_checks_existence_and_real_read_without_shell_interpolation(tmp_path):
    calls = []
    responses = [(0, '', ''), (0, '', '')]
    def runner(command, timeout=30):
        calls.append((command, timeout))
        return responses.pop(0)
    runtime = refresh.DockerRuntime(runner)
    target = refresh.RefreshTarget('node', 'client', 'container-id', 'plex-appb-client',
                                   'running', 'http://127.0.0.1:32435', tmp_path)
    assert runtime.media_probe(target, '/data/radarr/Movie Name') == (True, 'readable')
    assert calls[0][0][-1] == '/data/radarr/Movie Name'
    assert 'find -L' in calls[1][0][5] and 'bs=1048576' in calls[1][0][5]


def test_force_probe_still_requires_path_but_skips_real_read(tmp_path):
    calls = []
    def runner(command, timeout=30):
        calls.append(command)
        return 0, '', ''
    target = refresh.RefreshTarget('node', 'client', 'container-id', 'plex-appb-client',
                                   'running', 'http://127.0.0.1:32435', tmp_path)
    assert refresh.DockerRuntime(runner).media_probe(target, '/data/radarr/Movie', 'force') == (True, 'force')
    assert len(calls) == 1


def test_missing_published_endpoint_preserves_only_that_target_queue(tmp_path):
    root = tmp_path / 'config'; preferences(root)
    item = container('one', 'future-node-42', 32435, root)
    item['NetworkSettings']['Ports']['32400/tcp'] = None
    docker = FakeDocker([item]); plex = FakePlex(); catalog = {}
    engine, logs = make_engine(tmp_path, docker, plex, catalog)
    engine.run_cycle(); catalog['radarr/Movie/file.strm'] = '1'; engine.run_cycle()
    target = refresh.discover_targets(docker.items, 'future-node-42')[0]
    assert len(queue_state(engine, target)['entries']) == 1
    assert any(fields.get('result') == 'plex_endpoint_unavailable' for _, fields in logs)


def test_agent_package_contract_contains_versioned_refresh_component():
    assert 'rdad_refresh.py' in upgrade_contract.FILES
