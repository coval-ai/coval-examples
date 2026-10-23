"""Drive one scripted call against a deployed (or local) agent the way Coval's WebSocket simulator does.

Speaks each caller line (synthesized with Workers AI aura-2) as real-time 16 kHz PCM JSON frames,
waits for the agent to finish replying, and reports how long each reply took to start.

  python3 scripts/fake_coval_call.py wss://coval-demo-voice-agents.<subdomain>.workers.dev retail \\
      --org my-retail-org "Hi, is the B M twenty two forty one in stock?" "Thanks, bye."

Environment: CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN (Workers AI, for the caller's voice),
AGENT_AUTH_TOKEN (the Worker secret). Agent audio is saved to scripts/out/<slug>-agent.wav.
"""

import argparse
import asyncio
import base64
import json
import os
import time
import urllib.request
import wave
from pathlib import Path

import websockets

OUT = Path(__file__).resolve().parent / "out"
CALLER_RATE = 16000
AGENT_RATE = 48000
FRAME = 640  # 20 ms of 16 kHz PCM16
SILENCE = b"\x00" * FRAME


def synthesize(text: str) -> bytes:
    account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/deepgram/aura-2-en"
    body = {
        "text": text,
        "speaker": "orion",
        "encoding": "linear16",
        "sample_rate": CALLER_RATE,
        "container": "none",
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    return urllib.request.urlopen(request, timeout=60).read()


async def run(base: str, slug: str, org: str, lines: list[str]) -> None:
    audio = [synthesize(line) for line in lines]
    url = f"{base.rstrip('/')}/agents/{slug}/ws?org={org}"
    headers = {"Authorization": f"Bearer {os.environ.get('AGENT_AUTH_TOKEN', '')}"}
    received = bytearray()
    last_agent_audio = 0.0
    closed = asyncio.Event()

    async with websockets.connect(url, additional_headers=headers, max_size=None) as ws:
        await ws.send(json.dumps({"simulation_id": "local-fake-coval"}))

        async def reader() -> None:
            nonlocal last_agent_audio
            try:
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get("action") == "audio_message":
                        chunk = base64.b64decode(message["audio_bytes"])
                        received.extend(chunk)
                        if any(chunk):
                            last_agent_audio = time.time()
            except websockets.ConnectionClosed as exc:
                print(f"  agent closed the call: {exc.code} {exc.reason}")
            closed.set()

        async def send_frames(pcm: bytes) -> None:
            for offset in range(0, len(pcm), FRAME):
                if closed.is_set():
                    return
                frame = pcm[offset : offset + FRAME].ljust(FRAME, b"\x00")
                message = {
                    "action": "audio_message",
                    "audio_bytes": base64.b64encode(frame).decode(),
                    "sender": "USER",
                }
                try:
                    await ws.send(json.dumps(message))
                except websockets.ConnectionClosed:
                    closed.set()
                    return
                await asyncio.sleep(0.02)

        async def wait_for_reply(timeout: float = 60.0) -> float | None:
            began = time.time()
            heard = None
            while time.time() - began < timeout and not closed.is_set():
                await send_frames(SILENCE * 5)
                if last_agent_audio > began and heard is None:
                    heard = last_agent_audio - began
                if heard is not None and time.time() - last_agent_audio > 1.5:
                    return heard
            return heard

        reading = asyncio.create_task(reader())
        greeting = await wait_for_reply()
        print(f"greeting heard after {greeting:.2f}s" if greeting else "no greeting")
        latencies = []
        for line, pcm in zip(lines, audio):
            if closed.is_set():
                break
            print(f"caller: {line}")
            await send_frames(pcm)
            latency = await wait_for_reply()
            latencies.append(latency)
            print(
                f"  agent audio began {latency:.2f}s after the caller stopped"
                if latency
                else "  no reply"
            )
        await asyncio.sleep(2)
        if not closed.is_set():
            await ws.close()
        reading.cancel()

    OUT.mkdir(exist_ok=True)
    with wave.open(str(OUT / f"{slug}-agent.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(AGENT_RATE)
        wav.writeframes(bytes(received))
    seconds = round(len(received) / (AGENT_RATE * 2), 1)
    print(json.dumps({"agent_audio_seconds": seconds, "latencies": latencies}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url", help="wss:// (or ws:// for local dev) base URL of the Worker")
    parser.add_argument("slug")
    parser.add_argument("lines", nargs="+")
    parser.add_argument("--org", default="")
    args = parser.parse_args()
    asyncio.run(run(args.base_url, args.slug, args.org, args.lines))


if __name__ == "__main__":
    main()
