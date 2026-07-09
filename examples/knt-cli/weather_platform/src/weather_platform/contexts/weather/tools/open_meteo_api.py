from typing import Any
from kntgraph.core.result import Ok, Err, Result
from kntgraph.tools.worker import tool_worker

import httpx


@tool_worker(name="open_meteo_api", description="Fetches weather for coordinates.")
class OpenMeteoApi:
    async def invoke(
        self, latitude: float, longitude: float, *, idempotency_key: str
    ) -> Result[dict[str, Any], Exception]:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current_weather=true"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
                return Ok(data["current_weather"])
        except Exception as e:
            return Err(e)
