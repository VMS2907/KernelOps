# Live technical challenges (Team VAT, KO-16, OSH021)

The organizers offer five challenge options: 1 Easy, 2 Medium, 2 Hard. They confirm at the event
which ones we must attempt. **All five are implemented, tested in CI and demoable with one command
each**, so whichever is assigned is ready.

```bash
python -m safewrite challenge            # all five, ~5 s, exit status 0 = every check passed
python -m safewrite challenge c4         # just one
python -m safewrite challenge c2 --keep  # keep the files afterwards and print where they are
```

Every challenge runs on **real files** in a scratch directory, prints the **actual disk state before
and after**, the **protocol / resource / scheduling trace**, and a list of ✓/✗ checks. Faults are
**simulated** by a controlled adapter (`safewrite/vfs/faulty.py`) placed between the engine and the
real disk. Nothing fills the host disk, touches hardware or disturbs other programs.

| # | Challenge | Level | One command | What it proves | Checks |
|---|---|---|---|---|---|
| C1 | SafeWrite crash points | Easy | `challenge c1` | real `os._exit` at `during_write` / `after_validation` / `after_replace` → exactly A / A / B | 8 |
| C2 | Invalid destination refusal | Medium | `challenge c2` | a valid-looking temp is **not promoted**; damaged file and temp kept byte-identical | 8 |
| C3 | Space / size constraint | Medium | `challenge c3` | 1 MiB cap; ENOSPC halfway through the temp → A kept, actionable error, retry works | 7 |
| C4 | Post-rename flush error | Hard | `challenge c4` | `fsync(dir)` EIO after the rename → `VisibleNotDurable`, not "aborted" | 10 |
| C5 | Concurrent writers extension | Hard | `challenge c5` | two real writer processes + barrier: commits serialized, recovery never touches a live save | 16 |

Automated tests: `tests/test_challenges.py` (24 tests, part of the normal suite and of CI on
Linux, macOS and Windows).

---

## The error contract (used by C3, C4, C5)

A failed save now says **which side of the commit point** it failed on, because the right reaction
is opposite on the two sides (`safewrite/errors.py`):

| Exception | `committed` | Readers now see | Store afterwards | What the caller should do |
|---|---|---|---|---|
| `CommitAborted` (incl. `PayloadTooLarge`, `WriteConflict`) | `False` | the **previous** version | still writable | fix the cause (free space, shrink, re-read and merge), retry |
| `VisibleNotDurable` | `True` | the **new** version | read-only until reopened | don't report "not saved"; don't retry fsync; reopen and save again for a durable acknowledgement |

Both are `OSError` (with the real `errno`) **and** `StoreError`, so every existing handler keeps
working. `e.phase` names the protocol step (`write-temp`, `fsync-temp`, `validate-temp`, `rename`,
`fsync-dir`, `size-check`, `conflict`).

The shadow engine's save is now two explicit halves (`safewrite/engines/shadow.py`):

```
prepare(key, B)      create unique temp → write → fsync → read back + validate     (invisible)
publish(candidate)   [COMMIT lock] → rename(tmp, dest) ★ commit point → fsync(dir)  [unlock]
```

The single-writer syscall sequence is unchanged, byte for byte: the explorer still reports
**63,673 crash states, 0 violations in the safe engines, 9/9 ablations caught**, cell for cell
identical to before.

---

## C1 · SafeWrite crash points (Easy)

> *Initialize valid A then attempt B with each documented crash point. Recover and compare exact
> payload bytes. A = 64 bytes 0x41, B = 64 bytes 0x42; test `during_write`, `after_validation` and
> `after_replace` process-crash points. Focus: verify fixed-temp commit semantics.*

**What we built.** The three crash points are now documented on the engine
(`ShadowEngine.CRASH_POINTS`) and accepted by the real-crash CLI:

| Crash point | Where the process dies |
|---|---|
| `during_write` | *inside* `write(2)` of the temp file: half of B's record reached it |
| `after_validation` | temp complete, fsynced and validated; `rename` not yet issued |
| `after_replace` | `rename` done (B visible under the real name); directory not yet fsynced |

