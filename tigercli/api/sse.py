import json
from fastapi.responses import StreamingResponse


async def sse_stream(event_generator):
    async def generate():
        async for event_type, data in event_generator:
            yield sse_event(event_type, data)
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
