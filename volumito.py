#!/usr/bin/env python3

"""
Volumito V1 - alternative TUI using urwid (if available)

This file is a lightweight rework that tries another TUI framework (urwid)
which generally handles colors and attributes more reliably across terminals.

If urwid is not installed, the script will instruct how to install it.

TODO:
- Add error handling for network issues and display a message in the UI
- Add support for more Volumio commands (e.g. stop, repeat)
- Add google/wikipedia search keybind for current track/artist/album
"""

import os
import sys
import threading
import time

try:
    import yaml
except ImportError:
    print("PyYAML is not installed. Please install it with: pip install pyyaml")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("requests is not installed. Please install it with: pip install requests")
    sys.exit(1)

try:
    import socketio
except ImportError:
    print("python-socketio is not installed. Please install it with: pip install python-socketio")
    sys.exit(1)

try:
    import urwid
except ImportError:
    print("urwid is not installed. Please install it with: pip install urwid")
    sys.exit(1)

try:
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
except ImportError:
    print("zeroconf is not installed. Please install it with: pip install zeroconf")
    sys.exit(1)


VOLUMIO_SERVICE = "_Volumio._tcp.local."


def _safe_str(v, fallback='-'):
    """Return str(v) unless v is None or empty, in which case return fallback."""
    if v is None:
        return fallback
    s = str(v)
    return s if s.strip() else fallback


def discover_volumio_devices(timeout=2):
    """Return Volumio devices advertised on the local network via mDNS."""
    devices = []
    lock = threading.Lock()

    class Listener(ServiceListener):
        def add_service(self, zeroconf, service_type, name):
            info = zeroconf.get_service_info(service_type, name, timeout=1000)
            if not info:
                return
            addresses = info.parsed_addresses()
            address = next(
                (address for address in addresses if ":" not in address),
                None,
            )
            if not address:
                return
            with lock:
                if not any(device["name"] == name for device in devices):
                    devices.append({
                        "name": name.removesuffix("." + VOLUMIO_SERVICE),
                        "host": address,
                        "port": info.port,
                    })

        def update_service(self, zeroconf, service_type, name):
            self.add_service(zeroconf, service_type, name)

        def remove_service(self, zeroconf, service_type, name):
            return

    zeroconf = None
    browser = None
    try:
        zeroconf = Zeroconf()
        browser = ServiceBrowser(zeroconf, VOLUMIO_SERVICE, Listener())
        time.sleep(timeout)
    except OSError:
        return devices
    finally:
        if browser is not None:
            browser.cancel()
        if zeroconf is not None:
            zeroconf.close()
    return devices


