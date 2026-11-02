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
GUARD_ADDON = 'service.librarywatchdog'
CLEAN_SETTINGS = os.path.join(os.path.dirname(PROFILE.rstrip('/')),
                              'service.libraryautoupdate', 'settings.xml')
STATE_FILE = os.path.join(PROFILE, 'state.json')
MARKER_FILE = os.path.join(PROFILE, 'repair-all.request')
REPORT_FILE = os.path.join(PROFILE, 'repair-all.report')

LOG_PREFIX = 'nfo-sync'
DEBOUNCE_S = 10            # pauza po onScanFinished, ať scan úplně doběhne
POLL_S = 5                 # perioda hlavní smyčky (marker + debounce)
REFRESH_POLL_S = 3          # interval zjišťování dokončení refreshu
REFRESH_TIMEOUT_S = 240     # čekání na refresh jedné položky (velké show =
                            # fetch celého episode-listu přes TMDb trvá minuty)
SCAN_SHOW_BASE_S = 120      # základní limit skenu adresáře jedné show
SCAN_SHOW_EP_S = 12         # navíc na epizodu (scrape ~2-5 s/epizoda)
RATING_RETRY_S = 24 * 3600  # cooldown opakované opravy bez hodnocení
GUARD_QUIET_S = 15 * 60     # hlídky obnovit až po klidové periodě od poslední
                            # úpravy nfo (jinak watchdog reaguje na naše zápisy
                            # skenem, který blokuje refreshy - 1. 11. 2026)
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


def report(line, level=xbmc.LOGINFO):
    REPORT_LINES.append(line)
    log(line, level)


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


def wait_no_playback(monitor):
    # Kodi během přehrávání MLČKY zahazuje všechny refreshy (vrátí OK,
    # nic se nestane - TBBT 105/278, každá epizoda 240s timeout, 0 posun,
    # pozorováno 1. 11. 2026). Během sledování tedy pass počká a po
    # skončení přehrávání sam pokračuje. Na obrazovku nic nevyskakuje.
    waited = 0
    while playback_active() and not monitor.abortRequested():
        xbmc.sleep(30 * 1000)
        waited += 30
        if waited % 300 == 0:
            log('přehrává se už %d s, čekám na konec' % waited)


