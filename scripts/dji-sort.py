#!/usr/bin/env python3
"""dji-sort — разбор фото/видео с дронов DJI в архив по папкам <CC>_<Place>_<ДД.ММ.ГГГГ>.

Этапы:
  analyze  — инвентаризация источника (карта дрона / папка), дедуп против архива (размер → SHA-256),
             кластеризация по дате + ~3 км, reverse-geocoding (Nominatim) → plan.json с предложенными папками.
  execute  — раскладка по plan.json: копирование (или --move) с ретраями (NTFS/Tuxera Errno 22),
             суффикс _N при коллизии имён, SHA-256-верификация копии.
  sync     — печатает (и с --run выполняет rsync) команды долива на ryzen + скан Immich + альбомы.

Примеры:
  dji-sort.py analyze --src /Volumes/SD_Card/DCIM --archive /Volumes/NVME-SSD/DJI --plan plan.json
  # отредактировать в plan.json поле "folder" у кластеров (по умолчанию — предложение геокодера), затем:
  dji-sort.py execute --plan plan.json
  dji-sort.py sync --archive /Volumes/NVME-SSD/DJI --run

Зависимости: python3 (stdlib), exiftool в PATH, сеть для Nominatim (analyze).
GPS: фото — EXIF; видео Mini 3 Pro / Mini 2 — из .SRT-сайдкара или самого MP4; видео Mini 5 Pro без
субтитров GPS не имеют — попадают в кластер той же даты, ближайший по времени.
Кэш хэшей архива: ~/.cache/dji-sort/archive-hashes.json (ключ relpath|size|mtime).
"""
import argparse, errno, hashlib, json, math, os, re, shutil, subprocess, sys, time, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime

MEDIA_EXT = {'.jpg', '.jpeg', '.dng', '.mp4', '.mov', '.srt'}
COUNTRY_CC = {'Georgia': 'GEO', 'Turkey': 'TUR', 'Türkiye': 'TUR', 'Bulgaria': 'BUL', 'Armenia': 'ARM',
              'Greece': 'GR', 'Azerbaijan': 'AZE', 'Russia': 'RUS', 'Italy': 'ITA', 'Spain': 'ESP',
              'France': 'FRA', 'Germany': 'GER', 'Cyprus': 'CYP', 'Egypt': 'EGY', 'UAE': 'UAE',
              'United Arab Emirates': 'UAE', 'Thailand': 'THA'}
CLUSTER_KM = 3.0
CACHE = os.path.expanduser('~/.cache/dji-sort/archive-hashes.json')
UA = 'dji-sort/1.0 (home-lab; personal drone archive)'
RETRY_ERRNOS = {errno.EINVAL, errno.EIO, errno.EAGAIN}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def retry(fn, what, tries=6):
    """NTFS через Tuxera спорадически кидает Errno 22 на create/rename/stat — повторяем."""
    for i in range(tries):
        try:
            return fn()
        except OSError as e:
            if e.errno not in RETRY_ERRNOS or i == tries - 1:
                raise
            log(f'  retry {i+1}/{tries} {what}: {e}')
            time.sleep(1 + i)


def sha256(path, bufsize=8 * 1024 * 1024):
    def _do():
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(bufsize), b''):
                h.update(chunk)
        return h.hexdigest()
    return retry(_do, f'sha256 {os.path.basename(path)}')


def walk_media(root):
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not d.startswith('.') and d not in ('_dubli', 'DJI_quarantine')]
        for fn in fns:
            if fn.startswith('.') or fn.startswith('._'):
                continue
            if os.path.splitext(fn)[1].lower() in MEDIA_EXT:
                p = os.path.join(dp, fn)
                st = retry(lambda: os.stat(p), f'stat {fn}')
                out.append({'path': p, 'name': fn, 'size': st.st_size, 'mtime': int(st.st_mtime)})
    return out


