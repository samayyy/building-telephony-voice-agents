"""telephony_audio.py - audio conversion between telephony WebSocket streams and Gemini Live.

Gemini Live takes 16 kHz PCM16 LE mono in (``audio/pcm;rate=16000``) and returns 24 kHz PCM16 LE mono.
Telephony streams are G.711 mu-law / A-law at 8 kHz (Twilio, Plivo default, Telnyx PCMU/PCMA)
or linear PCM16 LE at 8/16/24 kHz (Exotel, Plivo L16, Vonage, Telnyx L16).

Tested (scripts/audio_selftest.py) with Python 3.14.5, numpy 2.5.4, scipy 1.18.1, soxr 1.1.0, audioop-lts 0.2.2:
  * numpy G.711 mu-law and A-law codecs are bit-exact with audioop for all 65,536 int16 inputs and 256 codes;
  * LowLatencyResampler: ~82-84 dB SNR (with out-of-band content) and ~0.5-1.5 ms delay at 20 ms chunks;
  * soxr ResampleStream HQ: ~80-81 dB but holds back ~12-43 ms (up to ~66 ms on a steady tone) when streamed;
  * audioop.ratecv aliases badly on 24 kHz -> 8 kHz (about 4.5-8 dB SNR when the input has energy above 4 kHz;
    4.5-15 dB across the ratios used here).

Dependencies: numpy, scipy (default resampler). Optional: soxr (StreamResampler), audioop-lts on Python >= 3.13
(import name ``audioop``; used for G.711 if present, otherwise the bit-exact numpy tables are used).
"""
from __future__ import annotations

import asyncio
import base64
import time
from math import gcd
from typing import Iterator, Optional, Protocol

import numpy as np

try:  # stdlib on <= 3.12, the audioop-lts package on >= 3.13
    import audioop  # type: ignore
except ImportError:  # pragma: no cover - numpy fallback below is bit-exact
    audioop = None

GEMINI_IN_RATE = 16_000   # audio/pcm;rate=16000, 16-bit LE mono
GEMINI_OUT_RATE = 24_000  # always 24 kHz 16-bit LE mono

ULAW_SILENCE = b"\xff"     # mu-law code for 0
ALAW_SILENCE = b"\xd5"     # A-law code for ~0
PCM16_SILENCE = b"\x00\x00"


# --------------------------------------------------------------------------- G.711 mu-law
_ULAW_BIAS, _ULAW_CLIP = 0x84, 32635


def _build_ulaw_decode_table() -> np.ndarray:
    u = ~np.arange(256, dtype=np.int32) & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    mag = (((mantissa << 3) + _ULAW_BIAS) << exponent) - _ULAW_BIAS
    return np.where(sign != 0, -mag, mag).astype(np.int16)


def _build_ulaw_encode_table() -> np.ndarray:
    # index = (pcm16 >> 2) + 8192 (14-bit), the same quantisation as audioop.lin2ulaw
    s = (np.arange(16384, dtype=np.int32) - 8192) << 2
    sign = np.where(s < 0, 0x80, 0)
    mag = np.minimum(np.abs(s), _ULAW_CLIP) + _ULAW_BIAS
    exponent = np.floor(np.log2(mag >> 7)).astype(np.int32).clip(0, 7)
    mantissa = (mag >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8)


_ULAW_DECODE = _build_ulaw_decode_table()
_ULAW_ENCODE = _build_ulaw_encode_table()


def ulaw_to_pcm16_np(ulaw: bytes) -> bytes:
    return _ULAW_DECODE[np.frombuffer(ulaw, dtype=np.uint8)].astype("<i2").tobytes()


def pcm16_to_ulaw_np(pcm: bytes) -> bytes:
    x = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
    return _ULAW_ENCODE[(x >> 2) + 8192].tobytes()


def ulaw_to_pcm16(ulaw: bytes) -> bytes:
    return audioop.ulaw2lin(ulaw, 2) if audioop is not None else ulaw_to_pcm16_np(ulaw)


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    return audioop.lin2ulaw(pcm, 2) if audioop is not None else pcm16_to_ulaw_np(pcm)


# --------------------------------------------------------------------------- G.711 A-law
def _build_alaw_decode_table() -> np.ndarray:
    a = np.arange(256, dtype=np.int32) ^ 0x55
    sign = a & 0x80
    exponent = (a >> 4) & 0x07
    mantissa = a & 0x0F
    mag = np.where(exponent == 0, (mantissa << 4) + 8, ((mantissa << 4) + 0x108) << np.maximum(exponent - 1, 0))
    return np.where(sign != 0, mag, -mag).astype(np.int16)


