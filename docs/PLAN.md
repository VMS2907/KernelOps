# SafeWrite: master plan for the final round

**Problem OSH021, Crash-Consistent Document Storage.** Team VAT: Tanush Keshri, Vyom Singh, Abhishek Kumar Maurya.

> **One-line pitch:** *SafeWrite is a crash-consistent document store with four
> interchangeable recovery engines and a power-loss simulator that enumerates every
> disk state a crash can leave behind. It proves, state by state, that a document
> always comes back as the old version or the new version and never as a mixture.*

---

## 1. Where we stand (honest gap analysis of the submitted round-1 design)

The round-1 design (temp file → fsync → validate → `rename` → dir fsync) is
**correct**, but it is the textbook answer, and every team on this problem will
submit some version of it. The judges' rubric is the seven deliverables. Here is how
round 1 scored against each one, and what the final product changes:

| Deliverable | Round 1 | Gap | Final product |
|---|---|---|---|
| Problem analysis | Good: truncate-then-write hazard explained | Treats "crash" as one thing | Separates **process crash** (page cache survives) from **power loss** (cache lost, writes reordered, sectors torn), then measures both |
| Architecture | One module, one technique | Nothing to compare against | Layered engine: VFS → 4 engines → crash simulator → Crash Lab UI |
| OS concept mapping | Copy-on-write and journaling listed as *"conceptual comparison only"* | **Two of the five core concepts are not implemented** | Copy-on-write (shadow paging, dual superblocks) and journaling (undo + redo WAL) are **implemented and tested** |
| Synchronization | Mapped to `fsync` | In OS courses, "synchronization" means mutual exclusion and coordination | `flock` process lock, RW lock, **group commit** (condition variables), **MVCC** lock-free readers, race demo with lost updates |
| Algorithms | Pseudocode for one protocol | No proof that every step is needed | Every step is **ablated**: remove it and the explorer shows which crash states break |
| Walkthrough + edge cases | 9 hand-picked cases | Crash only at 3 chosen points | **Every syscall** is a crash point, with every legal disk state after it (thousands of states) |
| Trade-offs | Table of opinions | No numbers | Measured: fsyncs/commit, write amplification, latency, recovery time, space |
| Prototype | CLI + 12 tests | No visual and no real concurrency | CLI + web Crash Lab + real `kill -9` torture + exhaustive tests + CI on 3 OSes |

The problem statement says teams *"must justify the chosen approach"*. The strongest
justification is to **build all the approaches, break them on purpose, and show
the data**. That is the core of this plan.

---

## 2. The winning thesis: "proven, not promised"

Most teams will show: *"we kill the program at 3 points, and it recovers."*

We will show:

1. **Breadth.** Every technique the problem statement names (*temporary files,
   logging, copy-on-write, journaling*) is a working engine behind one API, next to a
   deliberately unsafe baseline that produces exactly the "corrupted mixture" the
   problem warns about.
2. **Rigor.** A power-loss simulator that models what the OS and disk really do:
   volatile page cache, `fsync`/directory-`fsync` barriers, metadata journaling
   order, reordering of unflushed writes, torn sectors, and zero-filled extents. It
   **enumerates every crash state** at **every syscall** and checks the invariant
   *recovered ∈ {old, new}* plus *acknowledged ⇒ new*. This is the methodology of
   the OSDI research tools **ALICE** (2014) and **CrashMonkey/B3** (2018), applied to
   our own engines.
3. **Ablation = justification.** Remove one safeguard (the `fsync` before
   `rename`, the directory `fsync`, a checksum, a barrier, the second superblock)
   and the matrix turns red in the exact crash states that safeguard exists for. Each
   line of the protocol has a counter-example.
4. **Reality.** The same engines run on real files with real syscalls (`strace`
   shows them), survive thousands of real `SIGKILL`s, and serve a live web demo
   where the judges pull the plug themselves.
5. **Depth of OS concepts.** Real synchronization (flock, RW locks, group commit
   with condition variables, MVCC snapshot pinning), atomicity vs. durability measured
   separately, recovery that is idempotent and survives **crashes during
   recovery**.

