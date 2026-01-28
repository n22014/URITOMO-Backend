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

@router.websocket("/{session_id}")
async def meeting_websocket(
    websocket: WebSocket,
    session_id: str,
    token: Optional[str] = Query(None)
):
    """
    WebSocket endpoint for a live meeting session.
    Path: /meeting/{session_id}
    """
    # 1. Authenticate (optional but recommended for chat)
    user_id = None
    if token:
        user_id = verify_token(token)
    
    # 2. Check if session exists
    # 2. Check if session exists (Try-Catch to prevent connection failure on DB error)
    try:
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(
                select(RoomLiveSession).where(RoomLiveSession.id == session_id)
            )
            live_session = result.scalar_one_or_none()
    
            if not live_session:
                # await websocket.close(code=1008) # Policy Violation
                # return
                
                # 開発用: セッションがなければ自動作成する
                # まずRoomがあるか確認
                from app.models.room import Room
                room_result = await db_session.execute(select(Room).where(Room.id == session_id)) # 簡易的にsession_id = room_idとする
                room = room_result.scalar_one_or_none()
                
                if not room:
                     print(f"[WS] Auto-creating Room {session_id}")
                     room = Room(
                         id=session_id, 
                         title=f"Room {session_id}", 
                         created_at=datetime.utcnow(),
                         created_by="system" # 必須カラム
                     )
                     db_session.add(room)
                
                print(f"[WS] Auto-creating LiveSession {session_id}")
                live_session = RoomLiveSession(
                    id=session_id, 
                    room_id=session_id, 
                    title=f"Session {session_id}", 
                    started_at=datetime.utcnow(),
                    status="active"
                )
                db_session.add(live_session)
                await db_session.commit()
    except Exception as db_err:
        print(f"[WS Warning] DB Session check failed for {session_id}: {db_err}")
        # DBなしでもチャット機能自体はオンメモリで動くので続行する

    # 3. Handle connection via manager
    await manager.connect(session_id, websocket, user_id)
    
    try:
        # Send initial success message
        await websocket.send_json({
            "type": "session_connected",
            "data": {
                "session_id": session_id,
                "user_id": user_id
            }
        })

        while True:
            # Receive message from client
            try:
                data = await websocket.receive_json()
            except Exception:
                # Invalid JSON
                break
            
            msg_type = data.get("type")
            
            if msg_type == "chat":
                if not user_id:
                    # await websocket.send_json({
                    #     "type": "error",
                    #     "message": "Authentication required for chat"
                    # })
                    # continue
                    
                    # 開発用: 認証なしでもチャット可能にする
                    user_id = f"debug_user_{session_id[-6:]}"
                
                # Save and Broadcast Chat
                await handle_chat_message(session_id, user_id, data)
                
                # Trigger Translation (Fire and Forget or Background Task)
                # Note: data.get("text") should exist if handle_chat_message succeeded conceptually, 
                # but better safely access it.
                chat_text = data.get("text")
                if chat_text:
                    import asyncio
                    # Run translation in background relative to WS loop response
                    asyncio.create_task(
                         handle_translate_and_broadcast(
                             session_id, 
                             chat_text, 
                             data.get("lang", "ja")
                         )
                    )

            elif msg_type == "translation":
                # Check if it's a pre-translated log from Agent or a translation request
                payload = data.get("data", {})
                # If it has translated_text, it's likely a log from the agent -> Save to AIEvent
                if isinstance(payload, dict) and (payload.get("translated_text") or payload.get("translatedText")):
                     await handle_ai_event(session_id, user_id or "agent_transcriber", data)
                else:
                    # Manual Request: Translate it (Keep existing logic for backward compatibility)
                    text = data.get("text")
                    if text:
                         await handle_translate_and_broadcast(session_id, text, data.get("source_lang", "ja"))

            elif msg_type == "explanation":
                 await handle_ai_event(session_id, user_id or "agent_transcriber", data)
            
            elif msg_type == "summary":
                print(f"[WS] Summary requested by {user_id}")
                await handle_summary_request(session_id, data)

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            
            else:
                # Default echo or unknown type
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