def playback_active():
    try:
        return xbmc.Player().isPlaying()
    except Exception:
        return False


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
        if kind == 'tvshow':
            # TV scraper bere seznam epizod pres <episodeguide> - bez
            # nej pada na RuntimeError a show se neprescrapuje
            lines.append('\t<episodeguide>')
            lines.append('\t\t<url>%s</url>' % url)
            lines.append('\t</episodeguide>')
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
            if '<episodeguide>' in text.lower():
                return 'identity'
            # stara identita bez <episodeguide> - doplnit z uniqueids
            ids = extract_ids(text)
            if not vfs_write(nfo_path, identity_xml('tvshow', ids)):
                return 'write-failed'
            log('tvshow: doplnen episodeguide do %s' % nfo_path)
            return 'rewritten'
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
        self.scan_at = None     # čas posledního onScanFinished (debounce)
        self.busy = False       # probíhá cyklus/repair-all
        self.scan_active = False  # OnScanStarted..OnScanFinished (viz níže)
        self.quiet_until = None   # kdy obnovit hlídky po klidové periodě

    def onScanFinished(self, database):
        if database == 'video':
            self.scan_at = time.time()
            self.scan_active = False

    def onNotification(self, sender, method, data):
        # Library.IsScanningVideo u tichých (JSON-RPC) skenů nelže - jediný
        # spolehlivý signál "skenuje se" jsou notifikace skeneru
        if method == 'VideoLibrary.OnScanStarted':
            self.scan_active = True
        elif method == 'VideoLibrary.OnScanFinished':
            self.scan_active = False
            if sender == 'videolibrary':
                self.scan_at = time.time()

    def scanning(self):
        return (self.scan_active
                or xbmc.getInfoLabel('Library.IsScanningVideo').lower()
                == 'true')

    def refresh_verified(self, method, params, poll, label):
        # spustí refresh a SERIALIZOVANĚ čeká na skutečné dokončení pollingem
        # obsahu (rating > 0). Důvody, proč ne jinak (1. 11. 2026):
        # - VideoLibrary.OnUpdate po refreshu epizody NEdoCHÁZÍ (4383 falešných
        #   timeoutů) a refresh epizody navíc mění její episodeid v DB
        # - hromadné pálění refreshů vytváří backlog, který se pak s
        #   běžícím skenem špatně seká (Kodi je pak tiše zahazuje)
        wait_no_playback(self)
        if rpc(method, params) is None:
            report('%s | refresh odmítnut' % label)
            return False
        deadline = time.time() + REFRESH_TIMEOUT_S
        while not self.abortRequested():
            xbmc.sleep(REFRESH_POLL_S * 1000)
            if poll():
                return True
            if time.time() >= deadline:
                break
        # Kodi refresh čas od času tiše zahodí (sk busy show-jobem,
        # přehráváním, skenem) - jednou zkusit znovu po krátké pauze
        log('%s: refresh nepotvrzen, opakuji za 30 s' % label)
        xbmc.sleep(30 * 1000)
        wait_no_playback(self)
        if rpc(method, params) is None:
            report('%s | refresh odmítnut (opakování)' % label)
            return False
        deadline = time.time() + REFRESH_TIMEOUT_S
        while not self.abortRequested():
            xbmc.sleep(REFRESH_POLL_S * 1000)
            if poll():
                return True
            if time.time() >= deadline:
                break
        report('%s | refresh nepotvrzen (timeout)' % label, xbmc.LOGWARNING)
        return False

    def refresh_movie(self, movie):
        # RefreshMovie (jako RefreshEpisode) mění movieid v DB -> poll přes
        # stabilní identitu (cesta k souboru); úspěch = nové ID nebo rating
        path = movie.get('file') or ''
        label = 'movie | %s' % movie.get('title')
        baseline_id = movie.get('movieid')

        def find():
            res = rpc('VideoLibrary.GetMovies',
                      {'properties': ['file', 'rating']}) or {}
            for m in res.get('movies') or []:
                if (m.get('file') or '') == path:
                    return m
            return None

        def applied(row):
            return row and ((row.get('rating') or 0) > 0
                            or row.get('movieid') != baseline_id)

        for attempt in (1, 2):
            row = find()
            if not row:
                report('%s | nenalezen v DB' % label)
                return False
            if applied(row):
                return True
            if rpc('VideoLibrary.RefreshMovie',
                   {'movieid': row['movieid']}) is None:
                report('%s | refresh odmítnut' % label)
                return False
            deadline = time.time() + REFRESH_TIMEOUT_S
            while not self.abortRequested():
                xbmc.sleep(REFRESH_POLL_S * 1000)
                if applied(find()):
                    return True
                if time.time() >= deadline:
                    break
            if attempt == 1 and self.scanning():
                wait_library_idle(self)
            else:
                break
        report('%s | refresh nepotvrzen (timeout)' % label, xbmc.LOGWARNING)
        return False

    def refresh_show_stale(self, show, episodes):
        # POZOR: RefreshTVShow(refreshepisodes=true) je DESTRUKTIVNÍ - show
        # i s epizodami smaže a znovu nedá (Stargate Universe, 1. 11. 2026)
        # a sken adresáře existující položky nepřescrapuje (The Expanse,
        # 0/62 i po touch nfo). Jediné dokázané primitivy jsou:
        # - RefreshTVShow bez parametrů (show-level, ID zůstává) - JEN když
        #   show nemá rating; show-job totiž ještě chvíli běží po napsání
        #   ratingu a zahodí následující RefreshEpisode (Panika E15,
        #   2. 11. 2026)
        # - RefreshEpisode po jedné + poll identity (show+řada+epizoda,
        #   ID epizody se mění - 4259->8009)
        title = show.get('title')
        show_id = show['tvshowid']
        fresh = rpc('VideoLibrary.GetTVShowDetails',
                    {'tvshowid': show_id, 'properties': ['rating']}) or {}
        if not (fresh.get('tvshowdetails') or {}).get('rating'):
            log('tvshow %s: show-level refresh (nemá rating)' % title)
            if rpc('VideoLibrary.RefreshTVShow',
                   {'tvshowid': show_id}) is None:
                report('tvshow | %s | refresh odmítnut' % title)
            deadline = time.time() + 120
            while not self.abortRequested() and time.time() < deadline:
                fresh = rpc('VideoLibrary.GetTVShowDetails',
                            {'tvshowid': show_id,
                             'properties': ['rating']}) or {}
                if (fresh.get('tvshowdetails') or {}).get('rating'):
                    break
                xbmc.sleep(REFRESH_POLL_S * 1000)
            # show-job po ratingu ještě dobrigává obrázky - krátká pauza,
            # ať první RefreshEpisode není zahazovaná
            xbmc.sleep(60 * 1000)
        stale = [e for e in episodes if not (e.get('rating') or 0)]
        log('tvshow %s: %d/%d epizod potřebuje refresh'
            % (title, len(stale), len(episodes)))
        done = 0
        for idx, ep in enumerate(stale, 1):
            if self.abortRequested():
                break
            ok = refresh_episode(self, ep)
            done += ok
            log('tvshow %s: S%02dE%02d %s (%d/%d)'
                % (title, ep.get('season') or 0, ep.get('episode') or 0,
                   'ok' if ok else 'TIMEOUT', idx, len(stale)))
        report('tvshow | %s | %d/%d stale epizod opraveno'
               % (title, done, len(stale)))
        return done == len(stale)