class VolumitoV1:
    def __init__(self, config_dir="~/.config/volumito/"):
        self.config_dir = os.path.expanduser(config_dir)
        self.config_file = os.path.join(self.config_dir, "config.yaml")
        self.config = self.check_config(self.config_dir)
        self.discovered_devices = []
        if not self.config.get("volumio_host"):
            self.discovered_devices = discover_volumio_devices()
            if self.discovered_devices:
                self._set_server(self.discovered_devices[0])
        self.status = {}
        self.queue = []
        self._status_lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._notification_lock = threading.Lock()
        self._notification = None
        self.session = requests.Session()
        self._stop_event = threading.Event()
        self.updater = None
        self.loop = None
        # recent seek target to avoid UI bounce (tuple: (seconds, timestamp))
        self._recent_seek = None
        # playlist walker/listbox references (built in build_ui)
        self._queue_walker = None
        self._queue_listbox = None
        self._queue_rows = []
        self._queue_snapshot = None
        # columns widget reference so we can show/hide playlist pane
        self._columns = None
        self._playlist_visible = False
        self._main_widget = None
        self._music_menu_open = False
        self._music_pages = []
        self._music_menu_error = None
        self._music_menu_walker = None
        self._music_menu_items = []
        # user-controlled toggle: when True, hide queue pane regardless of queue size
        self._queue_force_hidden = False

    def check_config(self, config_dir=None):
        config_dir = config_dir or self.config_dir
        config_dir = os.path.expanduser(config_dir)
        config_file = os.path.join(config_dir, "config.yaml")
        if not os.path.exists(config_file):
            return {}
        with open(config_file, "r") as f:
            content = f.read()
            try:
                config = yaml.safe_load(content)
            except Exception as e:
                print(f"Error parsing config: {e}")
                sys.exit(1)
        if config is None:
            return {}
        if not isinstance(config, dict):
            print(f"Config file at {config_file} must contain a YAML mapping.")
            sys.exit(1)
        return config

    def _set_server(self, device):
        self.config["volumio_host"] = device["host"]
        self.config["volumio_port"] = device.get("port", 80)
        os.makedirs(self.config_dir, exist_ok=True)
        with open(self.config_file, "w") as config_file:
            yaml.safe_dump(self.config, config_file, sort_keys=False)

    def _base_url(self):
        host = self.config.get("volumio_host")
        if not host:
            return None
        port = self.config.get("volumio_port")
        if port and int(port) != 80:
            return f"http://{host}:{int(port)}"
        return f"http://{host}"

    def _api_url(self, path):
        base_url = self._base_url()
        return f"{base_url}/api/v1/{path}" if base_url else None

    def read_status(self):
        url = self._api_url("getState")
        if not url:
            return {}
        try:
            r = self.session.get(url, timeout=3)
            if r.status_code != 200:
                return {}
            return r.json()
        except Exception:
            return {}

    def read_queue(self):
        url = self._api_url("getQueue")
        if not url:
            return []
        try:
            r = self.session.get(url, timeout=3)
            if r.status_code != 200:
                return []
            data = r.json()
            # Volumio returns {"queue": [...]} or just a list
            if isinstance(data, dict):
                return data.get("queue", [])
            if isinstance(data, list):
                return data
            return []
        except Exception:
            return []

    def _updater(self):
        tick = 0
        while not self._stop_event.is_set():
            s = self.read_status()
            if isinstance(s, dict):
                with self._status_lock:
                    self._merge_status(s)
            # fetch queue every 5 seconds (less critical than state)
            if tick % 5 == 0:
                q = self.read_queue()
                if isinstance(q, list):
                    with self._queue_lock:
                        self.queue = q
            tick += 1
            time.sleep(1)

    def _make_queue_item(self, track, index, current_index):
        """Build a selectable row for a single queue entry."""
        title = _safe_str(track.get("title") or track.get("name"), "Unknown")
        artist = _safe_str(track.get("artist"), "")
        label = f" {index + 1:>3}. {title}"
        if artist:
            label += f"  [{artist}]"
        style = 'queue_current' if index == current_index else 'queue_item'
        focus_style = 'queue_selected_current' if index == current_index else 'queue_selected'
        return urwid.AttrMap(
            urwid.SelectableIcon(label, cursor_position=0, wrap='clip'),
            style,
            focus_style,
        )

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
        self.shuffle_label = urwid.Text('Shuffle: ')
        self.shuffle_value = urwid.Text('?', wrap='clip')
        self.bitrate_label = urwid.Text('Sample Rate: ')
        self.bitrate_value = urwid.Text('?', wrap='clip')
        self.elapsed_label = urwid.Text('Elapsed: ')
        self.elapsed_value = urwid.Text('--:--', wrap='clip')
        self.length_label = urwid.Text('Length: ')
        self.length_value = urwid.Text('--:--', wrap='clip')
        self.volume_label = urwid.Text('Volume: ')
        self.volume_text = urwid.Text('--')
        self.server_label = urwid.Text('Server: ')
        self.server_text = urwid.Text(_safe_str(self.config.get("volumio_host"), "Not found"))
        self.last_key = urwid.Text('')
        # progress bar for visual track progress
        self.progress = urwid.ProgressBar('pg normal', 'highlight', 0, 100)

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
        row_shuffle = urwid.Columns([
            ('fixed', 16, self.shuffle_label),
            urwid.AttrMap(self.shuffle_value, 'normal')
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

        legend = urwid.Text([
            ('bold', '+/-'), ' vol | ',
            ('bold', 'p'), ' play/pause | ',
            ('bold', '</>'), ' prev/next | ',
            ('bold', 'up/down'), ' select | ',
            ('bold', 'Enter'), ' play | ',
            ('bold', '[/]'), ' seek | ',
            ('bold', 'f'), ' favorite | ',
            ('bold', 'm'), ' music | ',
            ('bold', 't'), ' mute | ',
            ('bold', 's'), ' shuffle | ',
            ('bold', 'u'), ' queue | ',
            ('bold', 'h'), ' hosts | ',
            ('bold', 'q'), ' quit',
        ])

        self._info_pile = urwid.Pile([
            urwid.AttrMap(self.header, 'header'),
            row_title,
            row_artist,
            row_album,
            row_state,
            row_shuffle,
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

        # Playlist pane (initially empty)
        self._queue_walker = urwid.SimpleListWalker([urwid.Text(' No queue')])
        self._queue_listbox = urwid.ListBox(self._queue_walker)
        queue_box = urwid.LineBox(self._queue_listbox, title='Playlist')

        # Start with just the info pane; playlist added dynamically
        self._columns = urwid.Columns([('weight', 1, urwid.Filler(self._info_pile, valign='top'))])
        self._queue_box = queue_box
        self._playlist_visible = False
        self._main_widget = self._columns

        return self._columns

    def _update_queue_pane(self, queue, current_index):
        """Update queue rows while preserving list focus and scroll position."""
        # Respect user toggle: never show if force-hidden
        show = len(queue) > 1 and not self._queue_force_hidden

        if show:
            snapshot = tuple(
                (track.get("uri"), track.get("title"), track.get("name"), track.get("artist"))
                for track in queue
            )
            if snapshot != self._queue_snapshot:
                focus_position = self._queue_walker.focus
                self._queue_rows = [
                    self._make_queue_item(track, i, current_index)
                    for i, track in enumerate(queue)
                ]
                self._queue_walker[:] = self._queue_rows
                if self._queue_snapshot is None:
                    try:
                        focus_position = int(current_index)
                    except (TypeError, ValueError):
                        focus_position = 0
                if not 0 <= focus_position < len(self._queue_rows):
                    focus_position = 0
                self._queue_walker.set_focus(focus_position)
                self._queue_snapshot = snapshot

            for index, row in enumerate(self._queue_rows):
                current = index == current_index
                row.set_attr_map({None: 'queue_current' if current else 'queue_item'})
                row.set_focus_map({
                    None: 'queue_selected_current' if current else 'queue_selected'
                })
        else:
            self._queue_walker[:] = [urwid.Text(' No queue')]
            self._queue_rows = []
            self._queue_snapshot = None

        if show and not self._playlist_visible:
            # Add playlist column
            self._columns.contents = [
                (urwid.Filler(self._info_pile, valign='top'), self._columns.options('weight', 1)),
                (self._queue_box, self._columns.options('weight', 1)),
            ]
            self._playlist_visible = True
            self._columns.focus_position = 1
        elif not show and self._playlist_visible:
            # Remove playlist column
            self._columns.contents = [
                (urwid.Filler(self._info_pile, valign='top'), self._columns.options('weight', 1)),
            ]
            self._playlist_visible = False

    def _refresh_queue_pane(self):
        with self._queue_lock:
            queue = list(self.queue)
        with self._status_lock:
            current_index = self.status.get('position', 0)
        try:
            current_index = int(current_index)
        except (TypeError, ValueError):
            current_index = 0
        self._update_queue_pane(queue, current_index)

    def refresh_ui(self, loop=None, user_data=None):
        with self._status_lock:
            s = dict(self.status)
        with self._queue_lock:
            queue = list(self.queue)

        title = _safe_str(s.get('title'))
        artist = _safe_str(s.get('artist'))
        album = _safe_str(s.get('album'))
        state = _safe_str(s.get('status'))
        samplerate = s.get('samplerate')
        bitrate = s.get('bitrate')
        # Prefer samplerate, fall back to bitrate, then to '-'
        display_rate = samplerate if samplerate is not None else bitrate
        display_rate = _safe_str(display_rate)
        vol = s.get('volume')
        shuffle = s.get('random', False)

        self.title_value.set_text(title)
        self.artist_value.set_text(artist)
        self.album_value.set_text(album)
        self.state_value.set_text(state)
        self.shuffle_value.set_text('on' if shuffle else 'off')
        self.bitrate_value.set_text(display_rate)
        with self._notification_lock:
            notification = self._notification
            self._notification = None
        if notification:
            self.last_key.set_text(notification)

        # update volume text
        try:
            vnum = int(vol) if vol is not None else 0
        except Exception:
            try:
                vnum = int(float(vol))
            except Exception:
                vnum = 0
        self.volume_text.set_text(f"{vnum} (muted)" if s.get('mute') else str(vnum))

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
        for k in ('seek', 'elapsed', 'progress'):
            if k in s:
                seek_candidates.append(s.get(k))
        for k in ('duration', 'trackDuration', 'totalTime', 'length', 'tracklength'):
            if k in s:
                dur_candidates.append(s.get(k))
        # extend with deep search
        seek_candidates.extend(_deep_find_all(s, ['seek', 'elapsed', 'progress']))
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

        # Update playlist pane
        # Determine current queue index from status
        current_index = s.get('position', None)
        if current_index is None:
            # Try to match by title
            current_title = _safe_str(s.get('title'), '')
            current_index = next(
                (i for i, t in enumerate(queue) if t.get('title') == current_title),
                0
            )
        try:
            current_index = int(current_index)
        except Exception:
            current_index = 0

        try:
            self._update_queue_pane(queue, current_index)
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

        if klower in ('esc', 'escape') and self._main_widget:
            if self._music_menu_open:
                self._music_menu_back()
            elif self._playlist_visible:
                self._queue_force_hidden = True
                self._refresh_queue_pane()
            else:
                self.loop.widget = self._main_widget
            return

        # quit
        if klower == 'q':
            raise urwid.ExitMainLoop()

        if self._music_menu_open and klower == 'm':
            self._close_music_menu()
            return

        if self._music_menu_open and klower in ('enter', 'return'):
            self._activate_music_menu_item()
            return

        if self.loop and self.loop.widget is not self._main_widget:
            return

        if klower == 'h':
            self._show_host_picker()
            return
        if klower == 'm':
            self._open_music_menu()
            return

        if klower in ('enter', 'return'):
            self._play_selected_queue_track()
            return

        # toggle queue pane visibility
        if klower == 'u':
            self._queue_force_hidden = not self._queue_force_hidden
            self._refresh_queue_pane()
            return

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

        if klower == 'f':
            self._favorite_current_track()
            return

        if klower == 't':
            self._toggle_mute()
            return

        # shuffle
        if klower in ('s',):
            self._toggle_shuffle()
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

    def _show_host_picker(self):
        if not self.discovered_devices:
            self.discovered_devices = discover_volumio_devices()

        if self.discovered_devices:
            choices = []
            for device in self.discovered_devices:
                button = urwid.Button(f"{device['name']} ({device['host']})")
                urwid.connect_signal(
                    button,
                    "click",
                    self._select_server,
                    user_args=(device,),
                )
                choices.append(button)
        else:
            choices = [urwid.Text("No Volumio units found on the LAN.")]
        cancel = urwid.Button("Cancel")
        urwid.connect_signal(cancel, "click", self._close_host_picker)
        choices.append(cancel)
        picker = urwid.LineBox(
            urwid.ListBox(urwid.SimpleFocusListWalker(choices)),
            title="Volumio servers",
        )
        self.loop.widget = urwid.Overlay(
            picker,
            self._main_widget,
            align="center",
            width=("relative", 70),
            valign="middle",
            height=("relative", 60),
        )

    def _close_host_picker(self, button=None):
        self.loop.widget = self._main_widget

    def _select_server(self, device, button=None):
        self._set_server(device)
        self.server_text.set_text(device["host"])
        with self._status_lock:
            self.status.clear()
        with self._queue_lock:
            self.queue.clear()
        self._close_host_picker()

    def _open_music_menu(self):
        self._music_menu_open = True
        self._music_pages = []
        self._music_menu_error = None
        self._load_music_page(None, "Music")

    def _load_music_page(self, uri, title):
        url = self._api_url("browse")
        if not url:
            self._music_menu_error = "No Volumio server selected."
            self._render_music_menu()
            return
        try:
            response = self.session.get(
                url,
                params={"uri": uri} if uri else None,
                timeout=3,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("navigation"), dict):
                raise ValueError("Volumio returned an invalid browse response.")
            sections = data["navigation"].get("lists", [])
            if not isinstance(sections, list):
                raise ValueError("Volumio returned an invalid browse list.")
            items = []
            for section in sections:
                if not isinstance(section, dict):
                    continue
                section_items = section.get("items")
                if isinstance(section_items, list):
                    items.extend(item for item in section_items if isinstance(item, dict))
                elif section.get("uri"):
                    items.append(section)
        except (requests.RequestException, ValueError) as error:
            self._music_menu_error = f"Could not load {title}: {error}"
            self._render_music_menu()
            return

        self._music_pages.append({
            "uri": uri,
            "title": title,
            "items": items,
            "focus_index": None,
        })
        self._music_menu_error = None
        self._render_music_menu()

    def _make_music_menu_row(self, label):
        return urwid.AttrMap(
            urwid.SelectableIcon(label, cursor_position=0, wrap="clip"),
            "queue_item",
            "queue_selected",
        )

    @staticmethod
    def _is_playable_music_item(item):
        item_type = str(item.get("type", "")).lower()
        return item_type in ("song", "track", "webradio") or bool(item.get("trackType"))

    def _render_music_menu(self):
        page = self._music_pages[-1] if self._music_pages else {"title": "Music", "items": []}
        rows = []
        self._music_menu_items = []
        back_offset = 0
        if len(self._music_pages) > 1:
            rows.append(self._make_music_menu_row("< Back"))
            self._music_menu_items.append(None)
            back_offset = 1

        for item in page["items"]:
            label = _safe_str(item.get("title") or item.get("name"), "Unknown")
            if item.get("uri") and not self._is_playable_music_item(item):
                label += " >"
            rows.append(self._make_music_menu_row(label))
            self._music_menu_items.append(item)
        if not rows:
            rows.append(urwid.Text("No items found."))
        if self._music_menu_error:
            rows.append(urwid.Text(self._music_menu_error, wrap="any"))

        walker = urwid.SimpleListWalker(rows)
        focus_index = page.get("focus_index")
        if focus_index is None:
            focus_index = back_offset
        else:
            focus_index += back_offset
        if 0 <= focus_index < len(rows) and rows[focus_index].selectable():
            walker.set_focus(focus_index)
        self._music_menu_walker = walker

        menu = urwid.LineBox(
            urwid.ListBox(walker),
            title=page["title"],
        )
        self.loop.widget = urwid.Overlay(
            menu,
            self._main_widget,
            align="center",
            width=("relative", 75),
            valign="middle",
            height=("relative", 70),
        )

    def _activate_music_menu_item(self):
        if not self._music_menu_walker:
            return
        index = self._music_menu_walker.focus
        if index >= len(self._music_menu_items):
            return
        item = self._music_menu_items[index]
        if item is None:
            self._music_menu_back()
            return
        if self._music_pages:
            self._music_pages[-1]["focus_index"] = index - (1 if len(self._music_pages) > 1 else 0)
        self._select_music_item(item)

    def _select_music_item(self, item, button=None):
        if self._is_playable_music_item(item):
            self._play_music_item(item)
            return
        uri = item.get("uri")
        if not uri:
            self._music_menu_error = "This menu item cannot be opened."
            self._render_music_menu()
            return
        title = _safe_str(item.get("title") or item.get("name"), "Music")
        self._load_music_page(uri, title)

    def _play_music_item(self, item):
        page = self._music_pages[-1] if self._music_pages else {}
        siblings = [
            track for track in page.get("items", [])
            if self._is_playable_music_item(track)
        ]
        if not siblings:
            siblings = [item]
        selected_index = next(
            (index for index, track in enumerate(siblings) if track is item or track == item),
            0,
        )
        ordered_tracks = siblings[selected_index:] + siblings[:selected_index]
        url = self._api_url("replaceAndPlay")
        if not url:
            self._music_menu_error = "No Volumio server selected."
            self._render_music_menu()
            return
        try:
            response = self.session.post(
                url,
                json={"list": ordered_tracks, "index": 0},
                timeout=5,
            )
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and (result.get("Error") or result.get("error")):
                raise RuntimeError(result.get("Error") or result.get("error"))
        except (requests.RequestException, ValueError, RuntimeError) as error:
            self._music_menu_error = f"Could not play track: {error}"
            self._render_music_menu()
            return

        self._close_music_menu()

    def _music_menu_back(self):
        if len(self._music_pages) > 1:
            self._music_pages.pop()
            self._music_menu_error = None
            self._render_music_menu()
            return
        self._close_music_menu()

    def _close_music_menu(self):
        self._music_menu_open = False
        self._music_pages = []
        self._music_menu_error = None
        self.loop.widget = self._main_widget

    def _favorite_current_track(self):
        with self._status_lock:
            track = dict(self.status)

        uri = track.get("uri")
        service = track.get("service")
        track_type = str(track.get("trackType") or track.get("type") or "").lower()
        if track_type == "webradio" or str(service).lower() == "webradio":
            service = "webradio"
        if not uri or not service:
            self.last_key.set_text("Cannot favorite the current track: missing URI or service.")
            return

        payload = {
            "service": service,
            "uri": uri,
            "title": track.get("title"),
        }
        title = _safe_str(track.get("title"), "current track")
        self._send_socket_event(
            "addToFavourites",
            payload,
            f"favorite request for {title}",
            f"Favorite request sent for {title}.",
        )

    def _toggle_mute(self):
        with self._status_lock:
            muted = bool(self.status.get("mute", False))
        command = "unmute" if muted else "mute"
        action = "Unmute" if muted else "Mute"
        url = self._api_url(f"commands/?cmd=volume&volume={command}")
        if not url:
            self.last_key.set_text(f"Cannot {action.lower()}: no Volumio server selected.")
            return

        self.last_key.set_text(f"Sending {action.lower()} command...")

        def send_command():
            try:
                response = self.session.get(url, timeout=3)
                response.raise_for_status()
                result = response.json()
                if isinstance(result, dict) and (result.get("Error") or result.get("error")):
                    raise ValueError(result.get("Error") or result.get("error"))
            except (requests.RequestException, ValueError) as error:
                message = f"Could not {action.lower()}: {error}"
            else:
                with self._status_lock:
                    self.status["mute"] = not muted
                message = f"{action} command sent."
            with self._notification_lock:
                self._notification = message

        threading.Thread(target=send_command, daemon=True).start()

    def _send_socket_event(self, event, payload, action, success_message):
        base_url = self._base_url()
        if not base_url:
            self.last_key.set_text(f"Cannot send {action}: no Volumio server selected.")
            return

        self.last_key.set_text(f"Sending {action}...")

        def send_event():
            client = socketio.Client(request_timeout=3, reconnection=False)
            message = None
            try:
                client.connect(base_url, transports=["polling"])
                if payload is None:
                    client.emit(event)
                else:
                    client.emit(event, payload)
            except (
                socketio.exceptions.SocketIOError,
                requests.RequestException,
                OSError,
                TimeoutError,
            ) as error:
                message = f"Could not send {action}: {error}"
            else:
                message = success_message
            finally:
                if client.connected:
                    try:
                        client.disconnect()
                    except (
                        socketio.exceptions.SocketIOError,
                        requests.RequestException,
                        OSError,
                        TimeoutError,
                    ) as error:
                        message = f"{action.capitalize()} may have been sent, but disconnect failed: {error}"
                with self._notification_lock:
                    self._notification = message

        threading.Thread(target=send_event, daemon=True).start()

    def _play_selected_queue_track(self):
        if not self._playlist_visible:
            return
        with self._queue_lock:
            queue_length = len(self.queue)
        selected_index = self._queue_walker.focus
        if selected_index >= queue_length:
            return
        self._send_cmd("play", f"N={selected_index}")

    def _change_volume(self, delta):
        url = self._api_url("commands/?cmd=volume&volume=")
        if not url:
            return
        try:
            with self._status_lock:
                cur = int(self.status.get('volume', 0))
        except Exception:
            cur = 0
        new = max(0, min(100, cur + delta))
        with self._status_lock:
            self.status['volume'] = new
        url += str(new)
        threading.Thread(target=lambda: self._safe_get(url), daemon=True).start()

    def _toggle_play(self):
        url = self._api_url("commands/?cmd=")
        if not url:
            return
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
        url += cmd
        threading.Thread(target=lambda: self._safe_get(url), daemon=True).start()

    def _toggle_shuffle(self):
        url = self._api_url("commands/?cmd=random")
        if not url:
            return
        with self._status_lock:
            cur = bool(self.status.get('random', False))
        new_state = not cur
        with self._status_lock:
            self.status['random'] = new_state
            self.status['shuffle'] = new_state  # keep shuffle in sync for UI display
        threading.Thread(target=lambda: self._safe_get(url), daemon=True).start()

    def _send_cmd(self, cmd, query=None):
        url = self._api_url(f"commands/?cmd={cmd}")
        if not url:
            return
        if query:
            url += f"&{query}"
        # send the command and then refresh state to reflect track change quickly
        def _worker(u):
            try:
                self._safe_get(u)
            except Exception:
                pass
            # attempt to fetch updated state
            try:
                r = self.session.get(self._api_url("getState"), timeout=2)
                if r.status_code == 200:
                    with self._status_lock:
                        self._merge_status(r.json())
            except Exception:
                pass
        threading.Thread(target=_worker, args=(url,), daemon=True).start()

    def _merge_status(self, new):
        """Merge new status dict into self.status while avoiding seek bounce
        if a recent seek was requested, prefer the recent seek unless the device's
        reported seek is close to the requested one."""
        if not isinstance(new, dict):
            return
        recent = getattr(self, '_recent_seek', None)
        if recent and (time.time() - recent[1]) < 3:
            target = recent[0]
            # try to extract reported seek/position from new
            reported = None
            for k in ('seek', 'elapsed', 'progress'):
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
            # otherwise ignore seek keys from device so UI keeps requested position
            cleaned = dict(new)
            for k in ('seek', 'elapsed', 'progress'):
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
        base_url = self._api_url("commands/")
        if not base_url:
            return
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
        for k in ('seek', 'elapsed', 'progress'):
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
            orig_seek = s.get('seek')
            try:
                seek_is_milliseconds = float(orig_seek) > 1000
            except (TypeError, ValueError):
                seek_is_milliseconds = False
            self.status['seek'] = int(new_pos * 1000) if seek_is_milliseconds else int(new_pos)
            # mark recent seek to avoid immediate bounce from device-reported old position
            try:
                self._recent_seek = (float(new_pos), time.time())
            except Exception:
                self._recent_seek = (new_pos, time.time())

        secs = int(new_pos)
        seek_url = f"{base_url}?cmd=seek&position={secs}"

        def _worker():
            try:
                self.session.get(seek_url, timeout=2)
            except Exception:
                pass
            try:
                r = self.session.get(self._api_url("getState"), timeout=2)
                if r.status_code == 200:
                    with self._status_lock:
                        self._merge_status(r.json())
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def run(self):
        # start updater thread
        self.updater = threading.Thread(target=self._updater, daemon=True)
        self.updater.start()

        # build UI
        top = self.build_ui()
        palette = [
            ('header',        'bold',    'dark gray'),
            ('highlight',     'standout', 'default'),
            ('normal',        'default', 'default'),
            ('bold',          'bold',    'dark gray'),
            ('pg normal',     'default', 'dark gray'),
            ('queue_item',    'default', 'default'),
            ('queue_current', 'black',   'light cyan'),
            ('queue_selected', 'black', 'light green'),
            ('queue_selected_current', 'black', 'light cyan'),
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