---

## 3. Product scope

### 3.1 One API, five engines

```python
store = SafeStore.open("mystore", engine="cow")   # recovery runs automatically on open
store.put("inventory.csv", data)                  # atomic and durable when it returns
store.get("inventory.csv")
with store.transaction() as tx:                   # multi-document atomic commit
    tx.put("alice", b"70"); tx.put("bob", b"80")
store.history("inventory.csv")                    # version history (COW engine)
store.verify()                                    # scrub every checksum
```

| Engine | Technique | Recovers to | Commit point | fsyncs / commit | Multi-doc atomic |
|---|---|---|---|---|---|
| `naive` | in-place overwrite (baseline, **unsafe on purpose**) | garbage | none | 0–1 | ✗ |
| `shadow` | temp file + `rename` (our round-1 design, hardened) | old **or** new | `rename` | 2 | ✗ (demonstrated) |
| `undo` | rollback journal (SQLite style) | **previous** version | journal invalidation | 3 | ✓ |
| `wal` | redo write-ahead log + checkpoint (ext4-jbd2 / Postgres style) | **new** version if committed | COMMIT record durable | **1** (amortized by group commit) | ✓ |
| `cow` | copy-on-write shadow paging, dual superblocks, Merkle checksums (LMDB / ZFS / btrfs style) | newest durable root | superblock flip | 2 | ✓, plus version history and lock-free readers |

**Default engine: `cow`.** Recovery is O(1) (pick the valid superblock; nothing is
replayed), version history comes free, readers never block writers, and "old or
new" follows from the structure rather than from replay logic. The data in §6 is
the justification.

### 3.2 Ablations (the deliberately broken variants)

| Variant | Safeguard removed | Expected failure in the explorer |
|---|---|---|
| `naive-trunc` | everything (`O_TRUNC` then write) | empty or prefix file → **corrupt** |
| `naive` | everything (in-place overwrite) | **mixture of A and B** |
| `shadow/no-fsync` | `fsync(tmp)` before `rename` | rename persists before data → zero-length file → **old version destroyed** (the 2009 ext4 "O_PONIES" bug) |
| `shadow/no-dirsync` | `fsync(dir)` after `rename` | acknowledged write rolls back → **durability violation** |
| `undo/no-journal-sync` | journal `fsync` before in-place writes | data modified, journal not durable → **mixture** |
| `wal/no-checksum` | record checksums | torn tail replayed as committed → **garbage** |
| `wal/early-ack` | ack before log `fsync` | **acknowledged commit lost** |
| `cow/no-barrier` | `fsync` between data and superblock | superblock points at unwritten blocks → **corrupt** |
| `cow/one-super` | second superblock slot | torn superblock → **store unreadable** |

### 3.3 Interfaces

* **CLI**: `python -m safewrite …`: `init put get rm ls history verify recover info`,
  `crash` (a real `os._exit` at *any* syscall index or phase name), `trace`,
  `explore`, `torture`, `bench`, `serve`, `demo`.
* **Crash Lab (web UI, zero dependencies, works offline)**: `python -m safewrite serve`
  1. **Lab**: pick engine + scenario, scrub the syscall timeline, see page cache
     vs. disk, press **⚡ Pull the plug**, watch recovery narrate itself, and get a
     verdict with a byte-provenance strip (which bytes came from A, from B, or zeros).
  2. **Explorer**: the engines × scenarios matrix, with thousands of crash states
     per cell, green/red. Click a red cell to replay that exact state in the Lab.
  3. **Torture**: real `kill -9` loop with live counters (OK-old / OK-new /
     CORRUPT / LOST).
  4. **Bench**: fsyncs per commit, write amplification, latency, recovery time,
     group-commit scaling.
  5. **Documents**: a real store on disk. Edit, save, browse version history,
     restore, scrub, and "kill the writer mid-save" for real.

---

## 4. Architecture

