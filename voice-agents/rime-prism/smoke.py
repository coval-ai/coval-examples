"""Replay a 16 kHz PCM question through a running bridge with real playback pacing.

Usage: BRIDGE_TOKEN=... python smoke.py <question.pcm> [ws://127.0.0.1:8877/ws]
Writes the session report and the agent audio to SMOKE_OUTPUT_DIR (default: ./smoke-output).
"""

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect


async def main():
    audio = Path(sys.argv[1]).read_bytes()
    endpoint = sys.argv[2] if len(sys.argv) > 2 else "ws://127.0.0.1:8877/ws"
    sim = "local_smoke_" + str(int(time.time()))
    token = os.environ["BRIDGE_TOKEN"]
    output = bytearray()
    greeting_played = asyncio.Event()
    playback = asyncio.Queue()
    generation = 0
    clear_count = 0
    async with connect(
        endpoint, additional_headers={"Authorization": "Bearer " + token}
    ) as ws:
        await ws.send(json.dumps({"simulation_id": sim}))

        async def player():
            while True:
                gen, event = await playback.get()
                if gen != generation:
                    continue
                if event["type"] == "audio_chunk":
                    chunk = base64.b64decode(event["data"])
                    output.extend(chunk)
                    await asyncio.sleep(len(chunk) / 48000)
                else:
                    await ws.send(json.dumps(event))

        async def receive():
            nonlocal generation, clear_count
            async for raw in ws:
                e = json.loads(raw)
                if e["type"] == "audio_chunk":
                    await playback.put((generation, e))
                    greeting_played.set()
                elif e["type"] == "mark":
                    await playback.put((generation, e))
                elif e["type"] == "clear":
                    generation += 1
                    clear_count += 1
                elif e["type"] == "error":
                    print(e)

        tasks = [asyncio.create_task(player()), asyncio.create_task(receive())]
        await asyncio.wait_for(greeting_played.wait(), 20)
        await asyncio.sleep(7)
        for i in range(0, len(audio), 1280):
            await ws.send(
                json.dumps(
                    {
                        "type": "audio_chunk",
                        "data": base64.b64encode(audio[i : i + 1280]).decode(),
                    }
                )
            )
            await asyncio.sleep(0.04)
        await asyncio.sleep(18)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(1)
    base = (
        endpoint.replace("wss://", "https://")
        .replace("ws://", "http://")
        .removesuffix("/ws")
    )
    async with httpx.AsyncClient(timeout=25) as client:
        data = (
            await client.get(
                base + "/sessions/" + sim, headers={"Authorization": "Bearer " + token}
            )
        ).json()
    target = Path(os.getenv("SMOKE_OUTPUT_DIR", "smoke-output"))
    target.mkdir(parents=True, exist_ok=True)
    (target / (sim + ".json")).write_text(json.dumps(data, indent=2))
    import wave

    with wave.open(str(target / (sim + ".wav")), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(24000)
        f.writeframes(output)
    print(
        json.dumps(
            {
                "simulation_id": sim,
                "audio_seconds": len(output) / 48000,
                "turns": data.get("turns"),
                "tools": data.get("tools"),
                "errors": data.get("errors"),
                "playback_acks": data.get("playback_acks"),
                "clears": clear_count,
            },
            indent=2,
        )
    )


asyncio.run(main())
