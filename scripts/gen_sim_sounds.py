"""Generate the Vita simulator's UI sounds (Assets/Sounds/sim/*.wav).

Every sound is synthesised from scratch here (sine partials, filtered noise and
envelopes) so the app ships only its own audio. Re-run after changing a recipe:

    python scripts/gen_sim_sounds.py
"""
import math
import os
import wave

import numpy as np

SR = 48_000
OUT = os.path.join(os.path.dirname(__file__), "..", "Assets", "Sounds", "sim")
RNG = np.random.default_rng(1729)  # fixed seed: the files are reproducible


def t_axis(ms):
    return np.arange(int(SR * ms / 1000)) / SR


def biquad(x, kind, f0, q=0.707):
    """RBJ cookbook low/high/band-pass biquad."""
    w = 2 * math.pi * f0 / SR
    a = math.sin(w) / (2 * q)
    c = math.cos(w)
    if kind == "lp":
        b = [(1 - c) / 2, 1 - c, (1 - c) / 2]
    elif kind == "hp":
        b = [(1 + c) / 2, -(1 + c), (1 + c) / 2]
    else:  # band-pass, 0 dB peak
        b = [a, 0.0, -a]
    a0, a1, a2 = 1 + a, -2 * c, 1 - a
    b0, b1, b2 = (v / a0 for v in b)
    a1, a2 = a1 / a0, a2 / a0
    y = np.zeros_like(x)
    x1 = x2 = y1 = y2 = 0.0
    for i, v in enumerate(x):
        o = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, v, y1, o
        y[i] = o
    return y


def fade_edges(x, ms=2):
    n = int(SR * ms / 1000)
    x[:n] *= np.linspace(0, 1, n)
    x[-n:] *= np.linspace(1, 0, n)
    return x


