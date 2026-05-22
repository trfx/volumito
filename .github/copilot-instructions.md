Purpose

This file guides Copilot-style sessions about how to work effectively in this repository (what to run, where the logic lives, and repository-specific conventions). Keep changes focused to volumito.py and volumio_client.py unless asked otherwise.

Build / test / lint commands

- No CI, test suites, or linter configs exist in this repo.
- Install runtime deps: pip install -r requirements.txt
- Run the app (single-command): python3 volumito.py
- Typical dev workflow:
  - python -m venv venv && source venv/bin/activate
  - pip install -r requirements.txt
  - python3 volumito.py
- Single-test guidance: there are no unit tests. If adding tests, place them under tests/ and run with pytest (not present by default).

High-level architecture (big picture)

- Repo purpose: a simple terminal UI (TUI) remote controller for Volumio.
- Two main modules:
  - volumito.py: the TUI implementation. Key class: VolumitoV1. Responsibilities:
    - Read/create config (~/.config/volumito/config.yaml) to get volumio_host
    - Build urwid UI, handle keybindings and user input
    - Maintain an internal status dict with a thread-safe lock
    - Start an updater thread (_updater) that polls device state every second
    - Schedule a UI refresh via urwid.MainLoop.set_alarm_in
    - Merge and sanitize device state via _merge_status to avoid UI "bounce" after seeks
    - Use daemon threads for background network operations
  - volumio_client.py: small HTTP client wrapping the Volumio API. Responsibilities:
    - get_state(): GET /api/v1/getState with a 3s timeout
    - send_command(...): wrapper for /api/v1/commands/?cmd=...
    - send_seek(position_seconds): attempts multiple seek variants (secs, ms, position, seek)
    - parse_status_times(status): best-effort heuristic to derive (seek_s, duration_s) from many possible field names and units (ms vs s)

- Runtime model: the UI polls device state in the background, updates a shared status dict (protected by a lock), and the UI's refresh loop reads that dict frequently to update widgets. Commands (volume, play/pause, next/prev, seek) are executed via background threads to avoid blocking the UI.

Key conventions and repository-specific patterns

- Configuration:
  - Default config path: ~/.config/volumito/config.yaml
  - Key expected: volumio_host (e.g., volumio.local). The first run creates a default file and exits so the user can edit it.

- Tolerant / defensive parsing:
  - Volumio API fields are inconsistent across versions and plugins. volumio_client.parse_status_times does deep searches and tries multiple unit interpretations (s vs ms).
  - When changing position/seek, VolumitoV1 uses a _recent_seek marker to avoid immediately accepting outdated device-reported positions (prevents UI bounce). Any assistant changes that touch seek logic must preserve this behavior.

- Networking and timeouts:
  - VolumioClient uses requests.Session with short timeouts (3s). Code frequently swallows exceptions and returns empty dicts — handle cautiously when adding features that assume state presence.

- Threading / UI interaction:
  - UI operations are performed on the urwid MainLoop; blocking network calls are always executed in background daemon threads.
  - Shared state is protected by self._status_lock. Always acquire the lock when mutating or reading critical fields.

- Seeking and command semantics:
  - send_seek tries multiple parameter names and ms/s variants to increase compatibility. If modifying seek behavior, keep the candidate fallback list and the order (secs, position, msecs, seek secs).
  - _merge_status intentionally strips seek/position keys when a recent seek is outstanding, to preserve the user's requested position until the device reports a matching value.

Helpful places to inspect for context

- volumito.py — main TUI app, keybindings, status merging logic, and lifecycle
- volumio_client.py — HTTP API client, parsing heuristics, send_seek variants
- requirements.txt — pinned dependencies (urwid, PyYAML, requests, etc.)
- README.md — minimal usage documentation; initial-run behavior noted there

AI assistant / helper configs

- No special AI assistant config files (CLAUDE.md, AGENTS.md, .cursorrules, .windsurfrules, etc.) were found in this repository.

Notes for future Copilot sessions

- Preserve the behavior that tolerates inconsistent API fields. Tests or refactors should include example payloads (ms vs s, nested fields) because the parsing heuristics are central to the app's robustness.
- Avoid converting blocking network calls into main-loop work; keep them in background threads or convert to async with care.
- If adding tests, add a small fixtures directory with representative API payloads used by parse_status_times and _merge_status.

Summary

- Added repository-specific Copilot instructions covering run commands, architecture summary, and conventions to preserve while editing.

If you'd like different formatting, additions (examples of API payloads for tests), or inclusion of suggested lint/test commands, say which area to expand.