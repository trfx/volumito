import urwid

import volumito


def test_missing_config_does_not_create_config_directory(tmp_path):
    config_dir = tmp_path / "volumito"
    app = object.__new__(volumito.VolumitoV1)

    assert app.check_config(config_dir=str(config_dir)) == {}
    assert not config_dir.exists()


def test_discovery_uses_volumio_mdns_service(monkeypatch):
    class FakeInfo:
        port = 8080

        def parsed_addresses(self):
            return ["192.168.1.23", "fe80::1"]

    class FakeZeroconf:
        def get_service_info(self, service_type, name, timeout):
            assert service_type == volumito.VOLUMIO_SERVICE
            return FakeInfo()

        def close(self):
            pass

    class FakeBrowser:
        def __init__(self, zeroconf, service_type, listener):
            listener.add_service(
                zeroconf,
                service_type,
                "Living Room._Volumio._tcp.local.",
            )

        def cancel(self):
            pass

    monkeypatch.setattr(volumito, "Zeroconf", FakeZeroconf)
    monkeypatch.setattr(volumito, "ServiceBrowser", FakeBrowser)
    monkeypatch.setattr(volumito.time, "sleep", lambda timeout: None)

    assert volumito.discover_volumio_devices() == [{
        "name": "Living Room",
        "host": "192.168.1.23",
        "port": 8080,
    }]


def test_configless_start_selects_first_discovered_server(monkeypatch, tmp_path):
    devices = [
        {"name": "Living Room", "host": "192.168.1.23", "port": 8080},
        {"name": "Kitchen", "host": "192.168.1.24", "port": 80},
    ]
    monkeypatch.setattr(volumito, "discover_volumio_devices", lambda: devices)

    app = volumito.VolumitoV1(config_dir=str(tmp_path / "volumito"))

    assert app.config["volumio_host"] == "192.168.1.23"
    assert app._api_url("getState") == "http://192.168.1.23:8080/api/v1/getState"
    assert app.discovered_devices == devices
    assert app.check_config() == {
        "volumio_host": "192.168.1.23",
        "volumio_port": 8080,
    }

    app.build_ui()
    app.loop = urwid.MainLoop(app._main_widget, [])
    app._show_host_picker()
    assert isinstance(app.loop.widget, urwid.Overlay)

    button = urwid.Button("Kitchen")
    urwid.connect_signal(
        button,
        "click",
        app._select_server,
        user_args=(devices[1],),
    )
    urwid.emit_signal(button, "click")

    assert app.config["volumio_host"] == "192.168.1.24"
    assert app.server_text.get_text()[0] == "192.168.1.24"
    assert app._api_url("getState") == "http://192.168.1.24/api/v1/getState"
    assert app.loop.widget is app._main_widget
    assert app.check_config() == {
        "volumio_host": "192.168.1.24",
        "volumio_port": 80,
    }


def test_configless_start_without_discovery_stays_idle(monkeypatch, tmp_path):
    monkeypatch.setattr(volumito, "discover_volumio_devices", lambda: [])

    app = volumito.VolumitoV1(config_dir=str(tmp_path / "volumito"))

    assert app.read_status() == {}
    assert app.read_queue() == []
    assert app._api_url("getState") is None
    app._change_volume(2)
    app._toggle_play()
    app._toggle_shuffle()
    app._send_cmd("next")
    app._seek_relative(30)


def test_playlist_navigation_and_enter_play_selected_track(monkeypatch, tmp_path):
    monkeypatch.setattr(volumito, "discover_volumio_devices", lambda: [])
    app = volumito.VolumitoV1(config_dir=str(tmp_path / "volumito"))
    app.build_ui()
    app.loop = urwid.MainLoop(app._main_widget, [])
    app.queue = [{"title": "First"}, {"title": "Second"}, {"title": "Third"}]
    app.status = {"position": 1}
    app._update_queue_pane(app.queue, 1)
    commands = []
    monkeypatch.setattr(
        app,
        "_send_cmd",
        lambda command, query=None: commands.append((command, query)),
    )

    app._columns.keypress((100, 24), "up")
    assert app._queue_walker.focus == 0
    app._columns.keypress((100, 24), "down")
    app._columns.keypress((100, 24), "down")
    app._columns.keypress((100, 24), "down")
    assert app._queue_walker.focus == 2

    app._update_queue_pane(app.queue, 1)
    assert app._queue_walker.focus == 2
    app.unhandled_input("enter")

    assert commands == [("play", "N=2")]
    app.unhandled_input("esc")
    assert not app._playlist_visible
    app.unhandled_input("u")
    assert app._playlist_visible


