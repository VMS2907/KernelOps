# SafeWrite: the full presentation script (0 to 100)

About **16 minutes** plus Q&A. Every line in quotes is something you say. Everything else is something
you do. Timings are targets; the "cut" notes say what to drop if you are running late.

---

## The one idea everyone must leave with

> **We cannot prevent crashes. Nobody can. Power cuts, frozen laptops and killed apps will happen.
> SafeWrite prevents what a crash does to your file: after any crash, your file is either the old
> version or the new version, complete, never a broken mixture.**

Seat belt analogy (use it whenever someone is confused): *"A seat belt doesn't stop the accident. It
stops the injury. SafeWrite doesn't stop the crash. It stops the damage."*

- **Crash** = the program dies in the middle of saving. We cause these on purpose, on both sides.
- **Corruption** = the file is left broken (empty, cut in half, half old and half new). That is what
  SafeWrite prevents.

---

## T-30 minutes: setup (do this before anyone walks in)

### 1. Window #1: the SERVER (never close this one)
Open PowerShell (normal, not Administrator):
```powershell
cd $HOME\Desktop\KernelOps
git pull
python -m safewrite arena --pool 8 --per-phone-queue 1
```
- Check the terminal says `phones: up to 60` and shows a `PHONES open: http://…:8765/tap` line.
- If the firewall pops up: tick **Private networks**, click **Allow**.
- The Arena page opens in the browser by itself.
- Windows Wi-Fi settings: the network profile must be **Private**.

### 2. Window #2: the DEMO commands
```powershell
cd $HOME\Desktop\KernelOps
Remove-Item -Recurse -Force shop-unsafe, shop-safe, shop-cow -ErrorAction SilentlyContinue
```
Make the font big: **Ctrl + mouse wheel**.

### 3. Window #3: the LIVE PROOF (programs being killed)
```powershell
cd $HOME\Desktop\KernelOps
while ($true) { Clear-Host; "LIVE: programs saving Final_Assignment.txt right now"; Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -match 'torture' | ForEach-Object { $s = if ($_.CommandLine -match 'naive-') {'NORMAL SAVE'} else {'SAFEWRITE  '}; "{0}   PID {1}" -f $s, $_.ProcessId } | Sort-Object; Start-Sleep -Milliseconds 700 }
```
Make the font big. You should see 16 lines: 8 `NORMAL SAVE` and 8 `SAFEWRITE`.

### 4. Browser tabs, in this order
1. The video
2. Arena (`http://127.0.0.1:8765/arena`, opened automatically)
3. Crash Lab (`http://127.0.0.1:8765/`), with the **Explorer** tab already clicked once so it is loaded

### 5. Test run (2 minutes)
- On the Arena tab press **S**, then **A** (auto-tap). Window #3 should show PIDs changing every second.
  Press **A** again to stop auto-tap, then **R** twice to reset to the lobby.
- Scan the QR with your own phone: the name page must load. Tap once when live: you must hear the "faah".
- Practise the three Window #2 demos once (Parts 3, 4, 6), then run the `Remove-Item` line again so
  they start fresh on stage.

### 6. Final checks
Laptop on the charger. Notifications off. Phone volume up. The Arena tab is showing the QR lobby.

### Roles (suggested)
| Who | Parts |
|---|---|
| Speaker 1 | 1, 2, 3, 4 (hook, video, the broken save, the safe save) |
| Speaker 2 | 5, 6 (the Arena, the audience picks the crash) |
| Speaker 3 | 7, 8, 9 (Crash Lab, close, Q&A lead) |

---

## PART 1 · 0:00 – The hook (30 s)
**Screen:** nothing yet, or the title slide.

> "Quick show of hands. Who here has lost work because a laptop died, an app froze, or the battery ran
> out while you were saving?"

Wait for hands. Point at a few.

> "Keep that feeling. Today you're going to cause that disaster yourselves, sixty of you at once, on
> this laptop, for real. And we'll show you a way to save files where it never happens."

## PART 2 · 0:30 – The video (2 min)
**Screen:** browser Tab 1. Play the video.

When it ends:
> "That's the idea. But don't believe a video. Let's do it for real."

---

## PART 3 · 2:30 – The broken save, on a real file (2 min)
**Screen:** Window #2.