def _build_alaw_encode_table() -> np.ndarray:
    # index = (pcm16 >> 3) + 4096 (13-bit), the same quantisation as audioop.lin2alaw
    s = np.arange(8192, dtype=np.int32) - 4096
    mask = np.where(s >= 0, 0xD5, 0x55)
    mag = np.where(s >= 0, s, -s - 1)
    seg_end = np.array([0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF])
    seg = np.searchsorted(seg_end, mag)
    mant = np.where(seg < 2, (mag >> 1) & 0x0F, (mag >> np.maximum(seg, 1)) & 0x0F)
    code = np.where(seg >= 8, 0x7F, (np.minimum(seg, 7) << 4) | mant)
    return ((code ^ mask) & 0xFF).astype(np.uint8)


_ALAW_DECODE = _build_alaw_decode_table()
_ALAW_ENCODE = _build_alaw_encode_table()


def alaw_to_pcm16_np(alaw: bytes) -> bytes:
    return _ALAW_DECODE[np.frombuffer(alaw, dtype=np.uint8)].astype("<i2").tobytes()


def pcm16_to_alaw_np(pcm: bytes) -> bytes:
    x = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
    return _ALAW_ENCODE[(x >> 3) + 4096].tobytes()


def alaw_to_pcm16(alaw: bytes) -> bytes:
    return audioop.alaw2lin(alaw, 2) if audioop is not None else alaw_to_pcm16_np(alaw)


def pcm16_to_alaw(pcm: bytes) -> bytes:
    return audioop.lin2alaw(pcm, 2) if audioop is not None else pcm16_to_alaw_np(pcm)


# --------------------------------------------------------------------------- resamplers
class Resampler(Protocol):
    def process(self, pcm: bytes) -> bytes: ...
    def flush(self) -> bytes: ...


class LowLatencyResampler:
    """Stateful polyphase FIR resampler (scipy firwin + lfilter with carried state).

    Recommended default. Exact integer ratios used in telephony<->Gemini work: 8k->16k (x2), 24k->8k (/3),
    24k->16k (x2/3), 16k->24k (x3/2), 16k->8k (/2). Measured ~82-84 dB SNR and ~0.5-1.5 ms delay at 20 ms chunks,
    including when the input has energy above the output Nyquist (Gemini's 24 kHz voice does).
    One instance per call per direction. Keep it across barge-ins (it is just a filter delay line)."""

    def __init__(self, in_rate: int, out_rate: int, taps_per_phase: int = 24, beta: float = 8.0):
        from scipy.signal import firwin, lfilter  # local import: scipy is only needed for this class
        self._lfilter = lfilter
        g = gcd(in_rate, out_rate)
        self.L, self.M = out_rate // g, in_rate // g
        self.passthrough = self.L == self.M == 1
        if self.passthrough:
            return
        n = taps_per_phase * max(self.L, self.M)  # e.g. 24k->16k (L=2, M=3): 73 taps at 48 kHz
        n |= 1  # odd length, symmetric
        cutoff = 0.92 / max(self.L, self.M)  # fraction of the intermediate-rate Nyquist
        self.h = (firwin(n, cutoff, window=("kaiser", beta)) * self.L).astype(np.float64)
        self.zi = np.zeros(len(self.h) - 1)  # filter state at the intermediate (upsampled) rate
        self.phase = 0                       # next output index modulo M at the intermediate rate

    def process(self, pcm: bytes) -> bytes:
        if self.passthrough or not pcm:
            return pcm
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        up = np.zeros(len(x) * self.L)
        up[:: self.L] = x
        y, self.zi = self._lfilter(self.h, 1.0, up, zi=self.zi)
        out = y[self.phase :: self.M]
        self.phase = (self.phase - len(up)) % self.M
        return np.clip(np.rint(out), -32768, 32767).astype("<i2").tobytes()

    def flush(self) -> bytes:  # an FIR holds back nothing worth flushing (~1 ms of group delay)
        return b""


class StreamResampler:
    """soxr.ResampleStream wrapper. Excellent quality but, streamed in 20 ms chunks, HQ holds back ~12-43 ms
    (up to ~66 ms on a steady tone) and emits nothing for the first 2-6 chunks. Call flush() at end of turn if you use it."""

    def __init__(self, in_rate: int, out_rate: int, quality: str = "HQ"):
        self.passthrough = in_rate == out_rate
        if not self.passthrough:
            import soxr  # optional dependency
            self._rs = soxr.ResampleStream(in_rate, out_rate, 1, dtype="int16", quality=quality)

    def process(self, pcm: bytes) -> bytes:
        if self.passthrough or not pcm:
            return pcm
        return self._rs.resample_chunk(np.frombuffer(pcm, dtype="<i2")).astype("<i2").tobytes()

    def flush(self) -> bytes:
        if self.passthrough:
            return b""
        return self._rs.resample_chunk(np.zeros(0, dtype=np.int16), last=True).astype("<i2").tobytes()


