"""Local Mihomo sampled traffic monitor. Python 3.8+, standard library only, Windows/macOS/Linux."""
import argparse
from contextlib import contextmanager
import csv
import datetime as dt
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import uuid
from urllib.parse import urlparse, parse_qs

IS_WINDOWS = os.name == 'nt'
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
    import msvcrt
else:
    import fcntl

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
APP_ID = 'io.github.clash-verge-rev.clash-verge-rev'
TZ = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
PORT = 18769
STOP = threading.Event()
DB = DATA / 'traffic.sqlite3'


def tz_label():
    seconds = int((dt.datetime.now(TZ).utcoffset() or dt.timedelta()).total_seconds())
    sign = '+' if seconds >= 0 else '-'
    hours, minutes = divmod(abs(seconds) // 60, 60)
    return f'UTC{sign}{hours:02d}:{minutes:02d}'


STATUS = {'state': 'starting', 'interval_seconds': 0.5, 'accuracy': 'sampled', 'timezone': tz_label()}


def now():
    return dt.datetime.now(TZ)


class PipeRaw(io.RawIOBase):
    """Use nonblocking Windows pipe reads with a finite HTTP request deadline."""
    def __init__(self, path):
        super().__init__()
        self.file = open(path, 'r+b', buffering=0)
        self.deadline = time.monotonic() + 5
        mode = wintypes.DWORD(1)  # PIPE_NOWAIT, byte mode
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.SetNamedPipeHandleState.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p]
        api.SetNamedPipeHandleState.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(self.file.fileno())
        if not api.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None):
            self.file.close()
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        self.api = api
        api.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        api.ReadFile.restype = wintypes.BOOL

    def readable(self):
        return True

    def readinto(self, buf):
        block = ctypes.create_string_buffer(len(buf))
        count = wintypes.DWORD()
        while time.monotonic() < self.deadline:
            ok = self.api.ReadFile(self.handle, block, len(buf), ctypes.byref(count), None)
            if ok and count.value:
                buf[:count.value] = block.raw[:count.value]
                return count.value
            error = 0 if ok else ctypes.get_last_error()
            if error == 109:
                return 0
            if error not in (0, 232, 536):
                raise ctypes.WinError(error)
            time.sleep(0.01)
        raise TimeoutError('Mihomo pipe response timed out')

    def makefile(self, *_args, **_kwargs):
        return io.BufferedReader(self)

    def close(self):
        if not self.closed:
            self.file.close()
        super().close()


# Credentials and the API endpoint change only when Clash Verge restarts, so cache them.
_API_CACHE = {'target': None}


def clash_config_path():
    override = os.environ.get('VPN_MONITOR_CONFIG_DIR')
    if override:
        base = Path(override)
    elif IS_WINDOWS:
        base = (Path(os.environ.get('APPDATA') or (Path.home() / 'AppData' / 'Roaming')) / APP_ID)
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support' / APP_ID
    else:
        base = Path(os.environ.get('XDG_DATA_HOME') or (Path.home() / '.local' / 'share')) / APP_ID
    runtime = base / 'config.yaml'
    legacy = base / 'clash-verge.yaml'
    if runtime.exists() or not legacy.exists():
        return runtime
    return legacy


def _config_value(text, key):
    match = re.search(rf'^{re.escape(key)}:[ \t]*(.*)$', text, re.M)
    if not match:
        return ''
    value = match.group(1).strip()
    if value.startswith('"'):
        end = value.find('"', 1)
        if end > 0:
            return value[1:end].replace('\\\\', '\\')
    elif value.startswith("'"):
        end = value.find("'", 1)
        if end > 0:
            return value[1:end]
    else:
        value = value.split(' #', 1)[0].strip()
    return '' if value in ('', 'null', '~') else value


def _read_api_config():
    path = clash_config_path()
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as exc:
        raise RuntimeError(f'Cannot read Clash Verge config: {path}') from exc
    return {
        'secret': _config_value(text, 'secret'),
        'pipe': _config_value(text, 'external-controller-pipe'),
        'unix': _config_value(text, 'external-controller-unix'),
        'http': _config_value(text, 'external-controller'),
    }