> "When you press Save, your computer doesn't write the file in one go. It writes it in pieces. Think of
> rewriting your assignment on paper: you erase the old page first, then start writing the new one.
> If someone snatches the pen halfway, the page is half new, half old. That's exactly what happens
> when an app crashes in the middle of a save."

Create a real file, version A:
```powershell
python -m safewrite init shop-unsafe --engine naive
python -m safewrite put  shop-unsafe inventory.csv --file examples/inventory_A.csv
```
Now save version B, but **kill the program in the middle of the save**:
```powershell
python -m safewrite crash shop-unsafe inventory.csv --file examples/inventory_B.csv --at 2
```
> "That line in red: we just killed a real program, halfway through saving."

Open the file:
```powershell
notepad shop-unsafe\docs\inventory.csv
```
Point at the top: **VERSION B**. Press **Ctrl+End**: the bottom says **END OF VERSION A**.

> "This file opens fine. No error message. But the top is the new version and the bottom is the old
> one. Half your assignment is today's, half is last week's, and nothing tells you. This is how most
> apps save."

Close Notepad.

## PART 4 · 4:30 – The safe save, on a real file (2 min)
**Screen:** Window #2.

> "Now the same thing, the SafeWrite way. The trick: never touch the old file. Write the new version
> next to it, make sure it's completely on the disk, and only then swap it in, in one single step.
> That single step is called the **commit point**."

```powershell
python -m safewrite init shop-safe --engine shadow
python -m safewrite put  shop-safe inventory.csv --file examples/inventory_A.csv
python -m safewrite crash shop-safe inventory.csv --file examples/inventory_B.csv --at 4
explorer shop-safe\docs
```
File Explorer shows **two files**:
> "Look: `inventory.csv` is the old version, never touched. The `.tmp` file is the new version, written
> completely, but the program was killed before the swap."

Now "turn the computer back on" by running recovery:
```powershell
python -m safewrite recover shop-safe --key inventory.csv --out recovered.csv
notepad recovered.csv
```
Read the recovery line out loud: *"deleted uncommitted temp file"*.
> "When the computer comes back, SafeWrite looks at what's on the disk and cleans up the unfinished
> work. Your file is the complete old version. Old or new. Never a mixture."

---

## PART 5 · 6:30 – The Arena: the audience crashes it (4 min)
**Screen:** browser Tab 2 (Arena). Press **F11** for full screen.

### Joining (1 min)
> "Phones out. Volume up, silent mode off. Join this Wi-Fi, scan the QR code, type your name.
> Don't press anything yet."

While they join, read names off the screen:
> "I see Priya… Rahul… come on, back row, we've got 20… 30… who's going to top the leaderboard?"

When the joined count stops climbing:
> "Here's what happens when you press. On this laptop, two real programs are saving
> `Final_Assignment.txt` over and over. Every time you press, you kill both of them in the middle of
> the save. The same press, at the same moment. One saves the normal way, like every app you use. The
> other uses SafeWrite. Ready? Three, two, one…"

Press **S**.
> "GO! Press as fast as you can!"

### While they press (2 min): show it's real, then show the result

**First, the proof it's real.** Press **Alt+Tab** to Window #3.
> "Is this a fake animation? No. These are the real programs running on this laptop right now: 8
> normal save, 8 SafeWrite. Watch the PID numbers. Every number that changes is a program that one of
> you just killed in the middle of saving. Old one dies, new one starts. That's you, doing that."

**Then, the result.** Press **Alt+Tab** back to the Arena.

Point at the **left (red)** side:
> "This is the normal save. Look at the gallery: Rahul's crash, the file is EMPTY, the assignment is
> gone. Priya's: cut short. More than half of the crashed files are destroyed."

Point at the **right (green)** side:
> "Same presses, same moment, same kill. SafeWrite: corrupted files… zero. Read the feed: 'crashed
> save 482, recovered version 481.' The program died, but the file came back complete."

Point at the **top bar**:
> "And we're honest: 'taps received' is bigger than 'real crashes', because the laptop can only kill
> about 25 pairs of programs a second. We never count a tap as a crash unless a program really died
> and we checked the file."

Call out the leaderboard:
> "And our top crasher is… [name]! [Name] has destroyed [n] assignments today!"

