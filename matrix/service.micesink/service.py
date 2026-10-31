# SPDX-License-Identifier: GPL-3.0-or-later
#
# MICE (Miracast over Infrastructure, MS-MICE) sink jako Kodi service
# doplnek - kiosk se hlasi jako "network display". Inzerce bezi pres
# avahi (_display._tcp, port 7250 - soubor nasazuje role kiosk_mice).
# Zdroj (GNOME Network Displays / Windows) se pripoji na TCP 7250 a posle
# SOURCE_READY; doplnek pak otevre RTSP spojeni ZPET na zdroj (sink je
# RTSP klient, zdroj RTSP server), vyjedna WFD parametry (M1-M7) a po
# PLAY prehraje prichazejici RTP/MPEG-TS (H264+AAC) Kodim -
# PlayMedia(rtp://) - dekoder (HW na RPi5), renderer i ALSA sink resi
# Kodi player. Zadny GStreamer, zadne okno navic.
#
# Kolize s Steam Linkem/Moonlighttem resi shared flock
# /run/lock/kiosk-stream.lock (stejny soubor pouzivaji oba wrappery):
# behem castu drzi lock tento doplnek, SL/Moonlight pak exitnou kodem 2
# ("uz bezi"); naopak bezici SL/Moonlight odmitne doplnek novy cast.
#
# Reference: MS-MICE spec (learn.microsoft.com); gnome-network-displays
# src/wfd/ + nd-wfd-mice-sink.c (GPL-3.0, presne poradi M1-M7 a tvary
# parametru); lazycast d2.py (funkcni MICE sink, dukaz wfd_video_formats
# retezce). Licence tohoto souboru: GPLv3+.

import fcntl
import os
import socket
import threading
import time

import xbmc

MICE_PORT = 7250
RTP_PORT = 16384
RTCP_PORT = 16385
RTSP_DEFAULT_PORT = 7236
LOCK_FILE = "/run/lock/kiosk-stream.lock"
STREAM_URL = "rtp://0.0.0.0:%d" % RTP_PORT
# s: Kodi bindne RTP port az po otevreni demuxeru - ranne pakety se
# ztrati, IDR cteme az po teto grace dobe
PLAY_BIND_GRACE = 5
# s: limit na cely RTSP handshake
ESTABLISH_TIMEOUT = 20

PUBLIC = ("org.wfa.wfd1.0, OPTIONS, DESCRIBE, GET_PARAMETER, PAUSE, "
          "PLAY, SETUP, SET_PARAMETER, TEARDOWN")

# Presne tvary, ktere g-n-d parsuje (wfd-params.c): AAC musi mit modes
# bit 0x1 (48 kHz/2ch), jinak zdroj audio vubec nevybere. Video: H264
# baseline profil (0x01), level 0x10, CEA maska 0001FFFF (az 1080p60),
# VESA/HH masky z funkcniho lazycast retezce.
# Audio: AAC (g-n-d) i LPCM (Windows / starsi g-n-d) - zdroj si vybere,
# Kodi prehraje oboji (MPEG-TS nese audio tak jak je).
CAPABILITIES = (
    "wfd_client_rtp_ports: RTP/AVP/UDP;unicast %d %d mode=play\r\n"
    "wfd_audio_codecs: AAC 00000001 00, LPCM 00000002 00\r\n"
    "wfd_video_formats: 00 00 01 10 0001FFFF 3FFFFFFF 00000FFF 00 0000"
    " 0000 00 none none\r\n" % (RTP_PORT, RTCP_PORT))

CMD_SOURCE_READY = 0x01
CMD_STOP_PROJECTION = 0x02
TLV_RTSP_PORT = 0x02


def log(msg):
    xbmc.log("[service.micesink] %s" % msg, level=xbmc.LOGINFO)