```
               ┌──────────────────────────────────────────────────────────┐
  CLI / Web UI │  SafeStore API: put · get · delete · transaction · history│
               └───────────────┬──────────────────────────────────────────┘
                               │  one interface, pluggable engines
     ┌────────────┬────────────┼─────────────┬──────────────┬────────────┐
     │  naive     │  shadow    │  undo       │  wal         │  cow       │
     │ (baseline) │ tmp+rename │ rollback jnl│ redo log+ckpt│ shadow pg. │
     └────────────┴────────────┴──────┬──────┴──────────────┴────────────┘
                 synchronization: flock · RWLock · GroupCommit · MVCC pins
                                      │  every persistence op goes through:
                ┌─────────────────────┴───────────────────────┐
                │ VFS: open pwrite pread fsync rename unlink  │
                │      fsync_dir truncate · note(phase)       │
                └───┬───────────────────┬─────────────────┬───┘
                    │                   │                 │
               OsVFS (real         RecordingVFS       MemVFS (in-memory,
               syscalls, strace-   (trace + inode     fast recovery runs
               visible, real       tracking + crash   on crash images)
               SIGKILL)            injection)
                                        │ trace
                              ┌─────────┴──────────────────────────────┐
                              │ Crash simulator                        │
                              │  persistence model (power-loss/process)│
                              │  → enumerate crash states per syscall  │
                              │  → materialize image → engine.recover()│
                              │  → classify: OLD / NEW / CORRUPT /     │
                              │    LOST / PARTIAL-TX / ERROR           │
                              └────────────────────────────────────────┘
```

**Design rule:** engines never call `os.*` directly. Every byte goes through the VFS.
That is why the same engine code can run against a real disk, be traced, be
crash-injected, and be replayed against thousands of simulated post-crash disks.

---

## 5. The crash model (the scientific core)

At a crash after syscall *k*, which disk states are possible?

* **fsync(file)** forces every earlier data write/truncate on that inode.
* **fsync(dir)** forces every earlier namespace op (create/rename/unlink).
* **Namespace ops persist in order** (a journaled FS commits metadata as a prefix).
* **Unforced data writes** may be dropped, persisted, or reordered (any subset),
  and one of them may be **torn** at sector granularity (512 B) or persist as **zeros**
  (size updated, data not).
* **Process-crash model** (`kill -9`): the page cache survives, so every issued op
  persists and only the in-flight write may be partial.

For every crash point and every state the model allows (bounded and deduplicated),
we build the disk image, run the engine's recovery, read the documents back, and
check:

1. **Atomicity**: every document is byte-identical to the old or the new version
   (for transactions, *all* documents are old or *all* are new).
2. **Durability**: if the call had returned (acknowledged) before the crash, the
   result is the new version.
3. **Idempotent recovery**: running recovery twice gives the same state.
4. **Crash-during-recovery**: crash recovery itself at every step, recover again,
   and the invariant still holds.

---

## 6. OS concept → where it lives (the mapping the judges asked for)

| Core concept | Where it is implemented | What we demonstrate |
|---|---|---|
| **File systems** | `vfs/osfs.py`: inodes vs. names, dir entries, `rename(2)`, `O_TRUNC`, `ftruncate`, sector size; `cow` builds a mini file system (superblock, block allocator, catalog) inside a volume file | strace of the real syscalls; the explorer models inodes so "rename persisted but data didn't" is representable |
| **Atomicity** | commit points: `rename` (shadow), journal invalidation (undo), checksummed COMMIT record (wal), superblock flip (cow) | every crash state is A or B; single-doc *and* multi-doc |
| **Synchronization** | `concurrency.py`: `flock` (kernel-released on death, so no stale locks), RW lock, group commit (leader/follower on a condition variable), MVCC snapshot pins; plus `fsync` barriers to storage | lost-update race without the lock vs. correct with it; group-commit throughput scaling |
| **Recovery** | each engine's `recover()`: temp sweep, rollback, redo replay, superblock selection; structured recovery log | idempotent, crash-during-recovery safe, O(1) for COW vs. O(log) for WAL, measured |
| **Copy-on-write** | `engines/cow.py`: never overwrite live blocks; Merkle checksums; dual superblocks; GC with retention | version history, instant recovery, readers never block |
| **Journaling** | `engines/undo.py` (old values) and `engines/wal.py` (new values, ARIES-style redo, checkpoint LSN + salt) | undo → *previous* version, redo → *new* version, exactly the two outcomes the problem statement names |