# ---------- metadata ----------
def exif_batch(paths):
    """exiftool по списку файлов (без -fast2 — иначе у DJI MP4 теряются теги)."""
    if not paths:
        return {}
    tags = ['-j', '-n', '-q', '-FileName', '-Model', '-CreateDate', '-DateTimeOriginal',
            '-GPSLatitude', '-GPSLongitude', '-Duration', '-ImageSize']
    res = {}
    for i in range(0, len(paths), 200):
        chunk = paths[i:i + 200]
        try:
            out = subprocess.run(['exiftool', *tags, *chunk], capture_output=True, text=True, timeout=600).stdout
            for rec in json.loads(out or '[]'):
                res[rec.get('SourceFile')] = rec
        except Exception as e:
            log(f'exiftool failed on chunk {i}: {e}')
    return res


SRT_LAT = re.compile(r'\[latitude:\s*([-\d.]+)\]\s*\[longitude:\s*([-\d.]+)\]')
SRT_GPS = re.compile(r'GPS\s*\(([-\d.]+),\s*([-\d.]+)')  # Mini 2: GPS (lon, lat, alt)
SRT_TIME = re.compile(r'(\d{4})[-.](\d{2})[-.](\d{2})\s+(\d{2}):(\d{2}):(\d{2})')


def srt_info(path):
    try:
        with open(path, 'r', errors='ignore') as f:
            head = f.read(4000)
    except OSError:
        return None
    lat = lon = None
    m = SRT_LAT.search(head)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
    else:
        m = SRT_GPS.search(head)
        if m:
            lon, lat = float(m.group(1)), float(m.group(2))
    t = SRT_TIME.search(head)
    dt = f'{t.group(1)}:{t.group(2)}:{t.group(3)} {t.group(4)}:{t.group(5)}:{t.group(6)}' if t else None
    if lat == 0 and lon == 0:
        lat = lon = None
    return {'lat': lat, 'lon': lon, 'dt': dt}


NAME_DT = re.compile(r'DJI_(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})_')


def enrich(files):
    meta = exif_batch([f['path'] for f in files if not f['name'].lower().endswith('.srt')])
    srts = {os.path.splitext(f['path'])[0]: f['path'] for f in files if f['name'].lower().endswith('.srt')}
    for f in files:
        m = meta.get(f['path'], {})
        f['model'] = m.get('Model')
        f['lat'], f['lon'] = m.get('GPSLatitude'), m.get('GPSLongitude')
        dt = m.get('DateTimeOriginal') or m.get('CreateDate')
        base = os.path.splitext(f['path'])[0]
        if f['name'].lower().endswith(('.mp4', '.mov')):
            nm = NAME_DT.search(f['name'])  # у Mini 5 Pro CreateDate в UTC, имя файла — локальное время
            if nm:
                dt = '{}:{}:{} {}:{}:{}'.format(*nm.groups())
            if (f['lat'] is None) and base in srts:
                s = srt_info(srts[base])
                if s:
                    f['lat'], f['lon'] = s['lat'], s['lon']
                    dt = dt or s['dt']
        if f['name'].lower().endswith('.srt'):
            s = srt_info(f['path']) or {}
            f['lat'], f['lon'], dt = s.get('lat'), s.get('lon'), s.get('dt')
            nm = NAME_DT.search(f['name'])
            if nm:
                dt = '{}:{}:{} {}:{}:{}'.format(*nm.groups())
        if not dt:  # фото без EXIF (битое/обрезанное) — дата из имени DJI_YYYYMMDDHHMMSS
            nm = NAME_DT.search(f['name'])
            if nm:
                dt = '{}:{}:{} {}:{}:{}'.format(*nm.groups())
        f['dt'] = dt
        f['date'] = dt[:10].replace(':', '-') if dt else None
        f['sidecar_of'] = base + '.MP4' if f['name'].lower().endswith('.srt') else None
    return files


