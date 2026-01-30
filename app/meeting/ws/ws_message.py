import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select, func
from app.infra.db import AsyncSessionLocal
from app.models.message import ChatMessage
from app.models.room import RoomMember, RoomLiveSession
from app.models.ai import AIEvent
from app.meeting.ws.manager import manager
from app.summarization.logic.meeting_data import fetch_meeting_transcript, format_transcript_for_ai
from app.summarization.logic.ai_summary import summarize_meeting, save_summary_to_db
from app.models.room import Room

from app.translation.deepl_service import deepl_service
from app.core.logging import get_logger

logger = get_logger(__name__)

async def handle_chat_message(session_id: str, user_id: str, data: dict):
    """
    Handle incoming chat message:
    1. Validate data
    2. Fetch RoomMember and latest sequence
    3. Save to DB (Original)
    4. Broadcast to all session members (Original)
    5. Perform Translation
    6. Save Translation Event
    7. Broadcast Translation
    """
    text = data.get("text")
    if not text:
        return
    
    async with AsyncSessionLocal() as db_session:
        # 1. Get Session and Room ID
        session_result = await db_session.execute(
            select(RoomLiveSession).where(RoomLiveSession.id == session_id)
        )
        live_session = session_result.scalar_one_or_none()
        
        if live_session:
            room_id = live_session.room_id
        else:
            # Fallback: check if session_id is actually a room_id
            room_result = await db_session.execute(
                select(Room).where(Room.id == session_id)
            )
            room = room_result.scalar_one_or_none()
            if room:
                room_id = room.id
            else:
                print(f"[WS Chat] Session/Room {session_id} not found.")
                return

        # 2. Get RoomMember ID for this user
        member_result = await db_session.execute(
            select(RoomMember).where(
                RoomMember.room_id == room_id,
                RoomMember.user_id == user_id
            )
        )
        member = member_result.scalar_one_or_none()
        
        # Auto-register user as RoomMember if not exists (for development convenience)
        if not member:
            from app.models.user import User
            user_result = await db_session.execute(
                select(User).where(User.id == user_id)
            )
            user = user_result.scalar_one_or_none()
            
            if not user:
                # print(f"[WS Chat] User {user_id} not found in database")
                # return
                # 開発用: Userがいなくてもダミーメンバーとして登録
                print(f"[WS Chat] User {user_id} not found. Creating dummy member.")
                display_name = f"Guest_{user_id[-6:]}"
            else:
                display_name = user.display_name or f"User_{user_id[:6]}"

            # Create RoomMember
            member = RoomMember(
                id=f"rm_{uuid.uuid4().hex[:16]}",
                room_id=room_id,
                user_id=user_id,
                display_name=display_name,
                role="member",
                joined_at=datetime.utcnow()
            )
            db_session.add(member)
            await db_session.flush()
            print(f"[WS Chat] Auto-registered user {user_id} as RoomMember in room {room_id}")

        # 3. Get next sequence number for this room
        seq_result = await db_session.execute(
            select(func.max(ChatMessage.seq)).where(ChatMessage.room_id == room_id)
        )
        max_seq = seq_result.scalar() or 0
        next_seq = max_seq + 1

        # 4. Create ChatMessage (Original)
        source_lang = data.get("lang", "Korean") # Default to Korean based on user context
        
        message_id = f"msg_{uuid.uuid4().hex[:16]}"
        new_message = ChatMessage(
            id=message_id,
            room_id=room_id,
            seq=next_seq,
            sender_type="human",
            sender_member_id=member.id,
            message_type="text",
            text=text,
            lang=source_lang,
            created_at=datetime.utcnow()
        )

        db_session.add(new_message)
        await db_session.commit()
        await db_session.refresh(new_message)

        # 5. Broadcast Original Message
        broadcast_data = {
            "type": "chat",
            "data": {
                "id": new_message.id,
                "room_id": new_message.room_id,
                "seq": new_message.seq,
                "sender_member_id": new_message.sender_member_id,
                "display_name": member.display_name,
                "text": new_message.text,
                "lang": new_message.lang,
                "created_at": new_message.created_at.isoformat()
            }
        }
        await manager.broadcast(session_id, broadcast_data)
        print(f"📡 Broadcast (Chat) | Room: {new_message.room_id} | Sender: {member.display_name}")

        # 6. Perform Translation (Sync/Async)
        # DeepL translate is synchronous in our service currently, but it's fine for now 
        # as we are in an async handler, though blocking the loop is not ideal if high load.
        # Ideally deepl_service should be async or run in executor.
        # For this implementation, we run it directly.
        
        target_lang = "Japanese" if "Korean" in source_lang else "Korean"
        
        try:
            translated_text = deepl_service.translate_text(
                text=text, 
                source_lang=source_lang, 
                target_lang=target_lang
            )
            
            # 7. Save Translation Event
            # Using specific ID format or UUID
            trans_id = f"trans_{uuid.uuid4().hex[:16]}"
            
            ai_event = AIEvent(
                id=trans_id,
                room_id=room_id,
                seq=next_seq, 
                event_type="translation",
                original_text=text,
                original_lang=source_lang,
                translated_text=translated_text,
                translated_lang=target_lang,
                meta={
                     "related_message_id": message_id,
                     "participant_id": user_id,
                     "participant_name": member.display_name
                },
                created_at=datetime.utcnow()
            )
            
            db_session.add(ai_event)
            await db_session.commit()
            
            # 8. Broadcast Translation (with message_type='chat' so frontend can filter)
            trans_broadcast_data = {
                "type": "translation",
                "data": {
                    "room_id": room_id,
                    "participant_id": user_id,
                    "participant_name": member.display_name,
                    "Original": text,
                    "original_text": text,  # Also include as original_text for consistency
                    "translated": translated_text,
                    "translated_text": translated_text,  # Also include as translated_text for consistency
                    "timestamp": ai_event.created_at.isoformat(),
                    "sequence": str(next_seq),
                    "lang": target_lang,
                    "message_type": "chat",  # Mark as chat translation (not STT)
                    "source_lang": source_lang,
                    "target_lang": target_lang,
                    "related_message_id": message_id,  # Link to original chat message
                }
            }
            
            await manager.broadcast(session_id, trans_broadcast_data)
            print(f"🌎 Broadcast (Translation) | From: {source_lang} -> To: {target_lang} | MsgID: {message_id}")
            
        except Exception as e:
            logger.error(f"Translation failed in websocket: {e}")
            # We don't fail the chat, just skip translation broadcast