The engine can also run the round-1 layout, **one fixed temp name** per document
(`temp_names="fixed"` → `docs/doc~.tmp`), next to the hardened unique names. C1 runs both.

**Run it step by step on real files** (Windows: use `py` if `python` is not found):

```bash
python -m safewrite init c1 --engine shadow --temp-names fixed
python -c "open('A.bin','wb').write(b'\x41'*64); open('B.bin','wb').write(b'\x42'*64)"
python -m safewrite put     c1 doc --file A.bin
python -m safewrite crash   c1 doc --file B.bin --list                    # the plan + the 3 crash points
python -m safewrite crash   c1 doc --file B.bin --phase during_write      # really dies, exit 77
python -m safewrite recover c1 --key doc --out out.bin                    # out.bin = 64 × 0x41
```

Repeat the last two lines with `--phase after_validation` (→ A) and `--phase after_replace` (→ B),
running `put c1 doc --file A.bin` in between.

**What you will see** (`challenge c1`):

```
crash point after_validation: temp complete, fsynced and validated; rename not yet issued
· CRASH: process killed with os._exit(77) at phase 'rename'
disk right after the crash (before recovery):
  docs/doc         88 B  valid, payload A (64 × 0x41)
  docs/doc~.tmp    88 B  valid, payload B (64 × 0x42)
  ✂ recovery: deleted uncommitted temp file doc~.tmp (it was never renamed, so never committed)
✓ [fixed temp] after_validation: process killed (exit 77); recovered payload is exactly A: 41 41 41 …

crash point       real kill -9 (both layouts)   power loss (all states)
during_write      A                             A×6  corrupt×0
after_validation  A                             A×2  corrupt×0
after_replace     B                             A×2 B×1  corrupt×0
```

**What to say.** The temp file is never the committed state, even when it is complete and passes
its checksum (`after_validation`). Only `rename(2)` commits: it atomically switches which inode the
name points to. With a *process* crash the kernel survives, so a done rename is a done rename (→ B).
Under *power loss* (simulator, every allowed disk state) `after_replace` may give A **or** B because
the directory was not fsynced yet. Both are correct: `put()` had not returned, so nothing was promised.

**Assumptions.** Process crash = the kernel and its page cache survive. Journaled metadata (ext4,
XFS, APFS, NTFS).

---

## C2 · Invalid destination refusal (Medium)

> *Supply an invalid destination plus a valid-looking uncommitted temp. Recover without promoting
> the temp or destroying forensic content. Truncated DEST header; valid B temp record; no retained
> backup. Focus: separate integrity from commitment.*

**What we built.** Recovery now checks **integrity first**, then decides what to do with temps:

* destination **intact** (or absent) → leftover temps are deleted as before (they were never committed);
* destination **damaged** → its temps are **neither deleted nor promoted**: they are moved, byte for
  byte, with `rename(2)` to `quarantine/` (never overwriting an earlier one); the damaged file stays
  exactly where and as it is; reads of it raise `IntegrityError`; `verify` reports it;
* a later save over the damaged document first moves the damaged bytes to
  `quarantine/<name>.damaged`, so **nothing that was ever on disk is destroyed** until a human
  deletes `quarantine/`.

**What you will see** (`challenge c2`):

```
disk before recovery:
  docs/doc              10 B  INVALID (file is 10 bytes, shorter than the 24-byte header)  raw sha256 0ca200c5fdb5
  docs/doc~9.tmp        88 B  valid, payload B (64 × 0x42)                                 raw sha256 d723402cbab0
  ! recovery: kept doc~9.tmp: an uncommitted candidate for damaged doc. It passes its checksum, but it
    was never renamed, so it was never committed and is NOT promoted. Moved to quarantine/doc~9.tmp for forensics
  ✗ recovery: doc: committed document failed validation (…); reads of it are refused, its bytes are left exactly as found
disk after recovery:
  docs/doc              10 B  INVALID (…)                          raw sha256 0ca200c5fdb5   ← identical
  quarantine/doc~9.tmp  88 B  valid, payload B (64 × 0x42)         raw sha256 d723402cbab0   ← identical
```

