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
    x = tick(5800, 100, 3.5)
    echo = int(SR * 0.045)
    x[echo:] += 0.12 * x[: len(x) - echo]
    return fade_edges(x)


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
    """A bright major-chord bloom: bell notes rolled upward, a soft shimmer, a long tail."""
    t = t_axis(1800)
    x = np.zeros(len(t))
    notes = [587.33, 739.99, 880.0, 1174.66, 1479.98]  # D major, rolled
    for k, f in enumerate(notes):
        start = 0.04 * k
        tt = np.clip(t - start, 0, None)
        on = (t >= start).astype(float)
        env = on * np.minimum(1.0, tt / 0.015) * np.exp(-tt / 0.55)
        tone = np.sin(2 * math.pi * f * tt) + 0.3 * np.sin(2 * math.pi * 2 * f * tt) + 0.08 * np.sin(2 * math.pi * 2.76 * f * tt)
        x += tone * env * (0.9 - 0.08 * k)
    shimmer = biquad(RNG.standard_normal(len(t)), "bp", 7500, q=1.5) * np.exp(-t / 0.12) * np.minimum(1.0, t / 0.01)
    x += 0.35 * shimmer
    swell = np.interp(t, [0, 0.21, 1.8], [0.55, 1.0, 1.0])
    x = fade_edges(x * swell, ms=20)
    # Let the bell tail ring on and fade smoothly to silence (it used to stop
    # at 1.8 s while still audible). The first 1.2 s are unchanged.
    t2 = t_axis(2600)
    tail = np.zeros(len(t2))
    for k, f in enumerate(notes):
        tt = np.clip(t2 - 0.04 * k, 0, None)
        tone = np.sin(2 * math.pi * f * tt) + 0.3 * np.sin(2 * math.pi * 2 * f * tt) + 0.08 * np.sin(2 * math.pi * 2.76 * f * tt)
        tail += tone * np.exp(-tt / 0.55) * (0.9 - 0.08 * k)
    # By 1.2 s the shimmer has died away and the sound is just these bell
    # partials, so switching to `tail` there is seamless.
    cross = int(SR * 1.2)
    out = np.concatenate([x[:cross], tail[cross:]])
    fade = np.interp(t2, [0, 1.2, 2.6], [1.0, 1.0, 0.0]) ** 2
    return out * fade


if __name__ == "__main__":
    # Levels (peak dBFS) set so each sound's perceived loudness (ITU-R BS.1770
    # K-weighted, over its audible part) matches the console's own sound for
    # the same action: tap -25.7, back -27.2, swipe -23.3, peel -35.4 and
    # open -34.9 LUFS-style units.
    save("tap.wav", tap(), -14.7)
    save("back.wav", back(), -18.4)
    save("swipe.wav", swipe(), -12.2)
    save("peel.wav", peel(), -15.9)
    save("open.wav", open_chime(), -19.8)
    print("wrote", os.path.abspath(OUT))
