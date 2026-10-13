"""Original score and sound design for the launch film, synthesized from scratch.

Every sound is generated here with numpy (oscillators, filtered noise, a
convolution reverb), so the film has no licensing questions. The picture
drives the sound: scenes.js exports a cue list (video/build/timeline.json)
and this script places one-shots on those cues and arranges the music
around the film's sections.

    python video/score.py [video/build/timeline.json] [video/build/score.wav]

Music: 100 BPM, A minor (Am-F-C-G), resolving to C add9 on the logo and the end card.
"""

import json
import os
import subprocess
import sys
import wave

import numpy as np

SR = 48000
ROOT = os.path.dirname(os.path.abspath(__file__))
BEAT = 0.6                 # 100 BPM
BAR = 4 * BEAT
GRID0 = 34.0               # the groove's first downbeat ("Never overwrite.")
rng = np.random.default_rng(2907)


# ---------------------------------------------------------------- basics
def midi(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def tax(dur):
    return np.arange(int(round(dur * SR))) / SR


def noise(n, r=None):
    return (r or rng).standard_normal(n)


def fft_filter(x, gain_fn):
    """Zero-phase filter: multiply the spectrum by gain_fn(freqs). Works on (n,) or (2, n)."""
    n = x.shape[-1]
    pad = 1 << int(np.ceil(np.log2(n + 8192)))
    X = np.fft.rfft(x, pad)
    X *= gain_fn(np.fft.rfftfreq(pad, 1 / SR))
    return np.fft.irfft(X, pad)[..., :n]


def lp(fc, order=2):
    return lambda f: 1 / np.sqrt(1 + (f / fc) ** (2 * order))


def hp(fc, order=2):
    return lambda f: 1 / np.sqrt(1 + (fc / np.maximum(f, 1e-3)) ** (2 * order))


def bp(lo, hi, order=2):
    return lambda f: lp(hi, order)(f) * hp(lo, order)(f)


def stft_filter(x, gain_fn, n=2048, hop=512):
    """Time-varying filter: gain_fn(times[frames, 1], freqs[1, bins]) -> gains."""
    win = np.hanning(n + 1)[:-1]
    xp = np.concatenate([np.zeros(n), x, np.zeros(n)])
    frames = 1 + (len(xp) - n) // hop
    idx = np.arange(n)[None, :] + hop * np.arange(frames)[:, None]
    X = np.fft.rfft(xp[idx] * win, axis=1)
    times = (np.arange(frames) * hop + n / 2 - n) / SR
    X *= gain_fn(times[:, None], np.fft.rfftfreq(n, 1 / SR)[None, :])
    y = np.fft.irfft(X, n, axis=1) * win
    out = np.zeros(len(xp))
    for i in range(frames):
        out[i * hop:i * hop + n] += y[i]
    return out[n:n + len(x)] / 1.5


def band_sweep(centre, width=0.6):
    """Gain for a band (width in octaves) whose centre frequency is a function of time."""
    return lambda t, f: np.exp(-0.5 * (np.log2(np.maximum(f, 1) / centre(t)) / width) ** 2)


def pan2(sig, p):
    p = np.clip(p, -1, 1)
    a = (p + 1) * np.pi / 4
    return np.stack([sig * np.cos(a), sig * np.sin(a)])


def fade(sig, a=0.002, r=0.01):
    n = sig.shape[-1]
    na, nr = min(int(a * SR), n), min(int(r * SR), n)
    env = np.ones(n)
    if na: env[:na] = np.linspace(0, 1, na)
    if nr: env[-nr:] *= np.linspace(1, 0, nr)
    return sig * env


def saw(phase, f0, fmax=6000.0, tilt=0.0):
    """Band-limited saw by additive synthesis; tilt darkens upper harmonics."""
    out = np.zeros_like(phase)
    kmax = max(1, int(fmax / max(f0, 1)))
    for k in range(1, kmax + 1):
        out += np.sin(k * phase) / k * np.exp(-tilt * (k - 1))
    return out


def plus(*sigs):
    """Sum mono signals of different lengths (zero-padded)."""
    out = np.zeros(max(len(x) for x in sigs))
    for x in sigs:
        out[: len(x)] += x
    return out


def phase_of(freq):
    return 2 * np.pi * np.cumsum(freq) / SR


# ---------------------------------------------------------------- mixer
class Mix:
    def __init__(self, dur):
        self.n = int(dur * SR)
        self.bus = {k: np.zeros((2, self.n)) for k in ("dry", "hall", "room", "duck")}

    def add(self, t, sig, gain=1.0, pan=0.0, bus="dry", hall=0.0, room=0.0):
        sig = np.asarray(sig, dtype=float) * gain
        if sig.ndim == 1:
            sig = pan2(sig, pan)
        i = int(round(t * SR))
        j = min(self.n, i + sig.shape[1])
        if j <= max(i, 0):
            return
        s = sig[:, max(0, -i):j - i]
        i = max(i, 0)
        self.bus[bus][:, i:j] += s
        if hall:
            self.bus["hall"][:, i:j] += s * hall
        if room:
            self.bus["room"][:, i:j] += s * room


def make_ir(t60, dur, bright, seed):
    r = np.random.default_rng(seed)
    t = tax(dur)
    ir = np.zeros((2, len(t)))
    for ch in range(2):
        nz = r.standard_normal(len(t))
        dark = fft_filter(nz, lp(bright * 0.35)) * 10 ** (-3 * t / t60)
        light = fft_filter(nz, lp(bright)) * 10 ** (-3 * t / (t60 * 0.45))
        ir[ch] = 0.7 * dark + 0.5 * light
        for d, g in ((0.011, .5), (0.019, .35), (0.027, .3), (0.041, .22), (0.053, .18)):
            k = int((d + 0.004 * ch) * SR)
            ir[ch, k] += g * (1 if r.random() > .5 else -1) * 4
    ir[:, : int(0.008 * SR)] = 0      # pre-delay
    return ir / np.sqrt(np.sum(ir ** 2) / 2)


def convolve(x, ir):
    n = x.shape[1] + ir.shape[1]
    N = 1 << int(np.ceil(np.log2(n)))
    Y = np.fft.rfft(x, N, axis=1) * np.fft.rfft(ir, N, axis=1)
    return np.fft.irfft(Y, N, axis=1)[:, : x.shape[1]]


# ---------------------------------------------------------------- drums
def kick(punch=1.0):
    t = tax(0.55)
    f = 52 + 140 * np.exp(-t / 0.028) * punch
    body = np.sin(phase_of(f)) * np.exp(-t / 0.17)
    knock = np.sin(phase_of(160 + 0 * t)) * np.exp(-t / 0.035) * 0.5
    click = fft_filter(noise(len(t)), bp(1800, 8000)) * np.exp(-t / 0.003) * 0.8
    return np.tanh(1.6 * (body + knock + click)) * 0.9


def clap():
    t = tax(0.4)
    env = sum(np.exp(-np.maximum(t - d, 0) / 0.0045) * (t >= d) for d in (0, 0.010, 0.021))
    env += np.exp(-np.maximum(t - 0.03, 0) / 0.11) * (t >= 0.03) * 0.8
    return fft_filter(noise(len(t)), bp(900, 3200)) * env * 0.6


def metal(t):
    return sum(np.sign(np.sin(2 * np.pi * f * 2.1 * t)) for f in (205.3, 304.4, 369.6, 522.7, 540.0, 800.0)) / 6


def hat(open_=False):
    t = tax(0.45 if open_ else 0.09)
    sig = 0.6 * noise(len(t)) + 0.6 * metal(t)
    sig = fft_filter(sig, hp(7200, 3))
    return sig * np.exp(-t / (0.13 if open_ else 0.017))


def shaker():
    t = tax(0.12)
    env = (t / 0.025) * np.exp(1 - t / 0.025)
    return fft_filter(noise(len(t)), bp(4500, 11000)) * env * 0.5


def crash(dur=3.0):
    t = tax(dur)
    out = []
    for _ in range(2):
        sig = 0.7 * noise(len(t)) + 0.5 * metal(t * (1 + 0.01 * rng.random()))
        out.append(fft_filter(sig, hp(3800, 2)) * np.exp(-t / 0.9) * (1 - np.exp(-t / 0.002)))
    return np.stack(out) * 0.5


# ---------------------------------------------------------------- tonal
def bass_note(f, dur, vel=1.0):
    t = tax(dur + 0.05)
    ph = phase_of(np.full(len(t), f))
    env = np.minimum(t / 0.004, 1) * (0.55 + 0.45 * np.exp(-t / 0.09)) * np.clip((dur + 0.05 - t) / 0.05, 0, 1)
    sub = np.sin(ph)
    grit = saw(ph, f, fmax=1800, tilt=0.18) * (0.35 + 0.4 * np.exp(-t / 0.07))
    return np.tanh(1.4 * (sub + grit) * env * vel) * 0.8


def pad_chord(notes, dur, bright=1.0, attack=0.25, release=0.7):
    t = tax(dur + release)
    env = np.minimum(t / attack, 1) * np.clip((dur + release - t) / release, 0, 1) ** 1.5
    out = np.zeros((2, len(t)))
    for m in notes:
        f0 = midi(m)
        for det, p in ((-9, -0.7), (0, 0), (9, 0.7)):
            f = f0 * 2 ** (det / 1200)
            ph = phase_of(np.full(len(t), f)) + rng.random() * 6.28
            v = saw(ph, f, fmax=900 + 2600 * bright, tilt=0.22 - 0.12 * bright)
            out += pan2(v, p) / 3
    return out * env / len(notes)


def pluck(f, dur=0.7, bright=1.0):
    t = tax(dur)
    out = np.zeros(len(t))
    kmax = int(min(9000, 2500 + 6000 * bright) / f)
    for k in range(1, max(kmax, 1) + 1):
        out += np.sin(2 * np.pi * k * f * t) / k ** 1.1 * np.exp(-t * (3.0 + 1.1 * k * f / 500 / bright))
    return out * np.minimum(t / 0.002, 1) * 0.6


def bell(f, dur=3.0):
    t = tax(dur)
    out = np.zeros(len(t))
    for ratio, a, d in ((1, 1, 2.4), (2.0, .45, 1.4), (2.76, .3, .9), (4.07, .18, .6), (5.4, .1, .35)):
        out += a * np.sin(2 * np.pi * f * ratio * t) * np.exp(-t / d)
    return out * np.minimum(t / 0.002, 1) * 0.4


# ---------------------------------------------------------------- sfx
def sub_drop(f0, f1, dur, tau):
    t = tax(dur)
    f = f1 + (f0 - f1) * np.exp(-t / tau)
    return np.sin(phase_of(f)) * np.exp(-t / (dur / 3)) * np.minimum(t / 0.004, 1)


def hit(low=False):
    t = tax(2.2)
    s = sub_drop(95, 38, 2.2, 0.18) * 1.0
    s += np.sin(phase_of(130 + 0 * t)) * np.exp(-t / 0.07) * 0.5
    s += np.sin(phase_of(110 + 130 * np.exp(-t / 0.04))) * np.exp(-t / 0.13) * 0.55   # body small speakers can play
    if not low:
        s += fft_filter(noise(len(t)), bp(500, 2800)) * np.exp(-t / 0.035) * 0.5
        s += fft_filter(noise(len(t)), lp(4000)) * np.exp(-t / 0.06) * 0.7
        s += fft_filter(noise(len(t)), bp(150, 900)) * np.exp(-t / 0.25) * 0.35
    return np.tanh(1.3 * s)


def boom(dur=4.5):
    t = tax(dur)
    s = sub_drop(110, 30, dur, 0.35) * 1.1
    s += fft_filter(noise(len(t)), lp(2500)) * np.exp(-t / 0.12) * 0.8
    s += fft_filter(noise(len(t)), bp(60, 400)) * np.exp(-t / 0.9) * 0.5
    return np.tanh(1.4 * s)


def riser(dur):
    t = tax(dur)
    n = len(t)
    k = t / dur
    nz = stft_filter(noise(n), band_sweep(lambda tt: 300 * (30 ** np.clip(tt / dur, 0, 1)), 0.9))
    tone = np.sin(phase_of(220 * 4 ** k)) * 0.25 + np.sin(phase_of(330 * 4 ** k)) * 0.15
    rev = crash(dur)[0][::-1] * 1.4
    return (nz * 0.9 + tone + rev) * k ** 2.2


def whoosh(dur=0.75):
    t = tax(dur)
    n = len(t)
    shape = np.sin(np.pi * np.clip(t / dur, 0, 1)) ** 2
    nz = stft_filter(noise(n), band_sweep(lambda tt: 500 + 2500 * np.sin(np.pi * np.clip(tt / dur, 0, 1)), 0.8))
    return np.stack([nz * shape * np.cos(t / dur * np.pi / 2), nz * shape * np.sin(t / dur * np.pi / 2)]) * 0.9


def tick(gain=1.0):
    t = tax(0.06)
    s = np.sin(2 * np.pi * 2300 * t) * np.exp(-t / 0.010) + 0.35 * np.sin(2 * np.pi * 4600 * t) * np.exp(-t / 0.006)
    s += fft_filter(noise(len(t)), hp(3000)) * np.exp(-t / 0.0015) * 0.4
    return s * 0.5 * gain


def key_click(soft=False):
    t = tax(0.12)
    press = fft_filter(noise(len(t)), bp(1800, 7000)) * np.exp(-t / 0.0035)
    rel_at = 0.035 + 0.03 * rng.random()
    rel = fft_filter(noise(len(t)), bp(2500, 8000)) * np.exp(-np.maximum(t - rel_at, 0) / 0.002) * (t >= rel_at) * 0.4
    thock = np.sin(2 * np.pi * (170 + 60 * rng.random()) * t) * np.exp(-t / 0.018) * 0.5
    return (press + rel + thock) * (0.55 + 0.3 * rng.random()) * (0.45 if soft else 1.0)


def heart():
    t = tax(0.6)
    s = np.zeros(len(t))
    for at, g in ((0, 1.0), (0.17, 0.7)):
        tt = np.maximum(t - at, 0)
        s += (t >= at) * g * (np.sin(2 * np.pi * 52 * tt) + 0.6 * np.sin(2 * np.pi * 104 * tt)
                              + 0.3 * np.sin(2 * np.pi * 156 * tt)) * np.exp(-tt / 0.085) * np.minimum(tt / 0.006, 1)
    s += fft_filter(noise(len(t)), bp(150, 600)) * (np.exp(-t / 0.02) + 0.7 * np.exp(-np.maximum(t - 0.17, 0) / 0.02) * (t >= 0.17)) * 0.5
    return np.tanh(1.8 * s)


def glitch(dur=0.32, seed=0):
    r = np.random.default_rng(seed)
    out = np.zeros((2, int(dur * SR)))
    i, prev = 0, None
    while i < out.shape[1]:
        g = int(r.uniform(0.018, 0.05) * SR)
        kind = r.integers(0, 3) if prev is not None else r.integers(0, 2)
        tt = np.arange(g) / SR
        if kind == 0:
            sig = np.sign(np.sin(2 * np.pi * r.uniform(180, 2400) * tt)) * 0.5
        elif kind == 1:
            sig = r.standard_normal(g) * 0.5
            sig = np.round(sig * 4) / 4                          # bit crush
        else:
            sig = np.resize(prev, g)                              # stutter the previous grain
        sig = fade(sig, 0.0008, 0.0015)
        prev = sig
        j = min(out.shape[1], i + g)
        out[:, i:j] += pan2(sig[: j - i], r.uniform(-0.8, 0.8))
        i = j + int(r.uniform(0, 0.012) * SR)
    return out * 0.6


def crt_on():
    t = tax(1.2)
    thump = np.sin(phase_of(70 + 40 * np.exp(-t / 0.03))) * np.exp(-t / 0.12)
    hum = fft_filter(np.sign(np.sin(2 * np.pi * 50 * t)), lp(900)) * np.exp(-t / 0.35) * 0.25
    stat = fft_filter(noise(len(t)), hp(1500)) * np.exp(-t / 0.08) * 0.5
    return thump + hum + stat


def clunk():
    t = tax(0.4)
    return (np.sin(phase_of(90 + 60 * np.exp(-t / 0.02))) * np.exp(-t / 0.07)
            + fft_filter(noise(len(t)), bp(800, 5000)) * np.exp(-t / 0.006) * 0.6)


def pop(f=500):
    t = tax(0.14)
    return np.sin(phase_of(f + f * 1.6 * (1 - np.exp(-t / 0.02)))) * np.exp(-t / 0.045) * 0.6


def flip():
    t = tax(0.5)
    s = np.zeros(len(t))
    for at, g in ((0, 1.0), (0.024, 0.6)):
        tt = np.maximum(t - at, 0)
        s += (t >= at) * g * fft_filter(noise(len(t)), hp(2200)) * np.exp(-tt / 0.003)
    s += np.sin(2 * np.pi * 140 * t) * np.exp(-t / 0.04) * 0.6
    for at, m in ((0.05, 88), (0.13, 93)):
        tt = np.maximum(t - at, 0)
        s += (t >= at) * np.sin(2 * np.pi * midi(m) * tt) * np.exp(-tt / 0.09) * 0.35
    return s


def scratch(dur=0.55):
    t = tax(dur)
    rate = 1.6 * np.sin(2 * np.pi * 3.6 * t) ** 2 + 0.05
    ph = phase_of(170 * rate)
    s = saw(ph, 170, fmax=4000, tilt=0.05) * 0.4 + noise(len(t)) * 0.3 * rate
    return fft_filter(s, bp(400, 4500)) * np.sin(np.pi * t / dur) ** 0.5 * 0.7


def stamp():
    return hit() * 1.0


# ---------------------------------------------------------------- music
AM, F, C, G = [57, 60, 64, 69], [53, 60, 65, 69], [55, 60, 64, 67], [55, 59, 62, 67]
PROG = [AM, F, C, G]
ROOTS = [33, 29, 36, 31]
CADD9 = [48, 55, 60, 62, 64, 67]
MOTIF = [(0.0, 67), (0.6, 72), (1.2, 74), (1.8, 76)]          # G C D E: the "SafeWrite" motif


def chord_at(t):
    return int(np.floor((t - GRID0) / BAR + 1e-6)) % 4


def arrangement(t):
    """What plays at time t inside the groove."""
    a = dict(kick=0, clap=0, hats=0, open=0, shaker=0, bass=0, pad=0, arp=0, bright=0.6, lead=0)
    if 34.0 <= t < 55.6:
        a.update(kick=1, bass=1, pad=1, hats=8)
        if t >= 38.8: a.update(clap=1)
        if t >= 43.6: a.update(arp=1, hats=16, open=1, bright=0.8)
    elif 58.0 <= t < 64.6:
        a.update(kick=1, bass=1, pad=1, hats=8, bright=0.7)
    elif 64.6 <= t < 71.2:
        a.update(kick=1, clap=1, bass=1, pad=1, hats=16, open=1, arp=1, bright=0.9)
    elif 71.2 <= t < 79.6:
        a.update(kick=1, clap=1, bass=1, pad=1, hats=16, open=1, shaker=1, arp=1, bright=1.0)
    elif 84.4 <= t < 95.8:
        a.update(kick=1, clap=1, bass=1, pad=1, hats=16, open=1, shaker=1, arp=1, lead=1, bright=1.0)
    return a


LEAD = [  # (beat within 4-bar phrase, midi, beats) over Am F C G
    (0, 76, 1.5), (1.5, 74, 0.5), (2, 72, 1), (3, 69, 1),
    (4, 72, 1.5), (5.5, 69, 0.5), (6, 72, 1), (7, 77, 1),
    (8, 76, 1.5), (9.5, 79, 0.5), (10, 76, 1), (11, 72, 1),
    (12, 74, 2), (14, 71, 1), (15, 74, 1),
]


def render_music(m):
    step = BEAT / 4
    kicks = []
    t = GRID0
    while t < 96.4 - 1e-6:
        a = arrangement(t + 1e-4)
        s16 = int(round((t - GRID0) / step))
        beat_i, sub = divmod(s16, 4)
        in_bar = beat_i % 4
        if a["kick"] and sub == 0:
            m.add(t, kick(), 0.62, room=0.06)
            kicks.append(t)
        if a["clap"] and sub == 0 and in_bar in (1, 3):
            m.add(t, clap(), 0.7, pan=0.05, hall=0.18, room=0.25)
        if a["hats"] == 8 and sub == 2:
            m.add(t, hat(), 0.34, pan=0.25, room=0.1)
        if a["hats"] == 16:
            m.add(t, hat(), (0.34 if sub == 2 else 0.17) * (1.25 if t >= 71.2 else 1.0), pan=0.25, room=0.1)
        if a["open"] and sub == 2 and in_bar in (1, 3):
            m.add(t, hat(True), 0.16, pan=-0.2, room=0.1)
        if a["shaker"] and sub in (1, 3):
            m.add(t, shaker(), 0.26, pan=-0.35)
        ci = chord_at(t + 1e-4)
        if a["bass"] and sub in (0, 2):
            vel = [1, .62, .85, .62, 1, .62, .85, .7][(in_bar * 2 + sub // 2) % 8]
            m.add(t, bass_note(midi(ROOTS[ci] + 12), 0.26, vel), 0.36, bus="duck")
        if a["pad"] and sub == 0 and in_bar == 0:
            m.add(t, pad_chord(PROG[ci], BAR, a["bright"]), 0.42, bus="duck", hall=0.35)
        if a["arp"]:
            pat = [0, 1, 2, 3, 2, 1, 3, 2]
            note = PROG[ci][pat[s16 % 8]] + 12
            p = pluck(midi(note), 0.35, a["bright"])
            g = 0.13 * (0.8 + 0.2 * (sub == 0))
            m.add(t, p, g, pan=-0.3, bus="duck")
            m.add(t + 3 * step, p, g * 0.45, pan=0.5, bus="duck", hall=0.2)     # ping-pong echo
            m.add(t + 6 * step, p, g * 0.2, pan=-0.5, bus="duck", hall=0.2)
        if a["lead"] and sub == 0 and in_bar == 0:
            for bt, nt, ln in LEAD:
                if not ci * 4 <= bt < ci * 4 + 4:
                    continue
                tl = t + (bt - ci * 4) * BEAT
                if tl < 95.8:
                    m.add(tl, plus(pluck(midi(nt), ln * BEAT + 0.4, 1.0), 0.5 * bell(midi(nt), ln * BEAT + 0.4)), 0.26, pan=0.1, hall=0.3)
        t += step

    # snare roll + filter rise into the proof section (the "build")
    t, k = 55.6, 0
    while t < 58.0 - 1e-6:
        p = (t - 55.6) / 2.4
        m.add(t, clap(), 0.12 + 0.3 * p, pan=0.05 * np.sin(k), room=0.3)
        t += BEAT / (2 if p < 0.5 else 4 if p < 0.85 else 8)
        k += 1
    m.add(55.6, pad_chord(AM, 2.4, 0.25), 0.22, hall=0.4)
    for tt in (55.6, 56.2, 56.8, 57.4):
        m.add(tt, bass_note(midi(45), 0.12, 0.6), 0.2)

    # sidechain: duck pad/bass/arp under each kick
    g = np.ones(m.n)
    env = 1 - 0.62 * np.exp(-tax(0.32) / 0.11)
    for tk in kicks:
        i = int(tk * SR)
        j = min(m.n, i + len(env))
        g[i:j] = np.minimum(g[i:j], env[: j - i])
    return g


def render_score(tl):
    m = Mix(tl["duration"])
    cues = tl["cues"]

    # room tone under the cold open
    t = tax(10.2)
    m.add(0, fft_filter(noise(len(t)), lp(700)) * 0.012 * np.minimum(t / 1.5, 1), 1.0)

    # rising drone (5.4 -> 10.2), then the power cut: its pitch collapses, everything stops
    t = tax(6.0)
    f_env = np.where(t < 4.8, 1.0, np.exp(-(t - 4.8) * 3.4))
    amp = np.where(t < 4.8, (np.minimum(t / 4.8, 1)) ** 2.2, np.exp(-(t - 4.8) * 4.0))
    drone = np.zeros(len(t))
    for mm, g in ((33, 1.0), (40, 0.6), (45, 0.5), (52, 0.25)):
        f0 = midi(mm)
        for det in (-6, 6):
            drone += g * saw(phase_of(f0 * 2 ** (det / 1200) * f_env), f0, fmax=3000, tilt=0.12)
    drone = stft_filter(drone, lambda tt, f: lp(np.where(tt < 4.8, 180 + 2400 * np.clip(tt / 4.8, 0, 1) ** 2, 2600 * np.exp(-(tt - 4.8) * 5)), 2)(f))
    drone *= amp * (t < 5.33)
    m.add(5.4, fade(drone, 0.01, 0.12), 0.16, hall=0.15)

    # eerie bed after the lights come back (11.4 -> 22)
    t = tax(10.8)
    bed = np.zeros(len(t))
    for f0, g in ((midi(33), 1.0), (midi(46), 0.45), (midi(52), 0.3)):
        bed += g * np.sin(phase_of(np.full(len(t), f0))) * (0.75 + 0.25 * np.sin(2 * np.pi * 0.23 * t + f0))
    glass = (np.sin(2 * np.pi * 1760 * t) + np.sin(2 * np.pi * 1767.5 * t)) * 0.04 * (0.5 + 0.5 * np.sin(2 * np.pi * 0.15 * t))
    air = stft_filter(noise(len(t)), band_sweep(lambda tt: 700 + 350 * np.sin(2 * np.pi * 0.11 * tt), 0.7)) * 0.35
    air *= 0.6 + 0.4 * np.sin(2 * np.pi * 0.31 * t)
    cluster = sum(np.sin(2 * np.pi * midi(mm) * t + mm) for mm in (69, 70, 76)) * 0.05 * (0.6 + 0.4 * np.sin(2 * np.pi * 0.2 * t))
    bed = (bed * 0.5 + glass * 2.0 + air + cluster) * np.minimum(t / 1.2, 1)
    bed[-int(0.3 * SR):] *= np.linspace(1, 0.3, int(0.3 * SR))
    m.add(11.4, bed, 0.2, hall=0.4)
    # sub stays under the stakes cards and dies into the riser
    t = tax(5.6)
    m.add(22.0, np.sin(2 * np.pi * midi(33) * t) * np.minimum(t / 0.1, 1) * np.clip((5.6 - t) / 1.0, 0, 1), 0.07)

    # tension pulse (16.2 -> 22): 8th-note bass pulse that grows
    tt = 16.2
    while tt < 22.0 - 1e-6:
        p = (tt - 16.2) / 5.8
        m.add(tt, bass_note(midi(45), 0.16, 0.5 + 0.5 * p), 0.10 + 0.22 * p)
        if int(round((tt - 16.2) / 0.3)) % 2 == 1:
            m.add(tt, hat(), 0.04 + 0.08 * p, pan=0.3)
        tt += 0.3

    # reveal: C add9 swell, then the motif on bells
    m.add(29.2, pad_chord(CADD9, 4.0, 0.8, attack=0.05, release=1.6), 0.55, hall=0.55)
    m.add(29.2, pad_chord([36, 43], 4.0, 0.3, attack=0.02, release=1.2), 0.45)
    for bt, nt in MOTIF:
        m.add(29.8 + bt, bell(midi(nt + 12), 3.0), 0.22, pan=-0.3 + bt / 3, hall=0.5)

    side = render_music(m)

    # dropout (79.6 -> 84.4): a dark pad and a sub pedal, then a riser back in
    m.add(79.6, pad_chord(F, 2.4, 0.15, attack=0.02), 0.26, hall=0.5)
    m.add(82.0, pad_chord(G, 2.4, 0.2), 0.24, hall=0.5)
    t = tax(4.8)
    m.add(79.6, np.sin(2 * np.pi * midi(29) * t) * np.clip(t / 0.05, 0, 1) * np.clip((4.8 - t) / 0.3, 0, 1), 0.10)
    m.add(83.2, riser(1.2), 0.5, hall=0.2)

    # finale: the stop before the end chord, and the end chord itself
    m.add(95.4, crash(1.0)[:, ::-1], 0.5, hall=0.2)
    m.add(96.4, pad_chord(CADD9, 3.8, 0.9, attack=0.03, release=2.0), 0.6, hall=0.6)
    m.add(96.4, pad_chord([36, 43], 4.2, 0.3, attack=0.02, release=1.4), 0.5)
    m.add(96.4, sub_drop(70, 32, 3.5, 0.4), 0.55)
    for bt, nt in MOTIF:
        m.add(96.6 + bt, bell(midi(nt + 12), 3.2), 0.22, pan=-0.3 + bt / 3, hall=0.6)
    m.add(96.4, crash(4.0), 0.55, hall=0.3)

    # one-shots on the picture's cues
    gl = 0
    for c in cues:
        t, k, g = c["t"], c["kind"], c.get("gain", 1.0)
        if k == "key":
            m.add(t, key_click(c.get("soft")), 0.5 if c.get("soft") else 0.7, pan=rng.uniform(-0.2, 0.2), room=0.15)
        elif k == "heart":
            m.add(t, heart(), 0.75)
        elif k == "whoosh":
            m.add(t - 0.3, whoosh(0.75), 0.35, hall=0.1)
        elif k == "tick":
            m.add(t, tick(g), 0.55, pan=rng.uniform(-0.3, 0.3), hall=0.1)
        elif k == "powerdown":
            m.add(t + 0.45, clunk(), 0.6, room=0.3)
        elif k == "crt_on":
            m.add(t, crt_on(), 0.45, room=0.3)
        elif k == "glitch":
            m.add(t, glitch(c.get("dur", 0.32), gl), 0.32, hall=0.12)
            gl += 1
        elif k == "hit":
            m.add(t, hit(c.get("low", False)), 0.85 * g, hall=0.45)
        elif k == "riser":
            m.add(t, riser(c.get("dur", 1.5)), 0.55, hall=0.25)
        elif k == "boom":
            m.add(t, boom(), 1.0 * g, hall=0.5)
            m.add(t, crash(3.5), 0.5, hall=0.3)
        elif k == "shimmer":
            for i, nt in enumerate((84, 88, 91, 95, 96)):
                m.add(t + i * 0.075, bell(midi(nt), 2.0), 0.1, pan=-0.6 + i * 0.3, hall=0.7)
        elif k == "groove_start":
            m.add(t, crash(3.0), 0.45, hall=0.3)
            m.add(t, hit(), 0.6, hall=0.3)
        elif k == "pop":
            m.add(t, pop(480 + 90 * (gl % 4)), 0.4, pan=rng.uniform(-0.4, 0.4), hall=0.15)
            gl += 1
        elif k == "flip":
            m.add(t, flip(), 0.6, hall=0.25)
        elif k == "pluck":
            nt = [69, 72, 76, 81][c.get("note", 0) % 4]
            m.add(t, plus(pluck(midi(nt), 1.2, 1.2), 0.6 * bell(midi(nt), 1.5)), 0.28, pan=-0.45 + 0.3 * c.get("note", 0), hall=0.35)
        elif k == "scratch":
            m.add(t, scratch(), 0.35)
        elif k == "arp_rise":
            dur = c.get("dur", 6.0)
            n = int(dur / (BEAT / 4))
            for i in range(n):
                tt = t + i * BEAT / 4
                p = i / max(n - 1, 1)
                ch = PROG[chord_at(tt + 1e-4)]
                nt = ch[i % 4] + 12 * int(p * 2.99)
                m.add(tt, pluck(midi(nt), 0.4, 0.5 + p), 0.05 + 0.10 * p, pan=0.4 * np.sin(i * 0.9), hall=0.25)
            m.add(t + dur - 1.6, riser(1.6), 0.4, hall=0.2)
        elif k == "stamp":
            m.add(t, stamp(), 0.95, hall=0.45)
            m.add(t, crash(3.0), 0.5, hall=0.3)
        elif k == "groove_return":
            m.add(t, crash(3.0), 0.5, hall=0.3)
            m.add(t, hit(), 0.7, hall=0.4)
        elif k == "end_chord":
            pass                                 # placed above with the finale

    return m, side


def master(m, side):
    print("reverb...", flush=True)
    m.bus["dry"] += m.bus["duck"] * side
    hall = convolve(m.bus["hall"], make_ir(2.8, 3.2, 6500, 1))
    room = convolve(m.bus["room"], make_ir(0.7, 0.9, 9000, 2))
    x = m.bus["dry"] + 0.42 * hall + 0.3 * room
    x = fft_filter(x, master_eq)
    # the blackout after the power cut is true digital silence, reverb tails included
    a, b = int(10.78 * SR), int(11.4 * SR)
    ramp = int(0.03 * SR)
    x[:, a:a + ramp] *= np.linspace(1, 0, ramp)
    x[:, a + ramp:b] = 0
    return x


EQ_POINTS = [(10, -30), (25, -16), (40, -9), (60, -4.5), (90, -1.5), (140, 0), (400, -0.5), (1000, 0.5),
             (2500, 2.5), (5000, 3.0), (10000, 2.5), (15000, 0.5), (17500, -3), (19500, -24), (24000, -80)]


def master_eq(f):
    """Tame the sub (laptop and phone speakers can't play it anyway) and lift presence."""
    fx, db = zip(*EQ_POINTS)
    return 10 ** (np.interp(np.log10(np.maximum(f, 1)), np.log10(fx), db) / 20)


def movmin(x, w):
    """Centred moving minimum, window 2w+1."""
    xp = np.pad(x, (w, w), constant_values=1.0)
    return np.lib.stride_tricks.sliding_window_view(xp, 2 * w + 1).min(axis=1)


def movavg(x, w):
    """Centred moving average, window 2w+1."""
    c = np.concatenate([[0.0], np.cumsum(np.pad(x, (w, w), mode="edge"))])
    return (c[2 * w + 1:] - c[:-2 * w - 1]) / (2 * w + 1)


def true_peak_env(x, over=4, chunk=SR, pad=4096):
    """Per-sample peak of the 4x-oversampled signal (what a true-peak meter sees)."""
    n = x.shape[1]
    env = np.zeros(n)
    for a in range(0, n, chunk):
        lo, hi = max(0, a - pad), min(n, a + chunk + pad)
        seg = x[:, lo:hi]
        up = np.fft.irfft(np.fft.rfft(seg, axis=1), over * seg.shape[1], axis=1) * over
        pk = np.abs(up).max(axis=0).reshape(-1, over).max(axis=1)
        b = min(n, a + chunk)
        env[a:b] = np.maximum(pk[a - lo:b - lo], np.max(np.abs(x[:, a:b]), axis=0))
    return env


def limiter(x, ceiling=10 ** (-1.5 / 20), look=0.002, release=0.15, block=64):
    """Look-ahead peak limiter. Every sample's gain is at most what that sample needs
    (a moving minimum, smoothed inside its own window), and recovery is slow."""
    need = np.minimum(1.0, ceiling / np.maximum(true_peak_env(x), 1e-9))
    L = int(look * SR)
    g = movavg(movmin(need, L), L // 2)
    nb = len(g) // block
    bm = g[: nb * block].reshape(nb, block).min(axis=1)
    rel = np.exp(-block / (release * SR))
    r = np.empty(nb)
    cur = 1.0
    for k in range(nb):
        cur = bm[k] if bm[k] < cur else bm[k] - (bm[k] - cur) * rel
        r[k] = cur
    slow = np.concatenate([np.repeat(r, block), np.full(len(g) - nb * block, r[-1] if nb else 1.0)])
    g = np.minimum(g, movavg(slow, block // 2))
    return x * g


def loudness(path):
    p = subprocess.run(["ffmpeg", "-nostats", "-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True)
    lines = [l for l in p.stderr.splitlines() if "I:" in l or "Peak:" in l]
    return " | ".join(l.strip() for l in lines[-2:])


def write_wav(path, x):
    x = x + (rng.random(x.shape) - rng.random(x.shape)) / 32768      # TPDF dither
    pcm = np.clip(np.round(x.T * 32767), -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def main(tl_path, out_path, target_lufs=-14.0):
    tl = json.load(open(tl_path))
    m, side = render_score(tl)
    x = master(m, side)
    # the end card fades to black over its last second; so does the sound
    n = x.shape[1]
    f0 = int((tl["duration"] - 1.6) * SR)
    x[:, f0:] *= np.linspace(1, 0, n - f0) ** 1.6
    # level: normalise to the loud sections, then limit; measure and trim once toward the target
    gain = 0.9 / np.percentile(np.abs(x), 99.95)
    for _ in range(2):
        y = limiter(x * gain)
        write_wav(out_path, y)
        info = loudness(out_path)
        try:
            lufs = float(info.split("I:")[1].split("LUFS")[0])
        except (IndexError, ValueError):
            break
        print(f"  integrated {lufs:.1f} LUFS ({info})")
        if abs(lufs - target_lufs) < 0.7:
            break
        gain *= 10 ** ((target_lufs - lufs) / 20)
    print("wrote", out_path)


if __name__ == "__main__":
    build = os.path.join(ROOT, "build")
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(build, "timeline.json"),
         sys.argv[2] if len(sys.argv) > 2 else os.path.join(build, "score.wav"))