def set_addon_enabled(addon_id, enabled):
    req = {"jsonrpc": "2.0", "id": 1, "method": "Addons.SetAddonEnabled",
           "params": {"addonid": addon_id, "enabled": enabled}}
    try:
        json.loads(xbmc.executeJSONRPC(json.dumps(req)))
        return True
    except Exception as err:
        log('enable %s selhal: %s' % (addon_id, err), xbmc.LOGWARNING)
        return False


def get_addon_enabled(addon_id):
    req = {"jsonrpc": "2.0", "id": 1, "method": "Addons.GetAddonDetails",
           "params": {"addonid": addon_id, "properties": ["enabled"]}}
    try:
        d = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
        return d.get('result', {}).get('addon', {}).get('enabled')
    except Exception:
        return None


def get_clean_libraries():
    # clean_libraries v service.libraryautoupdate = čistit knihovnu při
    # každé aktualizaci; uživatel ho chce ZAPNUTÝ, addon ho během cyklu
    # dočasně vypíná a po sobě vrací
    try:
        with open(CLEAN_SETTINGS, encoding='utf-8') as fh:
            m = re.search(r'<setting id="clean_libraries">([^<]*)</setting>',
                          fh.read())
        return m.group(1) == 'true' if m else None
    except Exception:
        return None


def set_clean_libraries(enabled):
    try:
        with open(CLEAN_SETTINGS, encoding='utf-8') as fh:
            text = fh.read()
        new = re.sub(r'(<setting id="clean_libraries">)[^<]*(</setting>)',
                     lambda m: m.group(1) + ('true' if enabled else 'false')
                     + m.group(2), text, count=1)
        if new != text:
            with open(CLEAN_SETTINGS, 'w', encoding='utf-8') as fh:
                fh.write(new)
        return True
    except Exception as err:
        log('nastavení clean_libraries selhalo: %s' % err, xbmc.LOGWARNING)
        return False


def wait_library_idle(monitor, max_wait_s=4 * 3600):
    # Kodi během aktivního skenu knihovny MLČKY odmítá všechny refreshy
    # (JSON-RPC vrátí OK, nic se nestane - pozorováno 1. 11. 2026, přes
    # hodinu blokující sken z watchdog/autoupdate). IsScanningVideo u
    # tichých (JSON-RPC) skenů ale nelže -> hlídá se i notifikace
    # OnScanStarted/OnScanFinished (monitor.scan_active).
    waited = 0
    while monitor.scanning():
        if waited >= max_wait_s or monitor.abortRequested():
            log('skenuje se i po %d s - refreshy můžou být ignorovány'
                % waited, xbmc.LOGWARNING)
            return
        xbmc.sleep(5000)
        waited += 5
        if waited % 300 == 0:
            log('skenuje se už %d s, čekám na konec' % waited)
    if waited:
        log('skenovalo se %d s, pokračuji' % waited)


def pause_guards():
    # během cyklu vypnout watchdog i auto-clean (jejich skeny/čistění se
    # srážely s hromadným refreshem - Wayland pád 12:06, videoversion
    # orfány, dialog "nejde čistit"); původní stav se uloží do state
    state = load_state()
    if state.get('guards_paused'):
        return
    saved = {'watchdog': get_addon_enabled(GUARD_ADDON),
             'clean': get_clean_libraries()}
    set_addon_enabled(GUARD_ADDON, False)
    set_clean_libraries(False)
    state['guards_paused'] = saved
    save_state(state)
    log('pauza hlídacích mechanismů (watchdog=%s, clean=%s)'
        % (saved.get('watchdog'), saved.get('clean')))


