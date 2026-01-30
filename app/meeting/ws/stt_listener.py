"""
STT Translation Redis Subscriber

This module subscribes to the stt:translations Redis channel and
broadcasts STT translation events to WebSocket clients.
"""
import asyncio
import json
from typing import Optional

from redis import asyncio as aioredis

from app.core.config import settings
from app.core.logging import get_logger
from app.meeting.ws.manager import manager

logger = get_logger(__name__)

STT_TRANSLATION_CHANNEL = "stt:translations"

# Global task reference for the subscriber
_subscriber_task: Optional[asyncio.Task] = None


async def _stt_translation_listener() -> None:
    """
    Listen for STT translation events from Redis and broadcast to WebSocket clients.
    """
    redis_url = settings.redis_url
    if not redis_url:
        logger.warning("Redis URL not configured, STT translation listener disabled")
        return

    logger.info(f"Starting STT translation listener on channel: {STT_TRANSLATION_CHANNEL}")

    while True:
        try:
            redis = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
            pubsub = redis.pubsub()
            await pubsub.subscribe(STT_TRANSLATION_CHANNEL)
            logger.info(f"Subscribed to {STT_TRANSLATION_CHANNEL}")

            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                
                try:
                    data = json.loads(message.get("data") or "{}")
                except json.JSONDecodeError:
                    continue

                room_id = data.get("room_id")
                msg_type = data.get("type", "translation")
                payload = data.get("data", {})

                if not room_id:
                    continue

                # Broadcast to all WebSocket clients in this room
                broadcast_message = {
                    "type": msg_type,
                    "data": payload
                }
                
                await manager.broadcast(room_id, broadcast_message)
                logger.debug(
                    f"STT Translation broadcast | Room: {room_id} | "
                    f"Speaker: {payload.get('speaker', 'Unknown')}"
                )

        except asyncio.CancelledError:
            logger.info("STT translation listener cancelled")
            break
        except Exception as exc:
            logger.error(f"STT translation listener error: {exc!r}")
            await asyncio.sleep(5)  # Retry after delay
        finally:
            try:
                await pubsub.close()
                await redis.close()
            except:
                pass


async def start_stt_translation_listener() -> None:
    """
    Start the STT translation listener as a background task.
    Should be called during application startup.
    """
    global _subscriber_task
    if _subscriber_task is None or _subscriber_task.done():
        _subscriber_task = asyncio.create_task(_stt_translation_listener())
        logger.info("STT translation listener task started")


async def stop_stt_translation_listener() -> None:
    """
    Stop the STT translation listener.
    Should be called during application shutdown.
    """
    global _subscriber_task
    if _subscriber_task and not _subscriber_task.done():
        _subscriber_task.cancel()
        try:
            await _subscriber_task
        except asyncio.CancelledError:
            pass
        logger.info("STT translation listener task stopped")