class RtspSession(threading.Thread):
    """WFD RTSP handshake a drzeni session voci zdroji (sink = klient).

    g-n-d zdroj je GstRTSPServer na 7236; po connectu posle M1 OPTIONS,
    ceka na nas M2 OPTIONS, posle M3 GET_PARAMETER (parametry), M4
    SET_PARAMETER (zvolene formaty), M5 trigger SETUP; my pak posleme
    M6 SETUP + M7 PLAY a Kodi prehrava RTP stream.
    """

    def __init__(self, src_ip, rtsp_port):
        super(RtspSession, self).__init__(daemon=True)
        self.src = src_ip
        self.port = rtsp_port
        self.sock = None
        self.buf = b""
        self.cseq = 0
        self.session_id = None
        self.stop_flag = threading.Event()
        self.play_started = None
        self.deadline = 0

    # -- nastroje ------------------------------------------------------
    def _send(self, data):
        self.sock.sendall(data.encode())

    def _next_msg(self, timeout):
        """(first_line, headers, body) nebo None po timeoutu.

        Headers lowercase; telo dle Content-Length.
        """
        end = time.time() + timeout
        while True:
            if b"\r\n\r\n" in self.buf:
                head, _, rest = self.buf.partition(b"\r\n\r\n")
                lines = head.decode(errors="replace").split("\r\n")
                headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        key, val = line.split(":", 1)
                        headers[key.strip().lower()] = val.strip()
                clen = int(headers.get("content-length", "0") or 0)
                if len(rest) >= clen:
                    body = rest[:clen]
                    self.buf = rest[clen:]
                    return lines[0], headers, body
            remain = end - time.time()
            if remain <= 0:
                return None
            self.sock.settimeout(remain)
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("RTSP spojeni zavreno zdrojem")
            self.buf += chunk

    def _reply(self, cseq, body=b"", public=False):
        extra = "Public: %s\r\n" % PUBLIC if public else ""
        if body:
            self._send("RTSP/1.0 200 OK\r\nCSeq: %s\r\n%s"
                       "Content-Type: text/parameters\r\n"
                       "Content-Length: %d\r\n\r\n%s"
                       % (cseq, extra, len(body), body.decode()))
        else:
            self._send("RTSP/1.0 200 OK\r\nCSeq: %s\r\n%s\r\n"
                       % (cseq, extra))

    def _serve_request(self, first, headers, body):
        """Zpracuje pozadavek zdroje (M1/M3/M4/M5/keep-alive/TEARDOWN)."""
        method = first.split(" ")[0]
        cseq = headers.get("cseq", "0")
        if method == "OPTIONS":
            self._reply(cseq, public=True)
        elif method == "GET_PARAMETER":
            # M3 (dotaz na parametry) i keep-alive (prazdne telo) -
            # vratit capabilities vdy, nevadi to
            self._reply(cseq, body=CAPABILITIES.encode())
        elif method == "SET_PARAMETER":
            self._reply(cseq)
        elif method == "TEARDOWN":
            self._reply(cseq)
            self.stop_flag.set()
        else:
            self._send("RTSP/1.0 501 Not Implemented\r\nCSeq: %s\r\n\r\n"
                       % cseq)

    def _request(self, method, uri, extra="", body=""):
        """Posle pozadavek a ceka 200 odpoved se stejnym CSeq.

        Prichozi pozadavky zdroje mezitim obslouzi (handshake je
        lockstep, ale odpovedi a pozadavky se mohou prolinat).
        """
        self.cseq += 1
        self._send("%s %s RTSP/1.0\r\nCSeq: %d\r\n%s\r\n%s"
                   % (method, uri, self.cseq, extra, body))
        while time.time() < self.deadline:
            msg = self._next_msg(5)
            if msg is None:
                continue
            first, headers, _ = msg
            if first.startswith("RTSP/"):
                if headers.get("cseq") == str(self.cseq):
                    code = first.split(" ")[1]
                    if code != "200":
                        raise RuntimeError("RTSP %s -> %s" % (method, first))
                    return headers
                log("zastupna odpoved (%s) ignorovana"
                    % headers.get("cseq"))
            else:
                self._serve_request(first, headers, _)
        raise TimeoutError("handshake timeout (cekani na odpoved %s)"
                           % method)

    def _wait_request(self, method, match=None):
        """Ceka na pozadavek `method` (prip. s `match` v tele), odpovi,
        vrati telo. Jine zpravy mezitim obslouzi."""
        while time.time() < self.deadline:
            msg = self._next_msg(5)
            if msg is None:
                continue
            first, headers, body = msg
            if first.startswith("RTSP/"):
                continue
            self._serve_request(first, headers, body)
            if first.split(" ")[0] == method \
                    and (match is None or match in body):
                return body
        raise TimeoutError("handshake timeout (cekani na %s)" % method)

    # -- session -------------------------------------------------------
    def run(self):
        try:
            self.sock = socket.create_connection((self.src, self.port),
                                                 timeout=8)
            self.deadline = time.time() + ESTABLISH_TIMEOUT
            base = "rtsp://%s:%d" % (self.src, self.port)

            # M1: zdroj posle OPTIONS (~500 ms po connectu)
            self._wait_request("OPTIONS")
            # M2: nas OPTIONS az po M1 - g-n-d by jinak prepl do rezimu
            # plain-RTSP (test pro VLC)
            self._request("OPTIONS", base + "/wfd1.0",
                          'Require: org.wfa.wfd1.0\r\n')
            # M3: GET_PARAMETER - odpoved s capabilities posle
            # _serve_request uvnitr _wait_request
            self._wait_request("GET_PARAMETER")
            # M4: SET_PARAMETER se zvolenymi formaty
            self._wait_request("SET_PARAMETER", match=b"wfd_video_formats")
            # M5: trigger SETUP
            self._wait_request("SET_PARAMETER",
                               match=b"wfd_trigger_method")
            # M6: SETUP - nase deklarovane RTP porty
            headers = self._request(
                "SETUP", base + "/wfd1.0/streamid=0",
                "Transport: RTP/AVP/UDP;unicast;client_port=%d-%d\r\n"
                % (RTP_PORT, RTCP_PORT))
            self.session_id = headers.get("session", "").split(";")[0]
            # M7: PLAY - zdroj zacina streamovat
            self._request("PLAY", base + "/wfd1.0/streamid=0",
                          "Session: %s\r\n" % self.session_id)

            log("stream od %s - prehravam %s" % (self.src, STREAM_URL))
            xbmc.executebuiltin("PlayerControl(Stop)")
            xbmc.executebuiltin('PlayMedia("%s")' % STREAM_URL)
            self.play_started = time.time()

            # IDR: Kodi bindne UDP port az ~1-2 s po PlayMedia, ranne
            # pakety padnou - vynutime klicovy snimek, az je demuxer nahore
            time.sleep(2)
            idr = "wfd_idr_request: none\r\n"
            self._request("SET_PARAMETER", base + "/wfd1.0",
                          "Content-Type: text/parameters\r\n"
                          "Content-Length: %d\r\n" % len(idr), body=idr)

            self._stream_loop()
        except Exception as exc:  # noqa: BLE001 - service addon loguje vse
            log("session konci: %s" % exc)
        finally:
            self._close()

    def _stream_loop(self):
        """Odpovidat na keep-alive (GET_PARAMETER ~25 s), hlidat konec
        prehravani (zdroj zmizel / uzivatel stopl / Kodi selhalo)."""
        player = xbmc.Player()
        while not self.stop_flag.is_set():
            try:
                self.sock.settimeout(1.0)
                chunk = self.sock.recv(4096)
                if not chunk:
                    log("RTSP spojeni zavreno zdrojem")
                    break
                self.buf += chunk
                while True:
                    msg = self._next_msg(0)
                    if msg is None:
                        break
                    first, headers, body = msg
                    if not first.startswith("RTSP/"):
                        self._serve_request(first, headers, body)
            except socket.timeout:
                pass
            if self.play_started \
                    and time.time() - self.play_started > PLAY_BIND_GRACE \
                    and not player.isPlaying():
                log("Kodi uz neprehrava - ukoncuji session")
                break

    def _close(self):
        try:
            if self.sock and self.session_id:
                self.cseq += 1
                self._send(
                    "TEARDOWN rtsp://%s:%d/wfd1.0/streamid=0 RTSP/1.0\r\n"
                    "CSeq: %d\r\nSession: %s\r\n\r\n"
                    % (self.src, self.port, self.cseq, self.session_id))
        except Exception:  # noqa: BLE001 - zavirani nesmi selhat
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:  # noqa: BLE001
            pass
        xbmc.executebuiltin("PlayerControl(Stop)")