def resume_guards():
    # vrátit původní stav (i po pádu/restartu Kodi - voláno při startu)
    state = load_state()
    saved = state.get('guards_paused')
    if saved is None:
        return
    if saved.get('watchdog') is not False:
        set_addon_enabled(GUARD_ADDON, True)
    if saved.get('clean') is not False:
        set_clean_libraries(True)
    del state['guards_paused']
    save_state(state)
    log('hlídací mechanismy obnoveny (watchdog=%s, clean=%s)'
        % (saved.get('watchdog'), saved.get('clean')))


def should_retry(state, key):
    return state.get('rating_retry', {}).get(key, 0) < time.time() \
        - RATING_RETRY_S


def mark_retry(state, key):
    state.setdefault('rating_retry', {})[key] = time.time()


def export_library():
    # VYPNUTO: ExportLibrary přes libnfs zakládá nové soubory s mode 000
    # (QNAP NFS4 + libnfs create bug, pozorováno 1. 11. 2026) - takové
    # soubory jsou nečitelné a blokují scanner. Write-back nahradí 1.1.0
    # (sestavení .nfo z DB přes JSON-RPC a přepis stávajících souborů přes
    # xbmcvfs - overwrite funguje, create ne).
    pass


def write_report():
    try:
        os.makedirs(PROFILE, exist_ok=True)
        with open(REPORT_FILE, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(REPORT_LINES) + '\n')
    except Exception as err:
        log('report se nepodařilo zapsat: %s' % err, xbmc.LOGWARNING)


def refresh_episode(monitor, episode):
    # poll přes stabilní identitu (show+řada+epizoda) - refresh epizody
    # totiž mění episodeid v DB (4259->8009, pozorováno 1. 11. 2026).
    # Úspěch = nové ID (refresh řádek vždy vytvoří znovu) NEBO rating > 0.
    # Pozor: rating>0 nemůže být jediný signál - epizody bez hlasů na TMDb
    # (A Town Called Panic S01E15-17, 2. 11. 2026) mají rating legálně 0.
    show_id = episode.get('tvshowid')
    season = episode.get('season') or 0
    number = episode.get('episode') or 0
    baseline_id = episode.get('episodeid')

    def poll():
        eps = rpc('VideoLibrary.GetEpisodes',
                  {'tvshowid': show_id, 'season': season,
                   'properties': ['episode', 'rating']}) or {}
        for ep in eps.get('episodes') or []:
            if ep.get('episode') == number:
                return ((ep.get('rating') or 0) > 0
                        or ep.get('episodeid') != baseline_id)
        return False

    return monitor.refresh_verified(
        'VideoLibrary.RefreshEpisode', {'episodeid': episode['episodeid']},
        poll, 'episode | %s S%02dE%02d'
        % (episode.get('showtitle'), season, number))


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
        # pozor: zastaralé položky mají rating 0.0 (z importu FULL nfo),
        # ne None - proto nejen `is None`
        no_rating = not (movie.get('rating') or 0)
        if not is_new and not (no_rating and should_retry(state, key)):
            continue
        action = repair_movie_nfo(movie)
        refresh_wanted = (action == 'rewritten'
                          or (action == 'identity' and no_rating))
        refreshed = refresh_wanted and monitor.refresh_movie(movie)
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
        no_rating = not (episode.get('rating') or 0)
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
        # epizoda bez .nfo už je čerstvě scrapeovaná ze skenu (má rating);
        # refresh jen u .nfo-smazaných (jejich data přišla z FULL nfo arru)
        refreshed = action == 'deleted' and refresh_episode(monitor, episode)
        changed = changed or bool(refreshed)
        report('episode | %s S%02dE%02d | %s%s'
               % (episode.get('showtitle'), episode.get('season') or 0,
                  episode.get('episode') or 0, action,
                  ' | refresh' if refreshed else ''))

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
    if not opts.get('skip_movies'):
        movies = [m for m in query_items('VideoLibrary.GetMovies',
                                         ['title', 'file', 'rating'])
                  if matches(m.get('title'))]
        log('repair-all: filmů v rozsahu %d' % len(movies))
        for idx, movie in enumerate(movies, 1):
            wait_no_playback(monitor)
            try:
                action = repair_movie_nfo(movie)
                if action == 'rewritten' or (action == 'identity'
                                             and not (movie.get('rating')
                                                      or 0)):
                    ok = monitor.refresh_movie(movie)
                    changed = changed or ok
                    report('movie | %s | %s | %s'
                           % (movie.get('title'), action,
                              'ok' if ok else 'nepotvrzeno'))
                else:
                    report('movie | %s | %s' % (movie.get('title'), action))
            except Exception as err:
                report('movie | %s | chyba: %s' % (movie.get('title'), err))
            if idx % 25 == 0:
                log('repair-all: filmů hotovo %d/%d' % (idx, len(movies)))
    else:
        log('repair-all: filmy přeskočeny (skip_movies)')

    episodes = query_items('VideoLibrary.GetEpisodes',
                           ['title', 'showtitle', 'tvshowid', 'season',
                            'episode', 'file', 'rating'])
    by_show = {}
    for episode in episodes:
        by_show.setdefault(episode.get('tvshowid'), []).append(episode)

    shows = [s for s in query_items('VideoLibrary.GetTVShows',
                                    ['title', 'file', 'rating'])
             if matches(s.get('title'))
             or any(matches(e.get('showtitle')) or matches(e.get('title'))
                    for e in by_show.get(s.get('tvshowid')) or [])]
    log('repair-all: seriálů v rozsahu %d' % len(shows))
    for idx, show in enumerate(shows, 1):
        wait_no_playback(monitor)
        show_id = show.get('tvshowid')
        scoped = by_show.get(show_id) or []
        try:
            # 1) identita nfo (show) + smazání epizodových nfo - PŘED
            #    refreshem, ať se přescrapuje už čistý stav
            action = repair_show_nfo(show, [e.get('file') or ''
                                            for e in scoped])
            nfo_fixed = [repair_episode_nfo(e) for e in scoped]
            # 2) jeden dávkový refresh: show + všechny epizody; jen pokud
            #    je co opravovat (chybí rating u show/epizod, nebo bylo
            #    přepsáno nfo)
            needs = (action == 'rewritten'
                     or not (show.get('rating') or 0)
                     or any(not (e.get('rating') or 0) for e in scoped)
                     or any(a == 'deleted' for a in nfo_fixed))
            refreshed = False
            if needs:
                refreshed = monitor.refresh_show_stale(show, scoped)
                changed = changed or refreshed
            report('tvshow | %s | %s%s'
                   % (show.get('title'), action,
                      ' | refresh' if needs else ''))
            if idx % 10 == 0 or idx == len(shows):
                log('repair-all: seriálů hotovo %d/%d' % (idx, len(shows)))
        except Exception as err:
            report('tvshow | %s | chyba: %s' % (show.get('title'), err))

    if changed:
        export_library()
    write_report()
    state = load_state()
    state['last_run'] = now_str()
    save_state(state)
    log('repair-all hotový (%d položek)' % len(REPORT_LINES))