# ---------- archive index / dedupe ----------
def load_cache():
    try:
        with open(CACHE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_cache(c):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(c, fh)
    os.replace(tmp, CACHE)


def dedupe(files, archive):
    """Файл считается дублем, если в архиве есть файл того же размера с тем же SHA-256."""
    arch = walk_media(archive)
    by_size = defaultdict(list)
    for a in arch:
        by_size[a['size']].append(a)
    cache = load_cache()
    for f in files:
        f['dup_of'] = None
        cands = by_size.get(f['size'], [])
        if not cands:
            continue
        try:
            f['sha256'] = f.get('sha256') or sha256(f['path'])
        except FileNotFoundError:
            log(f'  source vanished: {f["path"]}')
            f['dup_of'] = 'MISSING-SOURCE'
            continue
        for a in cands:
            rel = os.path.relpath(a['path'], archive)
            key = f'{rel}|{a["size"]}|{a["mtime"]}'
            h = cache.get(key)
            if not h:
                try:
                    h = cache[key] = sha256(a['path'])
                except FileNotFoundError:  # папку в архиве переименовали, пока мы работали — не падаем
                    log(f'  archive file vanished (renamed?): {rel}')
                    continue
            if h == f['sha256']:
                f['dup_of'] = rel
                break
    save_cache(cache)
    return files


# ---------- clustering / geocoding ----------
def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def cluster(files):
    days = defaultdict(list)
    for f in files:
        days[f['date'] or 'unknown-date'].append(f)
    clusters = []
    for day, fs in sorted(days.items()):
        located = [f for f in fs if f.get('lat') is not None]
        groups = []  # [{'center': (lat, lon), 'files': []}]
        for f in sorted(located, key=lambda x: x['dt'] or ''):
            for g in groups:
                if km(g['center'], (f['lat'], f['lon'])) <= CLUSTER_KM:
                    g['files'].append(f)
                    n = len(g['files'])
                    g['center'] = (sum(x['lat'] for x in g['files']) / n, sum(x['lon'] for x in g['files']) / n)
                    break
            else:
                groups.append({'center': (f['lat'], f['lon']), 'files': [f]})
        for f in fs:  # без GPS: к ближайшей по времени группе того же дня, иначе отдельный кластер
            if f.get('lat') is not None:
                continue
            if f.get('sidecar_of'):
                continue  # SRT ляжет туда же, куда его MP4 (см. execute)
            if groups:
                def tdist(g):
                    ts = [x['dt'] for x in g['files'] if x['dt']]
                    return min(abs(_secs(f['dt']) - _secs(t)) for t in ts) if ts and f['dt'] else 0
                min(groups, key=tdist)['files'].append(f)
            else:
                groups.append({'center': None, 'files': [f]})
        for g in groups:
            clusters.append({'date': day, 'center': g['center'], 'files': g['files']})
    return clusters


def _secs(dt):
    try:
        return datetime.strptime(dt, '%Y:%m:%d %H:%M:%S').timestamp()
    except Exception:
        return 0


def geocode(lat, lon):
    url = 'https://nominatim.openstreetmap.org/reverse?' + urllib.parse.urlencode(
        {'lat': f'{lat:.5f}', 'lon': f'{lon:.5f}', 'format': 'jsonv2', 'zoom': 14, 'accept-language': 'en'})
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except Exception as e:
        log(f'nominatim failed: {e}')
        return None, None
    a = d.get('address', {})
    place = next((a[k] for k in ('tourism', 'historic', 'natural', 'village', 'hamlet', 'town', 'city',
                                 'municipality', 'county') if k in a), d.get('name'))
    time.sleep(1.1)  # Nominatim: не чаще 1 запроса/с
    return COUNTRY_CC.get(a.get('country', ''), a.get('country_code', 'XX').upper()), place


def slug(s):
    s = re.sub(r'[^\w\-]+', '-', s or 'Unknown', flags=re.UNICODE).strip('-')
    return s[:40] or 'Unknown'


def propose_folder(cl):
    d = cl['date']
    dd = f'{d[8:10]}.{d[5:7]}.{d[0:4]}' if re.match(r'\d{4}-\d{2}-\d{2}', d) else 'UNKNOWN-DATE'
    if cl['center']:
        cc, place = geocode(*cl['center'])
        cl['geo'] = {'cc': cc, 'place': place}
        return f'{cc or "XX"}_{slug(place)}_{dd}'
    return f'XX_Unknown_{dd}'


# ---------- commands ----------
def cmd_analyze(a):
    files = walk_media(a.src)
    log(f'{len(files)} media files in {a.src}')
    enrich(files)
    if not a.no_dedupe:
        dedupe(files, a.archive)
    dups = [f for f in files if f.get('dup_of')]
    new = [f for f in files if not f.get('dup_of')]
    log(f'duplicates already in archive: {len(dups)}, new: {len(new)}')
    clusters = cluster(new)
    plan = {'src': a.src, 'archive': a.archive, 'mode': 'move' if a.move else 'copy', 'created': datetime.now().isoformat(),
            'duplicates': [{'path': f['path'], 'size': f['size'], 'dup_of': f['dup_of']} for f in dups], 'clusters': []}
    for cl in clusters:
        folder = propose_folder(cl)
        plan['clusters'].append({'date': cl['date'], 'center': cl['center'], 'geo': cl.get('geo'), 'folder': folder,
                                 'files': [{'path': f['path'], 'size': f['size'], 'model': f['model'], 'dt': f['dt'],
                                            'lat': f.get('lat'), 'lon': f.get('lon'), 'sha256': f.get('sha256')}
                                           for f in cl['files']]})
        log(f'  {folder}: {len(cl["files"])} files')
    with open(a.plan, 'w') as fh:
        json.dump(plan, fh, indent=1, ensure_ascii=False)
    log(f'plan written: {a.plan} — проверь/поправь "folder" у кластеров, затем execute')


def unique_dest(dest_dir, name, src_size, src_hash):
    """Коллизия имён (DJI повторяет DJI_XXXX): если уже лежит идентичный файл — вернуть None (пропустить),
    иначе подобрать суффикс _N, тоже проверяя, не лежит ли там наша копия с прошлого запуска."""
    base, ext = os.path.splitext(name)
    cand = os.path.join(dest_dir, name)
    n = 0
    while os.path.exists(cand):
        st = retry(lambda: os.stat(cand), f'stat {cand}')
        if st.st_size == src_size and sha256(cand) == src_hash:
            return None
        n += 1
        cand = os.path.join(dest_dir, f'{base}_{n}{ext}')
    return cand


def cmd_execute(a):
    with open(a.plan) as fh:
        plan = json.load(fh)
    archive, move = plan['archive'], plan['mode'] == 'move'
    done = skipped = failed = 0
    for cl in plan['clusters']:
        dest_dir = os.path.join(archive, cl['folder'])
        retry(lambda: os.makedirs(dest_dir, exist_ok=True), f'mkdir {dest_dir}')
        for f in cl['files']:
            src = f['path']
            if not os.path.exists(src):
                log(f'  missing (renamed/moved?): {src}')
                failed += 1
                continue
            h = f.get('sha256') or sha256(src)
            dest = unique_dest(dest_dir, os.path.basename(src), f['size'], h)
            if dest is None:
                skipped += 1
                continue
            tmp = dest + '.part'
            retry(lambda: shutil.copyfile(src, tmp), f'copy {os.path.basename(src)}')
            if sha256(tmp) != h:
                retry(lambda: os.remove(tmp), 'rm bad copy')
                log(f'  HASH MISMATCH after copy: {src}')
                failed += 1
                continue
            retry(lambda: os.replace(tmp, dest), f'rename {os.path.basename(dest)}')
            try:
                retry(lambda: os.utime(dest, (os.stat(src).st_atime, os.stat(src).st_mtime)), 'utime')
            except OSError:
                pass
            if move:
                retry(lambda: os.remove(src), f'rm src {src}')
            done += 1
            log(f'  {"moved" if move else "copied"} {os.path.relpath(dest, archive)}')
    log(f'done={done} skipped(identical exists)={skipped} failed={failed}')
    sys.exit(1 if failed else 0)


def cmd_sync(a):
    """rsync архива на ryzen; с --run дополнительно (если на ryzen есть файл ключа) — скан библиотеки и альбомы.
    Ключ читается на самом ryzen (cat в подшелле), на Mac и в вывод не попадает."""
    rs = ['rsync', '-a', '--exclude=.DS_Store', '--exclude=._*', a.archive.rstrip('/') + '/', f'{a.host}:{a.remote}/']
    psql = 'docker exec immich_postgres psql -U postgresimi -d immich -tAc'
    cnt = f'{psql} "select count(*) from asset where \\"libraryId\\"=\'{a.library}\' and \\"deletedAt\\" is null"'
    remote = (f'K=$(cat {a.key_file}) || exit 3; '
              f'curl -sf -X POST -H "x-api-key: $K" http://localhost:2283/api/libraries/{a.library}/scan && echo "scan queued"; '
              # скан асинхронный: ждём, пока число ассетов библиотеки перестанет расти (2 замера подряд без изменений)
              f'prev=-1; same=0; for i in $(seq 1 60); do n=$({cnt}); if [ "$n" = "$prev" ]; then same=$((same+1)); [ $same -ge 2 ] && break; else same=0; fi; prev=$n; sleep 5; done; echo "library assets: $n"; '
              f'docker run --rm --network immich_default -e API_URL=http://immich-server:2283/api -e API_KEY=$K '
              f'-e ROOT_PATH={a.remote} -e ALBUM_LEVELS=1 -e UNATTENDED=1 salvoxia/immich-folder-album-creator:latest 2>&1 | grep -i "album\\|added\\|error" | tail -5')
    print('# 1. долить зеркало на ryzen:')
    print(' '.join(rs))
    print(f'# 2+3. скан external library + альбомы из папок (ключ dji-library лежит на ryzen в {a.key_file}, chmod 600):')
    print(f"ssh {a.host} '{remote}'")
    if a.run:
        log('rsync...')
        rc = subprocess.call(rs)
        if rc:
            sys.exit(rc)
        log('immich scan + albums...')
        rc = subprocess.call(['ssh', a.host, remote])
        if rc == 3:
            log(f'нет файла ключа {a.key_file} на {a.host} — скан/альбомы пропущены (см. HOME-INFRA §6.8)')
        sys.exit(rc)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest='cmd', required=True)
    x = sp.add_parser('analyze')
    x.add_argument('--src', required=True)
    x.add_argument('--archive', required=True)
    x.add_argument('--plan', default='plan.json')
    x.add_argument('--move', action='store_true', help='перемещать (для разбора корня самого архива), по умолчанию копировать')
    x.add_argument('--no-dedupe', action='store_true')
    x.set_defaults(fn=cmd_analyze)
    x = sp.add_parser('execute')
    x.add_argument('--plan', default='plan.json')
    x.set_defaults(fn=cmd_execute)
    x = sp.add_parser('sync')
    x.add_argument('--archive', required=True)
    x.add_argument('--host', default='ryzen4700')
    x.add_argument('--remote', default='/mnt/media/DJI')
    x.add_argument('--library', default='3e4a2765-bb9f-45ea-b8d7-134d932f2147')
    x.add_argument('--key-file', default='/srv/immich/.dji-library.key', help='файл с API-ключом Immich на хосте ryzen')
    x.add_argument('--run', action='store_true', help='выполнить rsync, затем скан и альбомы (если есть файл ключа)')
    x.set_defaults(fn=cmd_sync)
    a = p.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