async def handle_summary_request(session_id: str, data: dict):
    """
    Handle summary generation request via WebSocket
    """
    print(f"[WS Summary] Request received for session {session_id}")
    async with AsyncSessionLocal() as db_session:
        # Get Room ID from Session
        session_result = await db_session.execute(
            select(RoomLiveSession).where(RoomLiveSession.id == session_id)
        )
        live_session = session_result.scalar_one_or_none()
        
        if not live_session:
            print(f"[WS Summary] Session {session_id} not found.")
            return
        
        room_id = live_session.room_id
        
        # Room info
        room_stmt = select(Room).where(Room.id == room_id)
        room_result = await db_session.execute(room_stmt)
        room = room_result.scalar_one_or_none()
        
        if not room:
            print(f"[WS Summary] Room {room_id} not found.")
            return

        # Fetch Transcript
        transcript = await fetch_meeting_transcript(db_session, room_id)
        
        if not transcript:
            print(f"[WS Summary] No transcript found for room {room_id}")
            # Send empty summary notification
            await manager.broadcast(session_id, {
                "type": "summary",
                "data": {
                    "content": "まだ会話データがありません。チャットで会話してから試してください。",
                    "action_items": [],
                    "key_decisions": [],
                    "from_seq": 0,
                    "to_seq": 0,
                    "created_at": datetime.utcnow().isoformat()
                }
            })
            return

        print(f"[WS Summary] Generating summary for {len(transcript)} messages...")
        
        # Notify "Processing..."
        await manager.broadcast(session_id, {
            "type": "summary_status",
            "status": "processing",
            "message": "要約を生成しています..."
        })

        # Generate Summary
        formatted_text = format_transcript_for_ai(transcript)
        summary_dict = await summarize_meeting(formatted_text)
        
        # Save to DB
        summary_data_to_save = {
            "room_title": room.title,
            "processed_at": datetime.utcnow().isoformat(),
            "filtered_message_count": len(transcript),
            "summary": summary_dict
        }
        await save_summary_to_db(room_id, summary_data_to_save, db_session)

        # Broadcast Result
        from_seq = 0 
        to_seq = len(transcript)
        
        # Frontend expects arrays for action_items and key_decisions
        task = summary_dict.get("task", "")
        decided = summary_dict.get("decided", "")
        
        action_items = [task] if task else []
        key_decisions = [decided] if decided else []

        await manager.broadcast(session_id, {
            "type": "summary",
            "data": {
                "summary_id": f"sum_{uuid.uuid4().hex[:8]}",
                "content": summary_dict.get("main_point", ""),
                "action_items": action_items,
                "key_decisions": key_decisions,
                "from_seq": from_seq,
                "to_seq": to_seq,
                "created_at": datetime.utcnow().isoformat()
            }
        })
        print(f"[WS Summary] Summary broadcasted for session {session_id}")


