# SafeWrite launch video: storyboard, voiceover, handoff

**Format:** ~100 s, 1920×1080, 30 fps, H.264 + AAC.

**Sources:** everything is real or original:
* real product footage of the Crash Lab;
* real CLI output and real files from a real killed process;
* animated typography and diagrams;
* an original score and sound design synthesized in code (no licensing problems).

Re-render with `python video/build.py` (see `video/README.md`).

---

## For the session reviewing reference videos on your computer

You are looking at top YC / startup launch videos in the browser to tune this cut. For 2–4 videos,
note the following and paste the notes back into the main session:

1. **Cold open:** what happens in the first 3 seconds that makes you keep watching?
2. **Pacing:** roughly how long each shot or text card stays up; where the cuts land on the beat.
3. **Typography:** font weight and size, text animation style (hard cuts, slide-ups, word-by-word).
4. **Product footage:** zoomed-in crops vs. full screen; cursor visible?; speed-ups?
5. **Music arc:** where it drops out, where the big hit lands, how it ends.
6. **Close:** the final line and what's on the last frame.

Then compare against the storyboard below and list concrete changes ("cut scene X to 2 s", "the drop
should land on the logo", …).

---

## Storyboard

Tempo: 100 BPM (one beat = 0.6 s). Cuts land on beats.

| # | Time | Picture | On-screen text | Sound |
|---|---|---|---|---|
| 1 | 0.0–5.4 | Black. Blinking cursor types. | **You just pressed Save.** → *Did it actually save?* | room tone, key clicks, slow sub heartbeat |
| 2 | 5.4–10.2 | A document breaks into chunks that fly one by one into a disk | **Saving isn't one step.** | soft ticks per chunk, rising drone |
| 3 | 10.2–11.4 | The third chunk is mid-air → flicker → **black** | (nothing) | **power-down sweep → dead silence** |
| 4 | 11.4–16.2 | Lights return; the saved file is half orange, half blue, glitching | **Half new. Half old.** → **And nothing tells you.** | glitch stutter, eerie drone |
| 5 | 16.2–22.0 | **Real terminal**: a save is killed (`⚡ CRASH`), then the real file: VERSION B lines… turning into VERSION A at line 113 | *Real program. Real file. Killed mid-save.* | tension pulse, typing |
| 6 | 22.0–28.0 | Hard cuts, one word card per beat pair | **Bank balances. · Hospital records. · Exam marks. · Your phone's update.** → **One crash. Silent corruption.** | impact hit on every card |
| 7 | 28.0–34.0 | Riser into the reveal: ⚡ mark → **SafeWrite** | *Crash-consistent document storage.* **Proven, not promised.** | reverse swell → big chord + sub drop |
| 8 | 34.0–43.6 | Animated copy-on-write: old blocks stay, new blocks fill empty space, **one pointer flips** | **Never overwrite.** → *Write the new version somewhere else.* → **Then flip one switch.** → *Crash before: old. Crash after: new.* → **Never a mix.** | beat drops in; click on the flip |
| 9 | 43.6–52.0 | **Real Crash Lab**: pull the plug again and again: green every time | *Pull the plug anywhere.* | driving beat |
| 10 | 52.0–55.6 | Four technique chips pop in | *Temp file · Undo journal · Redo log · Copy-on-write* **We built all four.** | plucks on each chip |
| 11 | 55.6–58.0 | Text | *Most people test **3** crashes.* → **We tested every one.** | build |
| 12 | 58.0–66.4 | **Real Explorer** streaming the matrix; giant counter | **63,673** crash states → **0 failures** | rising arpeggio → hit on "0" |
| 13 | 66.4–71.2 | Red ablation rows flash | **We broke it on purpose. 9 ways. Caught all 9.** | three hits |
| 14 | 71.2–79.6 | **Real torture run** (sped up): dots stream in | *Real `kill -9`. Real files.* unsafe ✗ vs SafeWrite **0 corruptions** | fast hats, ticking |
| 15 | 79.6–84.4 | Text | *Killing the program can't find the deadliest bug.* → **So we built a power-cut simulator.** | music drops out, then returns |
| 16 | 84.4–90.4 | **Real Documents tab**: Save & kill -9 → recovered | *Your laptop. Your disk.* | full band |
| 17 | 90.4–96.4 | Words build on beats | **Every crash. Old or new. Never a mix.** | biggest hit of the film |
| 18 | 96.4–102 | End card | **SafeWrite** ⚡ · Proven, not promised · Team VAT · Kernel Ops · github.com/VMS2907/KernelOps | final chord rings out |

---

## Voiceover script (record later, phone is fine)

Speak slowly and calmly. The pauses are part of it.

| Time | Line |
|---|---|
| 0.5 | "You just pressed save." |
| 3.2 | "Did it actually save?" |
| 5.6 | "Saving isn't one step. Your computer writes your file in pieces…" |
| 10.0 | "…and if the power cuts out halfway —" *(stop. silence.)* |
| 12.0 | "you get a file that's half new, half old. And nothing tells you." |
| 16.5 | "This is a real program, killed in the middle of a save. The top is today. The bottom is yesterday." |
| 22.2 | "Bank balances. Hospital records. Exam marks. Your phone's update." |
| 26.5 | "One crash — silent corruption." |
| 29.5 | "This is SafeWrite." |
| 34.2 | "We never overwrite your data. We write the new version somewhere else — then flip one switch." |
| 40.2 | "Crash before the switch: you get the old version. After: the new one. Never a mix." |
| 44.0 | "Pull the plug anywhere. It always comes back whole." |
| 52.2 | "We built all four classic techniques — and tested every one." |
| 55.8 | "Most people test three crashes. We tested every possible one." |
| 59.0 | "Sixty-three thousand crash states. Zero failures." |
| 66.6 | "Then we broke it on purpose, nine different ways — and caught every single one." |
| 71.5 | "Real kill-nine. Real files. The ordinary save breaks. SafeWrite doesn't." |
| 79.8 | "And the deadliest bug? Killing the program can't even find it. Only a power cut can — so we built a power-cut simulator." |
| 84.6 | "On your laptop. On your disk." |
| 90.6 | "Every crash. Old or new. Never a mix." |
| 96.6 | "SafeWrite. Proven, not promised." |