### The verdict (1 min)
After 30–60 seconds of pressing, press **F**. The summary slide appears.

Read it slowly:
> "[N] students. [X] real crashes. The normal save destroyed [Y] files. SafeWrite lost… zero."

Pause. Let it land.

> "Notice: we didn't stop a single crash. SafeWrite was killed exactly as many times as the normal
> save. We prevented the damage. Crashes will happen. Losing your work shouldn't."

Press **F11** to leave full screen.

---

## PART 6 · 10:30 – "Where exactly does SafeWrite save it?" The audience picks the crash (1.5 min)
**Screen:** Window #2.

> "Some of you are thinking: fine, but where does SafeWrite actually protect the file? Let's do it
> step by step, and you choose where we crash it."

```powershell
python -m safewrite init shop-cow --engine cow
python -m safewrite put  shop-cow inventory.csv --file examples/inventory_A.csv
python -m safewrite crash shop-cow inventory.csv --file examples/inventory_B.csv --list
```
The screen lists the numbered steps of one save and a **── COMMIT POINT ──** line.
> "This is everything the computer does for one save, step by step. This is the same engine as the
> green side of the Arena. Somebody shout a number."

Take a number, say **3**:
```powershell
python -m safewrite crash shop-cow inventory.csv --file examples/inventory_B.csv --at 3
python -m safewrite recover shop-cow --key inventory.csv --out recovered-cow.csv
notepad recovered-cow.csv
```
> "You picked step 3. We killed it exactly there. The file is complete: all version A, or all version
> B, never a mix. Before the commit point you get the old version. After it, the new one."

Want a second number? First put version A back:
```powershell
python -m safewrite put shop-cow inventory.csv --file examples/inventory_A.csv
```
*(Cut this part if you are short on time.)*

---

## PART 7 · 12:00 – Crash Lab: why this survives even a power cut (4 min)
**Screen:** browser Tab 3 (Crash Lab).

### 7a. Lab tab: inside one save (1.5 min)
Click **Lab**. Set engine **naive**, scenario **Overwrite a document**, model **power**. Click
**🎯 Find a violation**.
> "Killing a program is the easy case. The worst case is a power cut, because the computer also loses
> what was sitting in memory, waiting to be written. Each chip on this line is one step of a save. We
> cut the power here. The coloured bar is the file afterwards: blue bytes are the old version, orange
> the new. A mixture."

Switch engine to **cow**. Click **⚡ Pull the plug** three or four times.
> "Same power cut, at a random moment, each time. Always green: old version or new version. This
> green star is the commit point: one single step that switches from old to new. The step before it
> is an **fsync**. That's the computer promising 'this is physically on the disk now, not just in
> memory'. Without it, a power cut loses data."

### 7b. Explorer tab: proof, not luck (1.5 min)
Click **Explorer**. Point at the big numbers.
> "Clicking a few times isn't proof. So we built a simulator. For every step of every save, it builds
> every disk a power cut could possibly leave behind, and runs SafeWrite's real recovery code on each
> one. 63,673 crash states. Zero failures for the safe engines."

Point at the red **`shadow/no-fsync`** row:
> "And here we deliberately deleted one line of code, one fsync. The checker immediately found
> crashes where the file is empty. We did this for every safety rule: nine removed, nine caught. That
> proves every line is needed, and that our checker really catches bugs."

If anyone looks sceptical, click **↻ Re-run fresh**:
> "These numbers aren't pre-recorded. This is recomputing on this laptop right now."

### 7c. Trade-offs and OS concepts (1 min)
Click **Trade-offs**, point at the recovery-time chart:
> "Nothing is free. SafeWrite does one extra fsync per save, so saving is a little slower. In return,
> recovery takes about one millisecond, every time, no matter how much history there is."

Click **OS concepts**, one sentence per row:
> "File systems: a file's name and its data are separate, and both must be made safe. Atomicity: one
> commit point, all or nothing. Synchronization: locks, so two writers can't corrupt each other.
> Recovery: runs automatically on every start, even if it crashes itself. Copy-on-write and
> journaling: we built both and measured them."

---

## PART 8 · 16:00 – Close (30 s)
**Screen:** back to the Arena summary slide, or the title slide.

