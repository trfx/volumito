# volumito
A TUI remote controller for Volumio

## Requirements
   - Linux/MacOS (tested with alacritty)
   - Python3
   - Volumio (tested with v4+)

## Install
After cloning this repository execute the below. On startup, Volumito discovers
Volumio units advertised on the local network and connects to the first one it
finds. Press `h` to choose another discovered unit. A `~/.config/volumito/config.yaml`
is created or updated with the selected host and reused on later starts. Use
`m` to browse the Music menu: `Enter` opens folders or plays a track, and `Esc`
goes back one level. Press `f` to add the current track to Volumio favorites;
web radios are saved to radio favorites. Press `t` to toggle mute. Use `u` to
show or hide the playlist, `up` and `down` to select a queued track, and `[` or
`]` to seek backward or forward 30 seconds.
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python volumito.py
```

## Microphone audio meters (optional)

The meter strips are synthetic by default. The `sounddevice` and `numpy`
dependencies are included in `requirements.txt`. PortAudio may also be needed
on your platform.

```bash
pip install -r requirements.txt
```

Opt in by adding this setting to `~/.config/volumito/config.yaml`:

```yaml
microphone_meter: true
```

When enabled, Volumito opens the default microphone stream at startup. It does
not show its own consent dialog: the operating system may ask for permission,
but many Linux setups do not prompt and instead grant or deny access through
the audio system. If access is denied or no input device is available, the
strips stay hidden and the TUI reports the error; synthetic levels are not
shown in this case. Audio levels are processed only while Volumio is playing
and unmuted; other microphone frames are discarded. Audio is never recorded
or sent over the network.

Microphone metering measures sound in the room, not Volumio's digital output.
It can pick up other sounds, and speaker placement, room noise, and headphones
will affect the readings. With a mono microphone, both strips show the same
level.

## Screenshots
![Volumito screenshot](volumito-screenshot-dark.png)

![Volumito screenshot](volumito-screenshot-light.png)
