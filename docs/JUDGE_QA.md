# Judge Q&A: prepared answers

Short answers first. Every claim points at something we can show live. **Bold = say this first.**

---

### The basics

**1. Isn't `rename` already atomic? Why do you need anything else?**
**`rename` makes the *name* switch atomically. It does not make the *data* durable.** Without
`fsync(tmp)` first, the disk can persist the rename before the file's contents. After a power cut the
name points at an empty file, and the old version is gone too. Show: Explorer → `shadow/no-fsync` →
`DETECTED: file is 0 bytes`. This was ext4's well-known zero-length-file problem in 2009.

**2. Why fsync the directory?**
**A file's directory entry is separate metadata.** `fsync(file)` persists the file's bytes, not the
name that points to it. Skip `fsync(dir)` and `put()` can return success, then a power cut brings back
the old version. Show: `shadow/no-dirsync` → `LOST: commit was acknowledged, but recovery returned the
previous version`.

**3. A valid checksum means the data is fine, right?**
**A checksum proves *intact*, never *committed*.** A fully written, valid temp file was still never
renamed, so it was never committed, and promoting it would break the rules. Commit is decided only by
each engine's commit point. Checksums detect torn data, and the commit protocol decides which version
is current.

**4. What's the difference between a process crash and a power failure?**
**After `kill -9` the kernel page cache survives, so everything written is still there. After a
power cut, only what was fsynced is guaranteed, in any order, possibly torn.** That's why we have two
crash models, and why every missing-fsync bug passes `kill -9` tests (`python -m safewrite demo`, step 4).

**5. Why not just use SQLite / a database?**
**SQLite *is* one of these techniques (a rollback journal, our `undo` engine, or a WAL).** The problem
asks us to design and justify the mechanism, not to import one. We built all of them, measured them,
and broke each on purpose.

### The proof

**6. How do you know you tested every crash?**
**For each system call of a save, we enumerate every disk state the POSIX persistence rules allow:**
any subset of unflushed writes, each of them possibly torn (prefix, suffix, missing middle sector, or
zeros), with metadata in journal order. All 195 matrix cells are exhaustive within the per-point
budget. In total that is 63,673 states, plus 19,318 crashes injected *into recovery itself*.

**7. Isn't your simulator just your own assumptions? How is it realistic?**
**It encodes the guarantees POSIX actually gives, which are weaker than what ext4 usually does, so
it is a superset of real behaviour.** It is corroborated three ways: (a) simulated crash images are
written to a real directory and recovered with real syscalls, with identical results
(`test_real_disk_replay`); (b) real `os._exit` at every syscall; (c) real `kill -9` torture. The
methodology follows published research: ALICE (OSDI'14) and CrashMonkey (OSDI'18).

**8. Couldn't your tests pass because the checker is weak?**
**That's exactly what the ablations rule out.** We removed nine individual safeguards, and the same
checker finds counter-examples for all nine. A checker that can't catch bugs couldn't do that.

**9. What does "63,673 states" mean? Is the number just big for show?**
**Each state is one distinct possible post-crash disk, rebuilt and recovered.** The number that
matters is *0 violations* for the safe engines next to *non-zero* for every ablation.

### Design choices

**10. Why copy-on-write as the default instead of WAL?**
**Measured: COW recovery is ~1 ms whether 100 or 5,000 commits are pending, while WAL replay grows
from 23 to 77 ms.** COW also gives version history and lock-free readers for free, and "old or new"
follows from never overwriting live blocks. WAL wins raw small-commit throughput (1 fsync vs 2,
shared by group commit), so for a write-heavy log-like workload we'd pick WAL. It is a one-word switch.

**11. Undo vs. redo: what's the difference?**
**Undo saves the *old* bytes, so recovery rolls *back* to the previous version. Redo saves the *new*
bytes, so recovery rolls *forward* to the new version.** Those are the two outcomes the problem
statement names. In the Lab, the same mid-overwrite crash recovers A under undo and B under wal.

**12. What if the superblock write itself is torn?**
**There are two slots, and each has its own CRC.** We always write the *inactive* slot. A torn write
fails its CRC, so recovery uses the other slot, whose blocks were protected from reuse. `cow/one-super`
shows what happens with one slot: `UNRECOVERABLE`.

**13. Do you assume 512-byte sector writes are atomic?**
**The `power` model does (most drives guarantee it). The `paranoid` model does not; it tears at 16
bytes, and all four safe engines still pass** because every header carries its own CRC plus a
fallback.

**14. How do you make two documents change together?**
**One commit point covers the whole transaction:** one journal (undo), one COMMIT record (wal), or one
superblock (cow). Temp + rename cannot do it: the Explorer shows `shadow` leaving `alice=new,
bob=old` in 14 of 30 crash states. That's why shadow is not our default.

**15. What happens if the crash happens during recovery?**
**Recovery is idempotent and crash-safe, and we test it by crashing recovery itself.** Undo keeps
the journal active until the rollback is durable, and wal keeps the log until the homes are durable,
so a second recovery redoes the same work. COW recovery never writes at all.

**16. How does the WAL know where valid data ends?**
**Every record carries a CRC, a sequence number (LSN) and a salt.** The scan stops at the first
record that is torn (CRC), out of sequence (LSN), or from an older log generation (salt). Without
CRCs (`wal/no-checksum`), a record whose middle sector never reached the disk is replayed as garbage.

### Operating-systems concepts

