# -*- coding: utf-8 -*-
# service.nfo.sync - událostní synchronizace .nfo metadat pro kiosk.
#
# Proč: Sonarr/Radarr píšou FULL .nfo (Sonarr anglicky), při kterém Kodi
# nescrapuje online a cs-CZ nastavení scraperů se nepoužije. Scrapery přitom
# umí z .nfo brát JEN identitu, pokud text obsahuje URL themoviedb.org/
# movie/<id> resp. /tv/<id> (find_uniqueids_in_text, resp. parse_nfo_url v
# TV scraperu) - pak stáhnou čerstvá data online. Addon proto:
#   1. po skončení scanu knihovny (arr notifikace, service.libraryautoupdate
#      i ruční update - vše končí onScanFinished) projde nové položky,
#   2. jejich arr .nfo přepíše na identitní formát (jen uniqueids + URL
#      řádek, originál zálohuje na *.nfo.arr-backup); u epizod .nfo maže -
#      identitou epizody je SxxEyy v názvu souboru, epizodová identita v
#      .nfo se scrapery nerozpozná; položku pak refreshne přes JSON-RPC,
#   3. položky bez hodnocení bere jako poškozená metadata a opravuje je
#      znovu (cooldown 24 h, ať nescrapuje pořád dokola),
#   4. po vyřízení fronty exportuje knihovnu zpět do .nfo
#      (ExportLibrary, tichý separate-files export s overwrite), takže
#      .nfo u média = čerstvá česká data.
# Jednorázový průchod celé knihovny (repair-all) spustí marker
# repair-all.request v profilu addonu: JSON, volitelně {"only": "<část
# názvu>"} pro omezení; výsledek zapíše do repair-all.report. Marker
# umisťuje playbook kiosk_nfo_repair.yaml (make kiosk_nfo_repair).
#
# NFO bez rozpoznatelného ID (staré ruční importy) i RAR zdroje se
# přeskočí a zalogují - zůstávají FULL, jak jsou.
import json
import os
import re
import time

import xbmc
import xbmcaddon
import xbmcvfs

ADDON = xbmcaddon.Addon()
# getAddonInfo('profile') vrací special:// protokol - na reálnou cestu
# (/home/<user>/.kodi/userdata/...) ho převede až xbmcvfs.translatePath;
# bez toho selhávají open()/os.makedirs na state a report (Errno 13
# 'special:', pozorováno 1. 11. 2026)
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
BACKUPS_DIR = os.path.join(PROFILE, 'nfo-backups')
STATE_FILE = os.path.join(PROFILE, 'state.json')
MARKER_FILE = os.path.join(PROFILE, 'repair-all.request')
REPORT_FILE = os.path.join(PROFILE, 'repair-all.report')

LOG_PREFIX = 'nfo-sync'
DEBOUNCE_S = 10            # pauza po onScanFinished, ať scan úplně doběhne
POLL_S = 5                 # perioda hlavní smyčky (marker + debounce)
REFRESH_THROTTLE_MS = 400  # mezera mezi Refresh* JSON-RPC voláními
REFRESH_TIMEOUT_S = 90     # čekání na dokončení refreshu jedné položky
RATING_RETRY_S = 24 * 3600  # cooldown opakované opravy bez hodnocení
TIME_FMT = '%Y-%m-%d %H:%M:%S'

# URL řádky, které sami zapisujeme = poznámka "toto .nfo už je identitní"
IDENTITY_URL_RE = re.compile(
    r'themoviedb\.org/(?:movie|tv)/\d+|imdb\.com/title/tt\d+'
    r'|thetvdb\.com/series/\d+', re.I)

ID_RES = {
    'tmdb': [
        re.compile(r'<uniqueid[^>]*type=["\']tmdb["\'][^>]*>\s*(\d+)', re.I),
        re.compile(r'<id[^>]*moviedb=["\']tmdb["\'][^>]*>\s*(\d+)', re.I),
        re.compile(r'<tmdbid>\s*(\d+)', re.I),
    ],
    'imdb': [
        re.compile(r'<uniqueid[^>]*type=["\']imdb["\'][^>]*>\s*(tt\d+)', re.I),
        re.compile(r'<imdbid>\s*(tt\d+)', re.I),
    ],
    'tvdb': [
        re.compile(r'<uniqueid[^>]*type=["\']tvdb["\'][^>]*>\s*(\d+)', re.I),
        re.compile(r'<tvdbid>\s*(\d+)', re.I),
    ],
}

