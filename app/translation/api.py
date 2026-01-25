"""
STT (Speech-to-Text) Translation API Endpoints
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.infra.db import get_db
from app.translation.schemas import STTTranslationRequest, STTTranslationResponse
from app.translation.deepl_service import deepl_service
from app.models.ai import AIEvent
from app.models.room import Room
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

@router.post("/stt", response_model=STTTranslationResponse)
async def translate_stt(
    request: STTTranslationRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Translate STT transcription and store the event.
    Targeted specifically for real-time speech-to-text data.
    """
    try:
        # Determine target language based on source language
        # Requirement: Korean -> Japanese, Japanese -> Korean
        source_lang = request.Language
        target_lang = "Japanese" if "Korean" in source_lang else "Korean"
        
        # Perform translation
        translated_text = deepl_service.translate_text(
            text=request.Original,
            source_lang=source_lang,
            target_lang=target_lang
        )
        
        # Store in DB (AIEvent) only if it's a final transcript
        # For non-final (partial) results, we might skip DB storage to save space,
        # but the user didn't specify. Usually only final results are persisted.
        if request.is_final:
            try:
                try:
                    seq_int = int(request.sequence)
                except ValueError:
                    seq_int = 0
                    
                import uuid
                ai_event = AIEvent(
                    id=str(uuid.uuid4()),
                    room_id=request.room_id,
                    seq=seq_int,
                    event_type="translation",
                    original_text=request.Original,
                    original_lang=source_lang,
                    translated_text=translated_text,
                    translated_lang=target_lang,
                    meta={
                        "participant_id": request.participant_id,
                        "participant_name": request.participant_name,
                        "timestamp": request.timestamp,
                        "is_stt": True,
                        "is_speaking": request.is_speaking
                    }
                )
                
                db.add(ai_event)
                await db.commit()
            except Exception as e:
                logger.error(f"Failed to save STT translation event: {e}")
        
        return STTTranslationResponse(
            room_id=request.room_id,
            participant_id=request.participant_id,
            participant_name=request.participant_name,
            Original=request.Original,
            translated=translated_text,
            timestamp=request.timestamp,
            sequence=request.sequence,
            is_final=request.is_final
        )

    except Exception as e:
        logger.error(f"STT Translation endpoint error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