> "Today you crashed real programs with your own phones. You watched them die live on this laptop.
> You saw a real file broken in Notepad. And our simulator checked 63,673 power cuts.
>
> We can't stop crashes. Nobody can. But we can stop them from destroying your work. With a normal
> save, your file is a gamble. With SafeWrite, it's the old version or the new version. Never a
> mixture.
>
> Thank you."

---

## PART 9 – Q&A (memorise these)

**"SafeWrite's program is getting killed too, so SafeWrite is crashing as well?"** ⭐ *(most likely)*
> "Yes, on purpose. We kill both sides equally, the same number of times. SafeWrite isn't about not
> crashing. Nothing can promise that. It's about what your file looks like after the crash: on the
> normal side, broken; on SafeWrite, complete. Seat belt, not crash-proof car."

**"Is this a mock? Is the Arena just an animation?"**
> "Look at Window #3: the PIDs change every time someone presses. Those are real processes on this
> laptop being killed." Show Window #3 or Task Manager (Details tab, sort by name, the `python.exe`
> entries). For full proof, open `http://127.0.0.1:8765/api/arena/export` in the same browser:
> "every single crash, with the exact result we read from the disk."

**"Where is the crash actually happening?"**
> "On this laptop. Every press kills one normal-save program and one SafeWrite program with Windows'
> TerminateProcess, the same as End task in Task Manager. They don't get to finish the save."

**"If the files get broken, why can't I see a broken file in the folder?"**
> "The broken state exists only for a few milliseconds. Right after each kill we open the file, record
> what we found, and reset it for the next round. You saw a broken file you could actually open in
> Notepad in Part 3, and the gallery on the left lists every broken one."

**"Why does the normal save sometimes survive?"**
> "If your press lands right after a save finished, there's nothing half-written to break. That's why
> it's about 60% destroyed, not 100%. We show the survivors too. We don't hide them."

**"Why are 'taps received' and 'real crashes' different?"**
> "The laptop can kill about 25 pairs of programs per second. When you press faster than that, we
> count your tap but we don't pretend it was a crash."

**"The Lab and Explorer are simulations, right? Pre-recorded data?"**
> "The power-cut part is simulated, because we can't cut the power to this laptop live, and the
> battery would keep it alive anyway. But it runs SafeWrite's real recovery code, and you can re-run it
> live: [click ↻ Re-run fresh]. The Arena and the Notepad demos were 100% real."

**"What is fsync?"**
> "The computer normally keeps what you save in memory for a while and writes it to the disk later.
> fsync says: write it to the disk now, and don't come back until it's there. It's the difference
> between 'saved' and 'actually saved'."

**"Why not just use a database like SQLite?"**
> "SQLite uses exactly these techniques inside: a journal, or a write-ahead log. The task was to
> design and justify the mechanism, so we built four of them, measured them, and broke each one on
> purpose to prove every rule is needed."

**"What can't you protect against?"**
> "A disk that lies, saying data is saved when it isn't. No software can fix that. Also, we allow one
> writing program at a time."

**"Why is copy-on-write your default?"**
> "It recovers in about one millisecond no matter what, keeps old versions for free, and readers never
> wait. The cost is one extra fsync per save."

---

## If something goes wrong

| Problem | What to do | What to say |
|---|---|---|
| Phones show "WAITING…" | Click the Arena tab, press **S** | – |
| Phones can't open the page | Check they're on the same Wi-Fi. Still failing: press **A** (auto-tap) | "The Wi-Fi is fighting us, so the laptop will press for you. Every crash is still real, labelled AUTO." |
| No sound on a phone | Volume up, silent off, tap 🔊 once | "Volume up!" |
| "The arena is full" on a phone | More than 60 phones; seats free up a minute after someone leaves | "Share with your neighbour!" |
| Arena page looks frozen | Press **F5**. Nothing is lost | – |
| Want another round | **R** twice (lobby), then **S** | "Round two!" |
| Window #1 closed by accident | Run the setup step 1 commands again; everyone rescans the QR | – |
| `init` says "already a store" | `Remove-Item -Recurse -Force shop-unsafe, shop-safe, shop-cow` and redo | – |
| Running late | Skip Part 6 and 7c. Keep 3 → 5 → 7b → 8 | – |

**After the talk:** Ctrl+C in Window #3, then Ctrl+C in Window #1 (it kills every writer and deletes
its temp files).