def test_f_adds_current_track_to_volumio_favorites(monkeypatch, tmp_path):
    monkeypatch.setattr(volumito, "discover_volumio_devices", lambda: [])
    app = volumito.VolumitoV1(config_dir=str(tmp_path / "volumito"))
    app.config["volumio_host"] = "192.168.1.23"
    app.build_ui()
    app.loop = urwid.MainLoop(app._main_widget, [])
    calls = []

    class FakeSocketClient:
        connected = False

        def __init__(self, **options):
            calls.append(("client", options))

        def connect(self, url, transports):
            calls.append(("connect", url, transports))
            self.connected = True

        def emit(self, event, payload):
            calls.append(("emit", event, payload))

        def disconnect(self):
            calls.append(("disconnect",))
            self.connected = False

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(volumito.socketio, "Client", FakeSocketClient)
    monkeypatch.setattr(volumito.threading, "Thread", ImmediateThread)

    app.status = {
        "service": "mpd",
        "uri": "album/track.flac",
        "title": "A track",
    }
    app.unhandled_input("f")
    assert ("emit", "addToFavourites", {
        "service": "mpd",
        "uri": "album/track.flac",
        "title": "A track",
    }) in calls

    calls.clear()
    app.status = {
        "trackType": "webradio",
        "uri": "http://radio.example/stream",
        "title": "A station",
    }
    app.unhandled_input("f")
    assert ("emit", "addToFavourites", {
        "service": "webradio",
        "uri": "http://radio.example/stream",
        "title": "A station",
    }) in calls


def test_t_toggles_mute_state(monkeypatch, tmp_path):
    monkeypatch.setattr(volumito, "discover_volumio_devices", lambda: [])
    app = volumito.VolumitoV1(config_dir=str(tmp_path / "volumito"))
    app.config["volumio_host"] = "192.168.1.23"
    app.build_ui()
    app.loop = urwid.MainLoop(app._main_widget, [])
    requests = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "success"}

    class Session:
        def get(self, url, timeout):
            requests.append((url, timeout))
            return Response()

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    app.session = Session()
    monkeypatch.setattr(volumito.threading, "Thread", ImmediateThread)

    app.status = {"mute": False}
    app.unhandled_input("t")
    assert app.status["mute"] is True
    app.unhandled_input("t")

    assert requests == [
        ("http://192.168.1.23/api/v1/commands/?cmd=volume&volume=mute", 3),
        ("http://192.168.1.23/api/v1/commands/?cmd=volume&volume=unmute", 3),
    ]
    assert app.status["mute"] is False


def test_muted_state_is_visible_without_changing_volume_level(monkeypatch, tmp_path):
    monkeypatch.setattr(volumito, "discover_volumio_devices", lambda: [])
    app = volumito.VolumitoV1(config_dir=str(tmp_path / "volumito"))
    app.build_ui()
    app.status = {"volume": 42, "mute": True}

    app.refresh_ui()

    assert app.volume_text.get_text()[0] == "42 (muted)"


