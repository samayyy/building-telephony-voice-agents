"""Place an outbound phone call through LiveKit SIP.

Two ways, both shown:

  1. --mode agent  (recommended) Dispatch the agent into a new room with {"phone_number": ...} metadata. The
     agent (templates/livekit/agent_gemini_live.py) dials with create_sip_participant(wait_until_answered=True),
     handles busy/no-answer, and starts talking once the callee says hello.
  2. --mode direct  Dial from this script, and dispatch the agent only AFTER the callee answers. Useful when a
     backend owns the dialling logic (retries, AMD, campaign pacing). Trade-off: the callee hears silence until
     the agent joins (well under a second on a warm worker, 10-20 s on a cold LiveKit Cloud Build-plan agent),
     and the agent treats it like an inbound call (it plays GREETING_TEXT, so word it for outbound).

Env: LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET (api.LiveKitAPI reads them), SIP_OUTBOUND_TRUNK_ID.
Verified to construct against livekit-api 1.2.1 and 1.2.2; not run against a live trunk here.

Before dialling Indian numbers, read references/12-india-telephony-compliance.md (A2P declaration, 140/160
series, consent, DND, calling hours). Voicemail answers with 200 OK: wait_until_answered does not detect it.

Usage:
  python create_participant_example.py +91XXXXXXXXXX --agent my-agent
  python create_participant_example.py +91XXXXXXXXXX --agent my-agent --mode direct --caller-id +91YYYYYYYYYY
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid

from livekit import api

E164 = re.compile(r"^\+[1-9]\d{6,14}$")


async def via_agent(lkapi: api.LiveKitAPI, number: str, agent_name: str, extra: dict) -> None:
    room = f"out-{uuid.uuid4().hex[:12]}"
    dispatch = await lkapi.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
        agent_name=agent_name,
        room=room,
        metadata=json.dumps({"phone_number": number, **extra}),
    ))
    print(f"dispatched agent={agent_name} room={room} dispatch_id={dispatch.id}")
    # CLI equivalent:
    #   lk dispatch create --new-room --agent-name my-agent --metadata '{"phone_number": "+91XXXXXXXXXX"}'


async def direct(lkapi: api.LiveKitAPI, number: str, agent_name: str, trunk_id: str, caller_id: str | None,
                 extra: dict) -> None:
    room = f"out-{uuid.uuid4().hex[:12]}"
    identity = "sip-" + re.sub(r"\D", "", number)
    try:
        info = await lkapi.sip.create_sip_participant(api.CreateSIPParticipantRequest(
            sip_trunk_id=trunk_id,
            sip_call_to=number,
            sip_number=caller_id or "",  # caller ID; must be a number the trunk allows
            room_name=room,
            participant_identity=identity,
            participant_name="Callee",
            wait_until_answered=True,  # raises SipCallError on 486/603, 408/480, 5xx
            play_dialtone=False,
            # ringing_timeout=datetime.timedelta(seconds=30),  # max 80 s
            # max_call_duration=datetime.timedelta(minutes=15),
            # dtmf="ww1234#",                                    # dial-after-answer, w = 0.5 s
            # krisp_enabled=True,                                # trunk-side NC (Cloud); not with agent-side BVC
        ))
    except api.SipCallError as exc:
        print(f"call failed: {exc.sip_status_code} {exc.sip_status}", file=sys.stderr)
        try:
            await lkapi.room.delete_room(api.DeleteRoomRequest(room=room))  # tidy up the auto-created room
        except api.TwirpError:
            pass
        raise SystemExit(2) from exc
    print(f"answered room={room} participant={info.participant_identity} sip_call_id={info.sip_call_id}")
    # Dispatch WITHOUT phone_number so the agent does not dial again; it finds the SIP participant already active.
    await lkapi.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
        agent_name=agent_name, room=room, metadata=json.dumps({"direction": "outbound", **extra})))
    print(f"dispatched agent={agent_name} into room={room}")
    # CLI equivalent: lk sip participant create participant.json
    # {"sip_trunk_id": "ST_xxx", "sip_call_to": "+91...", "room_name": "out-1", "participant_identity": "sip-91...",
    #  "wait_until_answered": true}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("number", help="E.164 number to call, e.g. +91XXXXXXXXXX")
    parser.add_argument("--agent", default=os.getenv("AGENT_NAME", "my-agent"))
    parser.add_argument("--mode", choices=["agent", "direct"], default="agent")
    parser.add_argument("--trunk", default=os.getenv("SIP_OUTBOUND_TRUNK_ID", ""))
    parser.add_argument("--caller-id", default=None)
    parser.add_argument("--metadata", default="{}", help="extra JSON merged into the dispatch metadata")
    args = parser.parse_args()

    if not E164.match(args.number):
        parser.error("number must be E.164 with a leading +")
    extra = json.loads(args.metadata)
    async with api.LiveKitAPI() as lkapi:
        if args.mode == "agent":
            await via_agent(lkapi, args.number, args.agent, extra)
        else:
            if not args.trunk:
                parser.error("--trunk or SIP_OUTBOUND_TRUNK_ID is required for --mode direct")
            await direct(lkapi, args.number, args.agent, args.trunk, args.caller_id, extra)


if __name__ == "__main__":
    asyncio.run(main())
