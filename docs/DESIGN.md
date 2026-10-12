# SafeWrite: Design Document (final round)

**Problem OSH021, Crash-Consistent Document Storage.** Team VAT: Tanush Keshri, Vyom Singh, Abhishek Kumar Maurya.

> *The prototype must demonstrate failure during a write and recover to either the previous valid
> version or the new valid version, not a corrupted mixture. Teams may use temporary files, logging,
> copy-on-write or journaling-inspired techniques and must justify the chosen approach.*

This document covers the seven required deliverables in order:

1. [Problem analysis](#1-problem-analysis)
2. [System architecture and data flow](#2-system-architecture-and-data-flow)
3. [Mapping of OS concepts to the design](#3-mapping-of-os-concepts-to-the-design)
4. [Key algorithms and data structures](#4-key-algorithms-and-data-structures)
5. [Walkthrough of a realistic scenario, with edge cases](#5-walkthrough-of-a-realistic-scenario)
6. [Trade-offs, limitations and future work](#6-trade-offs-limitations-and-future-work)
7. [Working prototype and verification](#7-working-prototype-and-verification)

**Summary.** We built a document store with one API and four interchangeable crash-recovery engines:
temp file + rename, undo journal, redo write-ahead log, and copy-on-write shadow paging. A fifth
engine is a deliberately unsafe baseline. We also built a simulator that, for every system call of a
save, enumerates every disk state a crash could leave and runs each engine's *real* recovery code on it.
Across **63,673 crash states** the four safe engines produce **0 violations**. Every one of **9
deliberately removed safeguards** produces concrete counter-examples, which is how we justify each
line of each protocol. Real `kill -9` tests on real files agree. Copy-on-write is the default engine
because the measurements favour it for documents: O(1) recovery, history for free, lock-free readers.

---

## 1. Problem analysis

### 1.1 Anatomy of a save

"Saving a document" looks like one action to the user. To the operating system it is a sequence of
independent system calls:

```
open("report.csv", O_WRONLY|O_TRUNC)   ← version A is destroyed right here
write(fd, chunk1)                      ← lands in the page cache (RAM), not on disk
write(fd, chunk2)
write(fd, chunk3)
close(fd)                              ← still nothing is guaranteed to be on disk
```

Three layers can reorder or drop work:

| Layer | Holds | Lost on |
|---|---|---|
| Process buffers (stdio, Python `io`) | bytes not yet passed to `write(2)` | process crash |
| Kernel page cache | written but not yet flushed pages | power loss / kernel panic |
| Drive volatile cache | data the drive acknowledged but has not persisted | power loss (unless FLUSH/FUA) |

The kernel and the drive may write back dirty pages in **any order**, and a multi-sector write can
land **partially** (a *torn write*). Only `fsync(2)` creates an ordering point (a barrier). It
covers a file's data, *not* the directory entry that names the file: that needs `fsync` on the directory.

### 1.2 Two kinds of crash

| | Process crash (`kill -9`, segfault, OOM) | Power loss (outage, kernel panic, battery) |
|---|---|---|
| Page cache | **survives**: every `write()` that returned is visible | **lost**: only fsynced data is guaranteed |
| Ordering | program order | arbitrary between barriers |
| Torn writes | only the write in flight (page granularity) | any unflushed multi-sector write |
| Typical testing | easy (`kill -9`) | needs special tooling |

Our round-1 prototype only tested process crashes. One result of this project is that **every
missing-`fsync` bug passes process-crash testing** (`tests/test_crash_exhaustive.py::
test_power_loss_bugs_are_invisible_to_kill9_testing`). Testing with `kill -9` alone gives false
confidence.

### 1.3 Failure taxonomy (what "corrupted" means, precisely)

Our checker classifies every recovered store into one of these outcomes:

| Code | Meaning | Example cause |
|---|---|---|
| `OK` | every document equals a version the spec allows | correct protocol |
| `CORRUPT` | a document's bytes match **no** version: the "corrupted mixture" | in-place overwrite torn half-way |
| `DETECTED` | a checksum caught the damage, but neither version is available | rename persisted before the data |
| `LOST` | a commit the caller was told succeeded has disappeared | missing directory fsync |
| `TORN_TX` | some documents of one transaction are new, others old | non-atomic multi-file update |
| `UNRECOVERABLE` | recovery itself cannot open the store | the only superblock was torn |
| `NONIDEMPOTENT` | running recovery twice gives different answers | recovery that is not crash-safe |

### 1.4 Requirements (the invariants we verify)

* **A1 atomicity:** after any crash, each document is byte-identical to the version before the
  interrupted commit or the version after it.
* **A2 transactional atomicity:** for a multi-document commit, *all* documents are before or *all* after.
* **D durability:** once `put()`/`commit()` returns, the new version survives any crash.
* **R recovery:** recovery runs automatically on every open, is idempotent, and is itself crash-safe.
* **I integrity:** damaged bytes are detected, never silently returned.
* **C concurrency:** concurrent writers cannot lose updates, and a second process cannot corrupt the store.

### 1.5 Risk and impact

The danger is *silent* corruption. A torn inventory file still parses as CSV, just with wrong
quantities, and nothing tells the user. Real systems have hit exactly these bugs: ext4's 2009
zero-length-file problem (a rename reached the disk before the data did), PostgreSQL's 2018
"fsyncgate" (retrying a failed `fsync` silently lost data), and the OSDI'14 ALICE study, which found
dozens of crash vulnerabilities across 11 widely used applications such as Git, Mercurial, LevelDB and
HDFS.

---

## 2. System architecture and data flow

### 2.1 Layers

```
               ┌───────────────────────────────────────────────────────────┐
  CLI / Web UI │ SafeStore: open → flock → recover · put · get · tx · history │   store.py
               └──────────────────────────────┬────────────────────────────┘
     ┌────────────┬────────────┬──────────────┼─────────────┬──────────────┐
     │   naive    │   shadow   │     undo     │     wal     │     cow      │    engines/
     └────────────┴────────────┴──────┬───────┴─────────────┴──────────────┘
              synchronization: flock · RWLock · GroupCommit · MVCC pins          concurrency.py
                                      │
                ┌─────────────────────┴──────────────────────┐
                │ VFS: open pwrite pread fsync rename unlink │                    vfs/base.py
                │      fsync_dir ftruncate · note(phase)     │
                └───┬──────────────────┬─────────────────┬───┘
               OsVFS               RecordingVFS        MemVFS                     vfs/
               real syscalls       trace + inode ids   in-memory inodes
                                   + crash injection
                                       │ trace
                ┌──────────────────────┴────────────────────────────────────┐
                │ Crash simulator: persistence model → enumerate post-crash  │  crashsim/
                │ disks → engine.open() (real recovery) → judge → provenance │
                └───────────────────────────────────────────────────────────┘
```

| Component | Responsibility |
|---|---|
| **SafeStore** (`store.py`) | Public API. On open: take the process lock, read the store marker, run recovery. Transactions, `update(fn)`, fail-stop after I/O errors. |
| **Engines** (`engines/*.py`) | Each implements `format / recover / get / commit` with its own on-disk protocol and marks its protocol phases with `vfs.note()`. |
| **VFS** (`vfs/base.py`) | The *only* way engines touch storage. Data ops act on handles (inodes), namespace ops act on names, mirroring POSIX. |
| **OsVFS** | Real system calls: `pwrite`, `fsync` (`F_FULLFSYNC` on macOS), directory fsync via `O_DIRECTORY`, `os.replace`, Windows fallbacks. |
| **RecordingVFS** | Wraps any VFS; logs every mutating op with the **inode** it touches, and can crash (raise or `os._exit`) at syscall N or at a named phase. |
| **MemVFS** | In-memory file system with real inode semantics, used to replay thousands of crash images fast. Tested equivalent to the real FS. |
| **Crash simulator** (`crashsim/`) | Computes which disk images a crash can leave, rebuilds each one, recovers it, and judges the result. |
| **Torture / race / bench** | Real-process evidence: `kill -9` loops, a lost-update race, measurements. |
| **Crash Lab** (`server/`) | A stdlib HTTP server plus a vanilla-JS UI that visualises all of the above. |

**Design rule.** Engines never call `os.*`. That one rule is what makes the same engine code runnable
on a real disk, traceable, crash-injectable, and replayable against simulated disks.

### 2.2 Data flow

**Write path (cow, the default):** `put(key, B)` → `SafeStore._commit` → `CowEngine.commit` →
allocate free blocks → `pwrite` B's bytes → `pwrite` the new catalog → `fsync` (barrier) → `pwrite`
the superblock into the inactive slot → `fsync` (**commit point**) → update the in-memory root,
retain the old generation, run GC → return (the acknowledgement).

**Read path:** `get(key)` → pin the current generation (MVCC) → look up the extent in its catalog →
`pread` → verify CRC-32 → unpin. Readers take no write lock.

**Recovery path:** `SafeStore.open` → `flock` → read `SAFEWRITE.json` → `engine.open()` → read both
superblock slots → pick the highest generation whose superblock *and* catalog verify → load the
retained history → GC. There is no replay.

**Simulation path:** scenario → `record()` (engine runs on `RecordingVFS(MemVFS)`) → trace → for
each crash point: `enumerate_states()` → `build_image()` → `recover_and_judge()` → aggregate.

### 2.3 On-disk layouts

| Engine | Files | Format |
|---|---|---|
| shadow | `docs/<key>`, `docs/<key>~N.tmp` | 24-byte header (magic, length, payload CRC, header CRC) + payload |
| undo | `undo.journal`, `docs/<key>` | journal: 512-byte header (magic, seq, nrec, body length, body CRC, header CRC) + records (key, existed, old bytes) |
| wal | `wal.log`, `data/<key>` | log: 512-byte header (checkpoint LSN, salt, CRC) + records (magic, salt, LSN, txid, type PUT/DEL/COMMIT, key, value, CRC) |
| cow | `volume.img` | block 0 / 1 = superblock slots A / B (generation, root pointer, root CRC, high-water, CRC); then data and catalog blocks |

Keys are encoded into file names: lower-case letters, digits and `._-` are kept, and every other
byte, including upper-case, is percent-encoded. That makes names safe on case-insensitive file systems,
and `~` can never appear in a key, so temp files can never collide with documents.

---

## 3. Mapping of OS concepts to the design

| Concept | Where (code) | How it works here | Why it matters during a crash | Evidence |
|---|---|---|---|---|
| **File systems** | `vfs/osfs.py`, `vfs/memfs.py`, `crashsim/model.py`, `engines/cow.py` | Names vs. inodes: a handle keeps writing to its inode across `rename`; `fsync(file)` persists data and size, `fsync(dir)` persists names. The cow engine is a small file system inside a file: superblocks, block allocator, catalog, free-space GC. | "The rename reached the disk but the data didn't" is only expressible if you model inodes. That is the zero-length-file bug. | `shadow/no-fsync` reproduces it; MemVFS ≡ ext4 equivalence test |
| **Atomicity** | each engine's commit point | One indivisible event decides old vs. new: `rename(2)`; undo-journal deactivation (single-sector write + CRC); a CRC-framed COMMIT record; a superblock flip into the *other* slot. | Before the event, recovery yields A; after it, B. The event cannot be half-done because each is either atomic in the FS (rename) or self-validating (CRC) with a fallback. | 0 violations in 10,744 crash states |
| **Synchronization** | `concurrency.py`, `store.py`, `wal.py`, `cow.py` | **Mutual exclusion:** `flock` between processes (the kernel releases it on `kill -9`, so no stale locks), a writer-preferring RWLock for in-place engines, a transaction lock for read-modify-write. **Coordination:** WAL group commit (leader/follower on a condition variable). **Lock-free reads:** COW MVCC snapshot pins. **Storage ordering:** `fsync` barriers. | Without exclusion, two writers interleave; without barriers, the disk reorders. Both produce states no single history explains. | race demo (50/400 → 400/400); 16 threads ≈ 6.9 commits per fsync; second process refused; lock released after kill |
| **Recovery** | `Engine.recover()` in every engine, `SafeStore.open` | Runs on *every* open. shadow: delete uncommitted temps. undo: roll back an active journal. wal: redo committed transactions, stop at the first torn / out-of-sequence / stale record. cow: choose the newest valid superblock. | A crash can happen anywhere, including during recovery, so recovery must be idempotent and crash-safe. | idempotence checked on every state; 19,318 crashes *during* recovery explored |
| **Copy-on-write** | `engines/cow.py` | Never overwrite a block a committed generation uses. Write new blocks and a new catalog elsewhere, barrier, flip one of two superblocks. Superblock → catalog → documents form a Merkle checksum chain. GC frees blocks no retained or pinned generation reaches. | The old version stays intact until the flip, and the flip is a single checksummed sector with a fallback slot. | O(1) recovery (~1 ms flat); `cow/no-barrier` and `cow/one-super` counter-examples |
| **Journaling** | `engines/undo.py` (undo), `engines/wal.py` (redo) | **Undo:** journal the *old* bytes, fsync, overwrite in place; recovery rolls **back**. **Redo:** journal the *new* bytes with a COMMIT record, fsync once; recovery rolls **forward**; checkpoints copy the log into home files and reset it. | These are exactly the two outcomes the problem allows: the previous valid version (undo) or the new valid version (redo). | per-crash-point tables in §5.2 |

---

## 4. Key algorithms and data structures

Notation: `W` = `pwrite`, `F` = `fsync`, `FD` = `fsync(dir)`. Lines marked ★ are commit points.

### 4.1 Shadow file + rename (`engines/shadow.py`)

```
put(key, B):
    tmp ← docs/<key>~<n>.tmp                    # unique: concurrent writers never share a temp
    W(tmp, header(len, crc(B)) ‖ B)
    F(tmp)                                      # B durable BEFORE it can become visible
    verify read-back(tmp) == record             # catches I/O errors before commit
    rename(tmp → docs/<key>)                    # ★ atomic name switch
    FD(docs)                                    # the rename itself is durable → acknowledge

recover():
    for every *~*.tmp: unlink                   # never renamed ⇒ never committed
    FD(docs) if anything was removed
    validate every document (length + CRC)
```

### 4.2 Undo journal (`engines/undo.py`)

```
commit(changes):
    body ← for each key: (key, existed?, old file bytes)
    W(journal, 512, body);  W(journal, 0, header(seq, nrec, len(body), crc(body)))
    F(journal)                                  # undo information durable first
    for each key: W(doc, new record); ftruncate; F(doc)    # in place: may tear, that's fine
    FD(docs) if files were created/removed
    W(journal, 0, header(seq, nrec=0)); F(journal)         # ★ deactivate
recover():
    header torn or body CRC wrong  → the journal never became durable ⇒ documents untouched ⇒ deactivate
    header valid, nrec = 0          → nothing to do
    header valid, body valid        → restore every old file (or delete if it did not exist), F, deactivate
```

A torn *header* is safe in both places it is written. In step 1, the documents are not yet touched.
In the final step, the documents are already durable as B. Both cases map to "deactivate", which yields A
or B respectively.

### 4.3 Redo write-ahead log (`engines/wal.py`)

```
commit(changes):                                # many threads may call this at once
    GroupCommit.submit(changes)
      ├─ leader: take every queued tx; assign LSNs; build PUT/DEL…COMMIT records (CRC each)
      │          W(log, tail, all records); F(log)          # ★ one fsync for the whole batch
      │          apply batch to the in-memory table (LSN order); wake followers
      └─ follower: wait on the condition variable until its tx is durable
    if commits since checkpoint ≥ threshold: checkpoint()

checkpoint():                                   # exclusive
    for each dirty key: W(home, record) in place; F(home)   # may tear: the log still has it
    FD(data) if names changed
    W(log, 0, header(ckpt_lsn = last LSN, salt+1)); F(log)  # only now may the log forget
    ftruncate(log, 512)

recover():
    header torn → it is only written after every home is durable ⇒ reset the log
    scan from offset 512: stop at bad magic | wrong salt | LSN ≠ expected | short | CRC mismatch
    redo every tx that has a COMMIT record (full-value writes ⇒ idempotent); ignore the rest
    checkpoint()
```

The **salt** changes at every checkpoint, so records left over from an older generation of the log
can never be mistaken for new ones, even if a truncate did not persist.

### 4.4 Copy-on-write shadow paging (`engines/cow.py`)

```
commit(changes):                                         # one writer at a time
    for each (key, value): run ← alloc(⌈len/bs⌉);  W(volume, run, value)
    catalog ← {key: (run, len, crc, version)} + prev-root pointer;  W(volume, alloc(…), catalog)
    F(volume)                                            # barrier: tree durable before it is published
    W(volume, slot(1 − active), super(gen+1, root, crc(catalog), high-water) ‖ crc)
    F(volume)                                            # ★ published
    retained ← [new] + retained[:K−1];  gc()

recover():
    candidates ← slots whose superblock CRC verifies, newest generation first
    pick the first whose catalog CRC also verifies       # torn newest slot ⇒ fall back to the older one
    retained ← walk prev-root pointers (K generations, CRC-checked);  gc()

alloc(n):  first-fit run of n contiguous free blocks, else grow the high-water mark
gc():      free ← all blocks − blocks reachable from retained generations − blocks of pinned snapshots
snapshot(): pin(current root) … read … unpin            # MVCC: GC never frees a pinned block
```

**Safety argument.** The blocks of generation *g* are never reused while *g* is retained, and
`K ≥ 2` keeps *g* retained while *g+1* is being written. So if the flip to *g+1* tears, slot *g*
is still valid and its blocks are intact.

### 4.5 Crash-state enumeration (`crashsim/model.py`)

```
enumerate_states(trace, base, point, model):
    if model = process:                                  # kill -9: page cache survives
        return {all issued ops} ∪ {in-flight write cut at page boundaries}
    forced_data ← writes/truncates followed by fsync of the same inode (before point)
    forced_ns   ← namespace prefix covered by an fsync of the containing directory
    unforced    ← data ops not in forced_data
    for ns_len in [forced_ns .. issued namespace ops]:         # metadata persists in order
        for subset S ⊆ unforced (all 2^n, or a structured sample):   # lost / kept / reordered
            yield state(ns_len, forced_data ∪ S)
            for w in S, for t in tears(w):                   # prefix | suffix | hole | zero-fill,
                yield state(ns_len, forced_data ∪ S, tear = t)    # cut where the bytes actually change
```

`build_image` replays the base image plus the persisted ops **in issue order**, per inode. That
reproduces the classic "rename persisted, data didn't" state, because the dir entry points at an
inode whose data writes were dropped.

### 4.6 Judging and provenance (`crashsim/explorer.py`, `provenance.py`)

```
judge(recovered, states S0..Sn, acked a, started b):
    checksum error on read      → DETECTED
    unexpected document         → CORRUPT (phantom)
    recovered = S_j, a ≤ j ≤ b  → OK
    recovered = S_j, j < a      → LOST
    every doc ∈ some S_j        → TORN_TX
    else                        → CORRUPT, described byte by byte ("bytes 0-1024 from the new version, …")
then: recover again → must be identical (idempotence)
then: record recovery's own syscalls, crash it at each point, recover again, judge again
```

### 4.7 Data structures

| Structure | Where | Purpose |
|---|---|---|
| `Op` (kind, path, inode, offset, data, …) | `vfs/recording.py` | one recorded syscall; the simulator's input |
| `BaseImage` (names→inode, inode→bytes) | `vfs/recording.py` | durable state before the workload |
| `CrashState` (point, ns_len, persisted set, tear) | `crashsim/model.py` | one possible post-crash disk |
| `Tear` (op, prefix/suffix/hole/zeros, cut) | `crashsim/model.py` | how one write was cut |
| Superblock / catalog / `Entry`(start, nblocks, len, crc, version) | `engines/cow.py` | the COW tree |
| `retained: [Root]`, `pins: {gen: (Root, count)}`, `free: [block]` | `engines/cow.py` | history, MVCC, allocation |
| LSN, salt, `mem: {key: value}` | `engines/wal.py` | log sequence, generation tag, un-checkpointed state |
| `GroupCommit` (queue, enqueued/durable counters, condition variable) | `concurrency.py` | batching durable commits |

---

## 5. Walkthrough of a realistic scenario

### 5.1 The scenario

A relief warehouse keeps `inventory.csv`. Version **A** (1,584 bytes, 60 rows) is committed. A
clerk saves version **B** (1,167 bytes, 44 rows, every quantity changed). B is shorter on purpose:
an in-place overwrite then has to *truncate*, which opens one more crash window. Power fails at an
unknown moment. These exact documents are the `update` scenario. Every number below is produced by
`python -m safewrite explore` and can be replayed state by state in the Crash Lab.

### 5.2 Every crash point, every engine (power-loss model)

**cow (default).** 5 syscalls, 2 barriers.

| Crash right after | Phase | Possible disks | Recovers |
|---|---|---|---|
| (nothing) | start | 1 | A |
| #1 `pwrite(volume.img, off=4096, len=1167)` new blocks | write-new-blocks | 8 | A (superblock still names generation *g*) |
| #2 `pwrite(volume.img, off=5632, len=107)` catalog | write-catalog | 18 | A |
| #3 `fsync` barrier | fsync-barrier | 1 | A |
| #4 `pwrite(volume.img, off=0, len=512)` superblock | flip-superblock | 2 | **A or B**: unflushed, it may or may not have landed |
| #5 `fsync` | ★ committed | 1 | **B** |
| after `put()` returned | ack | 1 | **B** (durability) |

**undo.** Rolls *back* to the previous version (8 syscalls, 3 barriers).

| Crash right after | Recovers | Recovery does |
|---|---|---|
| #1–#2 journal body/header written, not fsynced | A | header or body CRC fails → "journal never durable" → deactivate |
| #3 `fsync(journal)` | A | journal active → restore old bytes |
| #4–#6 in-place overwrite / truncate / fsync, possibly torn | **A** | roll back from the journal |
| #7 deactivate write (unflushed) | A or B | |
| #8 `fsync(journal)` ★ | **B** | nothing |

**wal.** Rolls *forward* to the new version (8 syscalls, 4 barriers).

| Crash right after | Recovers | Recovery does |
|---|---|---|
| #1 log append (unflushed) | A (7 states) or B (1 state: the full record landed) | stop at the torn / missing record |
| #2 `fsync(log)` ★ | **B**, though no home file has changed yet | **redo** from the log |
| #3–#5 home overwritten in place, possibly torn | **B** | redo repairs the torn home |
| #6–#8 log reset | B | |

**shadow.** 5 syscalls, 2 barriers + 1 directory barrier. A until the rename; the unflushed
rename gives A or B; after `fsync(dir)` always B.

### 5.3 The same crash, three outcomes

At the moment the home file is half-overwritten:

* **naive** returns `bytes 0-512 from the new version, bytes 512-1584 from the old version`:
  a corrupted mixture, returned silently as if it were valid.
* **undo** returns A: the previous valid version, restored from the journal.
* **wal** returns B: the new valid version, replayed from the log.

These are the two outcomes the problem statement allows, produced by the two journaling directions.

### 5.4 Multi-document transaction: `alice` 100 → 70, `bob` 50 → 80

* **shadow:** two renames. A crash between them leaves `alice=70, bob=50`, so 30 units vanish.
  The explorer reports `TORN_TX` in 14 of 30 power-loss crash states. That is a documented limitation:
  per-file atomic rename cannot make two files change together.
* **undo / wal / cow:** one journal, one COMMIT record, or one superblock covers both documents:
  **0** torn transactions.

### 5.5 Edge cases

| Edge case | What happens | Where verified |
|---|---|---|
| **First-ever save interrupted** | the document either does not exist or is complete (round 1 treated this as an error) | `create` scenario |
| **Delete interrupted** | fully present or fully gone | `delete` scenario |
| **Torn superblock** (no sector atomicity) | its CRC fails → fall back to the other slot's generation | `paranoid` model; `test_torn_superblock_falls_back` |
| **Single superblock slot** | torn → no valid root → `UNRECOVERABLE` (this is *why* there are two slots) | `cow/one-super` |
| **Torn WAL tail** | stop scanning at the first bad record; uncommitted records ignored | `test_torn_tail_is_ignored` |
| **WAL record with a lost middle sector** | its CRC fails → treated as uncommitted; without CRCs it is replayed as garbage | `wal/no-checksum` |
| **Crash during recovery** | recovery's own writes are crash-tested; re-recovery converges | 19,318 nested crash states |
| **Recovery run twice** | identical result (idempotent) | checked on every crash state |
| **Disk full / EIO / fsync failure** | the commit raises; nothing is published; the store refuses further writes until reopened, because retrying a failed fsync is unsafe (PostgreSQL "fsyncgate"); reopening recovers A or B | `tests/test_faults.py`: an error injected at every syscall |
| **Two processes open the same store** | the second gets `StoreError: store is in use by another process (pid N)` | `test_second_process_is_refused` |
| **Writer killed while holding the lock** | the kernel drops the `flock` with the process: no stale lock | `test_lock_is_released_when_holder_is_killed` |
| **Concurrent read-modify-write** | `update(fn)` / transactions hold the store lock from read to commit: no lost updates | `race` command; `test_transactions_are_isolated` |
| **Reader during a write** | COW readers pin a generation and read it consistently, even after 29 newer commits | `test_snapshot_isolation_survives_gc` |
| **Bit rot after commit** | `verify` reports it; with COW, earlier retained versions remain readable | `verify` / `history` |
| **Case-insensitive file systems** | key encoding escapes upper case, so `A` and `a` never collide | `test_case_insensitive_filesystems_cannot_collide` |

---

## 6. Trade-offs, limitations and future work

### 6.1 Measured trade-offs

200 durable saves of 4 KB each on the development machine (ext4, Linux). Absolute numbers depend on
the disk; the ratios are what matter. Regenerate with `python -m safewrite bench`.

| Engine | p50 latency | Durable saves/s | fsyncs / commit | Write amplification | Recovery after crash | Multi-doc atomic | History |
|---|---|---|---|---|---|---|---|
| naive | 0.25 ms | 3,448 | 1 (or 0) | 1.00× | none possible | ✗ | ✗ |
| shadow | 0.84 ms | 1,012 | 2 | 1.01× | 0.39 ms | ✗ | ✗ |
| undo | 0.65 ms | 1,433 | 3.08 | **2.19×** | 0.11 ms | ✓ | ✗ |
| wal | **0.26 ms** | 2,473 | **1.26** | 1.26× (coalesced) | **grows with the log**: 23 → 77 ms for 100 → 5,000 commits | ✓ | ✗ |
| **cow** | 0.32 ms | **2,876** | 2 | 1.31× | **~1 ms flat** | ✓ | ✓ |

* **WAL** has the cheapest commit (one sequential append and one fsync, shared by concurrent
  committers: **6.9 commits per fsync at 16 threads**) and coalesces repeated updates before the
  checkpoint. It pays at recovery, which replays the log, and in complexity (checkpoints, salts, LSNs).
* **COW** pays a second fsync per commit but recovers in constant time, never replays, keeps history
  for free, and lets readers proceed without locks. It pays in fragmentation and GC.
* **Undo** writes everything twice and needs three fsyncs, but keeps documents as plain in-place files.
* **Shadow** is the simplest and portable, but per-file only and metadata-heavy.

**Our choice: copy-on-write as the default.** For a *document* store, where saves are human-paced,
recovery should be instant, and history is a feature, COW wins on everything except raw small-write
throughput. Where that matters most (many tiny concurrent commits), WAL is the right engine, and
the store makes it a one-word change: `--engine wal`.

### 6.2 Limitations (stated honestly)

1. **Honest hardware.** Durability assumes the drive honours FLUSH. A drive that acknowledges
   writes from a volatile cache without power-loss protection defeats every software scheme,
   including ZFS and PostgreSQL. We model the file-system contract, not lying firmware.
2. **Metadata ordering.** The `power` model assumes journaled metadata persists in order (ext4,
   XFS, NTFS, APFS). Non-journaled file systems (FAT, ext2) can reorder metadata; shadow's rename
   would not be safe there, while cow and wal (single file, fsync-ordered) would.
3. **Bounded enumeration.** Crash states are exhaustive per crash point up to a budget (all 195
   matrix cells are within it today); beyond it we sample, always including every prefix,
   single-drop and single-keep subset. The scenarios are small by design: crash-consistency bugs are
   about protocol *ordering*, not data volume.
4. **One writer process.** Enforced with `flock`. Many threads inside it are fine. Multi-process
   writers would need shared-memory coordination.
5. **Scale.** The COW catalog is a single serialized object, rewritten per commit (O(#docs)), and
   GC is a full mark-and-sweep. That is right for thousands of documents, not millions.
6. **Platform.** Windows has no directory fsync (NTFS journals metadata itself); macOS needs
   `F_FULLFSYNC`, which we use. Network file systems give weaker rename/fsync guarantees.

### 6.3 Future work

* Block-level record-and-replay of real kernel I/O (Linux `dm-log-writes`, in the spirit of
  CrashMonkey's recording block device) in a VM, to test the *real* ext4/XFS rather than our model of it.
* A COW B+-tree with path copying (as LMDB and btrfs use) for O(log n) commits at millions of keys,
  plus incremental GC.
* Hybrid **COW + WAL** (ZFS ZIL style): log small commits cheaply, fold them into COW snapshots.
* Multi-process writers via a shared-memory lock table; replication for machine-loss durability.
* An *audit mode* that traces any program with `strace` and runs its syscalls through our
  simulator, i.e. ALICE for arbitrary applications.

---

## 7. Working prototype and verification

### 7.1 Running it

Python ≥ 3.9, standard library only, Linux / macOS / Windows.

```bash
python -m safewrite serve                 # Crash Lab web UI, http://127.0.0.1:8765
python -m safewrite demo                  # narrated terminal demo
python -m safewrite explore --examples 1  # the full proof matrix
python -m unittest discover -s tests -t . # 53 tests
```

### 7.2 Verification matrix

| Layer of evidence | What it proves | Result |
|---|---|---|
| Exhaustive crash exploration, 3 models × 6 scenarios × 13 variants | A1, A2, D, R on every allowed post-crash disk | **63,673** states; safe engines **0** violations |
| Crashes during recovery | R: recovery is crash-safe | 19,318 nested states, 0 violations |
| Ablations | each safeguard is necessary; the checker can catch bugs | **9/9** caught |
| Simulated images replayed on the real disk | the in-memory model matches real syscalls | `test_real_disk_replay` |
| Real `os._exit` at every syscall, every safe engine | atomicity with real processes and files | `test_os_exit_at_every_syscall_recovers_old_or_new` |
| Real `kill -9` torture | atomicity under random real kills | 0 corruptions (safe); naive/trunc ≈ 50% corrupted |
| Fault injection (ENOSPC/EIO at every syscall) | I/O errors never corrupt committed data | `tests/test_faults.py` |
| Format tests | I: every flipped byte, truncation and extension detected | `tests/test_format.py` |
| Concurrency tests | C: no lost updates, isolation, exclusive process lock, lock released on kill | `tests/test_concurrency.py` |
| CI | portability | GitHub Actions: Ubuntu, macOS, Windows × Python 3.9, 3.12 |

### 7.3 From round 1 to the final round

The round-1 design (temp file → fsync → validate → rename → directory fsync) survives unchanged as
the `shadow` engine, hardened with unique temp names. What the final round adds:

* the techniques round 1 only *discussed*: copy-on-write and both journaling directions, implemented;
* real synchronization (process lock, isolation, group commit, MVCC) instead of only `fsync`;
* a verification method that covers every crash state, including power loss, instead of three
  hand-picked `os._exit` points;
* the one thing round 1 could not provide: **justification by measurement**, showing what breaks
  without each safeguard and what each technique costs.
