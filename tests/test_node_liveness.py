import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack

from fastapi import HTTPException
from app import main


class Request:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class NodeLivenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = patch.object(main, 'DB_FILE', Path(self.tmp.name) / 'test.db')
        self.hostname = patch.object(main, 'HOSTNAME', 'cronos')
        self.database.start()
        self.hostname.start()
        main.init_database()
        self.now = datetime.now(timezone.utc)
        stamp = self.now.isoformat()
        with main.db() as con:
            con.execute("INSERT INTO nodes(node_id,name,mode,status,rdad_ok,created_at,updated_at) VALUES('testnode','TEST','remote','online',1,?,?)", (stamp, stamp))
            con.execute("INSERT INTO node_agents(node_id,status,last_heartbeat,capabilities_json,updated_at) VALUES('testnode','online',?, ?,?)", (stamp, json.dumps({'deployment_executor': True}), stamp))
            con.execute("INSERT OR IGNORE INTO node_tag_assignments(node_id,tag_id,assigned_at) VALUES('testnode','appbox-node',?)", (stamp,))
            main.store_agent_metrics(con, 'testnode', {'docker_ok': True, 'rdad_present': True, 'cpu_count': 8}, 'test', stamp)

    def tearDown(self):
        self.hostname.stop()
        self.database.stop()
        self.tmp.cleanup()

    def node(self):
        return next(n for n in main.list_control_nodes() if n['node_id'] == 'testnode')

    def set_age(self, seconds):
        with main.db() as con:
            con.execute("UPDATE node_agents SET last_heartbeat=? WHERE node_id='testnode'", ((self.now - timedelta(seconds=seconds)).isoformat(),))

    def test_exact_boundaries_and_invalid_timestamps(self):
        for timeout in (180, 45):
            with patch.object(main, 'AGENT_ONLINE_SECONDS', timeout):
                for age, status in ((0, 'online'), (timeout-.001, 'online'), (timeout, 'online'), (timeout+.001, 'offline'), (-1, 'unknown')):
                    with self.subTest(timeout=timeout, age=age):
                        stamp = (self.now - timedelta(seconds=age)).isoformat()
                        result = main.heartbeat_liveness(stamp, now=self.now)
                        self.assertEqual(result['status'], status)
                        self.assertEqual(result['agent_online'], status == 'online')
                for value in (None, '', 'invalid', '2026-01-01T00:00:00', 123):
                    self.assertEqual(main.heartbeat_liveness(value, now=self.now)['status'], 'unknown')
                    self.assertEqual(main.heartbeat_liveness(value, maintenance=True, now=self.now)['status'], 'maintenance')

    def test_persisted_online_never_overrides_expired_heartbeat(self):
        self.set_age(181)
        node = self.node()
        self.assertEqual(node['persisted_status'], 'online')
        self.assertEqual(node['status'], 'offline')
        self.assertEqual(node['agent_status'], 'offline')
        self.assertFalse(node['actionable'])
        api = json.loads(main.api_node_status('testnode').body)
        self.assertEqual(api['status'], 'offline')
        self.assertFalse(api['provisioning_allowed'])
        for mode in ('manual', 'automatic'):
            with self.assertRaises(HTTPException):
                main.evaluate_placement(mode, 'testnode')

    def test_maintenance_overrides_recent_heartbeat_and_cannot_be_cleared_by_heartbeat(self):
        with main.db() as con:
            con.execute("UPDATE nodes SET maintenance=1 WHERE node_id='testnode'")
        with patch.object(main, 'authenticate_agent'):
            asyncio.run(main.agent_heartbeat('testnode', Request({'agent_version':'test', 'capabilities':{'deployment_executor': True}})))
        node = self.node()
        self.assertEqual(node['status'], 'maintenance')
        self.assertTrue(node['agent_online'])
        self.assertFalse(node['provisioning_allowed'])
        with self.assertRaises(HTTPException):
            main.evaluate_placement('manual', 'testnode')

    def test_online_placement_and_cronos_exclusion(self):
        self.assertEqual(main.evaluate_placement('manual', 'testnode')['selected']['node_id'], 'testnode')
        self.assertEqual(main.evaluate_placement('automatic', None)['selected']['node_id'], 'testnode')
        with self.assertRaises(HTTPException):
            main.evaluate_placement('manual', 'cronos')
        cronos = json.loads(main.api_node_status('cronos').body)
        self.assertEqual(cronos['liveness_reason'], 'local_control_plane')
        self.assertFalse(cronos['actionable'])

    def test_bare_metal_is_never_automatic_and_manual_requires_confirmation(self):
        with main.db() as con:
            con.execute("INSERT INTO node_tag_assignments(node_id,tag_id,assigned_at) VALUES('testnode','bare-metal',?)", (main.now_iso(),))
        node = self.node()
        self.assertEqual(node['status'], 'online')
        self.assertFalse(node['automatic_placement_allowed'])
        self.assertIn('bare-metal', node['automatic_placement_block_reason'].lower())
        with self.assertRaises(HTTPException) as rejected:
            main.evaluate_placement('manual', 'testnode')
        self.assertIn('confirmation', rejected.exception.detail.lower())
        self.assertEqual(
            main.evaluate_placement('manual', 'testnode', allow_bare_metal_override=True)['selected']['node_id'],
            'testnode',
        )
        api = json.loads(main.api_node_status('testnode').body)
        self.assertFalse(api['automatic_placement_allowed'])
        self.assertIn('bare-metal', api['automatic_placement_block_reason'].lower())

    def test_light_heartbeat_leaves_metrics_and_history_unchanged(self):
        with main.db() as con:
            before = dict(con.execute("SELECT * FROM agent_node_metrics WHERE node_id='testnode'").fetchone())
            count = con.execute("SELECT count(*) FROM node_metrics WHERE node_id='testnode'").fetchone()[0]
        with patch.object(main, 'authenticate_agent'):
            asyncio.run(main.agent_heartbeat('testnode', Request({'agent_version':'test', 'capabilities':{}})))
        with main.db() as con:
            after = dict(con.execute("SELECT * FROM agent_node_metrics WHERE node_id='testnode'").fetchone())
            self.assertEqual(before, after)
            self.assertEqual(count, con.execute("SELECT count(*) FROM node_metrics WHERE node_id='testnode'").fetchone()[0])

    def test_metrics_do_not_refresh_liveness_and_ignore_older_samples(self):
        self.set_age(181)
        with patch.object(main, 'authenticate_agent'):
            asyncio.run(main.agent_metrics('testnode', Request({'collected_at':main.now_iso(), 'metrics':{'cpu_count':16, 'rdad_present':True}})))
        self.assertEqual(self.node()['status'], 'offline')
        with main.db() as con:
            main.store_agent_metrics(con, 'testnode', {'cpu_count':99}, 'test', (self.now-timedelta(seconds=300)).isoformat())
        self.assertEqual(self.node()['cpu_count'], 16)

    def stale_metrics(self):
        with main.db() as con:
            con.execute("UPDATE agent_node_metrics SET collected_at=? WHERE node_id='testnode'", ((self.now-timedelta(seconds=400)).isoformat(),))

    def test_old_metrics_only_block_automatic_placement(self):
        self.stale_metrics()
        node = self.node()
        self.assertEqual(node['status'], 'online')
        self.assertTrue(node['metrics_stale'])
        self.assertEqual(node['cpu_count'], 8)
        self.assertTrue(node['provisioning_allowed'])
        self.assertFalse(node['metrics_fresh'])
        self.assertFalse(node['automatic_placement_allowed'])
        self.assertIn('metrics stale', node['automatic_placement_block_reason'])
        self.assertIn('metrics stale', node['provisioning_warning'])
        self.assertEqual(main.evaluate_placement('manual', 'testnode')['selected']['node_id'], 'testnode')
        with self.assertRaises(HTTPException) as error:
            main.evaluate_placement('automatic', None)
        self.assertIn('metrics stale', error.exception.detail)
        api = json.loads(main.api_node_status('testnode').body)
        self.assertEqual(api['status'], 'online')
        self.assertTrue(api['execution_capable'])
        self.assertFalse(api['metrics_fresh'])
        self.assertGreater(api['metrics_age_seconds'], 180)
        self.assertLess(api['heartbeat_age_seconds'], 180)
        self.assertEqual(self.node()['status'], 'online')

    def test_stale_metrics_do_not_block_lifecycle_or_manual_deploy(self):
        self.stale_metrics()
        # Even an old negative RDAD report must not block unrelated lifecycle actions.
        with main.db() as con:
            con.execute("UPDATE nodes SET rdad_ok=0 WHERE node_id='testnode'")
        item = {'node_id':'testnode', 'path':self.tmp.name, 'client_id':'testbox',
                'type':'jellyfin', 'containers':['jellyfin-testbox']}
        Path(self.tmp.name, 'compose.yml').write_text(main.compose_for('testbox','jellyfin',8101,None), encoding='utf-8')
        for action in ('start', 'stop', 'restart', 'recreate', 'deploy'):
            with self.subTest(action=action), ExitStack() as stack:
                mocks = {name:stack.enter_context(patch.object(main,name)) for name in (
                    'update_step','update_job','record_event','save_appbox_status','fail_workflow')}
                queued = stack.enter_context(patch.object(main,'queue_agent_command',return_value='command'))
                stack.enter_context(patch.object(main,'wait_agent_command',return_value=(True, {'state':'running'}, '')))
                main.execute_remote_job({'job_id':'test-job','client_id':'testbox','action':action},item)
                mocks['fail_workflow'].assert_not_called()
                queued.assert_called_once()
                self.assertEqual(queued.call_args.args[2]['action'], action)
                self.assertEqual(mocks['update_job'].call_args.args[1], 'success')
        for action in ('start','stop','restart','recreate','deploy','claim'):
            with self.subTest(dispatch=action):
                command_id = main.queue_agent_command('testnode','appbox_action',{'action':action})
                with patch.object(main,'authenticate_agent'):
                    command = json.loads(main.agent_poll_commands('testnode', Request({})).body)['command']
                self.assertEqual(command['command_id'],command_id)

    def test_stale_metrics_claim_preserves_business_checks(self):
        self.stale_metrics()
        item = {'node_id':'testnode','type':'plex','containers':['plex-test']}
        with patch.object(main,'get_appbox',return_value=item), patch.object(main,'wait_agent_command',return_value=(True,{'claimed':True},'')), patch.object(main,'record_event'):
            self.assertEqual(main.claim_appbox('testbox','claim-abcdefgh').status_code,303)

    def test_ui_online_with_stale_metrics_keeps_manual_option_enabled(self):
        from html.parser import HTMLParser
        self.stale_metrics()
        node = self.node()
        class Options(HTMLParser):
            def __init__(self):
                super().__init__()
                self.options = []
            def handle_starttag(self, tag, attrs):
                if tag == 'option':
                    self.options.append(dict(attrs))
        html = main.templates.env.get_template('appboxes.html').render(nodes=[node], placement={})
        parser = Options()
        parser.feed(html)
        option = next(o for o in parser.options if o.get('data-node-placement') == 'testnode')
        self.assertNotIn('disabled', option)
        self.assertIn('metrics stale', html)
        agents = main.templates.env.get_template('agents.html').render(nodes=[node])
        self.assertIn('>ONLINE</span>', agents)
        self.assertIn('expirées', agents)

    def test_execution_policy_preserves_maintenance_and_capability_guards(self):
        self.stale_metrics()
        node = self.node()
        for action in ('start','stop','restart','recreate','claim','deploy'):
            self.assertIsNone(main.execution_block_reason(node,action))
            self.assertIsNotNone(main.execution_block_reason({**node,'capabilities':{}},action))
            self.assertIsNotNone(main.execution_block_reason({**node,'agent_online':False,'status':'offline'},action))
        maintenance = {**node,'maintenance':True,'status':'maintenance'}
        for action in ('start','restart','recreate','claim','deploy'):
            self.assertIsNotNone(main.execution_block_reason(maintenance,action))
        self.assertIsNone(main.execution_block_reason(maintenance,'stop'))

    def test_missing_heartbeat_and_maintenance_tag(self):
        with main.db() as con:
            con.execute("UPDATE node_agents SET last_heartbeat=NULL WHERE node_id='testnode'")
        self.assertEqual(self.node()['status'], 'unknown')
        with main.db() as con:
            con.execute("INSERT INTO node_tag_assignments(node_id,tag_id,assigned_at) VALUES('testnode','maintenance',?)", (main.now_iso(),))
        self.assertEqual(self.node()['status'], 'maintenance')

    def test_legacy_heartbeat_with_metrics_and_invalid_new_sample(self):
        with patch.object(main, 'authenticate_agent'):
            asyncio.run(main.agent_heartbeat('testnode', Request({'agent_version':'legacy', 'metrics':{'cpu_count':12}})))
            self.assertEqual(self.node()['cpu_count'], 12)
            for stamp in ('invalid', (self.now+timedelta(days=1)).isoformat()):
                with self.assertRaises(HTTPException):
                    asyncio.run(main.agent_metrics('testnode', Request({'collected_at':stamp,'metrics':{'cpu_count':99}})))
        self.assertEqual(self.node()['cpu_count'], 12)

    def test_expired_node_rejected_before_enqueuing_provisioning(self):
        self.set_age(181)
        with patch.object(main, 'get_appbox', return_value={'node_id':'testnode'}), patch.object(main, 'create_job') as create:
            with self.assertRaises(HTTPException):
                main.enqueue_action(Request({}), 'testbox', 'deploy')
        create.assert_not_called()

    def test_expiry_before_job_execution_and_command_delivery(self):
        self.set_age(181)
        job = {'job_id':'synthetic', 'client_id':'testbox', 'action':'deploy'}
        item = {'node_id':'testnode','path':self.tmp.name}
        with patch.object(main, 'fail_workflow') as fail, patch.object(main, 'queue_agent_command') as queue:
            main.execute_remote_job(job, item)
        fail.assert_called_once()
        queue.assert_not_called()
        main.queue_agent_command('testnode', 'appbox_action', {'action':'deploy'})
        with patch.object(main, 'authenticate_agent'):
            self.assertIsNone(json.loads(main.agent_poll_commands('testnode', Request({})).body)['command'])

    def test_templates_use_derived_status(self):
        self.set_age(181)
        node = self.node()
        for name in ('nodes.html', 'agents.html'):
            html = main.templates.env.get_template(name).render(nodes=[node], tags=[], placement={}, hostname='cronos')
            self.assertIn('data-node-liveness="testnode"', html)
            self.assertIn('OFFLINE', html)
            self.assertNotIn('>ONLINE<', html)