---

## 7. Verification strategy (what we will quote on stage)

* **Exhaustive crash exploration:** N crash states × 13 engine variants × 5
  scenarios. Safe engines: **0 violations**. Every ablation: **> 0 violations**,
  each with a concrete counter-example. (This also tests our tester: if the checker
  could not catch bugs, the ablations would pass.)
* **Real `SIGKILL` torture:** thousands of real kills at random microsecond offsets
  on a real ext4 file system. Safe engines: 0 corruptions; `naive`: many.
* **Unit + property tests** (stdlib `unittest`, zero dependencies): formats,
  engines, reopen persistence, large/empty/unicode docs, concurrency, MemVFS ≡
  OsVFS equivalence, and crash images materialized to real disk and recovered with
  real syscalls.
* **CI** on Ubuntu, macOS and Windows via GitHub Actions.

---

## 8. Milestones (build order; each one is demoable on its own)

| # | Milestone | Done when |
|---|---|---|
| M1 | VFS layer + formats | OsVFS/MemVFS/RecordingVFS pass equivalence tests |
| M2 | Five engines + ablations | unit tests green; CLI put/get/recover works on real disk |
| M3 | Crash simulator + `explore` | matrix: safe = 0 violations, ablations > 0 |
| M4 | Real crash injection + torture + concurrency + bench | `torture` 1000s of kills, 0 corruptions on safe engines |
| M5 | Crash Lab web UI | judges can pull the plug from the browser |
| M6 | Docs: README, final DESIGN.md (all 7 deliverables), Judge Q&A, demo script, CI | a stranger can clone, run, and understand in 5 minutes |

---

## 9. Demo plan (7 minutes)

1. **Hook (30 s):** the `naive` engine in the Lab. Pull the plug mid-save; the
   byte strip shows a blue/green/grey **mixture**. *"This is what your editor does."*
2. **The fix (60 s):** switch to `cow`. Pull the plug at every point: always A or
   B. Show the superblock flip as the commit point.
3. **The proof (90 s):** the Explorer matrix. Thousands of crash states, all green.
   Then toggle `shadow/no-fsync`: red. Click the cell: "rename persisted, data
   didn't, old version destroyed". *"Every line of our protocol has a
   counter-example."*
4. **Reality (60 s):** the Torture tab, with real `kill -9` against real files and live
   counters. Then the strace output of one save.
5. **Concepts (60 s):** undo returns to the *previous* version and redo returns to
   the *new* one; group commit with condition variables; MVCC readers.
6. **Trade-offs (60 s):** the Bench tab, and why we chose COW, backed by numbers.
7. **Close (30 s):** limitations, stated honestly (relies on honest disk flush,
   single-host), and future work.

---

## 10. Roles (suggested)

* **Vyom:** engines + crash simulator (the proof), presents §3 of the demo.
* **Tanush:** Crash Lab UI + torture/bench, presents the live demo.
* **Abhishek:** design doc, OS concept mapping, Q&A lead, presents concepts and trade-offs.

Everyone should be able to answer the top 10 questions in `docs/JUDGE_QA.md`.

---

## 11. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Demo laptop is Windows | stdlib only; `O_BINARY`, `os.replace`, msvcrt lock, no-`pwrite` fallback; CI on Windows |
| No internet at venue | UI is vanilla JS with no CDN; everything is served locally |
| Judge asks "is your simulator realistic?" | the model is stated explicitly, mirrors ALICE/CrashMonkey, and real SIGKILL + real-disk replay corroborate it |
| Judge asks "what about real power loss?" | state honestly that durability depends on the disk honoring FLUSH; we model the FS contract, not lying hardware; future work: dm-log-writes in a VM |
| Live demo hiccup | `python -m safewrite demo` is a scripted terminal fallback; pre-generated results in `results/` |