**What to say.** A checksum proves bytes are **intact**; it can never prove they were
**committed**. Commitment is decided only by the commit point (the rename), and this temp never got
there. It could be a save that failed (C3/C4), was cancelled, lost a conflict (C5), or is *older*
than the damaged version. Promoting it would invent a version nobody committed. So we refuse,
report, and keep every byte for a human or a forensic tool.

**Assumptions.** Damage after commit (bit rot, a bad sector, an outside tool) is outside the
protocol's control: it is detected and reported, never "repaired" by guessing.

---

## C3 · Space / size constraint (Medium)

> *Declare a 1 MiB payload cap and simulate storage exhaustion while writing temp. Retain A and give
> an actionable error. B size 1 MiB; inject ENOSPC halfway through temp via a controlled adapter.
> Never fill the host disk.*

**What we built.**

* **Declared cap:** `MAX_PAYLOAD = 1 MiB` (1,048,576 bytes), a shadow-engine option `max_payload`,
  checked **before anything is written**, for every document of a commit. Over the cap →
  `PayloadTooLarge` (`errno EFBIG`).
* **Controlled adapter:** `FaultyVFS(inner, free_bytes=N)` models a device with N bytes left. A
  write that does not fit writes what fits (a short write, as the kernel does) and raises `ENOSPC`;
  unlink/truncate/rename-over give space back. Every allocation and release goes to a resource trace.
* **On ENOSPC** the engine removes the partial temp (returning its space), leaves A untouched,
  keeps the store writable, and raises `CommitAborted` with the numbers.

**What you will see** (`challenge c3`):

```
resource trace (controlled adapter):
  enospc  pwrite   docs/doc~2.tmp   wanted 1,048,600 bytes at offset 0; 524,300 fit, then the device was full  (free now 0)
  free    unlink   docs/doc~2.tmp   released 524,300 bytes  (free now 524,300)
the error the caller gets:
  save of 'doc' aborted before its commit point: No space left on device while writing the temporary copy (docs/doc~2.tmp).
  Nothing was committed: 'doc' still reads as its previous version, and the temporary file was removed.
  Wrote 524,300 of 1,048,600 bytes before the device filled up.
  This save needs 1,048,600 bytes (1.00 MiB) free in the store's directory, because the new version is
  written in full beside the old one before replacing it.
  Free at least 524,300 more bytes and retry.
✓ A retained, exact bytes   ✓ temp removed   ✓ store still writable   ✓ retry commits B after freeing space
```

**What to say.** Storage allocation happens on `write(2)`; when the file system cannot allocate
blocks, `write` returns short and then fails with `ENOSPC`. Because the shadow protocol writes the
complete new copy *beside* the old one and touches the old one only at the rename, running out of
space can only ever lose the *new* copy, never A. The error says what happened, what state the
document is in, how much space is needed and why, and what to do.

**Assumptions.** The adapter reproduces the kernel's behaviour on a full device (short write, then
`ENOSPC`); the cap applies per document.

---

## C4 · Post-rename flush error (Hard)

> *Inject directory-fsync failure after replacement. Correct the error contract to distinguish
> already-visible B from precommit abort. Valid A; validated B candidate; replace succeeds; simulated
> directory fsync raises EIO.*

**Before:** any `OSError` became `StoreError("write failed, store is now read-only until reopened")`.
The caller could not tell whether the document was still A or already B, and would typically tell
the user "not saved" while every reader already saw B.

**Now:** a failure after the rename raises **`VisibleNotDurable`** (`committed=True`,
`visible="new"`, `phase="fsync-dir"`, `errno=EIO`). A failure before it raises **`CommitAborted`**
(`committed=False`, `visible="previous"`). If `rename` itself fails we check whether it took effect
before choosing (temp still there or `ENOENT` → aborted).

**What you will see** (`challenge c4`):

