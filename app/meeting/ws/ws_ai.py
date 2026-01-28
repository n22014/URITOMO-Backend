import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select, func
from app.infra.db import AsyncSessionLocal
from app.models.ai import AIEvent
from app.models.room import RoomMember, RoomLiveSession
from app.meeting.ws.manager import manager

async def handle_ai_event(session_id: str, user_id: str, data: dict):
    """
    Handle incoming AI events (translation, explanation):
    1. Validate data
    2. Try Save to DB (Skip if test session or DB error)
    3. Broadcast to all session members
    """
    event_type = data.get("type") # translation | explanation
    print(f"🤖 handle_ai_event called | Type: {event_type} | Session: {session_id} | User: {user_id}")

    # Standardize event_type for DB
    db_event_type = event_type
    if event_type == "explanation":
        db_event_type = "intent" # Or add 'explanation' to AIEvent.event_type if needed

    broadcast_payload = {
        "id": data.get("id") or f"ai_{uuid.uuid4().hex[:8]}",
        "type": event_type,
        "data": data.get("data") or data, # Support nested data or flat
        "created_at": datetime.utcnow().isoformat() + "Z"
    }

    # ★Stability Fix: If debug session, we broadcast AND persist to global_debug_room
    # (Adapted from test-jo logic, but keeping it flexible)
    DEBUG_SESSION_IDS = ["test_session_1", "1", "debug"]
    is_debug = session_id in DEBUG_SESSION_IDS or (session_id.isdigit() and int(session_id) < 100)
    
    room_id = "global_debug_room" if is_debug else None
    
    if is_debug:
        print(f"💡 Debug session {session_id}: Persisting {event_type} to {room_id}")

    try:
        async with AsyncSessionLocal() as db_session:
            # 1. Get Session and Room ID if not debug
            if not room_id:
                session_result = await db_session.execute(
                    select(RoomLiveSession).where(RoomLiveSession.id == session_id)
                )
                live_session = session_result.scalar_one_or_none()
                if not live_session:
                    print(f"⚠️ Session {session_id} not found in DB during AI handler")
                    # Even if session not found in DB (should be rare due to ws_base auto-create),
                    # we broadcast to connected clients
                    await manager.broadcast(session_id, {"type": event_type, "data": broadcast_payload})
                    return
                room_id = live_session.room_id

            # 2. Get next sequence number for AI events in this room
            seq_result = await db_session.execute(
                select(func.max(AIEvent.seq)).where(AIEvent.room_id == room_id)
            )
            max_seq = seq_result.scalar() or 0
            next_seq = max_seq + 1

            # 3. Create AIEvent
            ai_data = data.get("data") or data
            
            new_event = AIEvent(
                id=broadcast_payload["id"],
                room_id=room_id,
                seq=next_seq,
                event_type=db_event_type,
                created_at=datetime.utcnow()
            )

            if event_type == "translation":
                new_event.original_text = ai_data.get("originalText") or ai_data.get("original_text")
                new_event.original_lang = ai_data.get("originalLang") or ai_data.get("original_lang")
                new_event.translated_text = ai_data.get("translatedText") or ai_data.get("translated_text")
                new_event.translated_lang = ai_data.get("translatedLang") or ai_data.get("translated_lang")
                # meta for speaker info
                new_event.meta = {"speaker": ai_data.get("speaker")}
            elif event_type == "explanation":
                new_event.text = ai_data.get("explanation")
                new_event.meta = {
                    "term": ai_data.get("term"),
                    "detectedFrom": ai_data.get("detectedFrom")
                }

            db_session.add(new_event)
            await db_session.commit()
            
            broadcast_payload.update({
                "seq": next_seq,
                "created_at": new_event.created_at.isoformat() + "Z"
            })
            print(f"✅ AI Event saved to DB | ID: {new_event.id} | Type: {event_type}")

    except Exception as e:
        print(f"❌ Database error in AI event handler: {e}. Falling back to broadcast only.")
    
    # 4. Broadcast (Final)
    await manager.broadcast(session_id, {"type": event_type, "data": broadcast_payload})
