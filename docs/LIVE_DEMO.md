# Live demo on real files (Windows · macOS · Linux)

Everything here runs on **your laptop's real disk**. You will **kill a real program in the middle of
saving a real file**, open that file in Notepad, and then let SafeWrite recover it.

> **What is real and what is simulated?**
> * **Real:** the terminal demos below, the **Documents** tab and the **Torture** tab. A real process is
>   killed mid-save, and recovery runs on the real files it left behind.
> * **Simulated:** the **Lab** and **Explorer** tabs simulate *power cuts*. You can't safely cut a laptop's
>   power on stage, and a battery would keep it alive anyway. The simulator rebuilds every disk a
>   power cut could leave and runs the same recovery code on it.

---

## 0. One-time setup (do this the day before)

1. Install **Python 3.9 or newer** from python.org. On Windows, tick **"Add python.exe to PATH"** in the installer.
2. Get the code: `git clone https://github.com/VMS2907/KernelOps`, or on GitHub click **Code → Download ZIP** and unzip it.
3. Open a terminal **inside the KernelOps folder**:
   * Windows: open the folder in File Explorer, click the address bar, type `powershell`, press Enter.
   * macOS: right-click the folder → **New Terminal at Folder**.
4. Check everything works (about 40 seconds):

   ```
   python -m unittest discover -s tests -t .
   ```

   You should see `OK` at the end. (On Windows, if `python` isn't found, use `py` instead of `python` everywhere.)

Nothing to install: SafeWrite uses only Python's standard library.

---

## Demo 1: "The broken save" (2 minutes)

This is what a normal program does when it saves: it overwrites the file in place.

```powershell
python -m safewrite init shop-unsafe --engine naive
python -m safewrite put  shop-unsafe inventory.csv --file examples/inventory_A.csv
```

Open the file to show the audience. Every line says **VERSION A**:

| Windows | macOS |
|---|---|
| `notepad shop-unsafe\docs\inventory.csv` | `open -e shop-unsafe/docs/inventory.csv` |

**Close Notepad.** Now save version B, but kill the program halfway:

```powershell
python -m safewrite crash shop-unsafe inventory.csv --file examples/inventory_B.csv --list
python -m safewrite crash shop-unsafe inventory.csv --file examples/inventory_B.csv --at 2
```

The first command shows the save is 3 steps. The second one **really kills the program** right
before step 2 (`⚡ CRASH: process killed`).

Open the file again:

* The **top** says `### WAREHOUSE INVENTORY - VERSION B ###`.
* Press **Ctrl+End**: the **bottom** says `### END OF VERSION A ###`.
* Press **Ctrl+F** and search for `VERSION A`. The first hit is line 113: everything above it is the
  new version, everything below it is the old one. The program died after writing the first 4,096 bytes.

> 🎤 *"This file is lying to you. The top is today's inventory, the bottom is yesterday's. It opens
> fine. No error. The numbers are just wrong."*

---

## Demo 2: "The safe save" (2 minutes)

Same files, same crash, but with SafeWrite's safe protocol (the `shadow` engine keeps readable files):

```powershell
python -m safewrite init shop-safe --engine shadow
python -m safewrite put  shop-safe inventory.csv --file examples/inventory_A.csv
python -m safewrite crash shop-safe inventory.csv --file examples/inventory_B.csv --list
```

The plan shows **5 steps**, with a `── COMMIT POINT ──` line right after the `rename`. Kill it just
before the commit point:

```powershell
python -m safewrite crash shop-safe inventory.csv --file examples/inventory_B.csv --at 4
```

Now open the folder `shop-safe/docs` in File Explorer / Finder. You'll see **two** files:

* `inventory.csv`: still **all VERSION A**. We never touched it.
* `inventory.csv~2.tmp`: the **complete** new version B, but it never got its real name.

> 🎤 *"We never write over your file. We write the new version next to it, and only when it is
> completely on the disk do we swap the names in one single step. The program died before the swap,
> so your old file is perfectly safe."*

Now "turn the computer back on" by running recovery:

```powershell
python -m safewrite recover shop-safe --key inventory.csv --out recovered.csv
```

It prints `✂ deleted uncommitted temp file … (it was never renamed, so never committed)`. The folder now
has only `inventory.csv`, and `recovered.csv` is exactly version A.

**Then the other side of the commit point:** kill it at step 5 (after the rename):

```powershell
python -m safewrite crash   shop-safe inventory.csv --file examples/inventory_B.csv --at 5
python -m safewrite recover shop-safe --key inventory.csv --out recovered.csv
```

`recovered.csv` is now exactly **version B**.

> 🎤 *"Before the commit point: the old version. After it: the new version. Never a mix."*

**Try any step you like.** `--at 1`, `2`, `3`, `4` all give A, and `5` gives B. The judges can pick the
number.

---

## Demo 3: the web app on real files (3 minutes)

```powershell
python -m safewrite serve
```

Your browser opens **http://127.0.0.1:8765** (open it yourself if it doesn't).

1. **Documents** tab: a real store in the folder `demo-store` next to the code.
   * Edit the text and click **Save (atomic + durable)**.
   * Change it again, choose **kill the writer at: a random syscall**, and click
     **⚡ Save & kill -9 mid-save**. A separate program saves your text and is killed partway through.
     The green card shows where it died and that recovery found **the previous version** or **the new
     version**.
   * **Version history** lists old versions. Click **Restore** to bring one back.
2. **Torture** tab: tick `naive/trunc`, `wal` and `cow`, set 30 kills, and click **⚡ Start killing**. That
   is 90 real `kill -9`s in real time. The unsafe engine collects red ✗ marks; the safe ones stay at 0.
3. **Lab** tab (simulated power cut): engine `naive` → **🎯 Find a violation** shows the coloured broken
   file (blue = old bytes, orange = new bytes). Switch to `cow` and press **⚡ Pull the plug** a few
   times: always green.

Stop the server with **Ctrl+C**.

---

## Plan B: the all-in-one terminal demo

If the browser misbehaves:

```powershell
python -m safewrite demo
```

It walks through everything above with real crashes; press Enter between steps.

---

## Reset between rehearsals

| Windows (PowerShell) | macOS / Linux |
|---|---|
| `Remove-Item -Recurse -Force shop-unsafe, shop-safe, recovered.csv, demo-store` | `rm -rf shop-unsafe shop-safe recovered.csv demo-store` |

## Troubleshooting

| Problem | Fix |
|---|---|
| `python` not found (Windows) | use `py`, or reinstall Python with "Add to PATH" ticked |
| `store is in use by another process` | another terminal or the web server has that store open; close it |
| `already a SafeWrite store` on `init` | run the reset command above |
| Notepad still shows old text | close Notepad and reopen the file after each crash |
| Port 8765 busy | `python -m safewrite serve --port 8800` |
| Torture tab feels slow | lower the kills to 15; Windows starts processes more slowly than Linux |