class AudioopResampler:
    """audioop.ratecv (stateful, linear interpolation, NO anti-alias filter). Shown for comparison only:
    15 dB SNR upsampling a 2.5 kHz tone 8k->16k and ~8 dB downsampling 24k->8k with out-of-band content."""

    def __init__(self, in_rate: int, out_rate: int):
        if audioop is None:
            raise RuntimeError("audioop not available (pip install audioop-lts on Python >= 3.13)")
        self.in_rate, self.out_rate, self.state = in_rate, out_rate, None

    def process(self, pcm: bytes) -> bytes:
        if self.in_rate == self.out_rate or not pcm:
            return pcm
        out, self.state = audioop.ratecv(pcm, 2, 1, self.in_rate, self.out_rate, self.state)
        return out

    def flush(self) -> bytes:
        return b""


# --------------------------------------------------------------------------- framing
def frame_bytes(sample_rate: int, ms: int, bytes_per_sample: int) -> int:
    return sample_rate * ms // 1000 * bytes_per_sample


class FrameBuffer:
    """Accumulate bytes and emit fixed-size frames (e.g. 160 B mu-law = 20 ms at 8 kHz)."""

    def __init__(self, frame_bytes_: int):
        self.frame_bytes = frame_bytes_
        self._buf = bytearray()

    def push(self, data: bytes) -> Iterator[bytes]:
        self._buf.extend(data)
        while len(self._buf) >= self.frame_bytes:
            frame = bytes(self._buf[: self.frame_bytes])
            del self._buf[: self.frame_bytes]
            yield frame

    def flush(self, pad: Optional[bytes] = None) -> bytes:
        """Return the remainder; with ``pad`` (one silence sample), pad it up to a full frame."""
        rest = bytes(self._buf)
        self._buf.clear()
        if rest and pad is not None:
            rest += pad * ((self.frame_bytes - len(rest)) // len(pad))
        return rest

    def clear(self) -> None:  # barge-in
        self._buf.clear()

    def __len__(self) -> int:
        return len(self._buf)


# --------------------------------------------------------------------------- pacing
class Pacer:
    """Real-time sender: keeps at most ``lead_ms`` of audio queued at the provider.

    A small lead means a provider-side ``clear`` has little to drop (fast barge-in) and no bursts (Exotel samples
    report early hang-ups when bursting). Call reset() whenever you send ``clear``.
    Measured: 2.0 s of 20 ms frames with a 200 ms lead is sent in ~1.78 s."""

    def __init__(self, frame_ms: float, lead_ms: float = 200.0):
        self.frame_s, self.lead_s = frame_ms / 1000.0, lead_ms / 1000.0
        self.reset()

    def reset(self) -> None:
        self._t0: Optional[float] = None
        self._sent_s = 0.0

    def queued_s(self) -> float:
        """Approximate audio still queued (unplayed) at the provider."""
        if self._t0 is None:
            return 0.0
        return max(0.0, self._sent_s - (time.monotonic() - self._t0))

    async def wait_turn(self) -> None:
        now = time.monotonic()
        if self._t0 is None or self._sent_s < now - self._t0:  # idle gap: restart the clock
            self._t0, self._sent_s = now, 0.0
        ahead = self._sent_s - (now - self._t0)
        if ahead > self.lead_s:
            await asyncio.sleep(ahead - self.lead_s)
        self._sent_s += self.frame_s


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def b64d(s: str) -> bytes:
    return base64.b64decode(s)


# --------------------------------------------------------------------------- per-call codec
class Codec:
    """Per-call converter between a provider wire format and Gemini Live. One instance per call."""

    def __init__(self, wire: str, wire_rate: int, frame_ms: int, *, min_chunk_bytes: int = 0,
                 multiple_of: int = 0, resampler: Optional[type] = None):
        if wire not in ("ulaw", "alaw", "pcm16le"):
            raise ValueError(f"unsupported wire format {wire!r}")
        self.wire, self.wire_rate, self.frame_ms = wire, wire_rate, frame_ms
        bps = 2 if wire == "pcm16le" else 1
        fb = frame_bytes(wire_rate, frame_ms, bps)
        if multiple_of:
            fb = max(multiple_of, (fb // multiple_of) * multiple_of)
        self.frame_bytes = max(fb, min_chunk_bytes)
        mk = resampler or LowLatencyResampler
        self.up = mk(wire_rate, GEMINI_IN_RATE)     # caller -> Gemini
        self.down = mk(GEMINI_OUT_RATE, wire_rate)  # Gemini -> caller
        self.framer = FrameBuffer(self.frame_bytes)
        self.silence = {"ulaw": ULAW_SILENCE, "alaw": ALAW_SILENCE, "pcm16le": PCM16_SILENCE}[wire]

    @property
    def frame_seconds(self) -> float:
        bps = 2 if self.wire == "pcm16le" else 1
        return self.frame_bytes / bps / self.wire_rate

    def _decode(self, payload: bytes) -> bytes:
        if self.wire == "ulaw":
            return ulaw_to_pcm16(payload)
        if self.wire == "alaw":
            return alaw_to_pcm16(payload)
        return payload

    def _encode(self, pcm: bytes) -> bytes:
        if self.wire == "ulaw":
            return pcm16_to_ulaw(pcm)
        if self.wire == "alaw":
            return pcm16_to_alaw(pcm)
        return pcm

    def wire_to_pcm(self, payload: bytes) -> bytes:
        """Wire bytes -> PCM16 at the wire rate (for local VAD / level metering)."""
        return self._decode(payload)

    def to_gemini(self, payload: bytes) -> bytes:
        """Wire bytes -> PCM16 16 kHz for send_realtime_input."""
        return self.up.process(self._decode(payload))

    def from_gemini(self, pcm24k: bytes) -> Iterator[bytes]:
        """Gemini 24 kHz PCM16 -> complete wire frames (partial frame is kept until the next call)."""
        yield from self.framer.push(self._encode(self.down.process(pcm24k)))

    def end_of_turn(self) -> list[bytes]:
        """Flush resampler tail + partial frame (padded with silence) as whole frames."""
        frames: list[bytes] = []
        tail = self.down.flush()
        if tail:
            frames.extend(self.framer.push(self._encode(tail)))
        rest = self.framer.flush(pad=self.silence)
        if rest:
            frames.append(rest)
        return frames

    def silence_frame(self) -> bytes:
        return self.silence * (self.frame_bytes // len(self.silence))

    def barge_in(self) -> None:
        self.framer.clear()


# --------------------------------------------------------------------------- provider profiles
def twilio_codec(resampler: Optional[type] = None) -> Codec:      # audio/x-mulaw 8000; 20 ms = 160 B
    return Codec("ulaw", 8000, 20, resampler=resampler)


def exotel_codec(sample_rate: int = 16000, resampler: Optional[type] = None) -> Codec:
    # slin 16-bit LE; outbound chunks n*320 B, >= 3200 B, <= 100 KB. 100 ms chunks: 3200 B at 16k, 4800 B at 24k,
    # and at 8k the 3200 B minimum is 200 ms.
    return Codec("pcm16le", sample_rate, 100, min_chunk_bytes=3200, multiple_of=320, resampler=resampler)


def plivo_codec(content_type: str = "audio/x-l16;rate=16000", resampler: Optional[type] = None) -> Codec:
    if content_type.startswith("audio/x-mulaw"):
        return Codec("ulaw", 8000, 20, resampler=resampler)
    rate = int(content_type.split("rate=")[1]) if "rate=" in content_type else 8000
    return Codec("pcm16le", rate, 20, resampler=resampler)


def vonage_codec(sample_rate: int = 16000, resampler: Optional[type] = None) -> Codec:  # binary L16 frames
    return Codec("pcm16le", sample_rate, 20, resampler=resampler)


def telnyx_codec(codec: str = "PCMU", sample_rate: int = 8000, resampler: Optional[type] = None) -> Codec:
    # L16 byte order on Telnyx is unverified (RTP L16 is big-endian per RFC 3551): byteswap if it sounds like static.
    wire = {"PCMU": "ulaw", "PCMA": "alaw", "L16": "pcm16le"}[codec]
    return Codec(wire, sample_rate if wire == "pcm16le" else 8000, 20, resampler=resampler)


# --------------------------------------------------------------------------- levels / simple VAD
def rms_dbfs(pcm: bytes) -> float:
    """RMS level of PCM16 in dBFS (-inf for digital silence)."""
    if not pcm:
        return float("-inf")
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    r = float(np.sqrt(np.mean(x * x)))
    return 20 * np.log10(r / 32768.0) if r > 0 else float("-inf")


class EnergyVAD:
    """Very small energy VAD with hangover, for timers and greeting barge-in only (Gemini's server VAD owns turns).
    Replace with Silero/WebRTC VAD for production: line echo and noise can exceed a fixed threshold."""

    def __init__(self, threshold_dbfs: float = -38.0, hangover_s: float = 0.3):
        self.threshold, self.hangover = threshold_dbfs, hangover_s
        self.speaking = False
        self.speech_started_at: Optional[float] = None
        self.last_voice_at: float = 0.0

    def update(self, pcm: bytes, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        if rms_dbfs(pcm) > self.threshold:
            if not self.speaking:
                self.speaking, self.speech_started_at = True, now
            self.last_voice_at = now
        elif self.speaking and now - self.last_voice_at > self.hangover:
            self.speaking, self.speech_started_at = False, None
        return self.speaking

    def continuous_speech_s(self, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        return now - self.speech_started_at if self.speaking and self.speech_started_at else 0.0
