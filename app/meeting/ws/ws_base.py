import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy import select

from app.core.token import verify_token
from app.models.room import RoomLiveSession
from app.meeting.ws.manager import manager
from app.meeting.ws.ws_message import handle_chat_message, handle_summary_request, handle_translate_and_broadcast
from app.meeting.ws.ws_ai import handle_ai_event

from app.infra.db import AsyncSessionLocal

router = APIRouter(prefix="/meeting", tags=["websocket"])

@router.websocket("/{room_id}")
async def meeting_websocket(
    websocket: WebSocket,
    room_id: str,
    token: Optional[str] = Query(None)
):
    """
    WebSocket endpoint for a live meeting session.
    Path: /meeting/{room_id}
    """
    # 1. Authenticate (optional but recommended for chat)
    user_id = None
    if token:
        user_id = verify_token(token)
    
    print(f"🔌 WS Connection Attempt | Room: {room_id} | User: {user_id or 'Anonymous'}")
    
    # 2. Check if room exists
    # Use room_id to check RoomLiveSession (in this app, typically room_id and session_id are reused)
    try:
        async with AsyncSessionLocal() as db_session:
            # First, check if the room exists
            from app.models.room import Room
            room_stmt = select(Room).where(Room.id == room_id)
            room_result = await db_session.execute(room_stmt)
            room = room_result.scalar_one_or_none()

            if not room:
                print(f"[WS] Room {room_id} not found. Closing with 1008.")
                await websocket.close(code=1008)
                return

            # Check for active session
            result = await db_session.execute(
                select(RoomLiveSession).where(RoomLiveSession.room_id == room_id, RoomLiveSession.status == "active")
            )
            live_session = result.scalar_one_or_none()
            
            # For this spec, we consider room_id as the primary identifier. 
            # If no active session, we might want to fail or auto-create depending on environment.
            # Spec says "room_id가 없거나 잘못되면 종료", so we focus on room check.
            session_id = live_session.id if live_session else room_id

    except Exception as db_err:
        print(f"[WS Warning] DB check failed for {room_id}: {db_err}")
        # In case of DB failure, if we want to be strict, we'd close, 
        # but for robustness during development, we use room_id as session_id
        session_id = room_id

    # 3. Handle connection via manager
    await manager.connect(session_id, websocket, user_id)
    
    try:
        # Send initial success message (Spec: room_connected)
        await websocket.send_json({
            "type": "room_connected",
            "data": {
                "room_id": room_id,
                "user_id": user_id
            }
        })

        while True:
            try:
                data = await websocket.receive_json()
                msg_type = data.get("type")
                # print(f"📥 Received: {msg_type} from {user_id or 'Anonymous'}")
            except Exception:
                break
            
            msg_type = data.get("type")
            
            if msg_type == "chat":
                # Spec: token 없거나 유효하지 않으면 chat 요청은 에러 응답
                if not user_id:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Authentication required for chat"
                    })
                    continue
                
                # Spec: text 비어 있으면 서버가 무시
                text = data.get("text")
                if not text or not text.strip():
                    continue

                # Spec: token 유효하지만 room 멤버가 아니면 chat 요청은 무시됨
                async with AsyncSessionLocal() as db_session:
                    from app.models.room import RoomMember
                    member_stmt = select(RoomMember).where(RoomMember.room_id == room_id, RoomMember.user_id == user_id)
                    member_result = await db_session.execute(member_stmt)
                    if not member_result.scalar_one_or_none():
                        print(f"[WS] User {user_id} is not a member of room {room_id}. Ignoring chat.")
                        continue

                # Save and Broadcast Chat
                print(f"💬 Chat from {user_id}: {text[:50]}{'...' if len(text) > 50 else ''}")
                await handle_chat_message(session_id, user_id, data)
                
                # Background Translation
                import asyncio
                asyncio.create_task(
                     handle_translate_and_broadcast(
                         session_id, 
                         text, 
                         data.get("lang", "Korean")
                     )
                )

            elif msg_type == "ping":
                # Spec: pong
                # print(f"🏓 Ping from {user_id or 'Anonymous'}")
                await websocket.send_json({"type": "pong"})

            elif msg_type == "translation":
                # (Existing logic for internal translation events)
                payload = data.get("data", {})
                if isinstance(payload, dict) and (payload.get("translated_text") or payload.get("translatedText")):
                     await handle_ai_event(session_id, user_id or "agent_transcriber", data)
                else:
                    text = data.get("text")
                    if text:
                         await handle_translate_and_broadcast(session_id, text, data.get("source_lang", "ja"))

            elif msg_type == "explanation":
                 await handle_ai_event(session_id, user_id or "agent_transcriber", data)
            
            elif msg_type == "summary":
                await handle_summary_request(session_id, data)
            
            else:
                # Spec: unknown_type
                await websocket.send_json({
                    "type": "unknown_type",
                    "received": data
                })

    except WebSocketDisconnect:
        manager.disconnect(session_id, websocket, user_id)
    except Exception as e:
        print(f"WebSocket error in {session_id}: {e}")
        manager.disconnect(session_id, websocket, user_id)
        try:
            await websocket.close(code=1011)
        except:
            pass
