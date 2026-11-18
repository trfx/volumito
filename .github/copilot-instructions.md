## Volumito TUI architecture

- The application is implemented primarily in `volumito.py` as `VolumitoV1`;
  behavior tests live in `test_volumito.py`.
- Startup loads `~/.config/volumito/config.yaml`. If no host is configured,
  Zeroconf discovers Volumio units and the first result is selected.
- `requests.Session` handles Volumio REST calls. A daemon updater polls state
  once per second and the queue every five seconds, storing results in shared
  `status` and `queue` state protected by locks.
- Urwid owns the terminal UI and its widgets. `refresh_ui` updates widgets on
  the main loop, while network actions run in daemon threads. Protect shared
  state with the existing locks; do not mutate Urwid widgets from worker
  threads. Worker notifications are passed to the UI through `_notification`.
- The playlist is a selectable `ListBox` added to the main `Columns` layout
  when the queue has multiple tracks. `_update_queue_pane` uses queue snapshots
  to avoid rebuilding rows and losing focus/scroll state.
- The Music browser is a stack of browse pages populated by
  `/api/v1/browse`; its selectable rows are handled by `_activate_music_menu_item`.
  Track playback uses the REST `replaceAndPlay` endpoint.
- Most playback and mute controls use REST endpoints. Volumio favorites use a
  Socket.IO event; keep event payloads compatible with Volumio's backend.
