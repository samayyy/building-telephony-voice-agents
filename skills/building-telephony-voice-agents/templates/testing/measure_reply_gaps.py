#!/usr/bin/env python3
"""Measure reply gaps, talk-over and greeting timing from a two-channel call recording, using energy only.

Why
---
SDK latency metrics on realtime models are unreliable (LiveKit's realtime ttft reads ~0 s every turn because it is
measured from the first server message, which already carries audio). The recording itself is the ground truth:
per-channel speech on/off times give the caller-end -> agent-start gap for every turn, independent of any SDK.
In one production deployment this showed the greeting starting 1.2-2.5 s after recording start on every call and
proved a hang-up on a talking caller (caller speech 503.47-504.85 s, close at ~506.2 s).

What a recording measures
-------------------------
* LiveKit RecorderIO (session.start(record=...)) records the agent's own input/output: stereo, caller ch0, agent
  ch1. Gaps exclude the PSTN/SIP legs (add roughly 0.1-0.3 s each way for what the caller experiences).
* Carrier dual-channel recordings include the network; check which channel is which.

Reports
-------
greeting_onset (first agent speech), greeting_duration, reply gaps (caller stops -> agent starts) with p50/p90/max,
caller response gaps (agent stops -> caller starts: how long callers think; calibrate your hang-up grace with it),
talk-over events (agent starts while the caller is speaking), gaps above --long-gap, and an echo indicator (correlation
of caller-channel energy with agent energy, and the estimated echo return loss). PSTN lines carry the agent's own
echo back on the caller channel. If talk-over events line up with agent speech and the echo correlation is high,
rerun with --echo-margin 6: caller frames during agent speech then only count as speech when they are 6 dB above the
estimated echo level. It is opt-in because genuine double-talk lowers the estimate and can hide real talk-over;
listen to a flagged moment before trusting either reading.

Usage
-----
  python measure_reply_gaps.py call1.ogg call2.ogg               # stereo, caller=ch0, agent=ch1
  python measure_reply_gaps.py rec.wav --caller-channel 1 --agent-channel 0 --json gaps.json
  python measure_reply_gaps.py --caller-file caller.wav --agent-file agent.wav

Tuning: --thr-db auto = 10th-percentile noise floor + 15 dB (min -50 dBFS). Agent segments closer than --merge-agent
seconds are one turn (mid-sentence pauses). Raise --min-silence if callers' word gaps split turns.
Requirements: numpy, ffmpeg on PATH.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

import numpy as np

RATE = 16000


def decode(path: str, channel: int | None) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-i", path]
    if channel is not None:
        cmd += ["-af", f"pan=mono|c0=c{channel}"]
    cmd += ["-ac", "1", "-ar", str(RATE), "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768


def envelope_db(x: np.ndarray, win: float) -> np.ndarray:
    h = int(RATE * win)
    n = len(x) // h
    if n == 0:
        return np.zeros(0)
    frames = x[: n * h].reshape(n, h)
    return 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)


def threshold(db: np.ndarray, thr_db: float | None) -> tuple[float, float]:
    floor = float(np.percentile(db, 10)) if len(db) else -90.0
    return floor, (max(floor + 15, -50.0) if thr_db is None else thr_db)


def segments(mask: np.ndarray, win: float, min_silence: float, min_speech: float):
    out, start, last = [], None, None
    for i, active in enumerate(mask):
        t = i * win
        if active:
            if start is None:
                start = t
            last = t + win
        elif start is not None and t - last > min_silence:
            if last - start >= min_speech:
                out.append((round(start, 2), round(last, 2)))
            start = None
    if start is not None and last - start >= min_speech:
        out.append((round(start, 2), round(last, 2)))
    return out


def merge(segs, gap: float):
    out = []
    for s, e in segs:
        if out and s - out[-1][1] <= gap:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def pct(vals, q):
    return round(float(np.percentile(vals, q)), 2) if vals else None


def analyse(caller: np.ndarray, agent: np.ndarray, args) -> dict:
    cdb, adb = envelope_db(caller, args.win), envelope_db(agent, args.win)
    n = min(len(cdb), len(adb))
    cdb, adb = cdb[:n], adb[:n]
    thr = None if args.thr_db == "auto" else float(args.thr_db)
    cfloor, cthr = threshold(cdb, thr)
    afloor, athr = threshold(adb, thr)
    a_active = adb > athr
    c_active = cdb > cthr
    erl = None
    both = a_active & c_active
    if args.echo_margin > 0 and both.sum() > 20:
        # Echo return loss: how far below the agent the caller channel sits while both are "active" during agent
        # speech (mostly echo, since callers are usually silent then). While the agent is active, caller frames must
        # beat the expected echo level by --echo-margin dB to count as caller speech. If the caller channel is
        # quiet whenever the agent talks, there is no audible echo and nothing is suppressed.
        erl = float(np.median(adb[both] - cdb[both]))
        c_active &= ~a_active | (cdb > adb - erl + args.echo_margin)
    cseg = segments(c_active, args.win, args.min_silence, args.min_speech)
    aseg_raw = segments(a_active, args.win, args.min_silence, args.min_speech)
    aturns = merge(aseg_raw, args.merge_agent)

    reply_gaps, talk_over, long_gaps, caller_gaps = [], [], [], []
    for k, (a_start, a_end) in enumerate(aturns):
        prev_agent_end = aturns[k - 1][1] if k else 0.0
        # caller speech since the agent last spoke, ending before this agent turn started
        spoke = [c for c in cseg if c[1] > prev_agent_end and c[0] < a_start]
        if not spoke:
            continue
        last_c = spoke[-1]
        if last_c[1] > a_start:                       # caller still talking when the agent started
            talk_over.append({"agent_start": a_start, "caller_segment": list(last_c)})
            continue
        gap = round(a_start - last_c[1], 2)
        reply_gaps.append(gap)
        if gap > args.long_gap:
            long_gaps.append({"caller_end": last_c[1], "agent_start": a_start, "gap": gap})
    for k, (a_start, a_end) in enumerate(aturns):
        nxt = [c for c in cseg if c[0] >= a_end and (k + 1 == len(aturns) or c[0] < aturns[k + 1][0])]
        if nxt:
            caller_gaps.append(round(nxt[0][0] - a_end, 2))

    echo_corr = None
    if a_active.sum() > 20:
        echo_corr = round(float(np.corrcoef(cdb[a_active], adb[a_active])[0, 1]), 2)
    return {
        "duration_s": round(len(agent) / RATE, 1),
        "caller_floor_db": round(cfloor, 1), "caller_thr_db": round(cthr, 1),
        "agent_floor_db": round(afloor, 1), "agent_thr_db": round(athr, 1),
        "greeting_onset_s": aturns[0][0] if aturns else None,
        "greeting_duration_s": round(aturns[0][1] - aturns[0][0], 2) if aturns else None,
        "agent_turns": len(aturns), "caller_segments": len(cseg),
        "reply_gap_p50": pct(reply_gaps, 50), "reply_gap_p90": pct(reply_gaps, 90),
        "reply_gap_max": max(reply_gaps) if reply_gaps else None, "reply_gaps": reply_gaps,
        "caller_response_gap_p50": pct(caller_gaps, 50), "caller_response_gap_p90": pct(caller_gaps, 90),
        "talk_over": talk_over, "long_gaps": long_gaps,
        "echo_corr_caller_vs_agent": echo_corr,
        "echo_return_loss_db": round(erl, 1) if erl is not None else None,  # only with --echo-margin
        "caller_segments_list": cseg if args.verbose else None,
        "agent_turns_list": aturns if args.verbose else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recordings", nargs="*")
    ap.add_argument("--caller-channel", type=int, default=0)
    ap.add_argument("--agent-channel", type=int, default=1)
    ap.add_argument("--caller-file", default="")
    ap.add_argument("--agent-file", default="")
    ap.add_argument("--win", type=float, default=0.02, help="energy window (s)")
    ap.add_argument("--thr-db", default="auto")
    ap.add_argument("--min-silence", type=float, default=0.35)
    ap.add_argument("--min-speech", type=float, default=0.15)
    ap.add_argument("--merge-agent", type=float, default=0.8)
    ap.add_argument("--long-gap", type=float, default=2.0)
    ap.add_argument("--echo-margin", type=float, default=0.0,
                    help="opt-in: dB above the estimated agent echo that caller audio needs during agent speech "
                         "(try 6 when talk-over events look like echo; 0 = off)")
    ap.add_argument("--json", default="")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    jobs = []
    if args.caller_file and args.agent_file:
        jobs.append((f"{args.caller_file}+{args.agent_file}", decode(args.caller_file, None),
                     decode(args.agent_file, None)))
    for path in args.recordings:
        jobs.append((path, decode(path, args.caller_channel), decode(path, args.agent_channel)))
    if not jobs:
        sys.exit("give stereo recordings, or --caller-file and --agent-file")

    results, all_gaps = {}, []
    for name, caller, agent in jobs:
        r = analyse(caller, agent, args)
        results[name] = r
        all_gaps += r["reply_gaps"]
        print(f"{name}: greeting at {r['greeting_onset_s']}s for {r['greeting_duration_s']}s | "
              f"reply gap p50 {r['reply_gap_p50']} p90 {r['reply_gap_p90']} max {r['reply_gap_max']} "
              f"(n={len(r['reply_gaps'])}) | caller think p50 {r['caller_response_gap_p50']} | "
              f"talk-over {len(r['talk_over'])} | gaps>{args.long_gap}s {len(r['long_gaps'])} | "
              f"echo corr {r['echo_corr_caller_vs_agent']} ERL {r['echo_return_loss_db']} dB")
        for g in r["long_gaps"]:
            print(f"    long gap {g['gap']}s: caller ended {g['caller_end']}s, agent started {g['agent_start']}s")
        for t in r["talk_over"]:
            print(f"    talk-over: agent started {t['agent_start']}s during caller speech {t['caller_segment']}")
    if len(jobs) > 1 and all_gaps:
        print(f"ALL: reply gap p50 {pct(all_gaps, 50)} p90 {pct(all_gaps, 90)} n={len(all_gaps)}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=1)


if __name__ == "__main__":
    main()
