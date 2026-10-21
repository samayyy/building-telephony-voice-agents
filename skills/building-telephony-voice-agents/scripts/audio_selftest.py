#!/usr/bin/env python3
"""audio_selftest.py - self-test of templates/ws_bridge/telephony_audio.py.

Checks: G.711 mu-law/A-law numpy codecs are bit-exact with audioop; G.711 round-trip SNR; streaming
resampler quality (with out-of-band content, as Gemini's 24 kHz voice has) and true delay; per-provider
end-to-end loopback (Gemini 24k -> wire frames -> back to 16k) with exact frame sizes; pacer timing; CPU cost.

Run:
    python -m venv .venv && .venv/bin/pip install numpy scipy soxr audioop-lts   # audioop-lts only on >= 3.13
    .venv/bin/python scripts/audio_selftest.py            # from the skill root
Exit code 1 if any hard check fails.
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "templates", "ws_bridge"))
import telephony_audio as ta  # noqa: E402

try:
    import soxr  # noqa: F401
    HAVE_SOXR = True
except ImportError:
    HAVE_SOXR = False

FAILS: list[str] = []


def rep(name: str, val, ok: bool | None = None) -> None:
    flag = "" if ok is None else ("  ok" if ok else "  FAIL")
    if ok is not None and not ok:
        FAILS.append(name)
    print(f"{name:72s} {val}{flag}")


def b(x: np.ndarray) -> bytes:
    return x.astype("<i2").tobytes()


def a(bts: bytes) -> np.ndarray:
    return np.frombuffer(bts, dtype="<i2")


def tone(rate: int, secs: float = 2.0, freqs=(440.0, 1000.0, 2500.0), oob=()) -> np.ndarray:
    t = np.arange(int(rate * secs)) / rate
    x = sum(0.2 * np.sin(2 * np.pi * f * t) for f in freqs)
    x = x + sum(0.15 * np.sin(2 * np.pi * f * t) for f in oob if f < rate / 2)
    return (x * 32767 * 0.9 / max(1.0, np.abs(x).max())).astype(np.int16)


def snr(ref: np.ndarray, test: np.ndarray, rate: int, trim: float = 0.1, maxlag: int = 400) -> float:
    """Best SNR over integer lags with least-squares gain, edges trimmed."""
    ref = ref.astype(np.float64)
    test = test.astype(np.float64)
    n = min(len(ref), len(test))
    s = int(trim * rate)
    r = ref[s:n - s]
    best = -1e9
    for lag in range(-maxlag, maxlag + 1):
        lo, hi = s + lag, n - s + lag
        if lo < 0 or hi > len(test):
            continue
        tt = test[lo:hi]
        g = np.dot(r, tt) / max(np.dot(tt, tt), 1e-9)
        e = r - g * tt
        best = max(best, 10 * np.log10(np.dot(r, r) / max(np.dot(e, e), 1e-9)))
    return best


def stream(resampler, x: np.ndarray, in_rate: int, chunk_ms: int = 20) -> bytes:
    ch = in_rate * chunk_ms // 1000
    out = b"".join(resampler.process(b(x[i:i + ch])) for i in range(0, len(x), ch))
    return out + resampler.flush()


def main() -> int:
    print(f"python {sys.version.split()[0]}  numpy {np.__version__}  audioop {'yes' if ta.audioop else 'no'}"
          f"  soxr {'yes' if HAVE_SOXR else 'no'}")
    print("\n== 1. G.711 codecs ==")
    allpcm = np.arange(-32768, 32768, dtype=np.int16)
    codes = bytes(range(256))
    if ta.audioop is not None:
        au = ta.audioop
        rep("mu-law encode numpy == audioop (all 65536 int16)",
            ta.pcm16_to_ulaw_np(b(allpcm)) == au.lin2ulaw(b(allpcm), 2), ta.pcm16_to_ulaw_np(b(allpcm)) == au.lin2ulaw(b(allpcm), 2))
        rep("mu-law decode numpy == audioop (all 256 codes)",
            ta.ulaw_to_pcm16_np(codes) == au.ulaw2lin(codes, 2), ta.ulaw_to_pcm16_np(codes) == au.ulaw2lin(codes, 2))
        rep("A-law encode numpy == audioop (all 65536 int16)",
            ta.pcm16_to_alaw_np(b(allpcm)) == au.lin2alaw(b(allpcm), 2), ta.pcm16_to_alaw_np(b(allpcm)) == au.lin2alaw(b(allpcm), 2))
        rep("A-law decode numpy == audioop (all 256 codes)",
            ta.alaw_to_pcm16_np(codes) == au.alaw2lin(codes, 2), ta.alaw_to_pcm16_np(codes) == au.alaw2lin(codes, 2))
    else:
        print("audioop not installed: skipping bit-exactness (numpy tables are used)")
    x8 = tone(8000)
    v = snr(x8, a(ta.ulaw_to_pcm16(ta.pcm16_to_ulaw(b(x8)))), 8000)
    rep("SNR dB  mu-law 8k encode->decode (G.711 quantisation ceiling)", round(v, 1), v > 33)
    v = snr(x8, a(ta.alaw_to_pcm16(ta.pcm16_to_alaw(b(x8)))), 8000)
    rep("SNR dB  A-law 8k encode->decode", round(v, 1), v > 33)

    print("\n== 2. Streaming resamplers, 20 ms chunks, input has out-of-band tones when downsampling ==")
    kinds = [("LowLatency FIR", ta.LowLatencyResampler)]
    if HAVE_SOXR:
        kinds.append(("soxr HQ", lambda i, o: ta.StreamResampler(i, o, "HQ")))
    if ta.audioop is not None:
        kinds.append(("audioop.ratecv", ta.AudioopResampler))
    for label, mk in kinds:
        for fi, fo in ((8000, 16000), (24000, 8000), (24000, 16000), (16000, 24000)):
            # downsampling: add two tones above the output Nyquist (they must be filtered out, not aliased)
            oob = (fo / 2 + 1500.0, fo / 2 + 3500.0) if fo < fi else ()
            y = a(stream(mk(fi, fo), tone(fi, oob=oob), fi))
            v = snr(tone(fo), y, fo)
            ok = (v > 75) if label == "LowLatency FIR" else None
            rep(f"SNR dB  {label:15s} {fi:>5}->{fo:<5}", round(v, 1), ok)

    print("\n== 3. True delay (noise cross-correlation), 20 ms chunks ==")
    from scipy.signal import correlate, resample_poly
    rng = np.random.default_rng(0)
    for label, mk in kinds:
        for fi, fo in ((8000, 16000), (24000, 8000), (24000, 16000)):
            n = fi * 2
            lo = min(fi, fo)
            x = resample_poly(rng.standard_normal(n * 2), lo, fi * 2)  # band-limit to the lower rate
            x = resample_poly(x, fi * 2, lo)[:n]
            xi = (x / np.abs(x).max() * 16000).astype(np.int16)
            ref = resample_poly(xi.astype(float), fo, fi)
            r = mk(fi, fo)
            ch = fi // 50
            out = b"".join(r.process(b(xi[i:i + ch])) for i in range(0, n, ch))
            y = a(out).astype(float)
            m = min(len(y), len(ref))
            c = correlate(y[:m], ref[:m], mode="full")
            lag = int(np.argmax(c)) - (m - 1)
            held = len(ref) - len(y)
            total = (lag + held) / fo * 1000
            ok = (total < 5) if label == "LowLatency FIR" else None
            rep(f"delay ms {label:15s} {fi:>5}->{fo:<5} (filter {lag / fo * 1000:4.1f} + held-back {held / fo * 1000:5.1f})",
                round(total, 1), ok)

    print("\n== 4. Provider loopback: Gemini 24k -> wire frames -> 16k (as if echoed back) ==")

    def pipeline(label: str, codec: ta.Codec, expect_bytes: int, min_snr: float, binary: bool = False) -> None:
        g24 = tone(24000)
        frames: list[bytes] = []
        for i in range(0, len(g24), 960):  # Gemini delivers variable chunks; use 40 ms
            frames += list(codec.from_gemini(b(g24[i:i + 960])))
        frames += codec.end_of_turn()
        sizes = sorted({len(f) for f in frames})
        wire = b"".join(frames)
        if not binary:  # JSON providers carry base64
            wire = base64.b64decode(base64.b64encode(wire))
        back_codec = ta.Codec(codec.wire, codec.wire_rate, codec.frame_ms)  # fresh inbound resampler
        back = b"".join(back_codec.to_gemini(wire[i:i + codec.frame_bytes]) for i in range(0, len(wire), codec.frame_bytes))
        v = snr(tone(16000), a(back), 16000)
        rep(f"E2E SNR dB {label:30s} frame sizes {sizes}", round(v, 1), sizes == [expect_bytes] and v > min_snr)

    pipeline("Twilio mu-law 8k (160 B)", ta.twilio_codec(), 160, 33)
    pipeline("Telnyx PCMA 8k (160 B)", ta.telnyx_codec("PCMA"), 160, 33)
    pipeline("Exotel slin 8k (3200 B)", ta.exotel_codec(8000), 3200, 75)
    pipeline("Exotel slin 16k (3200 B)", ta.exotel_codec(16000), 3200, 75)
    pipeline("Exotel slin 24k (4800 B)", ta.exotel_codec(24000), 4800, 75)
    pipeline("Plivo L16 16k (640 B)", ta.plivo_codec("audio/x-l16;rate=16000"), 640, 75)
    pipeline("Plivo mu-law 8k (160 B)", ta.plivo_codec("audio/x-mulaw;rate=8000"), 160, 33)
    pipeline("Vonage L16 16k (640 B, binary)", ta.vonage_codec(16000), 640, 75, binary=True)
    pipeline("Vonage L16 24k (960 B, binary)", ta.vonage_codec(24000), 960, 75, binary=True)
    for rate in (8000, 16000, 24000):
        ex = ta.exotel_codec(rate)
        ok = ex.frame_bytes % 320 == 0 and ex.frame_bytes >= 3200
        rep(f"Exotel {rate // 1000}k chunk bytes / ms (multiple of 320, >= 3200)",
            (ex.frame_bytes, round(ex.frame_seconds * 1000, 1)), ok)

    print("\n== 5. Pacing, base64 overhead, CPU ==")

    async def pace_test() -> float:
        p = ta.Pacer(frame_ms=20, lead_ms=200)
        t0 = time.monotonic()
        for _ in range(100):
            await p.wait_turn()
        return time.monotonic() - t0
    dt = asyncio.run(pace_test())
    rep("Pacer: wall time to send 2.0 s of 20 ms frames, 200 ms lead (expect ~1.8 s)", round(dt, 3), 1.7 < dt < 1.95)
    frame = bytes(160)
    msg = '{"event":"media","streamSid":"MZ00000000000000000000000000000000","media":{"payload":"%s"}}' % ta.b64e(frame)
    rep("Twilio 20 ms frame: payload bytes / JSON message bytes / kbit/s",
        (160, len(msg), round(len(msg) * 8 * 50 / 1000, 1)))
    g = b(tone(24000, secs=60))
    t0 = time.perf_counter()
    c = ta.twilio_codec()
    n = sum(1 for _ in c.from_gemini(g))
    rep("CPU s: 60 s of Gemini audio -> 3000 Twilio frames", (round(time.perf_counter() - t0, 4), n), n == 3000)
    caller = ta.pcm16_to_ulaw(b(tone(8000, secs=60)))
    t0 = time.perf_counter()
    c = ta.twilio_codec()
    for i in range(0, len(caller), 160):
        c.to_gemini(caller[i:i + 160])
    rep("CPU s: 60 s of caller mu-law -> Gemini 16k (3000 x 20 ms frames)", round(time.perf_counter() - t0, 4))

    print("\nRESULT:", "PASS" if not FAILS else f"FAIL ({len(FAILS)}): {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