def test_decibel_meters_pulse_only_during_unmuted_play(monkeypatch, tmp_path):
    monkeypatch.setattr(volumito, "discover_volumio_devices", lambda: [])
    app = volumito.VolumitoV1(config_dir=str(tmp_path / "volumito"))
    app.build_ui()

    app.status = {"status": "play", "mute": False}
    playing_levels = []
    right_playing_levels = []
    for _ in range(8):
        app.refresh_ui()
        playing_levels.append(app.left_meter.get_text()[0])
        right_playing_levels.append(app.right_meter.get_text()[0])

    assert all("▮" in level for level in playing_levels)
    assert all("▮" in level for level in right_playing_levels)
    assert len(set(playing_levels)) > 1
    assert len(set(right_playing_levels)) > 1

    for status, muted in (("pause", False), ("playing", False), ("play", True)):
        app.status = {"status": status, "mute": muted}
        app.refresh_ui()
        assert app.left_meter.get_text()[0] == "·" * 16
        assert app.right_meter.get_text()[0] == "·" * 16


def test_microphone_meter_analyzes_stereo_frames():
    levels = []
    meter = volumito.MicrophoneAudioMeter(lambda: None, levels.append, lambda error: None)
    meter._channels = 2
    meter.set_active(True)

    meter._audio_callback(
        [[1.0, 0.25], [-1.0, -0.25], [1.0, 0.25], [-1.0, -0.25]],
        4,
        None,
        None,
    )

    assert levels[0][0] > levels[0][1] > 0


def test_microphone_meter_uses_a_sensitive_dbfs_range():
    assert volumito._dbfs_to_meter_level(-45) == 8
    assert volumito._dbfs_to_meter_level(-36) == 13
    assert volumito._dbfs_to_meter_level(-20) == 16


def test_microphone_missing_numpy_reports_the_optional_dependencies(monkeypatch):
    class FakeSoundDevice:
        @staticmethod
        def query_devices(kind):
            return {"max_input_channels": 2, "default_samplerate": 44100}

        @staticmethod
        def InputStream(**kwargs):
            raise ImportError("No module named 'numpy'")

    errors = []
    meter = volumito.MicrophoneAudioMeter(lambda: None, lambda levels: None, errors.append)
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", FakeSoundDevice)

    meter._run()

    assert len(errors) == 1
    assert "numpy" in errors[0]
    assert "pip install sounddevice numpy" in errors[0]


