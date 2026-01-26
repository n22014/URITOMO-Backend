from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.models.message import ChatMessage
from app.models.room import RoomMember # Explicitly import to help relation mapping
from typing import List, Dict, Any

async def fetch_meeting_transcript(db: AsyncSession, room_id: str) -> List[Dict[str, Any]]:
    """
    指定された会議室の全チャットメッセージを取得し、リスト形式で返します。
    AI要約に不要なシステムメッセージやAIメッセージはフィルタリングします。
    """
    stmt = (
        select(ChatMessage)
        .options(selectinload(ChatMessage.sender_member))
        .where(ChatMessage.room_id == room_id)
        # 要約対象は音声認識ログ(transcript)のみに変更
        .where(ChatMessage.message_type == "transcript")
        .order_by(ChatMessage.created_at)
    )
    result = await db.execute(stmt)
    messages = result.scalars().all()
    
    transcript = []
    for msg in messages:
        sender_name = msg.sender_member.display_name if msg.sender_member else "Unknown"
        translated_text = msg.meta.get("translated_text", "") if msg.meta else ""
        
        # 音声認識結果(原文)と翻訳結果を併記するとAIが理解しやすい
        content = f"{msg.text} (Translation: {translated_text})" if translated_text else msg.text
        
        transcript.append({
            "who": sender_name,
            "what": content,
            "when": msg.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "message_type": msg.message_type,
            "original_msg_id": msg.id
        })
    return transcript

def format_transcript_for_ai(transcript: List[Dict[str, Any]]) -> str:
    """
    会議録のリストをAIが扱いやすいテキスト形式にフォーマットします。
    """
    lines = []
    for entry in transcript:
        lines.append(f"[{entry['when']}] {entry['who']}: {entry['what']}")
    return "\n".join(lines)
