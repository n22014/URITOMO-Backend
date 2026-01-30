import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select, func
from app.infra.db import AsyncSessionLocal
from app.models.message import ChatMessage
from app.models.room import RoomMember, RoomLiveSession
from app.models.ai import AIEvent
from app.meeting.ws.manager import manager
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Translation service abstraction based on config
async def translate_text(text: str, source_lang: str, target_lang: str) -> Optional[str]:
    """
    Translate text using configured translation provider (OPENAI, DEEPL, or MOCK).
    """
    if not text:
        return None
    
    try:
        if settings.translation_provider == "OPENAI":
            from app.translation.openai_service import openai_service
            return await openai_service.translate_text(text, source_lang, target_lang)
        elif settings.translation_provider == "DEEPL":
            from app.translation.deepl_service import deepl_service
            return deepl_service.translate_text(text, source_lang, target_lang)
        else:
            # MOCK translation
            lang_code = target_lang[:2].upper() if target_lang else "TRANS"
            return f"[{lang_code}] {text}"
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return None

def _ai_event_payload(ai_event: AIEvent) -> dict:
    return {
        "id": ai_event.id,
        "room_id": ai_event.room_id,
        "seq": ai_event.seq,
        "event_type": ai_event.event_type,
        "original_text": ai_event.original_text,
        "original_lang": ai_event.original_lang,
        "translated_text": ai_event.translated_text,
        "translated_lang": ai_event.translated_lang,
        "text": ai_event.text,
        "lang": ai_event.lang,
        "meta": ai_event.meta,
        "created_at": ai_event.created_at.isoformat() if ai_event.created_at else None,
    }

def _normalize_lang(lang: Optional[str]) -> str:
    if not lang:
        return "Korean"
    lang_lower = lang.strip().lower()
    if lang_lower in {"ko", "kr", "korean"}:
        return "Korean"
    if lang_lower in {"ja", "jp", "japanese"}:
        return "Japanese"
    return lang

async def handle_chat_message(room_id: str, user_id: str, data: dict):
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
        # 1. Get RoomMember ID for this user
        member_result = await db_session.execute(
            select(RoomMember).where(
                RoomMember.room_id == room_id,
                RoomMember.user_id == user_id
            )
        )
        member = member_result.scalar_one_or_none()
        if not member:
            return

        # 3. Get next sequence number for this room
        seq_result = await db_session.execute(
            select(func.max(ChatMessage.seq)).where(ChatMessage.room_id == room_id)
        )
        max_seq = seq_result.scalar() or 0
        next_seq = max_seq + 1

        # 4. Create ChatMessage (Original)
        source_lang = _normalize_lang(data.get("lang")) # Default to Korean based on user context
        
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

        # 5. Perform Translation (using configured provider: OPENAI/DEEPL/MOCK)
        target_lang = "Japanese" if source_lang == "Korean" else "Korean"
        translated_text: Optional[str] = None

        try:
            # Use the unified translate_text function that handles provider selection
            translated_text = await translate_text(
                text=text,
                source_lang=source_lang,
                target_lang=target_lang
            )
            
            # 6. Save Translation Event if translation succeeded
            if translated_text:
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
            
        except Exception as e:
            logger.error(f"Translation failed in websocket: {e}")

        # 6. Broadcast Combined Message (Original + Translation if available)
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
                "translated_text": translated_text,
                "translated_lang": target_lang if translated_text else None,
                "created_at": new_message.created_at.isoformat()
            }
        }
        await manager.broadcast(room_id, broadcast_data)

async def handle_stt_message(room_id: str, user_id: str, data: dict):
    """
    Handle incoming STT (Speech-to-Text) message:
    1. If not is_final, just broadcast to others (partial UI)
    2. If is_final:
        - Save to DB (as ChatMessage with sender_type='human' or 'transcription')
        - Translate
        - Broadcast Final and Translation
    """
    text = data.get("text")
    is_final = data.get("is_final", False)
    if not text:
        return

    # If it's just a partial result, broadcast and return
    if not is_final:
        broadcast_data = {
            "type": "stt",
            "data": {
                "user_id": user_id,
                "text": text,
                "is_final": False,
                "lang": data.get("lang", "Korean")
            }
        }
        await manager.broadcast(room_id, broadcast_data)
        return

    # If it's final, process like a chat message (save & translate)
    async with AsyncSessionLocal() as db_session:
        # 1. Get RoomMember ID
        member_result = await db_session.execute(
            select(RoomMember).where(
                RoomMember.room_id == room_id,
                RoomMember.user_id == user_id
            )
        )
        member = member_result.scalar_one_or_none()
        if not member:
            return

        # 2. Get next sequence
        seq_result = await db_session.execute(
            select(func.max(ChatMessage.seq)).where(ChatMessage.room_id == room_id)
        )
        max_seq = seq_result.scalar() or 0
        next_seq = max_seq + 1

        # 3. Create ChatMessage (Final STT)
        source_lang = _normalize_lang(data.get("lang"))
        message_id = f"stt_{uuid.uuid4().hex[:16]}"
        
        new_message = ChatMessage(
            id=message_id,
            room_id=room_id,
            seq=next_seq,
            sender_type="transcription",  # Mark as transcription
            sender_member_id=member.id,
            message_type="text",
            text=text,
            lang=source_lang,
            created_at=datetime.utcnow()
        )

        db_session.add(new_message)
        await db_session.commit()
        await db_session.refresh(new_message)

        # 4. Translate using configured provider
        target_lang = "Japanese" if source_lang == "Korean" else "Korean"
        translated_text: Optional[str] = None
        
        try:
            translated_text = await translate_text(
                text=text, 
                source_lang=source_lang, 
                target_lang=target_lang
            )
            
            # 5. Save Translation Event if successful
            if translated_text:
                trans_id = f"stt_trans_{uuid.uuid4().hex[:16]}"
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
                        "participant_name": member.display_name,
                        "is_stt": True
                    },
                    created_at=datetime.utcnow()
                )
                
                db_session.add(ai_event)
                await db_session.commit()
        except Exception as e:
            logger.error(f"Translation failed for STT: {e}")

        # 6. Broadcast Final STT with Translation
        broadcast_data = {
            "type": "stt",
            "data": {
                "id": new_message.id,
                "room_id": new_message.room_id,
                "seq": new_message.seq,
                "user_id": user_id,
                "display_name": member.display_name,
                "text": new_message.text,
                "lang": new_message.lang,
                "translated_text": translated_text,
                "translated_lang": target_lang if translated_text else None,
                "is_final": True,
                "created_at": new_message.created_at.isoformat()
            }
        }
        await manager.broadcast(room_id, broadcast_data)