def _pipe_names(configured):
    # Stale sidecar pipes can coexist with the active one, so try all of them.
    try:
        names = [name for name in os.listdir('\\\\.\\pipe\\') if name.startswith('verge-mihomo-')]
    except OSError:
        names = []
    if configured:
        name = configured.rsplit('\\', 1)[-1]
        if name in names:
            names.remove(name)
        names.insert(0, name)
    return names


def _resolve_target():
    """Return (kind, address, secret); kind is pipe, unix or http."""
    override = os.environ.get('VPN_MONITOR_API', '').strip()
    config = _read_api_config()
    secret = config['secret']
    if override:
        if override.startswith('pipe:'):
            return ('pipe', _pipe_names(override[5:]), secret)
        if override.startswith('unix:'):
            return ('unix', override[5:], secret)
        address = override if '://' in override else 'http://' + override
        return ('http', address, secret)
    if IS_WINDOWS:
        names = _pipe_names(config['pipe'])
        if names:
            return ('pipe', names, secret)
    elif config['unix']:
        return ('unix', config['unix'], secret)
    if config['http']:
        address = config['http'] if '://' in config['http'] else 'http://' + config['http']
        return ('http', address, secret)
    if not IS_WINDOWS:
        return ('unix', str(Path(tempfile.gettempdir()) / 'verge-mihomo.sock'), secret)
    raise RuntimeError('No Mihomo API endpoint found; start Clash Verge or set VPN_MONITOR_API')


def _auth_headers(secret):
    return {'Authorization': f'Bearer {secret}'} if secret else {}


