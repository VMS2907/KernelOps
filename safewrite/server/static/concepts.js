// OS concepts: where each core concept lives in the design, plus the architecture.
import { h, svg, pill } from "./util.js";

const CONCEPTS = [
  ["File systems", "vfs/osfs.py, crashsim/model.py, engines/cow.py",
    "Inodes vs. names: a handle keeps writing to its inode after rename; fsync(file) persists data, fsync(dir) persists names. The cow engine is a mini file system: superblocks, block allocator, catalog, free-space GC.",
    "The simulator models inodes, so it can represent “the rename reached the disk but the data didn't”. That is the classic zero-length-file bug, and shadow/no-fsync reproduces it."],
  ["Atomicity", "every engine's commit point",
    "One indivisible event decides old vs new: rename(2) for shadow, journal deactivation for undo, a checksummed COMMIT record for wal, a superblock flip for cow.",
    "Exhaustive check at every syscall: every recovered state equals the old or the new version. Multi-document transactions are all-or-nothing in undo/wal/cow."],
  ["Synchronization", "concurrency.py, wal.py, cow.py, store.py",
    "flock process lock (the kernel drops it on kill -9: no stale locks), readers/writer lock for in-place engines, group commit (leader + followers on a condition variable), MVCC snapshot pins so COW readers never block. fsync is the barrier that orders writes with the disk.",
    "16 threads share ~1 fsync per 6 commits in WAL; a second process is refused while the store is open; snapshot reads stay consistent during writes."],
  ["Recovery", "Engine.recover() in each engine",
    "Runs on every open. shadow: delete uncommitted temps. undo: roll back an active journal. wal: redo committed transactions, ignore torn tails (CRC + LSN + salt). cow: pick the newest valid superblock, with nothing to replay.",
    "Idempotent (recover twice = same state) and crash-safe: we crash recovery itself at every syscall and recover again."],
  ["Copy-on-write", "engines/cow.py",
    "Never overwrite a live block. Write new data and a new catalog into free blocks, barrier, then flip one of two superblocks. Superblock → catalog → documents form a Merkle checksum chain.",
    "O(1) recovery (flat at ~1 ms for 100 or 5000 commits), version history, lock-free readers. cow/no-barrier and cow/one-super show why each safeguard exists."],
  ["Journaling", "engines/undo.py, engines/wal.py",
    "Undo: journal the OLD bytes first, then overwrite in place, so recovery rolls BACK to the previous version. Redo (WAL): log the NEW bytes with a commit record, so recovery rolls FORWARD to the new version. Checkpoints copy the log to home files.",
    "Exactly the two outcomes the problem statement names: “previous valid version” (undo) or “new valid version” (redo)."],
];

const ENGINES = [
  ["naive", "overwrite in place", "none", "garbage", "0–1", "✗", "baseline"],
  ["shadow", "temp file + rename", "rename()", "old or new (per doc)", "2", "✗", "our round-1 design"],
  ["undo", "rollback journal", "journal deactivated", "previous version", "3", "✓", "SQLite rollback mode"],
  ["wal", "redo log + checkpoint", "COMMIT record durable", "new if committed", "1 (shared)", "✓", "ext4 jbd2, Postgres"],
  ["cow", "shadow paging", "superblock flip", "newest durable root", "2", "✓", "LMDB, ZFS, btrfs"],
];

export class ConceptsView {
  constructor(root, app) {
    this.root = root;
    this.app = app;
    this.build();
  }

  activate() {}

