import datetime as dt
from contextlib import closing, contextmanager
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

module_path = Path(__file__).resolve().parent / 'vpn_monitor.py'
spec = importlib.util.spec_from_file_location('monitor', module_path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

setup_path = Path(__file__).resolve().parent / 'setup.py'
setup_spec = importlib.util.spec_from_file_location('setup_module', setup_path)
s = importlib.util.module_from_spec(setup_spec)
setup_spec.loader.exec_module(s)


class TrafficTests(unittest.TestCase):
    def setUp(self):
        self.time = dt.datetime(2026, 11, 9, 10, tzinfo=m.TZ)

    def stamp(self, day, hm):
        offset = self.time.strftime('%z')
        return f'{day}T{hm}:00{offset[:3]}:{offset[3:]}'

    def connection(self, uid='one', upload=100, download=200, route='proxy', start=None):
        return {'id': uid, 'start': (start or self.time - dt.timedelta(seconds=60)).isoformat(),
                'upload': upload, 'download': download, 'chains': [route, 'selector'],
                'metadata': {'process': 'test.exe', 'processPath': 'C:/test.exe', 'host': 'TEST.example'}}

    def test_baseline_delta_duplicate_and_restart(self):
        counter = m.Counter()
        c = self.connection()
        self.assertEqual(counter.consume([c], self.time), [])
        c.update(upload=110, download=230)
        rows = counter.consume([c], self.time + dt.timedelta(seconds=1))
        self.assertEqual(rows, [('2026-11-09', 'test.exe', 'C:/test.exe', 'test.example', 10, 30)])
        self.assertEqual(counter.consume([c], self.time + dt.timedelta(seconds=2)), [])
        self.assertEqual(m.Counter().consume([c], self.time + dt.timedelta(seconds=3)), [])

    def test_new_connections_and_old_first_seen(self):
        counter = m.Counter()
        counter.consume([], self.time)
        new = self.connection(uid='new', start=self.time + dt.timedelta(milliseconds=100))
        old = self.connection(uid='old')
        rows = counter.consume([new, old], self.time + dt.timedelta(seconds=1))
        self.assertEqual(sum(r[-2] for r in rows), 100)
        self.assertEqual(sum(r[-1] for r in rows), 200)

    def test_outbound_type_uses_concrete_chain(self):
        for route in ['DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'COMPATIBLE']:
            self.assertFalse(m.is_proxy(self.connection(route=route)))
        c = self.connection()
        c['chains'] = ['proxy', 'DIRECT']
        self.assertTrue(m.is_proxy(c))
        c['chains'] = []
        self.assertIsNone(m.is_proxy(c))

    def test_direct_unknown_and_reset_do_not_add_bytes(self):
        counter = m.Counter()
        c = self.connection()
        counter.consume([c], self.time)
        c.update(upload=1, download=1)
        self.assertEqual(counter.consume([c], self.time + dt.timedelta(seconds=1)), [])
        c.update(upload=10, download=20, chains=['DIRECT'])
        self.assertEqual(counter.consume([c], self.time + dt.timedelta(seconds=2)), [])
        c.update(upload=100, download=200, chains=[])
        self.assertEqual(counter.consume([c], self.time + dt.timedelta(seconds=3)), [])

    def test_midnight_split_and_conservation(self):
        start = dt.datetime(2026, 11, 9, 23, 59, 59, tzinfo=m.TZ)
        rows = m.split_days(start, start + dt.timedelta(seconds=2), 101, 203)
        self.assertEqual([r[0] for r in rows], ['2026-11-09', '2026-11-10'])
        self.assertEqual(sum(r[1] for r in rows), 101)
        self.assertEqual(sum(r[2] for r in rows), 203)

    def test_missing_process_and_domain_fallback(self):
        self.assertEqual(m.identity({'metadata': {'destinationIP': '1.2.3.4'}}), ('(unknown application)', '', '1.2.3.4'))

    def test_sqlite_persistence_and_csv(self):
        original = m.DATA, m.DB
        with tempfile.TemporaryDirectory() as folder:
            m.DATA, m.DB = Path(folder), Path(folder) / 'traffic.sqlite3'
            try:
                m.initialize()
                row = ('2026-11-09', 'test.exe', 'C:/test.exe', '=unsafe', 12, 34)
                m.save([row])
                m.save([row])
                m.save_totals([('2026-11-09', 100, 200)])
                m.export_csv('2026-11-09')
                result = m.report('2026-11-09')
                data = result['details'][0]
                self.assertEqual((data['upload'], data['download']), (24, 68))
                self.assertEqual(result['observed'], {'upload': 24, 'download': 68})
                self.assertEqual(result['totals'], {'upload': 100, 'download': 200})
                self.assertFalse((m.DATA / '2026-11-10.csv').exists())
                self.assertIn("'=unsafe", (m.DATA / '2026-11-09.csv').read_text(encoding='utf-8-sig'))
                with closing(sqlite3.connect(m.DB)) as db:
                    self.assertEqual(db.execute('SELECT SUM(upload+download) FROM daily').fetchone()[0], 92)
            finally:
                m.DATA, m.DB = original

    def test_api_failure_retries_and_recovers_without_backfill(self):
        original = m.DATA, m.DB
        with tempfile.TemporaryDirectory() as folder:
            m.DATA, m.DB = Path(folder), Path(folder) / 'traffic.sqlite3'
            try:
                m.initialize()
                m.STOP.clear()
                calls = []
                def api(_endpoint):
                    calls.append(1)
                    if len(calls) == 1:
                        raise TimeoutError('test timeout')
                    m.STOP.set()
                    return {'connections': [self.connection()]}
                with patch.object(m, 'api_get', side_effect=api), patch.object(m.STOP, 'wait'), patch.object(m.logging, 'exception'):
                    m.collect()
                result = m.report('2026-11-09')
                self.assertEqual(result['details'], [])
                self.assertEqual([r[1] for r in reversed(result['events'])], ['start', 'gap', 'recovered', 'stop'])
                self.assertEqual(m.STATUS['state'], 'running')
            finally:
                m.STOP.clear()
                m.DATA, m.DB = original

    @contextmanager
    def detailed_database(self):
        original = m.DATA, m.DB
        with tempfile.TemporaryDirectory() as folder:
            m.DATA, m.DB = Path(folder), Path(folder)/'traffic.sqlite3'
            try:
                m.initialize()
                yield
            finally:
                m.DATA, m.DB = original

    def query(self, **values):
        args={'start':'2026-11-09T10:00','end':'2026-11-09T10:02','route':'proxy'}
        args.update(values)
        return {k:[v] for k,v in args.items()}

    def test_minute_split_conserves_bytes_across_midnight(self):
        start=dt.datetime(2026,11,9,23,59,59,tzinfo=m.TZ)
        rows=m.split_minutes(start,start+dt.timedelta(seconds=2),101,203)
        self.assertEqual([r[0] for r in rows],[self.stamp('2026-11-09','23:59'),self.stamp('2026-11-10','00:00')])
        self.assertEqual(sum(r[1] for r in rows),101)
        self.assertEqual(sum(r[2] for r in rows),203)

    def test_proxy_and_direct_minutes_keep_daily_proxy_only(self):
        counter=m.Counter()
        a=self.connection(uid='proxy');b=self.connection(uid='direct',route='DIRECT')
        counter.consume([a,b],self.time)
        a.update(upload=120,download=250);b.update(upload=130,download=260)
        rows=counter.consume([a,b],self.time+dt.timedelta(seconds=1))
        self.assertEqual(sum(r[-2] for r in rows),20)
        self.assertEqual(sum(r[-1] for r in rows),50)
        amounts={r[4]:(r[5],r[6]) for r in counter.minute_rows}
        self.assertEqual(amounts,{'proxy':(20,50),'direct':(30,60)})

    def test_time_range_filters_and_half_open_end(self):
        with self.detailed_database():
            minutes=[(self.stamp('2026-11-09','10:00'),'test.exe','','a.example','proxy',10,20),
                     (self.stamp('2026-11-09','10:01'),'test.exe','','b.example','proxy',30,40),
                     (self.stamp('2026-11-09','10:02'),'test.exe','','a.example','proxy',999,999),
                     (self.stamp('2026-11-09','10:00'),'test.exe','','a.example','direct',100,200)]
            m.save([],minutes,timestamp=self.time)
            data=m.detail_report(self.query())
            self.assertEqual(data['totals'],{'upload':40,'download':60})
            self.assertEqual(sum(p['upload'] for p in data['series']),40)
            self.assertEqual(data['series'][0]['samples'],1)
            self.assertEqual(data['series'][1]['samples'],0)
            filtered=m.detail_report(self.query(domain='a.example'))
            self.assertEqual(filtered['totals'],{'upload':10,'download':20})
            direct=m.detail_report(self.query(route='direct'))
            self.assertEqual(direct['totals'],{'upload':100,'download':200})
            injected=m.detail_report(self.query(domain="' OR 1=1 --"))
            self.assertEqual(injected['totals'],{'upload':0,'download':0})

    def test_connection_observed_close_and_restart_baseline(self):
        with self.detailed_database():
            counter=m.Counter();c=self.connection()
            m.save(counter.consume([c],self.time),counter.minute_rows,counter.connection_rows)
            c.update(upload=110,download=220)
            m.save(counter.consume([c],self.time+dt.timedelta(seconds=1)),counter.minute_rows,counter.connection_rows)
            m.save(counter.consume([],self.time+dt.timedelta(seconds=2)),counter.minute_rows,counter.connection_rows)
            data=m.detail_report(self.query())
            self.assertEqual(data['connections'][0]['state'],'closed_observed')
            self.assertEqual(data['connections'][0]['upload'],10)
            self.assertEqual(data['connections'][0]['download'],20)
            restarted=m.Counter()
            m.save(restarted.consume([c],self.time+dt.timedelta(seconds=3)),restarted.minute_rows,restarted.connection_rows)
            self.assertEqual(m.detail_report(self.query())['totals'],{'upload':10,'download':20})

    def test_legacy_daily_data_is_preserved_without_fake_minutes(self):
        with self.detailed_database():
            m.save([('2026-11-09','legacy.exe','','old.example',123,456)])
            m.initialize()
            self.assertEqual(m.report('2026-11-09')['observed'],{'upload':123,'download':456})
            data=m.detail_report(self.query())
            self.assertEqual(data['totals'],{'upload':0,'download':0})
            self.assertTrue(data['historical_partial'])
            self.assertTrue(all(p['samples']==0 for p in data['series']))

    def test_detailed_csv_and_hourly_rollup(self):
        with self.detailed_database():
            m.save([], [(self.stamp('2026-11-09','10:01'),'=test.exe','','a.example','proxy',5,7)],timestamp=self.time)
            csv=m.detail_csv(self.query()).decode('utf-8-sig')
            self.assertIn("'=test.exe",csv)
            data=m.detail_report(self.query(bucket='hour'))
            self.assertEqual(len(data['series']),1)
            self.assertEqual(data['series'][0]['upload'],5)
            self.assertEqual(data['series'][0]['download'],7)

    def test_quarter_hour_boundaries_and_partial_window(self):
        with self.detailed_database():
            rows=[(self.stamp('2026-11-09',time),'test.exe','','a.example','proxy',u,u*2)
                  for time,u in [('10:07',1),('10:14',2),('10:15',4),('10:29',8),('10:30',16),('10:59',32),('11:00',64)]]
            m.save([],rows,timestamp=self.time.replace(minute=15))
            data=m.detail_report(self.query(start='2026-11-09T10:08',end='2026-11-09T11:00',bucket='quarter'))
            self.assertEqual([p['time'][11:16] for p in data['series']],['10:00','10:15','10:30','10:45'])
            self.assertEqual([p['upload'] for p in data['series']],[2,12,16,32])
            self.assertEqual([p['samples'] for p in data['series']],[0,1,0,0])
            self.assertEqual(sum(p['download'] for p in data['series']),data['totals']['download'])
            hourly=m.detail_report(self.query(start='2026-11-09T10:08',end='2026-11-09T11:01',bucket='hour'))
            self.assertEqual([p['upload'] for p in hourly['series']],[62,64])

    def test_invalid_window_is_rejected(self):
        for args in [self.query(end='2026-11-09T09:00'),self.query(end='2026-12-20T10:00'),self.query(start='2026-11-09T10:00:01'),self.query(route='invalid')]:
            with self.assertRaises(ValueError):
                m.detail_options(args)

    def test_api_config_parser_and_target_selection(self):
        text = ("secret: abc\n"
                "external-controller-pipe: \\\\.\\pipe\\verge-mihomo-x\n"
                "external-controller-unix: /tmp/verge-mihomo.sock\n"
                "external-controller: ''\n")
        self.assertEqual(m._config_value(text, 'secret'), 'abc')
        self.assertEqual(m._config_value(text, 'external-controller-pipe'), '\\\\.\\pipe\\verge-mihomo-x')
        self.assertEqual(m._config_value(text, 'external-controller-unix'), '/tmp/verge-mihomo.sock')
        self.assertEqual(m._config_value(text, 'external-controller'), '')
        config = {'secret': 'abc', 'pipe': '', 'unix': '/tmp/verge-mihomo.sock', 'http': ''}
        with patch.object(m, '_read_api_config', return_value=config), patch.object(m, 'IS_WINDOWS', False), patch.dict(os.environ, {'VPN_MONITOR_API': ''}):
            self.assertEqual(m._resolve_target(), ('unix', '/tmp/verge-mihomo.sock', 'abc'))
        with patch.object(m, '_read_api_config', return_value=config), patch.object(m, 'IS_WINDOWS', False), patch.dict(os.environ, {'VPN_MONITOR_API': 'http://127.0.0.1:9097'}):
            self.assertEqual(m._resolve_target(), ('http', 'http://127.0.0.1:9097', 'abc'))

    def test_setup_patcher_is_idempotent(self):
        sample = ("env_type: powershell\n"
                  "startup_script: \"E:\\\\old\\\\start.ps1\"\n"
                  "web_ui_list:\n"
                  "- \"https://example.invalid/?host=%host\"\n"
                  "clash_core: verge-mihomo\n")
        patched = s.patch_verge(sample)
        self.assertEqual(patched.count('http://127.0.0.1:18769/'), 1)
        self.assertIn("startup_script: '", patched)
        self.assertIn('clash_core: verge-mihomo', patched)
        self.assertEqual(s.patch_verge(patched), patched)
        self.assertIn('http://127.0.0.1:18769/', s.add_web_ui('web_ui_list: null\n', 'http://127.0.0.1:18769/'))


if __name__ == '__main__':
    unittest.main()