def test_microphone_opt_in_hides_strips_until_available_and_on_error(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(volumito, "discover_volumio_devices", lambda: [])
    config_dir = tmp_path / "volumito"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("microphone_meter: true\n")
    app = volumito.VolumitoV1(config_dir=str(config_dir))
    app.build_ui()

    assert not app._meter_rows_visible
    active_states = []

    class FakeMeter:
        def set_active(self, active):
            active_states.append(active)

    app._microphone_meter = FakeMeter()
    app.status = {"status": "pause", "mute": False}
    app.refresh_ui()
    assert not app._meter_rows_visible
    assert active_states[-1] is False

    app._microphone_ready_callback()
    app._microphone_levels_callback((12, 8))
    app.refresh_ui()
    assert app._meter_rows_visible
    assert app.left_meter.get_text()[0] == "·" * 16

    app.status = {"status": "play", "mute": False}
    app.refresh_ui()
    assert active_states[-1] is True
    assert "▮" in app.left_meter.get_text()[0]
    assert "▮" in app.right_meter.get_text()[0]

    app.status = {"status": "pause", "mute": False}
    app.refresh_ui()
    assert active_states[-1] is False
    assert app.left_meter.get_text()[0] == "·" * 16

    app._microphone_error_callback("Microphone access denied")
    app.refresh_ui()
    assert not app._meter_rows_visible
    assert app.last_key.get_text()[0] == "Microphone access denied"


def test_seek_sends_one_documented_position_and_preserves_queue_index(monkeypatch, tmp_path):
    monkeypatch.setattr(volumito, "discover_volumio_devices", lambda: [])
    app = volumito.VolumitoV1(config_dir=str(tmp_path / "volumito"))
    app.config["volumio_host"] = "192.168.1.23"
    app.status = {"seek": 120000, "duration": 600, "position": 2}
    requests = []

    class Response:
        status_code = 200

        def json(self):
            return {"seek": 90000, "duration": 600, "position": 2}

    class Session:
        def get(self, url, timeout):
            requests.append(url)
            return Response()

    class InlineThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    app.session = Session()
    monkeypatch.setattr(volumito.threading, "Thread", InlineThread)

    app._seek_relative(-30)

    assert requests == [
        "http://192.168.1.23/api/v1/commands/?cmd=seek&position=90",
        "http://192.168.1.23/api/v1/getState",
    ]
    assert app.status["seek"] == 90000
    assert app.status["position"] == 2


def test_music_menu_browses_folders_and_plays_selected_track(monkeypatch, tmp_path):
    monkeypatch.setattr(volumito, "discover_volumio_devices", lambda: [])
    app = volumito.VolumitoV1(config_dir=str(tmp_path / "volumito"))
    app.config["volumio_host"] = "192.168.1.23"
    app.build_ui()
    app.loop = urwid.MainLoop(app._main_widget, [])
    library = {"name": "Music Library", "uri": "music-library"}
    album = {"name": "Albums", "uri": "albums", "type": "folder"}
    first_track = {
        "title": "First track",
        "uri": "music-library/albums/first.flac",
        "service": "mpd",
        "type": "song",
    }
    second_track = {
        "title": "Second track",
        "uri": "music-library/albums/second.flac",
        "service": "mpd",
        "type": "song",
    }
    browse_results = {
        None: {"navigation": {"lists": [library]}},
        "music-library": {"navigation": {"lists": [{"items": [album]}]}},
        "albums": {"navigation": {"lists": [{"items": [first_track, second_track]}]}},
    }
    browse_requests = []
    play_requests = []

    class Response:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Session:
        def get(self, url, params=None, timeout=None):
            uri = params.get("uri") if params else None
            browse_requests.append((url, uri))
            return Response(browse_results[uri])

        def post(self, url, json, timeout=None):
            play_requests.append((url, json))
            return Response({"response": "success"})

    app.session = Session()

    app.unhandled_input("m")
    assert app._music_pages[-1]["title"] == "Music"
    assert app._music_menu_walker[0].selectable()
    app.unhandled_input("enter")
    assert app._music_pages[-1]["title"] == "Music Library"
    assert app._music_menu_items == [None, album]
    app.unhandled_input("enter")
    assert app._music_pages[-1]["title"] == "Albums"

    app.unhandled_input("esc")
    assert app._music_pages[-1]["title"] == "Music Library"
    app.unhandled_input("enter")
    app._music_menu_walker.set_focus(2)
    app.unhandled_input("enter")

    assert browse_requests == [
        ("http://192.168.1.23/api/v1/browse", None),
        ("http://192.168.1.23/api/v1/browse", "music-library"),
        ("http://192.168.1.23/api/v1/browse", "albums"),
        ("http://192.168.1.23/api/v1/browse", "albums"),
    ]
    assert play_requests == [(
        "http://192.168.1.23/api/v1/replaceAndPlay",
        {"list": [second_track, first_track], "index": 0},
    )]
    assert not app._music_menu_open
    assert app.loop.widget is app._main_widget


def test_music_menu_plays_only_selected_web_radio(monkeypatch, tmp_path):
    monkeypatch.setattr(volumito, "discover_volumio_devices", lambda: [])
    app = volumito.VolumitoV1(config_dir=str(tmp_path / "volumito"))
    app.config["volumio_host"] = "192.168.1.23"
    app.build_ui()
    app.loop = urwid.MainLoop(app._main_widget, [])
    web_radio = {"name": "Web Radio", "uri": "radio", "type": "folder"}
    top_500 = {
        "title": "Top 500 Radios",
        "uri": "top-500",
        "service": "webradio",
        "type": "radio-category",
    }
    first_station = {
        "title": "First station",
        "uri": "http://radio.example/first",
        "service": "webradio",
        "type": "webradio",
    }
    selected_station = {
        "title": "Selected station",
        "uri": "http://radio.example/selected",
        "service": "webradio",
        "type": "webradio",
    }
    browse_results = {
        None: {"navigation": {"lists": [{"items": [web_radio]}]}},
        "radio": {"navigation": {"lists": [{"items": [top_500]}]}},
        "top-500": {
            "navigation": {"lists": [{"items": [first_station, selected_station]}]}
        },
    }
    play_requests = []

    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Session:
        def get(self, url, params=None, timeout=None):
            uri = params.get("uri") if params else None
            return Response(browse_results[uri])

        def post(self, url, json, timeout=None):
            play_requests.append((url, json))
            return Response({"response": "success"})

    app.session = Session()

    app.unhandled_input("m")
    app.unhandled_input("enter")
    app.unhandled_input("enter")
    assert app._music_pages[-1]["title"] == "Top 500 Radios"
    app._music_menu_walker.set_focus(2)
    app.unhandled_input("enter")

    assert play_requests == [(
        "http://192.168.1.23/api/v1/replaceAndPlay",
        {"list": [selected_station], "index": 0},
    )]
    assert not app._music_menu_open


def test_music_menu_plays_my_web_radio_instead_of_browsing_stream_uri(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(volumito, "discover_volumio_devices", lambda: [])
    app = volumito.VolumitoV1(config_dir=str(tmp_path / "volumito"))
    app.config["volumio_host"] = "192.168.1.23"
    app.build_ui()
    app.loop = urwid.MainLoop(app._main_widget, [])
    web_radio = {"name": "Web Radio", "uri": "radio", "type": "folder"}
    my_web_radios = {
        "title": "My Web Radios",
        "uri": "radio/myWebRadio",
        "service": "radio",
        "type": "mywebradio-category",
    }
    first_station = {
        "title": "First station",
        "uri": "http://radio.example/first",
        "service": "webradio",
        "type": "mywebradio",
    }
    selected_station = {
        "title": "Selected station",
        "uri": "http://radio.example/selected",
        "service": "webradio",
        "type": "mywebradio",
    }
    browse_results = {
        None: {"navigation": {"lists": [{"items": [web_radio]}]}},
        "radio": {"navigation": {"lists": [{"items": [my_web_radios]}]}},
        "radio/myWebRadio": {
            "navigation": {"lists": [{"items": [first_station, selected_station]}]}
        },
    }
    browse_requests = []
    play_requests = []

    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Session:
        def get(self, url, params=None, timeout=None):
            uri = params.get("uri") if params else None
            browse_requests.append(uri)
            return Response(browse_results[uri])

        def post(self, url, json, timeout=None):
            play_requests.append((url, json))
            return Response({"response": "success"})

    app.session = Session()

    app.unhandled_input("m")
    app.unhandled_input("enter")
    app.unhandled_input("enter")
    assert app._music_pages[-1]["title"] == "My Web Radios"
    app._music_menu_walker.set_focus(2)
    app.unhandled_input("enter")

    assert browse_requests == [None, "radio", "radio/myWebRadio"]
    assert play_requests == [(
        "http://192.168.1.23/api/v1/replaceAndPlay",
        {"list": [selected_station], "index": 0},
    )]
    assert not app._music_menu_open


def test_music_menu_m_toggles_closed_from_nested_page(monkeypatch, tmp_path):
    monkeypatch.setattr(volumito, "discover_volumio_devices", lambda: [])
    app = volumito.VolumitoV1(config_dir=str(tmp_path / "volumito"))
    app.build_ui()
    app.loop = urwid.MainLoop(app._main_widget, [])

    def load_page(uri, title):
        app._music_pages.append({
            "uri": uri,
            "title": title,
            "items": [],
            "focus_index": None,
        })
        app._render_music_menu()

    monkeypatch.setattr(app, "_load_music_page", load_page)

    app.unhandled_input("m")
    assert app._music_menu_open
    app._music_pages.append({
        "uri": "nested",
        "title": "Nested",
        "items": [],
        "focus_index": None,
    })
    app._render_music_menu()

    app.unhandled_input("m")
    assert not app._music_menu_open
    assert app._music_pages == []
    assert app.loop.widget is app._main_widget

    app.unhandled_input("m")
    assert app._music_menu_open
    assert [page["title"] for page in app._music_pages] == ["Music"]