class MiceSink(object):
    """TCP 7250 listener + sprava aktualni session a stream locku."""

    def __init__(self):
        self.state_lock = threading.Lock()
        self.session = None
        self.lock_fd = None

    def stop_active(self):
        with self.state_lock:
            session = self.session
        if session:
            log("STOP_PROJECTION od zdroje")
            session.stop_flag.set()

    def handle_source(self, conn, src_ip):
        """Nove spojeni na 7250 (vlakno na klienta)."""
        try:
            conn.settimeout(5)
            data = b""
            while len(data) < 4:
                chunk = conn.recv(64)
                if not chunk:
                    return
                data += chunk
            total = int.from_bytes(data[0:2], "big")
            while 0 < total <= 4096 and len(data) < total:
                chunk = conn.recv(256)
                if not chunk:
                    break
                data += chunk
            cmd = data[3]
            if cmd == CMD_STOP_PROJECTION:
                self.stop_active()
                return
            if cmd != CMD_SOURCE_READY:
                log("neznamy MICE prikaz 0x%02x od %s" % (cmd, src_ip))
                return
            # RTSP port z TLV 0x02 (g-n-d posila 7236)
            rtsp_port = RTSP_DEFAULT_PORT
            i = 4
            while i + 3 <= len(data):
                tlv_type = data[i]
                tlv_len = int.from_bytes(data[i + 1:i + 3], "big")
                value = data[i + 3:i + 3 + tlv_len]
                if tlv_type == TLV_RTSP_PORT and len(value) == 2:
                    rtsp_port = int.from_bytes(value, "big")
                i += 3 + tlv_len
            self.start_session(conn, src_ip, rtsp_port)
        except Exception as exc:  # noqa: BLE001 - listener musi zit dal
            log("chyba MICE spojeni od %s: %s" % (src_ip, exc))
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def start_session(self, conn, src_ip, rtsp_port):
        fd = os.open(LOCK_FILE, os.O_WRONLY | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            log("stream lock drzi SL/Moonlight/jiny cast - odmitam %s"
                % src_ip)
            return
        session = RtspSession(src_ip, rtsp_port)
        with self.state_lock:
            if self.session is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                return
            self.session = session
            self.lock_fd = fd
        log("MICE session se %s:%d (RTSP)" % (src_ip, rtsp_port))
        session.start()

        # 7250 spojeni drzet otevrene; STOP_PROJECTION = konec
        conn.settimeout(1.0)
        while session.is_alive():
            try:
                chunk = conn.recv(64)
                if not chunk:
                    break
                if len(chunk) >= 4 and chunk[3] == CMD_STOP_PROJECTION:
                    session.stop_flag.set()
                    break
            except socket.timeout:
                continue
            except OSError:
                break
        session.stop_flag.set()
        session.join(5)
        with self.state_lock:
            self.session = None
            if self.lock_fd is not None:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                os.close(self.lock_fd)
                self.lock_fd = None
        log("session s %s ukoncena, stream lock uvolnen" % src_ip)


def start_listener(sink):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", MICE_PORT))
        srv.listen(4)
    except OSError as exc:
        log("NELZE poslouchat na TCP %d: %s" % (MICE_PORT, exc))
        return
    log("MICE sink poslucha na TCP %d, RTP %d-%d"
        % (MICE_PORT, RTP_PORT, RTCP_PORT))
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            break
        threading.Thread(target=sink.handle_source,
                         args=(conn, addr[0]), daemon=True).start()


if __name__ == "__main__":
    log("start (Kodi service addon)")
    sink = MiceSink()
    threading.Thread(target=start_listener, args=(sink,),
                     daemon=True).start()
    monitor = xbmc.Monitor()
    while not monitor.waitForAbort(10):
        pass
    log("konec (Kodi shutdown)")