def main():
    monitor = NFOSync()
    resume_guards()  # kdyby Kodi padl uprostřed cyklu (guards_paused ve state)
    log('start (profil: %s)' % PROFILE)
    while not monitor.abortRequested():
        if (not monitor.busy and monitor.quiet_until is not None
                and time.time() >= monitor.quiet_until):
            # hlídky se nevracejí hned po cyklu: naše zápisy nfo by je
            # vyvolaly ke skenu, který by blokoval refreshy (smyčka
            # editace->watchdog->sken, pozorováno 1. 11. 2026)
            monitor.quiet_until = None
            resume_guards()
        if not monitor.busy and xbmcvfs.exists(MARKER_FILE):
            monitor.busy = True
            try:
                pause_guards()
                wait_library_idle(monitor)
                try:
                    opts = json.loads(vfs_read(MARKER_FILE))
                except Exception:
                    opts = {}
                repair_all(monitor, opts if isinstance(opts, dict) else {})
            except Exception as err:
                log('repair-all selhal: %s' % err, xbmc.LOGERROR)
            finally:
                monitor.quiet_until = time.time() + GUARD_QUIET_S
                xbmcvfs.delete(MARKER_FILE)
                monitor.busy = False
        elif (not monitor.busy and monitor.scan_at
              and time.time() - monitor.scan_at >= DEBOUNCE_S):
            monitor.scan_at = None
            monitor.busy = True
            try:
                pause_guards()
                wait_library_idle(monitor)
                incremental(monitor)
            except Exception as err:
                log('cyklus selhal: %s' % err, xbmc.LOGERROR)
            finally:
                monitor.quiet_until = time.time() + GUARD_QUIET_S
                monitor.busy = False
        if monitor.waitForAbort(POLL_S):
            break
    resume_guards()
    log('stop')


if __name__ == '__main__':
    main()
