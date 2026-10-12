# SafeWrite launch film

A ~100 second launch video, built entirely from code. Storyboard and voiceover script: [`docs/VIDEO_PLAN.md`](../docs/VIDEO_PLAN.md).

Nothing in it is stock or staged:

| Layer | Source |
|---|---|
| Terminal scene | `capture_cli.py` runs the real CLI (`init`, `put`, `crash --at 2`) and stores the exact output plus the real bytes of the file the killed process left behind |
| Product footage | `capture_footage.js` drives the real Crash Lab (`python -m safewrite serve`) in headless Chromium and records it with the DevTools screencast API, with a visible cursor and click ripples |
| Motion graphics | `scenes.html` + `scenes.js`: a deterministic timeline; `window.renderAt(t)` draws any instant, so every frame is exact |
| Frames → video | `render.js` steps the timeline at 30 fps, screenshots each frame, pipes JPEGs into ffmpeg (H.264, CRF 16) and exports the cue list (`build/timeline.json`) |
| Score + sound design | `score.py` synthesizes everything with numpy (drums, bass, pads, plucks, bells, risers, impacts, glitches, keyboard clicks, a convolution reverb) and places each sound on the picture's cues; mastered to −14 LUFS, −1.5 dBTP |

## Build

```bash
pip install numpy                    # score
npm i -g playwright                  # footage + frames (uses the installed Chromium)
python video/build.py                # → video/build/SafeWrite_launch.mp4 (+ _web.mp4)
```

Faster loops:

```bash
python video/build.py --skip-capture             # keep footage, re-render picture + sound
python video/build.py --audio-only               # re-score only (after editing score.py)
node video/render.js --stills 12,30.5,64         # PNG stills for review
node video/render.js --from 40 --to 52           # preview a range
```

## Adding the voiceover

Record the lines in `docs/VIDEO_PLAN.md` (a phone is fine) as one take aligned to the film's start, or
line by line and placed at the listed times in any editor. Then:

```bash
python video/build.py --vo voice.wav
```

The music ducks under the voice automatically (sidechain compression) and the mix is limited.

## Tempo map

100 BPM (one beat = 0.6 s), A minor (Am–F–C–G), resolving to C add9 on the logo and on the end card.
The groove's first downbeat lands on "Never overwrite." at 34.0 s; the power cut at 10.2 s is followed by
true digital silence until the lights come back at 11.4 s.