def _pipe_request(name, secret, endpoint):
    pipe = PipeRaw('\\\\.\\pipe\\' + name)
    try:
        request = f'GET {endpoint} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {secret}\r\nConnection: close\r\n\r\n'
        pipe.file.write(request.encode('utf-8'))
        response = http.client.HTTPResponse(pipe)
        response.begin()
        if response.status != 200:
            raise RuntimeError(f'Mihomo HTTP {response.status}')
        return json.loads(response.read())
    finally:
        pipe.close()


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a Unix domain socket (macOS/Linux mihomo IPC)."""

    def __init__(self, path, timeout=5):
        super().__init__('localhost', timeout=timeout)
        self.unix_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.unix_path)
        self.sock = sock


def _unix_request(path, secret, endpoint):
    connection = UnixHTTPConnection(path)
    try:
        connection.request('GET', endpoint, headers=_auth_headers(secret))
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f'Mihomo HTTP {response.status}')
        return json.loads(response.read())
    finally:
        connection.close()


def _http_request(address, secret, endpoint):
    parsed = urlparse(address)
    host = parsed.hostname or '127.0.0.1'
    port = parsed.port or (443 if parsed.scheme == 'https' else 9090)
    factory = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    connection = factory(host, port, timeout=5)
    try:
        connection.request('GET', endpoint, headers=_auth_headers(secret))
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f'Mihomo HTTP {response.status}')
        return json.loads(response.read())
    finally:
        connection.close()


def api_get(endpoint):
    last_error = None
    for _attempt in range(2):
        if _API_CACHE['target'] is None:
            _API_CACHE['target'] = _resolve_target()
        target = _API_CACHE['target']
        try:
            if target[0] == 'pipe':
                if not IS_WINDOWS:
                    raise RuntimeError('Named pipes are only available on Windows')
                for name in target[1]:
                    try:
                        return _pipe_request(name, target[2], endpoint)
                    except Exception as exc:
                        last_error = exc
                raise last_error or RuntimeError('No running verge-mihomo pipe found')
            if target[0] == 'unix':
                return _unix_request(target[1], target[2], endpoint)
            return _http_request(target[1], target[2], endpoint)
        except Exception as exc:
            last_error = exc
            _API_CACHE['target'] = None
    raise last_error


@contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=10)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        with db:
            yield db
    finally:
        db.close()


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS daily(day TEXT, app TEXT, path TEXT, domain TEXT, upload INTEGER, download INTEGER, PRIMARY KEY(day,app,path,domain))')
        db.execute('CREATE TABLE IF NOT EXISTS totals(day TEXT PRIMARY KEY, upload INTEGER, download INTEGER)')
        db.execute('CREATE TABLE IF NOT EXISTS events(time TEXT, kind TEXT, detail TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS minute_usage(minute TEXT,app TEXT,path TEXT,domain TEXT,route TEXT,upload INTEGER,download INTEGER,PRIMARY KEY(minute,app,path,domain,route))')
        db.execute('CREATE TABLE IF NOT EXISTS connection_log(run TEXT,id TEXT,route TEXT,start TEXT,first_seen TEXT,last_seen TEXT,app TEXT,path TEXT,domain TEXT,destination TEXT,port TEXT,network TEXT,upload INTEGER,download INTEGER,state TEXT,PRIMARY KEY(run,id,route))')
        db.execute('CREATE INDEX IF NOT EXISTS connection_time ON connection_log(last_seen,first_seen)')
        db.execute('CREATE TABLE IF NOT EXISTS minute_coverage(minute TEXT PRIMARY KEY,samples INTEGER)')
        db.execute('CREATE TABLE IF NOT EXISTS monitor_meta(key TEXT PRIMARY KEY,value TEXT)')
        db.execute('INSERT OR IGNORE INTO monitor_meta VALUES(?,?)', ('detail_started_at', now().isoformat()))
        # A previous collector may have exited without observing connection closure.
        db.execute("UPDATE connection_log SET state='interrupted' WHERE state='active'")


def event(kind, detail):
    with connect() as db:
        db.execute('INSERT INTO events VALUES(?,?,?)', (now().isoformat(), kind, detail))


def identity(connection):
    metadata = connection.get('metadata') or {}
    path = metadata.get('processPath') or ''
    app = metadata.get('process') or path.replace('\\', '/').rsplit('/', 1)[-1] or '(unknown application)'
    domain = metadata.get('host') or metadata.get('sniffHost') or metadata.get('destinationIP') or '(unknown destination)'
    return app, path, domain.lower()


def is_proxy(connection):
    # Mihomo chains list the concrete outbound first, followed by selector groups.
    chains = connection.get('chains') or []
    if not chains:
        return None
    return chains[0].upper() not in {'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'COMPATIBLE'}


def split_days(start, end, upload, download):
    # Split the interval at Beijing midnight, conserving every observed byte.
    start, end = start.astimezone(TZ), end.astimezone(TZ)
    if end <= start:
        return [(end.date().isoformat(), upload, download)]
    result, allocated_u, allocated_d, cursor = [], 0, 0, start
    duration = (end - start).total_seconds()
    while cursor < end:
        boundary = dt.datetime.combine(cursor.date() + dt.timedelta(days=1), dt.time(), TZ)
        stop = min(end, boundary)
        cumulative = (stop - start).total_seconds() / duration
        u, d = round(upload * cumulative), round(download * cumulative)
        result.append((cursor.date().isoformat(), u - allocated_u, d - allocated_d))
        allocated_u, allocated_d, cursor = u, d, stop
    return result


class Counter:
    def __init__(self):
        self.previous = {}
        self.last = None
        self.run = str(uuid.uuid4())
        self.visible = {}
        self.minute_rows = []
        self.connection_rows = []

    def consume(self, connections, timestamp):
        current, rows, visible = {}, [], {}
        self.minute_rows, self.connection_rows = [], []
        for c in connections:
            key = (c['id'], c.get('start', ''))
            upload, download = max(0, int(c.get('upload', 0))), max(0, int(c.get('download', 0)))
            current[key] = (upload, download)
            app, path, domain = identity(c)
            proxy = is_proxy(c)
            route = 'proxy' if proxy is True else ('unknown' if proxy is None else 'direct')
            if (c.get('chains') or [''])[0].upper().startswith('REJECT'):
                route = 'rejected'
            previous = self.previous.get(key)
            begin = self.last
            du = dd = 0
            if begin is not None and previous is None:
                try:
                    begin = dt.datetime.fromisoformat(c['start'].replace('Z', '+00:00')).astimezone(TZ)
                except (KeyError, ValueError, TypeError):
                    begin = None
                # Older connections first seen after an outage establish a baseline.
                if begin is not None and self.last <= begin <= timestamp:
                    previous = (0, 0)
            if begin is not None and previous is not None and upload >= previous[0] and download >= previous[1]:
                du, dd = upload - previous[0], download - previous[1]
            if du or dd:
                if proxy is True:
                    for day, u, d in split_days(begin, timestamp, du, dd):
                        rows.append((day, app, path, domain, u, d))
                for minute, u, d in split_minutes(begin, timestamp, du, dd):
                    self.minute_rows.append((minute, app, path, domain, route, u, d))
            metadata = c.get('metadata') or {}
            entry = (self.run, c['id'], route, c.get('start', ''), timestamp.isoformat(), timestamp.isoformat(),
                     app, path, domain, metadata.get('destinationIP') or metadata.get('remoteDestination') or '',
                     str(metadata.get('destinationPort') or ''), metadata.get('network') or '', du, dd, 'active')
            visible[(c['id'], route)] = entry
            self.connection_rows.append(entry)
        # Disappearance is an observation, not an exact TCP/UDP close timestamp.
        for key, entry in self.visible.items():
            if key not in visible:
                self.connection_rows.append((*entry[:12], 0, 0, 'closed_observed'))
        self.visible = visible
        self.previous, self.last = current, timestamp
        return rows


def split_minutes(start, end, upload, download):
    start, end = start.astimezone(TZ), end.astimezone(TZ)
    if end <= start:
        return [(end.replace(second=0, microsecond=0).isoformat(), upload, download)]
    cursor, allocated_u, allocated_d, result = start, 0, 0, []
    duration = (end-start).total_seconds()
    while cursor < end:
        minute = cursor.replace(second=0, microsecond=0)
        stop = min(end, minute + dt.timedelta(minutes=1))
        ratio = (stop-start).total_seconds()/duration
        u, d = round(upload*ratio), round(download*ratio)
        result.append((minute.isoformat(), u-allocated_u, d-allocated_d))
        allocated_u, allocated_d, cursor = u, d, stop
    return result


def save(rows, minutes=(), connections=(), timestamp=None):
    if rows or minutes or connections or timestamp is not None:
        with connect() as db:
            db.executemany('INSERT INTO daily VALUES(?,?,?,?,?,?) ON CONFLICT(day,app,path,domain) DO UPDATE SET upload=upload+excluded.upload, download=download+excluded.download', rows)
            db.executemany('INSERT INTO minute_usage VALUES(?,?,?,?,?,?,?) ON CONFLICT(minute,app,path,domain,route) DO UPDATE SET upload=upload+excluded.upload,download=download+excluded.download', minutes)
            db.executemany('INSERT INTO connection_log VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run,id,route) DO UPDATE SET last_seen=excluded.last_seen,app=excluded.app,path=excluded.path,domain=excluded.domain,destination=excluded.destination,port=excluded.port,network=excluded.network,upload=upload+excluded.upload,download=download+excluded.download,state=excluded.state', connections)
            if timestamp is not None:
                db.execute('INSERT INTO minute_coverage VALUES(?,1) ON CONFLICT(minute) DO UPDATE SET samples=samples+1', (timestamp.astimezone(TZ).replace(second=0,microsecond=0).isoformat(),))


def save_totals(rows):
    # Mihomo totals include direct connections, so they are an upper bound only.
    if rows:
        with connect() as db:
            db.executemany('INSERT INTO totals VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET upload=upload+excluded.upload, download=download+excluded.download', rows)


def report(day):
    with connect() as db:
        rows = db.execute('SELECT app,path,domain,upload,download FROM daily WHERE day=? ORDER BY upload+download DESC', (day,)).fetchall()
        totals = db.execute('SELECT upload,download FROM totals WHERE day=?', (day,)).fetchone() or (0, 0)
        days = [r[0] for r in db.execute('SELECT DISTINCT day FROM daily ORDER BY day DESC')]
        events = db.execute('SELECT time,kind,detail FROM events ORDER BY rowid DESC LIMIT 8').fetchall()
    details = [dict(zip(('app', 'path', 'domain', 'upload', 'download'), r)) for r in rows]
    observed = (sum(r['upload'] for r in details), sum(r['download'] for r in details))
    return {'day': day, 'days': days, 'details': details, 'observed': {'upload': observed[0], 'download': observed[1]},
            'totals': {'upload': totals[0], 'download': totals[1]}, 'status': dict(STATUS), 'events': events}


def detail_options(query):
    today = now().replace(hour=0, minute=0, second=0, microsecond=0)
    def timestamp(key, default):
        raw = query.get(key, [default.isoformat()])[0]
        value = dt.datetime.fromisoformat(raw)
        if value.tzinfo is None:
            value = value.replace(tzinfo=TZ)
        value = value.astimezone(TZ)
        if value.second or value.microsecond:
            raise ValueError('时间范围须精确到整分钟')
        return value
    start = timestamp('start', today)
    end = timestamp('end', now().replace(second=0, microsecond=0)+dt.timedelta(minutes=1))
    if end <= start or end-start > dt.timedelta(days=31):
        raise ValueError('结束时间须晚于开始时间，范围最多 31 天')
    route = query.get('route', ['proxy'])[0]
    bucket = query.get('bucket', ['minute'])[0]
    if route not in ('proxy', 'direct', 'unknown', 'rejected', 'all') or bucket not in ('minute', 'quarter', 'hour'):
        raise ValueError('不支持的路由或时间粒度')
    return start, end, route, bucket, query.get('app', [''])[0], query.get('domain', [''])[0]


def detail_report(query, export=False):
    start, end, route, bucket, app, domain = detail_options(query)
    where = 'minute>=? AND minute<? AND instr(lower(app),lower(?))>0 AND instr(lower(domain),lower(?))>0'
    args = [start.isoformat(), end.isoformat(), app, domain]
    selected, selected_args = where, list(args)
    if route != 'all':
        selected += ' AND route=?'
        selected_args.append(route)
    with connect() as db:
        started = db.execute("SELECT value FROM monitor_meta WHERE key='detail_started_at'").fetchone()[0]
        by_route = [dict(zip(('route','upload','download'), r)) for r in db.execute('SELECT route,SUM(upload),SUM(download) FROM minute_usage WHERE '+where+' GROUP BY route', args)]
        totals = db.execute('SELECT COALESCE(SUM(upload),0),COALESCE(SUM(download),0) FROM minute_usage WHERE '+selected, selected_args).fetchone()
        details = [dict(zip(('app','path','domain','route','upload','download'),r)) for r in db.execute('SELECT app,path,domain,route,SUM(upload),SUM(download) FROM minute_usage WHERE '+selected+' GROUP BY app,path,domain,route ORDER BY SUM(upload+download) DESC'+('' if export else ' LIMIT 501'), selected_args)]
        apps = [dict(zip(('name','upload','download'),r)) for r in db.execute('SELECT app,SUM(upload),SUM(download) FROM minute_usage WHERE '+selected+' GROUP BY app ORDER BY SUM(upload+download) DESC LIMIT 100', selected_args)]
        domains = [dict(zip(('name','upload','download'),r)) for r in db.execute('SELECT domain,SUM(upload),SUM(download) FROM minute_usage WHERE '+selected+' GROUP BY domain ORDER BY SUM(upload+download) DESC LIMIT 100', selected_args)]
        # Only a validated enum controls this SQL expression.
        expression = {
            'minute': 'minute',
            'quarter': "substr(minute,1,14)||printf('%02d',CAST(substr(minute,15,2) AS INTEGER)/15*15)||':00+08:00'",
            'hour': "substr(minute,1,13)||':00:00+08:00'",
        }[bucket]
        points = {r[0]:(r[1],r[2]) for r in db.execute('SELECT '+expression+',SUM(upload),SUM(download) FROM minute_usage WHERE '+selected+' GROUP BY 1 ORDER BY 1', selected_args)}
        coverage = {r[0]:r[1] for r in db.execute('SELECT '+expression+',SUM(samples) FROM minute_coverage WHERE minute>=? AND minute<? GROUP BY 1', (start.isoformat(),end.isoformat()))}
        connection_where = 'first_seen<? AND last_seen>=? AND instr(lower(app),lower(?))>0 AND instr(lower(domain),lower(?))>0'
        connection_args = [end.isoformat(), start.isoformat(), app, domain]
        if route != 'all':
            connection_where += ' AND route=?'
            connection_args.append(route)
        connections = [dict(zip(('id','route','start','first_seen','last_seen','app','path','domain','destination','port','network','upload','download','state'),r)) for r in db.execute('SELECT id,route,start,first_seen,last_seen,app,path,domain,destination,port,network,upload,download,state FROM connection_log WHERE '+connection_where+' ORDER BY upload+download DESC LIMIT 100',connection_args)]
        events = db.execute('SELECT time,kind,detail FROM events WHERE time>=? AND time<? ORDER BY rowid DESC LIMIT 30', (start.isoformat(),end.isoformat())).fetchall()
    bucket_minutes = {'minute': 1, 'quarter': 15, 'hour': 60}[bucket]
    cursor = start.replace(minute=start.minute//bucket_minutes*bucket_minutes)
    series, step = [], dt.timedelta(minutes=bucket_minutes)
    # Future bins are not shown as measured zero traffic.
    observed_end = now().replace(second=0,microsecond=0)+dt.timedelta(minutes=1)
    while cursor < min(end, observed_end):
        stamp = cursor.isoformat()
        u,d = points.get(stamp,(0,0))
        series.append({'time':stamp,'upload':u,'download':d,'samples':coverage.get(stamp,0)})
        cursor += step
    return {'start':start.isoformat(),'end':end.isoformat(),'route':route,'bucket':bucket,
            'detail_started_at':started,'historical_partial':start < dt.datetime.fromisoformat(started),
            'totals':{'upload':totals[0],'download':totals[1]},'by_route':by_route,'series':series,
            'apps':apps,'domains':domains,'details':details if export else details[:500],
            'details_truncated':not export and len(details)>500,'connections':connections,
            'connection_bytes_scope':'observed connection totals within one collector session, not the selected time window',
            'events':events,'status':dict(STATUS)}


def detail_csv(query):
    output=io.StringIO(newline='')
    writer=csv.writer(output)
    writer.writerow(['开始时间(北京时间)','结束时间(不含)','应用','应用路径','域名/IP','路由','上传字节','下载字节','合计字节'])
    data=detail_report(query,export=True)
    for row in data['details']:
        fields=[row['app'],row['path'],row['domain'],row['route']]
        fields=["'"+value if value[:1] in ('=','+','-','@') else value for value in fields]
        writer.writerow([data['start'],data['end'],*fields,row['upload'],row['download'],row['upload']+row['download']])
    return output.getvalue().encode('utf-8-sig')


def export_csv(day):
    # Only rewrite the requested day; historical files change no more than once.
    target = DATA / f'{day}.csv'
    temporary = target.with_suffix('.csv.tmp')
    with temporary.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['日期(北京时间)', '应用', '应用路径', '域名或IP', '上传字节', '下载字节', '合计字节'])
        for r in report(day)['details']:
            # Prevent application/host names from being interpreted as Excel formulas.
            fields = [r['app'], r['path'], r['domain']]
            fields = ["'" + s if s[:1] in ('=', '+', '-', '@') else s for s in fields]
            writer.writerow([day, *fields, r['upload'], r['download'], r['upload'] + r['download']])
    try:
        os.replace(temporary, target)
    except PermissionError:
        logging.warning('CSV open in another application: %s', day)


def collect():
    counter, failed, last_export, last_export_day = Counter(), False, 0, None
    total_previous, total_timestamp, last_failure_log = None, None, 0
    event('start', 'Monitoring started; existing connections baseline only')
    STATUS['started_at'] = now().isoformat()
    while not STOP.is_set():
        tick = time.monotonic()
        try:
            payload = api_get('/connections')
            timestamp = now()
            # Mihomo returns null while the core is stopping/restarting.
            connections = payload.get('connections') or []
            rows = counter.consume(connections, timestamp)
            save(rows, counter.minute_rows, counter.connection_rows, timestamp)
            upload_total = int(payload.get('uploadTotal') or 0)
            download_total = int(payload.get('downloadTotal') or 0)
            if total_previous is not None and total_timestamp is not None:
                delta_up = max(0, upload_total - total_previous[0])
                delta_down = max(0, download_total - total_previous[1])
                if delta_up or delta_down:
                    save_totals(split_days(total_timestamp, timestamp, delta_up, delta_down))
            total_previous, total_timestamp = (upload_total, download_total), timestamp
            if failed:
                event('recovered', 'Reconnected; downtime excluded and baseline reset')
            failed = False
            STATUS.update(state='running', last_sample=timestamp.isoformat(), active_connections=len(connections), unknown_routes=sum(is_proxy(c) is None for c in connections), error=None)
        except Exception as exc:
            # Keep retrying after VPN shutdown/restart; make loss of coverage visible.
            if not failed or time.monotonic() - last_failure_log >= 60:
                logging.exception('Collection failed')
                last_failure_log = time.monotonic()
            if not failed:
                # A broken database must not kill the collector; keep retrying.
                try:
                    event('gap', type(exc).__name__ + ': ' + str(exc))
                    with connect() as db:
                        db.execute("UPDATE connection_log SET state='interrupted' WHERE run=? AND state='active'", (counter.run,))
                except Exception:
                    logging.exception('Failed to record collection gap')
            failed = True
            counter = Counter()
            total_previous, total_timestamp = None, None
            STATUS.update(state='waiting_for_mihomo', error=type(exc).__name__ + ': ' + str(exc))
        if time.monotonic() - last_export >= 15:
            day = now().date().isoformat()
            try:
                # On a date rollover, finish the previous day's CSV exactly once.
                if last_export_day and last_export_day != day:
                    export_csv(last_export_day)
                export_csv(day)
                last_export_day = day
                last_export = time.monotonic()
            except Exception:
                logging.exception('CSV export failed; SQLite records retained')
        STOP.wait(max(0.05, (3 if failed else 0.5) - (time.monotonic() - tick)))
    export_csv(now().date().isoformat())
    with connect() as db:
        db.execute("UPDATE connection_log SET state='interrupted' WHERE state='active'")
    event('stop', 'Monitoring stopped')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/':
            content = (ROOT / 'dashboard.html').read_bytes()
            mime = 'text/html; charset=utf-8'
        elif parsed.path == '/api/report':
            day = parse_qs(parsed.query).get('day', [now().date().isoformat()])[0]
            content = json.dumps(report(day), ensure_ascii=False).encode('utf-8')
            mime = 'application/json; charset=utf-8'
        elif parsed.path in ('/api/detail','/api/detail.csv'):
            try:
                query=parse_qs(parsed.query)
                content=detail_csv(query) if parsed.path.endswith('.csv') else json.dumps(detail_report(query),ensure_ascii=False).encode('utf-8')
            except (ValueError,TypeError) as exc:
                self.send_error(400,str(exc).encode('ascii','backslashreplace').decode())
                return
            mime='text/csv; charset=utf-8' if parsed.path.endswith('.csv') else 'application/json; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(content)))
        if parsed.path.endswith('.csv'):
            self.send_header('Content-Disposition','attachment; filename="vpn-traffic.csv"')
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    if args.probe:
        result = api_get('/connections')
        connections = result.get('connections') or []
        print(json.dumps({'api': _API_CACHE['target'][0] if _API_CACHE['target'] else 'unknown', 'connections': len(connections), 'proxy': sum(is_proxy(c) is True for c in connections), 'with_process': sum(bool(c.get('metadata', {}).get('process')) for c in connections)}))
        return
    # A locked local file prevents duplicate collectors even after a stale pid file.
    DATA.mkdir(parents=True, exist_ok=True)
    lock = (DATA / 'monitor.lock').open('a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        if IS_WINDOWS:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        print('Monitor is already running; this instance will exit.')
        return
    initialize()
    handler = RotatingFileHandler(DATA / 'monitor.log', maxBytes=1024*1024, backupCount=2, encoding='utf-8')
    logging.basicConfig(handlers=[handler], level=logging.INFO)
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    server.daemon_threads = True
    server.timeout = 0.5
    (DATA / 'monitor.pid').write_text(str(os.getpid()), encoding='ascii')
    (DATA / 'stop.request').unlink(missing_ok=True)
    worker = threading.Thread(target=collect, daemon=True)
    worker.start()
    try:
        while worker.is_alive() and not (DATA / 'stop.request').exists():
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        worker.join(timeout=10)
        server.server_close()
        (DATA / 'monitor.pid').unlink(missing_ok=True)
        (DATA / 'stop.request').unlink(missing_ok=True)
        lock.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # pythonw.exe has no console, so startup failures must go to a file.
        DATA.mkdir(parents=True, exist_ok=True)
        with (DATA / 'bootstrap.log').open('a', encoding='utf-8') as stream:
            stream.write(f'{now().isoformat()} {traceback.format_exc()}\n')
        raise