def save(name, x, peak_db):
    x = x / (np.abs(x).max() + 1e-12) * 10 ** (peak_db / 20)
    os.makedirs(OUT, exist_ok=True)
    with wave.open(os.path.join(OUT, name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def tick(freq, length_ms, tau_ms, body=0.35):
    """A glassy tick: a high sine pinged with a fast decay, plus a softer octave below."""
    t = t_axis(length_ms)
    x = np.sin(2 * math.pi * freq * t) * np.exp(-t / (tau_ms / 1000))
    x += body * np.sin(2 * math.pi * freq / 2 * t) * np.exp(-t / (tau_ms * 1.8 / 1000))
    click = RNG.standard_normal(len(t)) * np.exp(-t / 0.0006) * 0.25
    return x + biquad(click, "hp", 4000)


def tap():
    """Shaped after the console's click (measured, not copied): a 6 kHz tone
    with its 3rd harmonic (18 kHz, -12 dB) dying away almost linearly in
    ~17 ms, then a faint 4 ms blip around 6-7 kHz at ~38 ms."""
    RNG.standard_normal(int(SR * 0.1))  # keep the shared noise stream as before (other sounds unchanged)
    own = np.random.default_rng(6000)
    t = t_axis(106)
    # A sharp first ~2 ms, then a lower body fading almost linearly.
    env = np.clip(1 - t / 0.0178, 0, None) ** 0.9 * (0.55 + 0.45 * np.exp(-t / 0.0012))
    x = (np.sin(2 * math.pi * 6000 * t) + 0.25 * np.sin(2 * math.pi * 18000 * t)) * env
    # A hint of very high 'tick' right at the start.
    x += 0.35 * biquad(own.standard_normal(len(t)), "hp", 12000) * np.exp(-t / 0.0008)
    blip_at, blip_len = int(SR * 0.038), int(SR * 0.004)
    blip = biquad(own.standard_normal(blip_len), "bp", 6300, q=2.0) * np.hanning(blip_len)
    x[blip_at:blip_at + blip_len] += 0.33 * blip / (np.abs(blip).max() + 1e-12)
    return fade_edges(x, ms=0.3)


def back():
    return fade_edges(tick(5300, 60, 3.0, body=0.3))


def swipe():
    """An airy rising 'shff': bright noise swelling over ~70 ms, then a quick cut."""
    t = t_axis(90)
    n = biquad(biquad(RNG.standard_normal(len(t)), "hp", 9000), "lp", 19000)
    n += 0.25 * biquad(RNG.standard_normal(len(t)), "bp", 10000, q=2.0)
    env = np.minimum(1.0, (t / 0.07) ** 1.6) * np.where(t > 0.072, np.exp(-(t - 0.072) / 0.006), 1.0)
    return fade_edges(n * env)


def peel():
    """Paper crinkle: sparse noise bursts in a 1.5-8 kHz band whose top slides down."""
    t = t_axis(230)
    bursts = np.zeros(len(t))
    pos = int(SR * 0.006)
    while pos < len(t):
        ln = int(SR * RNG.uniform(0.002, 0.006))
        seg = RNG.standard_normal(min(ln, len(t) - pos)) * np.exp(-np.arange(min(ln, len(t) - pos)) / (SR * 0.0015))
        bursts[pos:pos + len(seg)] += seg * RNG.uniform(0.4, 1.0)
        pos += int(SR * RNG.uniform(0.005, 0.016))
    half = len(t) // 2
    bright = biquad(bursts, "lp", 8000)
    dull = biquad(bursts, "lp", 4200)
    mix = np.concatenate([np.linspace(1, 0, half), np.zeros(len(t) - half)])
    x = biquad(bright * mix + dull * (1 - mix), "hp", 1500)
    env = np.interp(t, [0, 0.02, 0.115, 0.17, 0.23], [0.15, 0.4, 1.0, 0.55, 0.0])
    return fade_edges(x * env)


def open_chime():
    """The launch: shaped after the console's own (measured, not copied).

    - 0-0.25 s: a bright sparkle, a high ~7.3 kHz ring over airy 7-11 kHz noise;
    - from 0.1 s: a strong bell on E6 (~1.32 kHz), peaking around 0.2 s;
    - then an A major 6th chord (A, C#, E, F# over several octaves) whose notes
      enter one by one, rising, so the sound climbs as it fades;
    - ~1.85 s long, dying away smoothly over the last ~0.45 s.
    """
    t = t_axis(1850)

    def bell(f, start, attack, tau, overtone=0.18):
        tt = np.clip(t - start, 0, None)
        env = (t >= start) * np.minimum(1.0, tt / attack) * np.exp(-tt / tau)
        return (np.sin(2 * math.pi * f * tt) + overtone * np.sin(2 * math.pi * 2 * f * tt)) * env

    # Sparkle: the high ring and its airy noise, held ~0.2 s then gone.
    hold = np.interp(t, [0, 0.004, 0.2, 0.45], [0, 1, 1, 0])
    sparkle = 0.55 * np.sin(2 * math.pi * 7336 * t) * hold
    sparkle += 0.6 * biquad(biquad(RNG.standard_normal(len(t)), "hp", 7000), "lp", 11000) * hold
    # The main bell, with a softer note a third below.
    main = bell(1318.5, 0.10, 0.012, 0.32) + 0.35 * bell(1108.7, 0.10, 0.012, 0.28)
    main *= np.interp(t, [0, 0.1, 0.2, 0.3, 1.85], [0, 0.8, 1.0, 0.5, 0.5])
    # The rising A6 chord: notes enter in order, low to high.
    chord = [440.0, 659.3, 740.0, 880.0, 1108.7, 1318.5, 1480.0, 1760.0, 2217.5, 2637.0, 2960.0, 3520.0]
    pad = np.zeros(len(t))
    for k, f in enumerate(chord):
        pad += bell(f, 0.22 + 0.09 * k, 0.05, 0.8, overtone=0.1) * (0.55 - 0.035 * k)
    x = sparkle + main + 0.55 * pad
    fade = np.interp(t, [0, 1.5, 1.85], [1.0, 1.0, 0.0])
    return fade_edges(x * fade)


if __name__ == "__main__":
    # Levels (peak dBFS) set so each sound's perceived loudness (ITU-R BS.1770
    # K-weighted, over its audible part) matches the console's own sound for
    # the same action: tap -25.7, back -27.2, swipe -23.3, peel -35.4 and
    # open -34.9 LUFS-style units.
    save("tap.wav", tap(), -12.6)
    save("back.wav", back(), -18.4)
    save("swipe.wav", swipe(), -12.2)
    save("peel.wav", peel(), -15.9)
    save("open.wav", open_chime(), -19.8)
    print("wrote", os.path.abspath(OUT))
