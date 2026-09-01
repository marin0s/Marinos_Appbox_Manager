import threading
import unittest
import asyncio
import json
import tempfile
import urllib.error
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_agent_deployment import agent
from test_node_liveness import Request
from app import main


class AgentLoopTests(unittest.TestCase):
    def test_heartbeat_does_not_collect_or_send_metrics(self):
        with patch.object(agent, 'collect_metrics', side_effect=AssertionError('heavy work')), patch.object(agent, 'api', return_value={}) as api:
            agent.heartbeat({'node_id':'test'}, {'docker_ok':True, 'cpu_count':8})
        payload = api.call_args.args[3]
        self.assertNotIn('metrics', payload)
        self.assertTrue(payload['capabilities']['independent_heartbeat'])
        self.assertTrue(payload['capabilities']['appbox_command_lease'])
        self.assertTrue(payload['capabilities']['appbox_progress'])
        self.assertTrue(payload['capabilities']['reference_build_command_lease'])
        self.assertTrue(payload['capabilities']['reference_build_delivery_ack'])

    def test_appbox_worker_reports_real_phase_activity_before_result(self):
        calls=[]
        command={'command_id':'deploy-one','command_type':'appbox_action','payload':{'action':'deploy'}}
        def api(config,method,path,payload=None,**_kwargs):
            calls.append((method,path,payload))
            if method=='GET':
                return {'command':command}
            return {}
        def execute(config,received,**kwargs):
            kwargs['progress_callback'](stage='preparing',percent=5,detail='preparing')
            kwargs['progress_callback'](stage='checksum_reference',percent=25,detail='checksum')
            kwargs['progress_callback'](stage='extraction',percent=50,detail='extraction')
            return {'state':'running'}
        class Runtime:
            cancel_event=threading.Event()
            def begin_command(self,command_id): pass
            def finish_command(self,command_id): pass
        with patch.object(agent,'api',side_effect=api), patch.object(agent,'execute_command',side_effect=execute):
            agent.command_cycle({'node_id':'test'},runtime=Runtime())
        progress=[payload for method,path,payload in calls if path.endswith('/progress')]
        self.assertEqual([item['stage'] for item in progress],['preparing','checksum_reference','extraction'])
        self.assertEqual(calls[-1][2]['status'],'success')

    def test_progress_outage_is_non_blocking_and_does_not_duplicate_worker(self):
        calls=[]
        command={'command_id':'deploy-retry','command_type':'appbox_action','payload':{'action':'deploy'}}
        progress_attempts=[0]
        executions=[0]
        def api(config,method,path,payload=None,**_kwargs):
            calls.append((method,path,payload))
            if method=='GET':
                return {'command':command}
            if path.endswith('/progress'):
                progress_attempts[0] += 1
                if progress_attempts[0] == 1:
                    raise urllib.error.URLError('temporary outage')
            return {}
        def execute(config,received,**kwargs):
            executions[0] += 1
            kwargs['progress_callback'](stage='preparing',percent=5,detail='preparing')
            kwargs['progress_callback'](stage='cache_reference',percent=10,detail='cache')
            kwargs['progress_callback'](stage='checksum_reference',percent=20,detail='checksum')
            return {'state':'running'}
        class Runtime:
            cancel_event=threading.Event()
            def begin_command(self,command_id): pass
            def finish_command(self,command_id): pass
        with patch.object(agent,'api',side_effect=api), patch.object(agent,'execute_command',side_effect=execute):
            agent.command_cycle({'node_id':'test','command_progress_timeout_seconds':1},runtime=Runtime())
        self.assertEqual(executions, [1])
        self.assertGreaterEqual(progress_attempts[0], 2)
        self.assertEqual(sum(1 for method,path,_ in calls if path.endswith('/result')), 1)

    def test_ack_response_lost_after_server_commit_retries_without_double_execution(self):
        command={'command_id':'delivery-one','command_type':'appbox_action','payload':{'action':'deploy'},
                 'delivery_token':'delivery-token-value-1234567890'}
        attempts=[0]; executions=[0]
        def api(config,method,path,payload=None,**_kwargs):
            if method=='GET': return {'command':command}
            if path.endswith('/ack'):
                attempts[0]+=1
                if attempts[0]==1:
                    # Server commit happened, but its HTTP response was lost.
                    raise TimeoutError('response lost after commit')
                return {'status':'claimed','idempotent':True}
            return {}
        def execute(*args,**kwargs): executions[0]+=1; return {'state':'running'}
        class Runtime:
            def __init__(self): self.cancel_event=threading.Event(); self.active=''
            def begin_command(self,command_id): self.active=command_id
            def finish_command(self,command_id): self.active=''
        runtime=Runtime()
        with patch.object(agent,'api',side_effect=api),patch.object(agent,'execute_command',side_effect=execute):
            agent.command_cycle({'node_id':'test','command_delivery_ack_attempts':2},runtime=runtime)
        self.assertEqual(attempts,[2])
        self.assertEqual(executions,[1])
        self.assertEqual(runtime.active,'')

    def test_offer_not_acknowledged_never_executes(self):
        command={'command_id':'delivery-expired','command_type':'appbox_action','payload':{'action':'deploy'},
                 'delivery_token':'expired-delivery-token-1234567890'}
        def api(config,method,path,payload=None,**_kwargs):
            if method=='GET': return {'command':command}
            if path.endswith('/ack'):
                raise urllib.error.HTTPError(path,409,'expired',None,None)
            return {}
        class Runtime:
            def __init__(self): self.cancel_event=threading.Event(); self.active=''
            def begin_command(self,command_id): self.active=command_id
            def finish_command(self,command_id): self.active=''
        runtime=Runtime()
        with patch.object(agent,'api',side_effect=api),patch.object(agent,'execute_command') as execute:
            with self.assertRaises(agent.CommandCancelled):
                agent.command_cycle({'node_id':'test'},runtime=runtime)
        execute.assert_not_called()
        self.assertEqual(runtime.active,'')

    def test_long_command_and_blocked_telemetry_do_not_block_heartbeats(self):
        loops = agent.AgentLoops({'node_id':'test'})
        loops.heartbeat_interval = .01
        loops.command_interval = .01
        loops.inventory_interval = .01
        command_entered, metrics_entered, release = (threading.Event() for _ in range(3))
        heartbeats_continue = threading.Event()
        calls = {'polls':0, 'beats':0, 'executions':0}
        clock = [datetime.now(timezone.utc)]
        statuses = []

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        def api(config, method, path, payload=None):
            if path.endswith('/heartbeat'):
                if command_entered.is_set() and metrics_entered.is_set():
                    # Advance beyond the default timeout while the same command is
                    # still blocked. Only real handler heartbeats keep CP online.
                    clock[0] += timedelta(seconds=120)
                    asyncio.run(main.agent_heartbeat('test', Request(payload)))
                    statuses.append(json.loads(main.api_node_status('test').body)['status'])
                    calls['beats'] += 1
                    if calls['beats'] >= 3:
                        heartbeats_continue.set()
                return {}
            if path.endswith('/commands'):
                calls['polls'] += 1
                return {'command':{'command_id':'one', 'command_type':'reference_build'}}
            return {}

        def execute(config, command, **_kwargs):
            calls['executions'] += 1
            command_entered.set()
            if not release.wait(5):
                raise AssertionError('test worker not released')
            return {'ok':True}

        def metrics(config):
            metrics_entered.set()
            release.wait(5)
            return {'docker_ok':True}

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.object(main, 'DB_FILE', Path(directory)/'loops.db'))
            stack.enter_context(patch.object(main, 'HOSTNAME', 'cronos'))
            stack.enter_context(patch.object(main, 'datetime', Clock))
            stack.enter_context(patch.object(main, 'authenticate_agent'))
            main.init_database()
            with main.db() as con:
                con.execute("INSERT INTO nodes(node_id,name,mode,status,created_at,updated_at) VALUES('test','TEST','remote','online',?,?)", (main.now_iso(), main.now_iso()))
            self.assertEqual(json.loads(main.api_node_status('test').body)['status'], 'unknown')
            stack.enter_context(patch.object(agent, 'api', side_effect=api))
            stack.enter_context(patch.object(agent, 'execute_command', side_effect=execute))
            stack.enter_context(patch.object(agent, 'collect_metrics', side_effect=metrics))
            stack.enter_context(patch.object(agent, 'send_inventory', return_value={}))
            runner = threading.Thread(target=loops.run)
            runner.start()
            try:
                self.assertTrue(heartbeats_continue.wait(3), 'heartbeat blocked by another worker')
                self.assertEqual(calls['executions'], 1)
                self.assertEqual(calls['polls'], 1, 'business work must remain sequential')
                self.assertTrue(all(status == 'online' for status in statuses))
                self.assertGreaterEqual(len(statuses), 3)
            finally:
                loops.stop.set()
                loops.inventory_request.set()
                release.set()
                runner.join(4)
            self.assertFalse(runner.is_alive())

    def test_inventory_command_requests_the_single_collector(self):
        request = threading.Event()
        with patch.object(agent, 'api', side_effect=[{'command':{'command_id':'refresh','command_type':'inventory'}},{}]), patch.object(agent, 'execute_command') as execute:
            agent.command_cycle({'node_id':'test'}, request)
        self.assertTrue(request.is_set())
        execute.assert_not_called()

    def test_failed_heartbeat_retries_and_respects_server_interval(self):
        loops = agent.AgentLoops({'node_id':'test', 'heartbeat_interval':60})
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                loops.stop.set()
        with patch.object(agent, 'heartbeat', side_effect=[RuntimeError('network unavailable'), {'heartbeat_interval':10}]), patch.object(loops.stop, 'wait', side_effect=wait), patch.object(loops, 'report_error') as error:
            loops.heartbeat_loop()
        self.assertEqual(waits, [60, 10])
        error.assert_called_once()
