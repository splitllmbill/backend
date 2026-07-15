from typing import Tuple, Dict, Any
from app.config import settings
from .provider_base import BaseLLMProvider

class GoogleGeminiProvider(BaseLLMProvider):
    def __init__(self):
        # Imported lazily so the app boots/deploys without the google-genai
        # package installed while the LLM feature is disabled. Re-add
        # google-genai to requirements.txt to use this provider.
        from google import genai
        self.client = genai.Client(
            vertexai=True,
            project=settings.GCP_PROJECT_ID,
            location=settings.GCP_LOCATION,
        )

    async def generate_content(self, model_name: str, prompt: str) -> Tuple[str, Dict[str, Any]]:
        response = self.client.models.generate_content(
            model=model_name,
            contents=prompt,
        )
        
        result_text = response.text
        
        # Try to extract usage metadata
        stats = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        
        try:
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                stats["prompt_tokens"] = getattr(response.usage_metadata, "prompt_token_count", 0)
                stats["completion_tokens"] = getattr(response.usage_metadata, "candidates_token_count", 0)
                stats["total_tokens"] = getattr(response.usage_metadata, "total_token_count", 0)
        except Exception:
            pass # Gracefully ignore if usage metadata is missing
            
        return result_text, stats
