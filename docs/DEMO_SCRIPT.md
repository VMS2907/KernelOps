# Stage demo script (7 minutes + Q&A)

> For the hands-on version on real files (Notepad + real kills), see **[LIVE_DEMO.md](LIVE_DEMO.md)**.

## Before you go on stage (pre-flight checklist)

```bash
git pull && python -m unittest discover -s tests -t .      # 78 tests, all OK
python -m safewrite challenge                             # live challenges C1-C5: all ✓ (see CHALLENGES.md)
python -m safewrite serve --no-browser                    # leave running
python -m safewrite explore --json results/explore.json --keep-details   # optional: refresh results
```

* Browser at `http://127.0.0.1:8765`, **dark theme**, zoom 110–125% for the projector.
* Open 3 browser tabs ahead of time: `#lab`, `#explorer`, `#torture`.
* A terminal with a large font, already `cd`'d into the repo, ready to run `python -m safewrite demo`.
* Fallback: if the browser fails, run `python -m safewrite demo`. It tells the same story in the terminal.
* Turn off notifications. Laptop on mains power (ironic otherwise).

## Roles

| Who | Part |
|---|---|
| **Vyom** | 0:00–2:30: problem, the corruption, the fix |
| **Tanush** | 2:30–5:00: the proof (Explorer), real crashes (Torture / Documents) |
| **Abhishek** | 5:00–7:00: OS concepts, trade-offs, limitations, close |

---

## 0:00 – Hook (30 s), Vyom

> "When you press save, your file isn't written once. It's written in several pieces, and the OS
> and the disk can reorder them. If the power goes at the wrong moment, you don't get the old file
> or the new file. You get a mixture, and nothing tells you."

**Lab tab** → engine `naive` → scenario *Overwrite a document* → model **power** → click **🎯 Find a violation**.

Point at the **Verdict** strip: blue = old version bytes, orange = new version bytes.
> "This file is half version A and half version B. It still opens. The quantities are just wrong."

## 0:30 – The fix (2 min), Vyom

Switch engine to **cow** (Copy-on-write).

* Point at the **syscall timeline**: 2 writes into *free* blocks, `fsync` barrier, **superblock**
  write, `fsync` → ★ COMMIT POINT.
  > "We never overwrite live data. We write the new version somewhere else, make it durable, then
  > flip one pointer. Before the flip, the old version is untouched. After it, the new one is complete."
* Click **⚡ Pull the plug** 3–4 times. Every verdict is green: *previous version* or *new version*.
* Click the chip `#4 pwrite(volume.img, off=0, len=512)` (the superblock write), then step through
  the 2 states with ▶: one recovers A, one recovers B. **Never a mixture.**
* Point at **Recovery log**: "superblock slot A gen 3 … selected generation …: nothing to replay,
  nothing to undo."

## 2:30 – The proof (1 min 30 s), Tanush

**Explorer tab**.
> "Clicking around isn't proof. So for every system call of every save, we generate every disk state
> a power cut can leave: lost writes, reordered writes, torn sectors. Then we run the real recovery
> code on each one."

* Point at the hero numbers: **63,673 crash states · 0 violations in the safe engines · 9/9 safeguards caught**.
* Point at a green row (`cow`), then a red one: **`shadow/no-fsync`**.
  > "Here we deleted one line, the fsync before rename. The checker immediately finds disks where the
  > file is empty and the old version is gone. Every safeguard in our protocols has a red row like this.
  > That's how we justify each line."
* Switch the model to **process**: the red ablation rows turn grey.
  > "And this is why `kill -9` tests aren't enough: they can't see missing fsyncs. Our round-1
  > testing couldn't have caught this."
* Optional: click a red cell → **Open in Lab →** to show the exact counter-example.

## 4:00 – Real crashes (1 min), Tanush

**Torture tab** → engines `naive/trunc, wal, cow` → 30 kills → **⚡ Start killing**.
> "This isn't simulated: a real process saving real files, killed with SIGKILL at random moments."

While it runs: naive/trunc collects red diamonds, cow/wal stay blue/orange (old or new).

*(If time allows)* **Documents tab** → edit text → **⚡ Save & kill -9 mid-save** → the card shows
"Writer killed at syscall #3 of 5 … recovered the PREVIOUS version".

## 5:00 – OS concepts (1 min), Abhishek

**OS concepts tab**: walk down the table, one sentence each:
* **File systems**: names vs. inodes; fsync(file) ≠ fsync(dir).
* **Atomicity**: one commit point per engine.
* **Synchronization**: flock, the transaction lock (race: 50/400 → 400/400), WAL group commit, MVCC readers.
* **Recovery**: idempotent, and we crash the recovery itself.
* **Copy-on-write and journaling**: both implemented. Undo rolls back to the previous version, redo
  rolls forward to the new one, which are exactly the two outcomes the problem statement allows.

## 6:00 – Trade-offs and close (1 min), Abhishek

**Trade-offs tab**: point at the *Recovery time* chart.
> "We chose copy-on-write as the default because recovery stays at about 1 millisecond no matter how
> much history there is, while the log has to replay. We get version history and lock-free readers for
> free. The price is a second fsync per save. For tiny high-rate writes, WAL with group commit is
> faster, and it's a one-word switch."

Close:
> "Honest limits: we can't beat a disk that lies about its cache, and we run one writer process at a
> time. Everything else (every crash state, every safeguard, every number) you can rerun yourself
> with one command. Thank you."

---

## 3-minute version (if time is cut)

1. Lab: `naive` → Find a violation (mixture) → switch to `cow` → Pull the plug ×3. (60 s)
2. Explorer: 63,673 states / 0 violations / 9 of 9 caught; point at `shadow/no-fsync`. (60 s)
3. Trade-offs: recovery chart, why COW. Limits. (60 s)

## If something breaks

| Problem | Do this |
|---|---|
| Browser / server misbehaves | Terminal: `python -m safewrite demo` (press Enter between steps) |
| Explorer empty | It streams; or click **Run all models**; `results/explore.json` preloads it on restart |
| Torture slow on the laptop | Lower the kills to 15, or show `docs/img/torture.png` |
| Port busy | `python -m safewrite serve --port 8800` |
| Question you can't answer | "Let me show you": open the Lab on that engine and pull the plug |
