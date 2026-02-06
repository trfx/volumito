#!/usr/bin/env python3

"""
Volumito V1 - alternative TUI using urwid (if available)

This file is a lightweight rework that tries another TUI framework (urwid)
which generally handles colors and attributes more reliably across terminals.

If urwid is not installed, the script will instruct how to install it.
"""

import os
import sys
import threading
import time
import yaml
import requests
import urwid

class VolumitoV1:
    def __init__(self):
        self.config = self.check_config()
        self.status = {}
        self._status_lock = threading.Lock()
        self.session = requests.Session()
        self._stop_event = threading.Event()
        self.updater = None
        self.loop = None
        # recent seek target to avoid UI bounce (tuple: (seconds, timestamp))
        self._recent_seek = None

    def check_config(self, config_dir="~/.config/volumito/"):
        config_dir = os.path.expanduser(config_dir)
        if not os.path.exists(config_dir):
            os.makedirs(config_dir)
        config_file = os.path.join(config_dir, "config.yaml")
        if not os.path.exists(config_file):
            with open(config_file, "w") as f:
                f.write("volumio_host: volumio.local\n")
            print(f"Created default config file at {config_file}. Please edit it to set your Volumio host and run again.")
            sys.exit(0)
        with open(config_file, "r") as f:
            content = f.read()
            if "volumio_host" not in content:
                print(f"Config file at {config_file} is missing volumio_host. Please update it.")
                sys.exit(1)
            try:
                return yaml.safe_load(content)
            except Exception as e:
                print(f"Error parsing config: {e}")
                sys.exit(1)

    def read_status(self):
        url = "http://" + self.config.get("volumio_host") + "/api/v1/getState"
        try:
            r = self.session.get(url, timeout=3)
            if r.status_code != 200:
                return {}
            return r.json()
        except Exception:
            return {}

    def _updater(self):
        while not self._stop_event.is_set():
            s = self.read_status()
            if isinstance(s, dict):
                with self._status_lock:
                    self._merge_status(s)
            time.sleep(1)

    def build_ui(self):
        # Widgets that will be updated
        self.header = urwid.Text(('header', 'Volumito - A Simple TUI for Volumio'), align='left')
        self.title_label = urwid.Text('Current Track: ')
        self.title_value = urwid.Text('?', wrap='clip')
        self.artist_label = urwid.Text('Artist: ')
        self.artist_value = urwid.Text('?', wrap='clip')
        self.album_label = urwid.Text('Album: ')
        self.album_value = urwid.Text('?', wrap='clip')
        self.state_label = urwid.Text('Playback State: ')
        self.state_value = urwid.Text('?', wrap='clip')
        self.bitrate_label = urwid.Text('Sample Rate: ')
        self.bitrate_value = urwid.Text('?', wrap='clip')
        self.elapsed_label = urwid.Text('Elapsed: ')
        self.elapsed_value = urwid.Text('--:--', wrap='clip')
        self.length_label = urwid.Text('Length: ')
        self.length_value = urwid.Text('--:--', wrap='clip')
        self.volume_label = urwid.Text('Volume: ')
        self.volume_text = urwid.Text('--')
        self.server_label = urwid.Text('Server: ')
        self.server_text = urwid.Text(self.config.get("volumio_host", "-"))
        self.last_key = urwid.Text('')
        # progress bar for visual track progress
        self.progress = urwid.ProgressBar('pg normal', 'pg complete', 0, 100)

        # Rows with colored value using palette entry 'title'
        row_title = urwid.Columns([
            ('fixed', 16, self.title_label),
            urwid.AttrMap(self.title_value, 'highlight')
        ])
        row_artist = urwid.Columns([
            ('fixed', 16, self.artist_label),
            urwid.AttrMap(self.artist_value, 'highlight')
        ])
        row_album = urwid.Columns([
            ('fixed', 16, self.album_label),
            urwid.AttrMap(self.album_value, 'normal')
        ])
        row_state = urwid.Columns([
            ('fixed', 16, self.state_label),
            urwid.AttrMap(self.state_value, 'normal')
        ])
        row_bitrate = urwid.Columns([
            ('fixed', 16, self.bitrate_label),
            urwid.AttrMap(self.bitrate_value, 'normal')
        ])
        row_elapsed = urwid.Columns([
            ('fixed', 16, self.elapsed_label),
            urwid.AttrMap(self.elapsed_value, 'normal')
        ])
        row_length = urwid.Columns([
            ('fixed', 16, self.length_label),
            urwid.AttrMap(self.length_value, 'normal')
        ])
        row_volume = urwid.Columns([
            ('fixed', 16, self.volume_label),
            urwid.AttrMap(self.volume_text, 'normal')
        ])

        row_server = urwid.Columns([
            ('fixed', 16, self.server_label),
            urwid.AttrMap(self.server_text, 'normal')
        ])

        # progress bar for visual track progress

        legend = urwid.AttrMap(urwid.Text('+:- vol | p: play/pause | <: prev | >: next | q: quit'), 'bold')

        pile = urwid.Pile([
            urwid.AttrMap(self.header, 'header'),
            row_title,
            row_artist,
            row_album,
            row_state,
            row_bitrate,
            row_elapsed,
            row_length,
            row_volume,
            row_server,
            self.progress,
            legend,
            urwid.Divider(),
            self.last_key,
        ])
        filler = urwid.Filler(pile, valign='top')
        return filler

    def refresh_ui(self, loop=None, user_data=None):
        with self._status_lock:
            s = dict(self.status)
        title = s.get('title', '-')
        artist = s.get('artist', '-')
        album = s.get('album', '-')
        state = s.get('status', '-')
        samplerate = s.get('samplerate')
        bitrate = s.get('bitrate')
        # Prefer samplerate, fall back to bitrate, then to '-'
        display_rate = samplerate if samplerate is not None else bitrate
        display_rate = '-' if display_rate is None else str(display_rate)
        vol = s.get('volume', '-')

        self.title_value.set_text(title)
        self.artist_value.set_text(artist)
        self.album_value.set_text(album)
        self.state_value.set_text(state)
        self.bitrate_value.set_text(display_rate)
        # update volume text
        try:
            vnum = int(vol) if vol is not None else 0
        except Exception:
            try:
                vnum = int(float(vol))
            except Exception:
                vnum = 0
        self.volume_text.set_text(f"{vnum}")

        # update progress bar
        def _parse_time(v):
            if v is None:
                return None
            try:
                return float(v)
            except Exception:
                pass
            try:
                parts = [float(x) for x in str(v).split(':')]
                if len(parts) == 3:
                    return parts[0] * 3600 + parts[1] * 60 + parts[2]
                if len(parts) == 2:
                    return parts[0] * 60 + parts[1]
                return float(parts[0])
            except Exception:
                return None

        # helper to search nested dict/list for candidate keys and return all matches
        def _deep_find_all(obj, candidates):
            res = []
            if obj is None:
                return res
            if isinstance(obj, dict):
                for k, v in obj.items():
                    kl = k.lower()
                    for cand in candidates:
                        if cand in kl:
                            res.append(v)
                    res.extend(_deep_find_all(v, candidates))
            elif isinstance(obj, list):
                for item in obj:
                    res.extend(_deep_find_all(item, candidates))
            return res

        # collect candidate values (prefer top-level if present)
        seek_candidates = []
        dur_candidates = []
        for k in ('seek', 'position', 'elapsed', 'progress'):
            if k in s:
                seek_candidates.append(s.get(k))
        for k in ('duration', 'trackDuration', 'totalTime', 'length', 'tracklength'):
            if k in s:
                dur_candidates.append(s.get(k))
        # extend with deep search
        seek_candidates.extend(_deep_find_all(s, ['seek', 'position', 'elapsed', 'progress']))
        dur_candidates.extend(_deep_find_all(s, ['duration', 'trackduration', 'totaltime', 'length', 'tracklength', 'time', 'total']))

        def _to_number(v):
            t = _parse_time(v)
            if t is None:
                return None
            return float(t)

        # try combinations of raw vs milliseconds conversion for seek/duration
        seek_s = None
        dur_s = None
        pairs = []
        for sv_raw in seek_candidates:
            svn = _to_number(sv_raw)
            if svn is None:
                continue
            for dv_raw in dur_candidates:
                dvn = _to_number(dv_raw)
                if dvn is None:
                    continue
                for sv_factor in (1.0, 1.0/1000.0):
                    for dv_factor in (1.0, 1.0/1000.0):
                        try:
                            svs = svn * sv_factor
                            dvs = dvn * dv_factor
                        except Exception:
                            continue
                        # plausible: seek between 0 and duration (allow small overshoot) and duration reasonable (<10h)
                        if 0 <= svs <= dvs * 1.1 and 0 < dvs < 36000:
                            pairs.append((svs, dvs, sv_factor, dv_factor))
        # prefer pairs where duration was not treated as milliseconds (dv_factor == 1.0), then smaller duration
        if pairs:
            pairs.sort(key=lambda x: (0 if x[3] == 1.0 else 1, x[1]))
            seek_s, dur_s, _, _ = pairs[0]
        else:
            # fallback: take first sensible duration (prefer not-converted)
            dur_s = None
            for dv_raw in dur_candidates:
                dvn = _to_number(dv_raw)
                if dvn is None:
                    continue
                if 0 < dvn < 36000:
                    dur_s = dvn
                    break
                if dvn > 1000:
                    # try ms->s
                    try:
                        cand = dvn / 1000.0
                        if 0 < cand < 36000:
                            dur_s = cand
                            break
                    except Exception:
                        pass
            seek_s = None
            for sv_raw in seek_candidates:
                svn = _to_number(sv_raw)
                if svn is None:
                    continue
                if 0 <= svn <= (dur_s or float('inf')) * 1.1:
                    seek_s = svn
                    break
            # final fallback convert if needed
            if seek_s is None:
                for sv_raw in seek_candidates:
                    svn = _to_number(sv_raw)
                    if svn is None:
                        continue
                    if svn > 1000:
                        seek_s = svn / 1000.0
                        break
                    seek_s = svn
            if dur_s is None:
                dur_s = 0

        if dur_s and dur_s > 0:
            pct = int(max(0, min(100, (seek_s / dur_s) * 100)))
            try:
                self.progress.set_completion(pct)
            except Exception:
                pass
            def _fmt(t):
                m = int(t // 60)
                sec = int(t % 60)
                return f"{m:02d}:{sec:02d}"
            try:
                self.elapsed_value.set_text(_fmt(seek_s))
                self.length_value.set_text(_fmt(dur_s))
            except Exception:
                pass
        else:
            try:
                self.progress.set_completion(0)
                self.elapsed_value.set_text('--:--')
                self.length_value.set_text('--:--')
            except Exception:
                pass

        # schedule next refresh
        if self.loop:
            self.loop.set_alarm_in(0.25, self.refresh_ui)

    def unhandled_input(self, key):
        # store last key for feedback (show ord if a single char)
        try:
            if isinstance(key, str) and len(key) == 1:
                self.last_key.set_text(f"Last key: {repr(key)} ord={ord(key)}")
            else:
                self.last_key.set_text(f"Last key: {repr(key)}")
        except Exception:
            self.last_key.set_text(f"Last key: {repr(key)}")

        # normalize key for matching
        kstr = key if isinstance(key, str) else str(key)
        klower = kstr.lower()

        # quit
        if klower == 'q':
            raise urwid.ExitMainLoop()

        # volume
        if kstr in ('+', '=', 'plus') or klower in ('+', '=', 'plus'):
            self._change_volume(+2)
            return
        if kstr in ('-', 'minus') or klower in ('-', 'minus'):
            self._change_volume(-2)
            return

        # play/pause
        if klower in ('p',):
            self._toggle_play()
            return

        # previous: accept comma, '<', left arrow names and common variants
        if kstr in (',', '<') or klower in ('left', 'key_left') or (isinstance(key, tuple) and 'left' in str(key).lower()):
            self._send_cmd('prev')
            return

        # seek backward/forward 30s
        if kstr in ('[',):
            self._seek_relative(-30)
            return
        if kstr in (']',):
            self._seek_relative(30)
            return

        # next: accept '.', '>', right arrow names and common variants
        if kstr in ('.', '>') or klower in ('right', 'key_right') or (isinstance(key, tuple) and 'right' in str(key).lower()):
            self._send_cmd('next')
            return

    def _change_volume(self, delta):
        try:
            with self._status_lock:
                cur = int(self.status.get('volume', 0))
        except Exception:
            cur = 0
        new = max(0, min(100, cur + delta))
        with self._status_lock:
            self.status['volume'] = new
        url = "http://" + self.config.get("volumio_host") + "/api/v1/commands/?cmd=volume&volume=" + str(new)
        threading.Thread(target=lambda: self._safe_get(url), daemon=True).start()

    def _toggle_play(self):
        with self._status_lock:
            cur = str(self.status.get('status', '')).lower()
        if 'play' in cur:
            cmd = 'pause'
            new_state = 'pause'
        else:
            cmd = 'play'
            new_state = 'play'
        with self._status_lock:
            self.status['status'] = new_state
        url = "http://" + self.config.get("volumio_host") + "/api/v1/commands/?cmd=" + cmd
        threading.Thread(target=lambda: self._safe_get(url), daemon=True).start()

    def _send_cmd(self, cmd):
        url = "http://" + self.config.get("volumio_host") + "/api/v1/commands/?cmd=" + cmd
        # send the command and then refresh state to reflect track change quickly
        def _worker(u):
            try:
                self._safe_get(u)
            except Exception:
                pass
            # attempt to fetch updated state
            try:
                r = self.session.get("http://" + self.config.get("volumio_host") + "/api/v1/getState", timeout=2)
                if r.status_code == 200:
                    with self._status_lock:
                        self._merge_status(r.json())
            except Exception:
                pass
        threading.Thread(target=_worker, args=(url,), daemon=True).start()

    def _merge_status(self, new):
        """Merge new status dict into self.status while avoiding seek/position bounce
        if a recent seek was requested, prefer the recent seek unless the device's
        reported seek is close to the requested one."""
        if not isinstance(new, dict):
            return
        recent = getattr(self, '_recent_seek', None)
        if recent and (time.time() - recent[1]) < 3:
            target = recent[0]
            # try to extract reported seek/position from new
            reported = None
            for k in ('seek', 'position', 'elapsed', 'progress'):
                if k in new:
                    try:
                        rv = float(new[k])
                        # convert ms to s heuristically
                        if rv > 1000:
                            rv = rv / 1000.0
                        reported = rv
                        break
                    except Exception:
                        continue
            if reported is not None and abs(reported - target) <= 2:
                # device has caught up to requested seek; accept full update
                self.status.update(new)
                self._recent_seek = None
                return
            # otherwise ignore seek/position keys from device so UI keeps requested position
            cleaned = dict(new)
            for k in ('seek', 'position', 'elapsed', 'progress'):
                cleaned.pop(k, None)
            self.status.update(cleaned)
            return
        # normal merge
        self.status.update(new)

    def _safe_get(self, url):
        try:
            self.session.get(url, timeout=3)
        except Exception:
            pass

    def _seek_relative(self, delta_seconds):
        """Seek relative by delta_seconds (positive or negative). Attempts several seek command variants and updates UI immediately."""
        with self._status_lock:
            s = dict(self.status)
        def _to_seconds_simple(v):
            if v is None:
                return None
            try:
                f = float(v)
            except Exception:
                try:
                    parts = [float(x) for x in str(v).split(':')]
                    if len(parts) == 3:
                        return parts[0]*3600 + parts[1]*60 + parts[2]
                    if len(parts) == 2:
                        return parts[0]*60 + parts[1]
                    return float(parts[0])
                except Exception:
                    return None
            if f > 1000:
                return f/1000.0
            return f

        seek = None
        for k in ('seek', 'position', 'elapsed', 'progress'):
            if k in s:
                seek = s.get(k)
                break
        duration = None
        for k in ('duration', 'trackDuration', 'totalTime', 'length', 'tracklength'):
            if k in s:
                duration = s.get(k)
                break

        seek_s = _to_seconds_simple(seek) or 0
        dur_s = _to_seconds_simple(duration) or None

        new_pos = max(0, seek_s + float(delta_seconds))
        if dur_s:
            new_pos = min(new_pos, dur_s)

        with self._status_lock:
            try:
                orig_seek = s.get('seek')
                if orig_seek is not None and isinstance(orig_seek, (int, float)) and float(orig_seek) > 1000:
                    self.status['seek'] = int(new_pos * 1000)
                else:
                    self.status['seek'] = int(new_pos)
                self.status['position'] = int(new_pos)
            except Exception:
                self.status['seek'] = int(new_pos)
                self.status['position'] = int(new_pos)
            # mark recent seek to avoid immediate bounce from device-reported old position
            try:
                self._recent_seek = (float(new_pos), time.time())
            except Exception:
                self._recent_seek = (new_pos, time.time())

        secs = int(new_pos)
        msecs = int(new_pos * 1000)
        candidates = [
            f"http://{self.config.get('volumio_host')}/api/v1/commands/?cmd=seek&value={secs}",
            f"http://{self.config.get('volumio_host')}/api/v1/commands/?cmd=seek&position={secs}",
            f"http://{self.config.get('volumio_host')}/api/v1/commands/?cmd=seek&seek={msecs}",
            f"http://{self.config.get('volumio_host')}/api/v1/commands/?cmd=seek&seek={secs}",
        ]

        def _worker(u_list):
            for u in u_list:
                try:
                    self.session.get(u, timeout=2)
                except Exception:
                    continue
            try:
                r = self.session.get(f"http://{self.config.get('volumio_host')}/api/v1/getState", timeout=2)
                if r.status_code == 200:
                    with self._status_lock:
                        self._merge_status(r.json())
            except Exception:
                pass

        threading.Thread(target=_worker, args=(candidates,), daemon=True).start()

    def run(self):
        # start updater thread
        self.updater = threading.Thread(target=self._updater, daemon=True)
        self.updater.start()

        # build UI
        top = self.build_ui()
        palette = [
            ('header', 'bold', 'dark gray'),
            ('highlight', 'standout', 'default'),
            ('normal', 'default', 'default'),
            ('bold', 'bold', 'dark gray'),
            ('pg normal', 'default', 'dark gray'),
            ('pg complete', 'white', 'dark blue'),
        ]
        self.loop = urwid.MainLoop(top, palette, unhandled_input=self.unhandled_input)
        # start periodic UI refresh
        self.loop.set_alarm_in(0.1, self.refresh_ui)
        try:
            self.loop.run()
        finally:
            self._stop_event.set()
            if self.updater:
                self.updater.join(timeout=1)


def main():
    v = VolumitoV1()
    v.run()


if __name__ == '__main__':
    main()