async def handle_save_transcript(session_id: str, user_id: str, data: dict):
    """
    Handle incoming translation/transcript log (already translated by Agent):
    1. Save to DB (as ChatMessage with type='transcript')
    2. Broadcast to clients
    """
    payload = data.get("data", {}) # Agent sends data in 'data' field
    original_text = payload.get("original_text")
    translated_text = payload.get("translated_text")
    
    if not original_text:
        return

    async with AsyncSessionLocal() as db_session:
        # 1. Get Room Info
        session_result = await db_session.execute(
            select(RoomLiveSession).where(RoomLiveSession.id == session_id)
        )
        live_session = session_result.scalar_one_or_none()
        if not live_session:
            return
        
        room_id = live_session.room_id

        # 2. Get/Create RoomMember (Agent or User)
        # Agent usually doesn't have a user_id, so we use a system user
        if not user_id or user_id == "agent_transcriber":
            display_name = "Agent"
            effective_user_id = "agent_system"
            sender_type = "ai"
        else:
            display_name = user_id
            effective_user_id = user_id
            sender_type = "human"

        member_result = await db_session.execute(
            select(RoomMember).where(RoomMember.room_id == room_id, RoomMember.user_id == effective_user_id)
        )
        member = member_result.scalar_one_or_none()
        
        if not member:
            member = RoomMember(
                id=f"rm_{uuid.uuid4().hex[:16]}",
                room_id=room_id,
                user_id=effective_user_id,
                display_name=display_name,
                role="bot" if sender_type == "ai" else "member",
                joined_at=datetime.utcnow()
            )
            db_session.add(member)
            await db_session.flush()

        # 3. Save as ChatMessage (type=transcript)
        # This allows the summarization logic to pick it up easily
        seq_result = await db_session.execute(select(func.max(ChatMessage.seq)).where(ChatMessage.room_id == room_id))
        max_seq = seq_result.scalar() or 0
        
        new_message = ChatMessage(
            id=f"trans_{uuid.uuid4().hex[:16]}",
            room_id=room_id,
            seq=max_seq + 1,
            sender_type=sender_type,
            sender_member_id=member.id,
            message_type="transcript", # New type for voice logs
            text=original_text,
            lang=payload.get("source_lang", "ja"),
            meta={"translated_text": translated_text},
            created_at=datetime.utcnow()
        )
        db_session.add(new_message)
        await db_session.commit()

        # 4. Broadcast
        # Broadcast as 'translation' type so frontend displays it in the translation tab
        await manager.broadcast(session_id, {
            "type": "translation",
            "data": {
                "id": new_message.id,
                "original_text": original_text,
                "translated_text": translated_text,
                "source_lang": new_message.lang,
                "speaker": display_name
            }
        })


async def handle_translate_and_broadcast(session_id: str, text: str, source_lang: str):
    """
    Translate text (from chat or manual request) and broadcast.
    Updates the ChatMessage in DB if it relates to a chat.
    """
    from app.core.config import settings
    
    target_lang = "en" if source_lang == "ja" else "ja"
    translated_text = ""

    # Mock Translation
    if settings.translation_provider == "MOCK":
        translated_text = f"[Mock Translate] {text}"
    
    # OpenAI Translation
    elif settings.translation_provider == "OPENAI" and settings.openai_api_key:
        try:
             from openai import AsyncOpenAI
             client = AsyncOpenAI(api_key=settings.openai_api_key)
             prompt = f"Translate the following text from {source_lang} to {target_lang}. Return only the translated text."
             response = await client.chat.completions.create(
                 model="gpt-4o",
                 messages=[
                     {"role": "system", "content": "You are a professional translator."},
                     {"role": "user", "content": prompt + "\n\n" + text}
                 ]
             )
             translated_text = response.choices[0].message.content.strip()
        except Exception as e:
            print(f"[Translation Error] {e}")
            translated_text = f"[Error] {text}"
    else:
        translated_text = f"[No Provider] {text}"

    # Broadcast
    await manager.broadcast(session_id, {
        "type": "translation_event", # Frontend handles this for popup or inline update
        "data": {
            "original_text": text,
            "translated_text": translated_text,
            "source_lang": source_lang,
            "target_lang": target_lang
        }
    })
    
    return translated_text

