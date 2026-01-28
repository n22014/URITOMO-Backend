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
    session_id: str
):
    """
    WebSocket endpoint for a live meeting session.
    Accepts first for debuggability, then strictly validates.
    """
    # 1. 接続を一旦確立 (403を回避し、エラー内容をブラウザに送れるようにするため)
    await websocket.accept()
    
    headers = dict(websocket.headers)
    origin = headers.get("origin", "unknown")
    token = websocket.query_params.get("token")
    
    print(f"[WS Debug] Connection established. Session: {session_id} | Origin: {origin}")

    # 2. 認証チェック (トークンの検証)
    user_id = None
    if not token:
        await websocket.send_json({"type": "error", "code": "AUTH_REQUIRED", "message": "Authentication token missing"})
        await websocket.close(code=1008)
        return

    try:
        user_id = verify_token(token)
        if not user_id:
             await websocket.send_json({"type": "error", "code": "AUTH_FAILED", "message": "Invalid or expired token"})
             await websocket.close(code=1008)
             return
    except Exception as e:
        await websocket.send_json({"type": "error", "code": "AUTH_ERROR", "message": str(e)})
        await websocket.close(code=1008)
        return

    # 3. セッション存在チェック (データベース)
    try:
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(
                select(RoomLiveSession).where(RoomLiveSession.id == session_id)
            )
            live_session = result.scalar_one_or_none()
            
            if not live_session:
                await websocket.send_json({
                    "type": "error", 
                    "code": "SESSION_NOT_FOUND", 
                    "message": f"Session {session_id} is not registered in the system. Use startLiveSession API first."
                })
                await websocket.close(code=1008)
                return
    except Exception as db_err:
        await websocket.send_json({"type": "error", "code": "DB_ERROR", "message": f"Database check failed: {db_err}"})
        await websocket.close(code=1008)
        return

    # --- すべてのチェックを通過 ---
    await manager.add_connection(session_id, websocket, user_id)
    print(f"[WS Success] User {user_id} joined session {session_id}")
    
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