REPORT_LINES = []


def log(msg, level=xbmc.LOGINFO):
    xbmc.log('%s: %s' % (LOG_PREFIX, msg), level)


def report(line):
    REPORT_LINES.append(line)
    log(line)


def now_str():
    return time.strftime(TIME_FMT)


def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state):
    try:
        os.makedirs(PROFILE, exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as fh:
            json.dump(state, fh)
    except Exception as err:
        log('state se nepodařilo uložit: %s' % err, xbmc.LOGWARNING)


def rpc(method, params=None):
    req = {'jsonrpc': '2.0', 'id': 1, 'method': method}
    if params is not None:
        req['params'] = params
    try:
        resp = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
    except Exception as err:
        log('JSON-RPC %s selhal: %s' % (method, err), xbmc.LOGWARNING)
        return None
    if resp.get('error'):
        log('JSON-RPC %s: %s' % (method, resp['error']), xbmc.LOGWARNING)
        return None
    return resp.get('result')


def query_items(method, properties):
    # VideoLibrary.GetMovies -> klíč "movies" v odpovědi
    key = method.split('.')[-1][3:].lower()
    result = rpc(method, {'properties': properties}) or {}
    return result.get(key) or []


# --- práce se soubory přes VFS (nfs://, smb:// i lokální cesty) ---------

def vdirname(path):
    return path.rsplit('/', 1)[0] if '/' in path else path


def vbasename(path):
    name = path.rsplit('/', 1)[-1] if '/' in path else path
    return name.rsplit('.', 1)[0] if '.' in name else name


def vfs_read(path):
    fh = xbmcvfs.File(path, 'r')
    try:
        data = fh.readBytes()
        if isinstance(data, str):
            return data
        # nektere .nfo na NAS nejsou validni UTF-8 (Windows-1250) -
        # pro hledani id/URL staci lossy dekodovani
        return bytes(data or b'').decode('utf-8', 'replace')
    finally:
        fh.close()


def vfs_write(path, content):
    fh = xbmcvfs.File(path, 'w')
    try:
        return fh.write(content)
    finally:
        fh.close()


def backup_nfo(nfo_path):
    # Záloha původního .nfo na LOKÁLNÍ disk (profil addonu). Přes NFS
    # xbmcvfs.copy zakládá nové soubory s mode 000 a nulovou délkou
    # (QNAP NFS4 + libnfs, pozorováno 1. 11. 2026) - takový soubor pak
    # nejde přepsat a zůstává k ničemu. Lokálně je to čisté ext4.
    match = re.search(r'/Public/(.*)$', nfo_path, re.I)
    relative = match.group(1) if match else vfilename(nfo_path)
    dest = os.path.join(BACKUPS_DIR, relative)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as out:
            out.write(vfs_read(nfo_path).encode('utf-8'))
        return True
    except Exception as err:
        log('záloha %s selhala: %s' % (nfo_path, err), xbmc.LOGWARNING)
        return False


def movie_nfo_candidates(file_path):
    directory = vdirname(file_path)
    return [directory + '/movie.nfo',
            directory + '/' + vbasename(file_path) + '.nfo']


def episode_nfo_candidates(file_path):
    return [vdirname(file_path) + '/' + vbasename(file_path) + '.nfo']


def show_nfo_candidates(show, episode_files):
    out = []
    base = (show.get('file') or '').rstrip('/')
    if base:
        out.append(base + '/tvshow.nfo')
    # fallback z cest epizod: přímo vedle epizod (plochá struktura) i o
    # adresář výš (show/s001/epizoda.mkv)
    for ep_file in episode_files[:3]:
        if not ep_file:
            continue
        season_dir = vdirname(ep_file)
        out.append(season_dir + '/tvshow.nfo')
        out.append(vdirname(season_dir) + '/tvshow.nfo')
    # vyhodit duplicity, zachovat pořadí priorit
    seen, unique = set(), []
    for cand in out:
        if cand not in seen:
            seen.add(cand)
            unique.append(cand)
    return unique


# --- identitní .nfo ------------------------------------------------------

def extract_ids(text):
    ids = {}
    for kind, patterns in ID_RES.items():
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                ids[kind] = match.group(1)
                break
    return ids


def identity_url(kind, ids):
    if ids.get('tmdb'):
        segment = 'movie' if kind == 'movie' else 'tv'
        return 'https://www.themoviedb.org/%s/%s' % (segment, ids['tmdb'])
    if kind == 'tvshow' and ids.get('tvdb'):
        # TV scraper URL převede na tmdb přes find API (SHOW_ID_REGEXPS)
        return 'https://thetvdb.com/series/%s' % ids['tvdb']
    if ids.get('imdb'):
        return 'https://www.imdb.com/title/%s' % ids['imdb']
    return None


def identity_xml(kind, ids):
    root = 'movie' if kind == 'movie' else 'tvshow'
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<%s>' % root]
    if ids.get('tmdb'):
        lines.append('\t<uniqueid type="tmdb" default="true">%s</uniqueid>'
                     % ids['tmdb'])
    if ids.get('imdb'):
        lines.append('\t<uniqueid type="imdb">%s</uniqueid>' % ids['imdb'])
    if ids.get('tvdb'):
        lines.append('\t<uniqueid type="tvdb">%s</uniqueid>' % ids['tvdb'])
    url = identity_url(kind, ids)
    if url:
        lines.append('\t%s' % url)
    lines.append('</%s>' % root)
    return '\n'.join(lines) + '\n'


# --- opravy jednotlivých typů --------------------------------------------

def repair_movie_nfo(movie):
    file_path = movie.get('file') or ''
    if not file_path:
        return 'no-file'
    nfo_path = next((c for c in movie_nfo_candidates(file_path)
                     if xbmcvfs.exists(c)), None)
    if not nfo_path:
        return 'no-nfo'  # bez .nfo Kodi scrapoval už při importu
    text = vfs_read(nfo_path)
    if IDENTITY_URL_RE.search(text):
        return 'identity'
    ids = extract_ids(text)
    if not ids:
        return 'no-id'
        backup_nfo(nfo_path)
    if not vfs_write(nfo_path, identity_xml('movie', ids)):
        return 'write-failed'
    log('movie "%s": %s -> identita' % (movie.get('title'), nfo_path))
    return 'rewritten'


def repair_episode_nfo(episode):
    file_path = episode.get('file') or ''
    if not file_path:
        return 'no-file'
    nfo_path = next((c for c in episode_nfo_candidates(file_path)
                     if xbmcvfs.exists(c)), None)
    if not nfo_path:
        return 'no-nfo'
        backup_nfo(nfo_path)
    if not xbmcvfs.delete(nfo_path):
        return 'delete-failed'
    return 'deleted'


def repair_show_nfo(show, episode_files):
    for nfo_path in show_nfo_candidates(show, episode_files):
        if not xbmcvfs.exists(nfo_path):
            continue
        text = vfs_read(nfo_path)
        if IDENTITY_URL_RE.search(text):
            return 'identity'
        ids = extract_ids(text)
        if not ids:
            return 'no-id'
        backup_nfo(nfo_path)
        if not vfs_write(nfo_path, identity_xml('tvshow', ids)):
            return 'write-failed'
        log('tvshow "%s": %s -> identita' % (show.get('title'), nfo_path))
        return 'rewritten'
    return 'no-nfo'


# --- monitor + cykly ------------------------------------------------------

class NFOSync(xbmc.Monitor):
    def __init__(self):
        super(NFOSync, self).__init__()
        self.scan_at = None   # čas posledního onScanFinished (debounce)
        self.busy = False     # probíhá cyklus/repair-all
        self.pending = {}     # (kind, id) -> deadline dokončení refreshu

    def onScanFinished(self, database):
        if database == 'video':
            self.scan_at = time.time()

    def onNotification(self, sender, method, data):
        if method != 'VideoLibrary.OnUpdate':
            return
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            return
        item = payload.get('item') or payload
        kind = item.get('type')
        item_id = item.get('id')
        if kind in ('movie', 'tvshow', 'episode') and item_id is not None:
            self.pending.pop((kind, item_id), None)

    def refresh(self, kind, item_id):
        methods = {'movie': ('VideoLibrary.RefreshMovie', 'movieid'),
                   'tvshow': ('VideoLibrary.RefreshTVShow', 'tvshowid'),
                   'episode': ('VideoLibrary.RefreshEpisode', 'episodeid')}
        method, key = methods[kind]
        if rpc(method, {key: item_id}) is None:
            return False
        self.pending[(kind, item_id)] = time.time() + REFRESH_TIMEOUT_S
        xbmc.sleep(REFRESH_THROTTLE_MS)
        return True

    def wait_pending(self):
        while self.pending and not self.abortRequested():
            now = time.time()
            for key in [k for k, deadline in self.pending.items()
                        if deadline <= now]:
                log('timeout čekání na refresh %s:%s' % key, xbmc.LOGWARNING)
                del self.pending[key]
            if self.pending:
                xbmc.sleep(2000)


def should_retry(state, key):
    return state.get('rating_retry', {}).get(key, 0) < time.time() \
        - RATING_RETRY_S


def mark_retry(state, key):
    state.setdefault('rating_retry', {})[key] = time.time()


def export_library():
    # separate-files export vedle médií s overwrite, bez obrázků
    # (video, separate=true, thumbs=false, overwrite=true, actorthumbs=false)
    log('export knihovny do .nfo')
    xbmc.executebuiltin('ExportLibrary(video,true,false,true,false)')
    xbmc.sleep(5000)  # export běží asynchronně, dej mu prostor rozběhnout se


def write_report():
    try:
        os.makedirs(PROFILE, exist_ok=True)
        with open(REPORT_FILE, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(REPORT_LINES) + '\n')
    except Exception as err:
        log('report se nepodařilo zapsat: %s' % err, xbmc.LOGWARNING)


def incremental(monitor):
    state = load_state()
    last = state.get('last_run')
    if not last:
        state['last_run'] = now_str()
        save_state(state)
        log('první běh - jen nastavuji last_run, backlog řeší repair-all')
        return
    del REPORT_LINES[:]
    changed = False

    for movie in query_items('VideoLibrary.GetMovies',
                             ['title', 'file', 'dateadded', 'rating']):
        key = 'movie:%s' % movie.get('movieid')
        is_new = last < (movie.get('dateadded') or '')
        no_rating = movie.get('rating') is None
        if not is_new and not (no_rating and should_retry(state, key)):
            continue
        action = repair_movie_nfo(movie)
        refresh_wanted = (action == 'rewritten'
                          or (action == 'identity' and no_rating))
        refreshed = refresh_wanted and monitor.refresh('movie',
                                                       movie['movieid'])
        changed = changed or refresh_wanted
        if no_rating:
            mark_retry(state, key)
        report('movie | %s | %s%s' % (movie.get('title'), action,
                                      ' | refresh' if refreshed else ''))

    episodes = query_items('VideoLibrary.GetEpisodes',
                           ['title', 'showtitle', 'tvshowid', 'season',
                            'episode', 'file', 'dateadded', 'rating'])
    todo = {}
    for episode in episodes:
        is_new = last < (episode.get('dateadded') or '')
        no_rating = episode.get('rating') is None
        key = 'episode:%s' % episode.get('episodeid')
        if is_new or (no_rating and should_retry(state, key)):
            todo[episode['episodeid']] = (episode, no_rating)
            if no_rating:
                mark_retry(state, key)

    # dotčené seriály: Sonarr při importu epizody přepíše tvshow.nfo na
    # svou anglickou FULL verzi -> vrať ho na identitu
    shows_touched = {}
    for episode, _ in todo.values():
        shows_touched.setdefault(episode.get('tvshowid'), []).append(
            episode.get('file') or '')
    if shows_touched:
        shows = {s['tvshowid']: s for s in
                 query_items('VideoLibrary.GetTVShows',
                             ['title', 'file', 'rating'])}
        for show_id, ep_files in shows_touched.items():
            show = shows.get(show_id) or {}
            action = repair_show_nfo(show, ep_files)
            if action == 'rewritten':
                changed = True
            report('tvshow | %s | %s' % (show.get('title'), action))

    for episode, no_rating in todo.values():
        action = repair_episode_nfo(episode)
        refreshed = action == 'deleted' and monitor.refresh(
            'episode', episode['episodeid'])
        changed = changed or bool(refreshed)
        report('episode | %s S%02dE%02d | %s%s'
               % (episode.get('showtitle'), episode.get('season') or 0,
                  episode.get('episode') or 0, action,
                  ' | refresh' if refreshed else ''))

    monitor.wait_pending()
    if changed:
        export_library()
    state['last_run'] = now_str()
    save_state(state)
    log('inkrementální cyklus hotový (%d položek)' % len(REPORT_LINES))


def repair_all(monitor, opts):
    del REPORT_LINES[:]
    only = (opts.get('only') or '').strip().lower()

    def matches(text):
        return not only or only in (text or '').lower()

    changed = False
    for movie in query_items('VideoLibrary.GetMovies',
                             ['title', 'file', 'rating']):
        if not matches(movie.get('title')):
            continue
        try:
            action = repair_movie_nfo(movie)
            if action == 'rewritten' or (action == 'identity'
                                         and movie.get('rating') is None):
                if monitor.refresh('movie', movie['movieid']):
                    changed = True
            report('movie | %s | %s' % (movie.get('title'), action))
        except Exception as err:
            report('movie | %s | chyba: %s' % (movie.get('title'), err))

    episodes = query_items('VideoLibrary.GetEpisodes',
                           ['title', 'showtitle', 'tvshowid', 'season',
                            'episode', 'file', 'rating'])
    by_show = {}
    for episode in episodes:
        by_show.setdefault(episode.get('tvshowid'), []).append(episode)

    for show in query_items('VideoLibrary.GetTVShows',
                            ['title', 'file', 'rating']):
        show_id = show.get('tvshowid')
        all_eps = by_show.get(show_id) or []
        # do rozsahu patří seriál podle vlastního názvu, názvu seriálu u
        # epizod (filtr "only" na seriál) i názvů epizod (filtr na epizodu)
        in_scope = (matches(show.get('title'))
                    or any(matches(e.get('showtitle'))
                           or matches(e.get('title')) for e in all_eps))
        if not in_scope:
            continue
        scoped = [e for e in all_eps
                  if matches(show.get('title'))
                  or matches(e.get('showtitle')) or matches(e.get('title'))]
        try:
            action = repair_show_nfo(show, [e.get('file') or ''
                                            for e in scoped])
            if action == 'rewritten' or (action == 'identity'
                                         and show.get('rating') is None):
                if monitor.refresh('tvshow', show_id):
                    changed = True
            report('tvshow | %s | %s' % (show.get('title'), action))
        except Exception as err:
            report('tvshow | %s | chyba: %s' % (show.get('title'), err))
        for episode in scoped:
            try:
                act = repair_episode_nfo(episode)
                if act == 'deleted' and monitor.refresh(
                        'episode', episode['episodeid']):
                    changed = True
                report('episode | %s S%02dE%02d | %s'
                       % (episode.get('showtitle'),
                          episode.get('season') or 0,
                          episode.get('episode') or 0, act))
            except Exception as err:
                report('episode | %s | chyba: %s'
                       % (episode.get('showtitle'), err))

    monitor.wait_pending()
    if changed:
        export_library()
    write_report()
    state = load_state()
    state['last_run'] = now_str()
    save_state(state)
    log('repair-all hotový (%d položek)' % len(REPORT_LINES))


def main():
    monitor = NFOSync()
    log('start (profil: %s)' % PROFILE)
    while not monitor.abortRequested():
        if not monitor.busy and xbmcvfs.exists(MARKER_FILE):
            monitor.busy = True
            try:
                try:
                    opts = json.loads(vfs_read(MARKER_FILE))
                except Exception:
                    opts = {}
                repair_all(monitor, opts if isinstance(opts, dict) else {})
            except Exception as err:
                log('repair-all selhal: %s' % err, xbmc.LOGERROR)
            finally:
                xbmcvfs.delete(MARKER_FILE)
                monitor.busy = False
        elif (not monitor.busy and monitor.scan_at
              and time.time() - monitor.scan_at >= DEBOUNCE_S):
            monitor.scan_at = None
            monitor.busy = True
            try:
                incremental(monitor)
            except Exception as err:
                log('cyklus selhal: %s' % err, xbmc.LOGERROR)
            finally:
                monitor.busy = False
        if monitor.waitForAbort(POLL_S):
            break
    log('stop')


if __name__ == '__main__':
    main()
