"""A Pipecat transport over one Coval Agent SDK AudioConnection.

Both directions are 16 kHz mono PCM16, the SDK's fixed format, so no resampling is needed.

Input and output run independently. The SDK fails a conversation whose unread input passes
64 KiB, so caller audio is drained into a local buffer as it arrives. Coval only sends audio while
the simulated caller is speaking, but speech-to-text with turn detection (Flux) measures pauses on
a continuous stream, so a 20 ms clock feeds the pipeline from that buffer and fills silence in
between, the way a phone line would. Output is paced by awaited send_audio() calls.
"""

import asyncio
import time

from coval_agent_sdk import AudioConnection, CovalError
from loguru import logger
from pipecat.frames.frames import EndWorkerFrame, InputAudioRawFrame, OutputAudioRawFrame, StartFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

SAMPLE_RATE = 16_000
FRAME_SECONDS = 0.02
FRAME_BYTES = int(SAMPLE_RATE * FRAME_SECONDS) * 2
# Buffered caller audio beyond this is sent at once rather than delayed further.
MAX_BUFFER_BYTES = 10 * FRAME_BYTES


class CovalAudioInput(BaseInputTransport):
    def __init__(self, connection: AudioConnection, params: TransportParams, **kwargs):
        super().__init__(params, **kwargs)
        self._connection = connection
        self._buffer = bytearray()
        self._tasks: list[asyncio.Task] = []

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)
        if not self._tasks:
            self._tasks = [self.create_task(self._receive()), self.create_task(self._clock())]

    async def _receive(self):
        try:
            async for pcm in self._connection.receive_audio():
                self._buffer.extend(pcm)
            # The iterator ends when Coval ends the conversation (the caller hung up).
            logger.info(f"Conversation {self._connection.simulation_id} ended")
        except CovalError as error:
            logger.error(f"Conversation {self._connection.simulation_id} failed: {error}")
        # Ask the pipeline to finish; it ends the conversation's trace spans as it shuts down.
        await self.push_frame(EndWorkerFrame())

    async def _clock(self):
        next_tick = time.monotonic()
        while True:
            take = FRAME_BYTES
            if len(self._buffer) > MAX_BUFFER_BYTES:
                take = len(self._buffer) - len(self._buffer) % FRAME_BYTES
            audio = bytes(self._buffer[:take]).ljust(FRAME_BYTES, b"\0")
            del self._buffer[:take]
            await self.push_audio_frame(
                InputAudioRawFrame(audio=audio, sample_rate=SAMPLE_RATE, num_channels=1)
            )
            next_tick = max(next_tick + FRAME_SECONDS, time.monotonic() - FRAME_SECONDS)
            await asyncio.sleep(max(0.0, next_tick - time.monotonic()))

    async def _stop_receiving(self):
        for task in self._tasks:
            await self.cancel_task(task)
        self._tasks = []

    async def stop(self, frame):
        await super().stop(frame)
        await self._stop_receiving()

    async def cancel(self, frame):
        await super().cancel(frame)
        await self._stop_receiving()


class CovalAudioOutput(BaseOutputTransport):
    def __init__(self, connection: AudioConnection, params: TransportParams, **kwargs):
        super().__init__(params, **kwargs)
        self._connection = connection

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        # send_audio paces playback to real time, so there is no simulated-playback sleep here.
        # It returns False once the conversation has ended, and raises if the connection failed;
        # either way the input side ends the pipeline.
        if self._connection.closed:
            return False
        try:
            return await self._connection.send_audio(frame.audio)
        except CovalError:
            return False


class CovalAudioTransport(BaseTransport):
    """Use input() first in the pipeline and output() after the TTS service."""

    def __init__(self, connection: AudioConnection):
        super().__init__()
        params = TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_enabled=True,
            audio_out_sample_rate=SAMPLE_RATE,
            # Send speech only. Coval's relay allows 64 KiB of agent audio that the simulator has
            # not yet read, so continuous silence at 32 KB/s would leave no room for a worker that
            # stalls for a moment; the simulator records the gaps between utterances itself.
            audio_out_auto_silence=False,
            audio_out_end_silence_secs=0,
        )
        self._input = CovalAudioInput(connection, params, name="CovalAudioInput")
        self._output = CovalAudioOutput(connection, params, name="CovalAudioOutput")

    def input(self) -> CovalAudioInput:
        return self._input

    def output(self) -> CovalAudioOutput:
        return self._output