**17. Where is "synchronization" in your project?**
**Four places:**
1. `flock` between processes. The kernel drops it if the holder is killed, so there are no stale locks.
2. A transaction lock for read-modify-write. The `race` demo shows **50/400** increments survive
   without it and **400/400** with it.
3. WAL **group commit**: a leader writes everyone's records and does one fsync while followers wait on
   a condition variable, giving about **6.9 commits per fsync at 16 threads**.
4. COW **MVCC**: readers pin a generation and never block.

Plus `fsync` as an ordering barrier with the disk.

**18. What if two programs open the store at once?**
**The second gets "store is in use by another process (pid N)".** The lock is a kernel `flock`, so
if the first process is `kill -9`ed the lock disappears with it (tested).

**19. How does garbage collection avoid freeing something still in use?**
**Mark and sweep from every *retained* generation (the last K ≥ 2) and every *pinned* snapshot.**
K ≥ 2 guarantees the previous generation survives while the next one is being published.

**20. How is this different from ext4's own journal?**
**ext4's journal keeps the *file system's* metadata consistent. It doesn't know that your 3 writes,
truncate and rename form one logical save.** A journaled FS can come back perfectly consistent and
still contain a half-overwritten document (that's `naive`). We provide application-level atomicity
on top.

**21. What happens if the disk is full or fsync fails?**
**The commit raises, nothing is published, and the store goes read-only until reopened.** After a
failed fsync the kernel may already have dropped the dirty pages, so retrying is unsafe. That was
PostgreSQL's 2018 "fsyncgate". Reopening runs recovery, which yields A or B. We inject ENOSPC/EIO at
every syscall in tests.

**22. What about Windows?**
**CI runs every test on Windows.** NTFS can't fsync a directory (it journals its own metadata), so
that call is a no-op there. Locking uses `msvcrt`, kills use `TerminateProcess`, and files are opened
in binary mode.

### Honesty

**23. What can't you handle?**
**A drive that lies about flushing its cache:** no software can. Other limits: one writer process at
a time; non-journaled file systems such as FAT may reorder metadata; the COW catalog is O(#docs) per
commit, which suits thousands of documents, not millions.

**24. Why Python? Isn't that far from the OS?**
**Every operation is a direct system call** (`os.pwrite`, `os.fsync`, `os.replace`, `fcntl.flock`),
which you can watch live with `strace`. Python made it possible to build five engines, a simulator,
a UI and a test suite. And because the cost of crash safety is dominated by `fsync`, language speed
barely moves the comparisons.

**25. What changed since round 1?**
**Round 1's temp + rename design is still here as the `shadow` engine.** We added: copy-on-write and
both journaling directions as real code; real synchronization; and power-loss verification of every
crash state instead of three hand-picked kill points. The power-loss model showed that round 1's
testing method could not have caught a missing fsync.

**26. With more time?**
Replay real kernel block I/O in a VM (dm-log-writes), a COW B+-tree for millions of keys, a hybrid
COW + WAL (like ZFS's ZIL), and an "audit any program" mode that runs strace traces through our simulator.

### Crash Arena (the audience crashes it live)

**27. Is the naive side rigged?**
**No. Both sides run the same writer program, get killed by the same tap, after the same random
delay, back to back.** The only difference is the engine: `naive/trunc` does what
`open(path, "w")` does, and `cow` is SafeWrite. Who dies first alternates on every tap, and the
same `_judge` code from the torture test classifies both files. Each crash record (`/api/arena/export`)
shows the delay, the kill order and the judge's verdict. The test suite checks that every crashed
tap killed exactly one writer per side. Naive *survives* about 40% of kills (`OLD`/`NEW`), and the
projector shows that too.

**28. Is a phone tap really a `kill -9`?**
**Yes. An accepted tap ends with `SIGKILL` (`TerminateProcess` on Windows) on two real writer
processes that are in the middle of their save loop.** Then this laptop opens each store, which
runs real recovery, and judges the bytes. Nothing is counted until that verdict exists. When the
room taps faster than the laptop can kill (~25 pairs a second on 4 cores), the extra taps are shown
as "dropped", never as crashes. "Taps received" and "real crashes" sit side by side on the projector.

**29. Why doesn't `kill -9` test durability?**
**Because the kernel page cache survives a killed process.** Every `write()` that returned is still
there, so a missing `fsync` can never show up. That is why we built the power-loss simulator,
where unflushed writes may be lost, reordered or torn. The arena proves *atomicity* on a real file
system; the simulator proves *durability*. The summary slide says exactly that.

**30. What if two taps arrive at once?**
**Each becomes its own pair of crashes, on its own pair of writers.** Taps go into a bounded fair
queue (round-robin across phones, at most 3 waiting per phone), and several crash lanes take them
in parallel. Each lane holds its own writers, and each writer has its own store directory and
process lock, so two crashes never touch the same file. If both taps arrive while every writer is
busy, they wait or are dropped, and both are still counted.

---

### Numbers to remember

| | |
|---|---|
| crash states explored | **63,673** (19,318 inside recovery) |
| safe-engine violations | **0** |
| ablations caught | **9 / 9** |
| COW recovery | **~1 ms**, flat |
| WAL replay | **23 → 77 ms** (100 → 5,000 commits) |
| WAL group commit | **6.9** commits per fsync at 16 threads |
| undo write amplification | **2.19×** |
| race | **50 / 400** vs **400 / 400** |
| Crash Arena (30 phones × 60 s) | **1,547** crash pairs, naive/trunc **61%** corrupted, cow **0**; ~25 pairs/s on 4 cores |
| tests | **70**, CI on Linux / macOS / Windows |