```
protocol trace:
  [ 1] creat(docs/doc~2.tmp)
  [ 2] pwrite(docs/doc~2.tmp, off=0, len=88)
  [ 3] fsync(docs/doc~2.tmp)
  [ 4] rename(docs/doc~2.tmp -> docs/doc)
        ── COMMIT POINT: rename done, B visible ──
  [ 5] fsync(dir docs)  ✗ EIO (injected by the adapter)
now:  VisibleNotDurable(committed=True, visible='new', phase='fsync-dir', errno=EIO)
✓ readers already see B   ✓ further writes refused until reopen   ✓ reopen → B; saving again → durable ack
contrast: EIO on fsync(temp) → CommitAborted(committed=False, visible='previous'): readers see A, store stays writable
power loss right after the failed fsync(dir) (simulator, every disk state): A×2 B×1  corrupt×0
```

**What to say.** `rename(2)` changes the directory in memory immediately, so every reader sees B at
once; only `fsync(dir)` forces that change into the journal. When that fsync fails, B is visible
but not durable: after a power cut either version can come back, each intact (the simulator shows
exactly that). Retrying the fsync is unsafe because Linux may report a writeback error once and
then forget it (PostgreSQL's 2018 "fsyncgate"), so the store goes read-only until it is reopened,
and the caller saves again to get a real durable acknowledgement.

**Assumptions.** The EIO is reported once; recovery on reopen re-validates; a new full save
re-establishes durability.

---

## C5 · Concurrent writers extension (Hard)

> *Extend the declared single-writer design to two controlled writers with a lock and unique
> temporary files. Keep recovery separate from live writes. Two writers B/C, same destination;
> barrier after candidate preparation; local declared filesystem; serialize commit and recovery.*

**The two ways the single-writer design breaks** (both reproduced live by `challenge c5`):

1. **Shared temp name.** B prepares `doc~.tmp`; C prepares the same name and `O_TRUNC` wipes B's
   candidate. B's rename "succeeds" but publishes **C's** bytes; C's rename fails with `ENOENT`
   although its bytes *are* the document. Both writers are told the wrong thing.
2. **Recovery during a live save.** In single-writer mode, opening a store sweeps every temp. A
   second opener deletes B's in-flight temp, and B's save is lost.

**The extension** (`SafeStore.open(path, multi_writer=True)`, shadow engine):

| Mechanism | OS primitive | Purpose |
|---|---|---|
| unique temp names `doc~w<pid>-<random>-<n>.tmp` | file names | writers can never touch each other's candidate |
| session lock `LOCK`: **shared** for writers, **exclusive** for a sweeping recovery and for single-writer sessions | `flock(2)` / `LockFileEx` | recovery can never run while a save is in flight; writers coexist |
| `COMMIT` lock, exclusive, around `rename` + `fsync(dir)` | `flock(2)` / `LockFileEx` | commits are serialized and acknowledged in commit order |
| recovery when other writers are live: **non-destructive** (temps left alone) | - | an opener never deletes a live save |
| orphans of a dead writer: swept by the next session that finds itself alone | kernel releases a dead holder's locks | no stale lock, no lost garbage |
| `put(key, data, expect=sha256_of_base)` checked under the COMMIT lock; `update(fn)` retries on conflict | - | lost updates are detected instead of silently overwritten |

**What you will see** (two writer processes and a recovery process, one clock):

```
+  96.9 ms  writer C │ opened the store (session lock SHARED, writer id w2918-ce25)
+  99.3 ms  writer C │ prepared doc~w2918-ce25-2.tmp: written, fsynced, validated (invisible)
+ 102.6 ms  writer B │ prepared doc~w2917-7b14-3.tmp: written, fsynced, validated (invisible)
+ 191.1 ms  recovery │ wants the session lock EXCLUSIVELY (a sweeping recovery)
+ 392.8 ms  writer C │ waits for the COMMIT lock               ← barrier released: both race to commit
+ 393.2 ms  writer B │ holds the COMMIT lock (waited 0.2 ms)
+ 394.4 ms  writer B │ rename(doc~w2917-7b14-3.tmp → doc)  ★ commit point
+ 395.0 ms  writer B │ fsync(dir docs): the rename is durable
+ 395.2 ms  writer B │ releases the COMMIT lock
+ 395.3 ms  writer C │ holds the COMMIT lock (waited 2.4 ms)
+ 396.6 ms  writer C │ rename(doc~w2918-ce25-2.tmp → doc)  ★ commit point
+ 397.7 ms  writer C │ closes the store (releases its session lock)
+ 403.4 ms  recovery │ got it: no writer is live any more; recovery runs
+ 403.8 ms  recovery │ reads doc = 64 × 0x43
✓ commit sections never overlap   ✓ both saves returned normally   ✓ doc = exactly the later committer's version
✓ the exclusive recovery waited until both writers had closed   ✓ it found nothing to sweep
```

A second round repeats the race with **conditional saves** (`expect` = sha256 of A): the first
committer wins, the second gets `WriteConflict` (its temp removed, nothing committed), so neither
update is lost silently. Mutual exclusion is also proven by a witness: inside the COMMIT lock each
writer creates a marker file with `O_EXCL`, which would fail if two were ever inside.

**What to say.** Two locks, two jobs. The session lock is a readers-writer lock between *live
writers* (shared) and *recovery* (exclusive): it guarantees recovery only ever sees temps whose
owners are dead. The COMMIT lock is a mutex around the commit point, so check, rename and fsync
happen as one step, and acknowledgements follow commit order. Both are kernel locks, so a writer
killed with `kill -9` releases them automatically. No stale lock files, no timeouts, no guessing.

**Assumptions.** A local file system (`flock` is unreliable over NFS). The shadow engine (undo,
wal and cow keep per-process state in memory, so they stay single-writer). Whole-document
replacement is last-writer-wins unless `expect` is used.

---

## Code map

| File | What changed |
|---|---|
| `safewrite/errors.py` | **new**: `CommitAborted`, `PayloadTooLarge`, `WriteConflict`, `VisibleNotDurable` |
| `safewrite/engines/shadow.py` | `prepare` / `publish` split, crash points, 1 MiB cap, temp layouts, quarantine recovery, commit lock, `expect` |
| `safewrite/vfs/faulty.py` | **new**: the controlled fault adapter (free-space budget, injected errors, resource trace) |
| `safewrite/concurrency.py` | `FileLock` (shared/exclusive, `flock` / `LockFileEx`), `ProcessLock` on top of it |
| `safewrite/store.py` | error contract, `put(expect=)`, multi-writer `open`, `wait=`, optimistic `update()` |
| `safewrite/cli.py` | `crash --phase during_write\|after_validation\|after_replace`, `challenge` command |
| `safewrite/challenges.py` | **new**: the five narrated demonstrations |
| `tests/test_challenges.py` | **new**: 24 tests |

## Likely questions

**Why not promote a valid temp when the destination is broken? It's the only good copy!** A good
copy and a committed copy are different things. We keep it (in `quarantine/`) and tell a human; we
do not silently decide it was committed. Guessing wrong would resurrect a save that was cancelled,
failed, or lost a conflict.

**After `VisibleNotDurable`, why not just call fsync again?** On Linux a writeback error can be
reported once and then cleared, so a second fsync may "succeed" without the data being safe. A
fresh save through the full protocol is the only honest way to get a durable acknowledgement.

**Why does a 1 MiB document need 1 MiB of free space even though it replaces a 1 MiB file?** The old
version must stay intact until the rename, so both exist at the same time. That is the price of
atomicity without a journal; the error message says so.

**What if a writer dies holding the COMMIT lock?** The kernel releases `flock`/`LockFileEx` locks
when the process dies, `kill -9` included. Its temp becomes an orphan that the next session to
find itself alone sweeps away.

**Why can't undo/wal/cow have multiple writers too?** They keep per-process state in memory (log
position, catalog, block allocator). Sharing that across processes needs shared memory or a lock
manager; that is listed as future work. The shadow engine's only shared state is the file system
itself.