  build() {
    this.root.append(
      h("div", { class: "card" },
        h("h2", {}, "The problem in one sentence"),
        h("p", { style: { marginTop: "6px" } },
          "A save is many system calls, and the disk may keep any subset of the ones that were not flushed. ",
          "Without a protocol, a crash leaves a file that is neither the old version nor the new one: ",
          h("b", {}, "a corrupted mixture, and nothing in the file says so.")),
        h("p", { class: "muted" },
          "Two kinds of crash: a ", h("b", {}, "process crash"), " (kill -9; the kernel page cache survives) and a ",
          h("b", {}, "power loss"), " (the cache is gone; unflushed writes may be lost, reordered or torn). ",
          "Our round-1 tests used only process crashes. The Explorer shows that every missing-fsync bug ",
          h("b", {}, "passes"), " those tests and is only caught by the power-loss model.")),
      h("div", { class: "card" }, h("h2", {}, "Architecture"), h("div", { class: "arch", style: { marginTop: "10px" } }, archSvg())),
      h("div", { class: "card" }, h("h2", {}, "Core OS concepts → where they live → how we prove them"),
        h("div", { style: { overflowX: "auto", marginTop: "8px" } }, h("table", { class: "concepts" },
          h("thead", {}, h("tr", {}, h("th", {}, "Concept"), h("th", {}, "Where"), h("th", {}, "How it works"), h("th", {}, "Evidence"))),
          h("tbody", {}, CONCEPTS.map(([c, w, how, ev]) => h("tr", {}, h("td", {}, c), h("td", { class: "mono" }, w), h("td", {}, how), h("td", {}, ev))))))),
      h("div", { class: "card" }, h("h2", {}, "Five engines, one API"),
        h("div", { style: { overflowX: "auto", marginTop: "8px" } }, h("table", { class: "concepts" },
          h("thead", {}, h("tr", {}, ["engine", "technique", "commit point", "recovers to", "fsyncs/commit", "multi-doc atomic", "real-world analogue"].map((x) => h("th", {}, x)))),
          h("tbody", {}, ENGINES.map((r) => h("tr", {}, r.map((x, i) => h("td", { class: i === 0 ? "mono" : "" },
            i === 5 ? (x === "✓" ? pill("good", "✓ yes") : pill("bad", "✗ no")) : x)))))))),
      h("div", { class: "card" }, h("h2", {}, "Assumptions & limitations (stated, not hidden)"),
        h("ul", { style: { margin: "8px 0 0", paddingLeft: "18px", lineHeight: "1.7" } },
          h("li", {}, "Durability assumes the drive honours FLUSH (fsync). A drive that lies about its write cache defeats every software scheme, ZFS and Postgres included."),
          h("li", {}, "The power model assumes metadata is journaled in order (ext4, XFS, NTFS, APFS). The paranoid model additionally drops 512-byte sector atomicity."),
          h("li", {}, "Single machine, one writer process at a time for undo/wal/cow (enforced by flock); many threads inside it. The shadow engine also has a multi-writer mode (shared session lock + COMMIT lock, local file systems)."),
          h("li", {}, "Crash states are exhaustive for these scenarios up to a per-point budget; larger traces are sampled with every prefix, every single-drop and every single-keep subset included."),
          h("li", {}, "Future work: dm-log-writes block-level replay in a VM, a COW B+-tree for millions of keys, incremental GC, Windows FlushFileBuffers on directories."))),
    );
  }
}

function archSvg() {
  const W = 980, H = 420;
  const box = (x, y, w, hh, title, sub, cls = "") => svg("g", {},
    svg("rect", { class: "box " + cls, x, y, width: w, height: hh, rx: 10 }),
    svg("text", { x: x + w / 2, y: y + (sub ? hh / 2 - 3 : hh / 2 + 4), "text-anchor": "middle", "font-weight": 600 }, title),
    sub ? svg("text", { class: "sub", x: x + w / 2, y: y + hh / 2 + 13, "text-anchor": "middle" }, sub) : null);
  const edge = (x1, y1, x2, y2) => svg("path", { class: "edge", d: `M${x1},${y1} L${x2},${y2}` });
  const engines = ["naive", "shadow", "undo", "wal", "cow"];
  const subs = ["unsafe baseline", "temp + rename", "rollback journal", "redo log", "shadow paging"];
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "SafeWrite architecture" },
    svg("defs", {}, svg("marker", { id: "arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
      svg("path", { d: "M0,0 L10,5 L0,10 z", fill: "var(--ink-3)" }))),
    box(40, 14, 260, 46, "CLI  ·  Crash Lab web UI", "python -m safewrite …"),
    box(360, 14, 300, 46, "SafeStore API", "open → lock → recover · put · get · tx · history", "hot"),
    box(720, 14, 220, 46, "Synchronization", "flock · RWLock · group commit · MVCC"),
    edge(300, 37, 358, 37), edge(660, 37, 718, 37),
    ...engines.map((e, i) => box(40 + i * 184, 100, 168, 52, e, subs[i], e === "cow" ? "hot" : "")),
    ...engines.map((_, i) => edge(510, 60, 124 + i * 184, 98)),
    box(240, 190, 500, 50, "VFS interface: every byte goes through here", "open · pwrite · pread · fsync · rename · unlink · fsync_dir · note(phase)", "hot"),
    ...engines.map((_, i) => edge(124 + i * 184, 152, 490, 188)),
    box(40, 290, 260, 56, "OsVFS", "real syscalls · strace-visible · kill -9"),
    box(360, 290, 260, 56, "RecordingVFS", "trace + inode identity + crash injection", "sim"),
    box(680, 290, 260, 56, "MemVFS", "in-memory inodes for fast replay"),
    edge(400, 240, 170, 288), edge(490, 240, 490, 288), edge(580, 240, 810, 288),
    box(300, 370, 380, 44, "Crash simulator", "enumerate disk states → recover → judge", "sim"),
    edge(490, 346, 490, 368), edge(680, 392, 810, 348),
  );
  return g;
}
