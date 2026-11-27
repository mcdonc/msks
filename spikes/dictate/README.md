# dictate

A one-shot speech-to-clipboard spike: press a shortcut, talk, pause, and
the transcript is on the clipboard. It is the first step toward
dictating to pi in a workspace VM. The guest has no audio device
(cloud-hypervisor) and the images have no audio stack, so capture
belongs to the host, where the GUI client runs with the microphone and
the clipboard.

It imports nothing from msks, it lives outside `src/msks/` so the repo
gates (import graph, lines, coverage) do not apply, and it answers one
question: what does the one-shot interaction feel like, and which
parameters matter? Standard library only.

## The interaction under test

- One shortcut. Press it, talk, pause. About a second of silence ends
  the clip. `whisper-cli` transcribes it, and the text goes to the
  clipboard and the primary selection. The user pastes.
- Press the same shortcut again (`toggle`) to finish at once. `stop`
  does the same. `cancel` discards. The commands reach a running
  capture through a PID file in `$XDG_RUNTIME_DIR` and a signal:
  `SIGUSR1` finishes, `SIGTERM` discards. A press during transcription
  is ignored.
- Feedback: a tone when listening starts, a tone when the text is on
  the clipboard, a level meter and a timer on stderr from a terminal,
  and a desktop notification when `notify-send` is installed.
- Speech: `pw-record` captures 16 kHz mono, an RMS gate finds
  speech and silence, `whisper-cli` transcribes with a ggml model
  downloaded on first use (`small` by default). `--prompt` biases
  spelling toward a vocabulary. The level that counts as speech sits
  `--margin` dB (14) above the tracked noise floor and never below
  `--threshold` (-60 dBFS). The first 0.3 s calibrates the floor, so
  speak after the start tone. The stop tolerates a few loud frames
  inside the quiet window.

## Layout

- `dictate.py`: the CLI (`record`, `toggle`, `stop`, `cancel`,
  `model`).
- `smoke.py`: offscreen checks. The detector, the WAV writer, the
  command builders, and the record loop over a synthetic source,
  including the finish and cancel signals. No microphone, no model, no
  clipboard.
- `run.sh`: runs either with the ambient python.

## Requirements

PipeWire (`pw-record`, `pw-cat`), whisper.cpp (`whisper-cli`),
`wl-clipboard` (`wl-copy`) or `xclip`, and optionally `notify-send`.
`wpctl status` lists the sources under Sources; `--target` takes a
node serial (the EasyCall speakerphone here is 64).

## Microphone access

PipeWire serves every program of your user without a prompt. The
prompts on macOS and Windows, and for Flatpak apps here, come from a
sandbox this spike does not have; a Flatpak is the only route to a
dialog. While it records, `pw-top` lists its stream. The start tone and
a desktop notification (when `notify-send` is installed) say that
capture is live; `--no-sound` removes the tone. The EasyCall's mute
button cuts the microphone at the device. A clip is written only under
`/tmp/dictate-*`, and it is deleted after transcription unless
`--keep` or `--dry-run` is set.

## Usage

```sh
./run.sh model            # download ggml-small.bin (466 MB) once
./run.sh                  # record until silence, copy the transcript
./run.sh --dry-run        # capture and stop, keep the clip, skip whisper
./run.sh --prompt "msks, devenv, NixOS, pi"
./run.sh toggle           # finish a running capture, else start one
./run.sh stop | cancel
./run.sh smoke            # the offscreen checks
```

Exit codes: 0 copied, 3 nothing said, 130 cancelled, 1 failure.

## Binding it in KDE Plasma

System Settings, Shortcuts, Add New, Global Shortcut, Command/URL.
Trigger: the chord. Action: the path to `run.sh toggle` with any flags.
Wayland delivers press events only, so the binding is a toggle, and the
silence stop makes the second press rare. Pick a chord that no terminal
or pi binding uses: the desktop consumes it for every window.

## Open questions

- Is the silence stop enough on its own, or does the second press get
  used constantly?
- Which `--threshold` and `--silence` work in a normal room with the
  speakerphone, and with the Scarlett 2i2?
- Is the per-utterance model load (about a second for `small`)
  acceptable, or does capture need a long-lived process?
- Clipboard only, or auto-paste later (ydotool on Wayland)?
- Where does it land: `msks dictate` as a client-local CLI command, or
  the GUI client with a tray icon?

## Findings

- A fixed threshold does not survive the speakerphone. The EasyCall's
  noise floor in a quiet room sits at -33 dBFS median with peaks to
  -19 (its echo cancellation and gain control lift the room). At
  -40 dBFS the detector started at once and ran to the cap. The
  tracked floor (the 10th percentile of the last 3 s, plus 14 dB)
  keeps it waiting, and the same setting works on a quiet interface.
- The floor needs a moment. Nothing counts as speech for the first
  0.3 s, so the start tone is also the cue to begin talking.
