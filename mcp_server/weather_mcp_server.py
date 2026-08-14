"""
Weather Data MCP Server.

Exposes weather data tools over MCP (Model Context Protocol) so a
Databricks Agent Bricks agent can call them like any other tool:
    - add_city_location(city, state)
    - get_city_forecasts(city, state, limit)
    - get_city_alerts(city, state, limit)
    - list_all_locations()

These tools interact with Lakebase (Databricks-managed Postgres) to manage
weather data locations and retrieve forecast/alert narratives.

Deploy this as a Databricks App (using app.yaml + FastMCP entrypoint pattern)
so an Agent Bricks agent (or any MCP client) can register its URL as an
external MCP server.

Run locally:
    python weather_mcp_server.py
"""

import os
import json
import logging
import requests
from typing import Any

from fastmcp import FastMCP

import lakebase

# Try to import sentence-transformers for semantic search
try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    logger = None  # Will be set after logging config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-mcp-server")

# Table names from environment variables
LOCATION_TABLE_NAME = os.environ.get("LOCATION_TABLE_NAME", "location")
WEATHER_DOCUMENTS_TABLE_NAME = os.environ.get("WEATHER_DOCUMENTS_TABLE_NAME", "weather_documents")
EMBEDDINGS_TABLE_NAME = os.environ.get("EMBEDDINGS_TABLE_NAME", "weather_embeddings")
CHUNK_EMBEDDINGS_TABLE_NAME = os.environ.get("CHUNK_EMBEDDINGS_TABLE_NAME", "weather_chunk_embeddings")
EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2")

# NWS API configuration
NWS_API_BASE_URL = os.environ.get("NWS_API_BASE_URL", "https://api.weather.gov")
GEOCODING_API_URL = "https://nominatim.openstreetmap.org/search"

mcp = FastMCP("weather-data-service")

# Initialize embedding model (lazy loading)
_embedding_model = None

def get_embedding_model():
    """Lazy-load the sentence-transformers model."""
    if not SENTENCE_TRANSFORMERS_AVAILABLE:
        raise ImportError("sentence-transformers library is not installed. Install it with: pip install sentence-transformers")
    
    global _embedding_model
    if _embedding_model is None:
        logger.info(f"Loading embedding model {EMBEDDING_MODEL_NAME}...")
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _embedding_model


def _geocode_city(city: str, state: str = None) -> dict[str, Any]:
    """
    Geocode a city name to latitude and longitude using OpenStreetMap Nominatim API.
    
    Args:
        city: City name (e.g., "Boston")
        state: Optional state abbreviation or name (e.g., "MA" or "Massachusetts")
    
    Returns:
        If single result: A dict with lat, lon, display_name
        If multiple results: A dict with multiple_results=True, count, and options list
        Raises an exception if not found.
    """
    query = f"{city}, {state}, USA" if state else f"{city}, USA"
    
    headers = {
        "User-Agent": "WeatherMCPServer/1.0 (Databricks)",
    }
    
    params = {
        "q": query,
        "format": "json",
        "limit": 10,  # Get up to 10 results to check for ambiguity
    }
    
    try:
        resp = requests.get(GEOCODING_API_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        results = resp.json()
        
        if not results:
            raise ValueError(f"City '{city}' not found")
        
        # If only one result, return it in the original format
        if len(results) == 1:
            result = results[0]
            return {
                "lat": float(result["lat"]),
                "lon": float(result["lon"]),
                "display_name": result.get("display_name", f"{city}, {state}"),
            }
        
        # If multiple results, return all options
        return {
            "multiple_results": True,
            "count": len(results),
            "query": query,
            "options": [
                {
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "display_name": r.get("display_name", ""),
                    "type": r.get("type", ""),
                    "importance": r.get("importance", 0),
                }
                for r in results
            ]
        }
    except Exception as e:
        logger.exception(f"Geocoding failed for {city}, {state}")
        raise ValueError(f"Failed to geocode city: {str(e)}")


def _get_nws_grid_metadata(lat: float, lon: float) -> dict[str, Any]:
    """
    Get NWS grid metadata for a latitude/longitude point.
    
    Args:
        lat: Latitude
        lon: Longitude
    
    Returns:
        The full NWS API response as a dict, with convenience fields grid_id, grid_x, grid_y, city, state added.
    """
    headers = {
        "User-Agent": "WeatherMCPServer/1.0 (Databricks)",
        "Accept": "application/geo+json",
    }
    
    try:
        url = f"{NWS_API_BASE_URL}/points/{lat},{lon}"
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        # Extract convenience fields from the full response
        properties = data.get("properties", {})
        rel_loc = properties.get("relativeLocation", {}).get("properties", {})
        
        # Add convenience fields directly to the full response for easy access
        data["grid_id"] = properties.get("gridId")
        data["grid_x"] = properties.get("gridX")
        data["grid_y"] = properties.get("gridY")
        data["city"] = rel_loc.get("city")
        data["state"] = rel_loc.get("state")
        
        return data
    except Exception as e:
        logger.exception(f"Failed to fetch NWS metadata for {lat}, {lon}")
        raise ValueError(f"Failed to fetch NWS grid metadata: {str(e)}")


@mcp.tool
def add_city_location(city: str, state: str = None, author: str = "mcp-server") -> dict:
    """
    Add a city to the location table if it doesn't already exist.
    
    This tool geocodes the city name to get latitude/longitude coordinates,
    fetches the NWS grid metadata, and inserts the location into the database.
    
    Args:
        city: City name (e.g., "Boston", "New York")
        state: Optional state abbreviation or name (e.g., "MA", "Massachusetts")
        author: Email or identifier of the user adding the location (default: "mcp-server")
    
    Returns:
        A dict with status, location data, and whether it was newly created or already existed.
    """
    try:
        # Step 1: Geocode the city to get lat/lon
        geocode_result = _geocode_city(city, state)
        
        # Check if geocoding returned multiple results (ambiguous city name)
        if geocode_result.get("multiple_results"):
            return {
                "status": "error",
                "message": f"Multiple locations found for '{city}'. Please be more specific by providing the state.",
                "multiple_results": True,
                "count": geocode_result["count"],
                "options": geocode_result["options"],
            }
        
        lat = geocode_result["lat"]
        lon = geocode_result["lon"]
        display_name = geocode_result["display_name"]
        
        logger.info(f"Geocoded {city}, {state} to {lat}, {lon}")
        
        # Step 2: Get NWS grid metadata and store raw API response
        nws_response = _get_nws_grid_metadata(lat, lon)
        nws_metadata = nws_response  # Keep the full response
        grid_id = nws_metadata["grid_id"]
        grid_x = nws_metadata["grid_x"]
        grid_y = nws_metadata["grid_y"]
        nws_city = nws_metadata.get("city") or city
        nws_state = nws_metadata.get("state") or state
        
        if not all([grid_id, grid_x is not None, grid_y is not None]):
            return {
                "status": "error",
                "message": "Could not extract grid information from NWS API",
            }
        
        logger.info(f"Got NWS metadata: {grid_id}/{grid_x},{grid_y}")
        
        # Step 3: Insert or update the location in the database with raw NWS response
        result = lakebase.run_insert_returning(
            f"""
            INSERT INTO {LOCATION_TABLE_NAME}
                (longitude, latitude, grid_id, grid_x, grid_y, city, state, author, payload, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
            ON CONFLICT (longitude, latitude) DO UPDATE
                SET grid_id = EXCLUDED.grid_id,
                    grid_x = EXCLUDED.grid_x,
                    grid_y = EXCLUDED.grid_y,
                    city = EXCLUDED.city,
                    state = EXCLUDED.state,
                    payload = EXCLUDED.payload
            RETURNING loc_id, longitude, latitude, grid_id, grid_x, grid_y, city, state, author, payload, created_at
            """,
            (lon, lat, grid_id, grid_x, grid_y, nws_city, nws_state, author, json.dumps(nws_response)),
        )
        
        location = result[0] if result else None
        
        if not location:
            return {
                "status": "error",
                "message": "Failed to insert location into database",
            }
        
        return {
            "status": "success",
            "message": f"Successfully added location for {city}, {state}",
            "location": {
                "loc_id": location["loc_id"],
                "city": location["city"],
                "state": location["state"],
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "grid_id": location["grid_id"],
                "grid_x": location["grid_x"],
                "grid_y": location["grid_y"],
                "author": location["author"],
                "created_at": str(location["created_at"]),
                "display_name": display_name,
            },
        }
        
    except Exception as e:
        logger.exception(f"Failed to add city location for {city}, {state}")
        return {
            "status": "error",
            "message": f"Failed to add city location: {str(e)}",
        }


@mcp.tool
def get_city_forecasts(city: str, state: str = None, limit: int = 10) -> dict:
    """
    Retrieve forecast narratives for a city from the weather_documents table.
    
    This tool looks up the location by city/state name and returns all forecast
    documents (source_type='forecast') with their narrative texts.
    
    Args:
        city: City name (e.g., "Boston")
        state: Optional state abbreviation or name (e.g., "MA")
        limit: Maximum number of forecast documents to return (default: 10)
    
    Returns:
        A dict with status, city info, and a list of forecast documents.
    """
    try:
        # Find the location by city/state
        where_conditions = ["city ILIKE %s"]
        params = [f"%{city}%"]
        
        if state:
            where_conditions.append("state ILIKE %s")
            params.append(f"%{state}%")
        
        where_clause = " AND ".join(where_conditions)
        
        locations = lakebase.run_query(
            f"SELECT loc_id, city, state, latitude, longitude FROM {LOCATION_TABLE_NAME} WHERE {where_clause}",
            tuple(params),
        )
        
        if not locations:
            return {
                "status": "not_found",
                "message": f"No location found for city '{city}', state '{state}'",
            }
        
        location = locations[0]
        loc_id = location["loc_id"]
        
        # Fetch forecast documents for this location
        forecasts = lakebase.run_query(
            f"""
            SELECT 
                document_id,
                source_type,
                is_day_time,
                event,
                headline,
                narrative_text,
                issued_at,
                effective_at,
                expires_at,
                created_at
            FROM {WEATHER_DOCUMENTS_TABLE_NAME}
            WHERE loc_id = %s AND source_type = 'forecast'
            ORDER BY issued_at DESC
            LIMIT %s
            """,
            (loc_id, limit),
        )
        
        return {
            "status": "success",
            "location": {
                "loc_id": location["loc_id"],
                "city": location["city"],
                "state": location["state"],
                "latitude": location["latitude"],
                "longitude": location["longitude"],
            },
            "count": len(forecasts),
            "forecasts": [
                {
                    "document_id": f["document_id"],
                    "event": f["event"],
                    "headline": f["headline"],
                    "narrative_text": f["narrative_text"],
                    "is_day_time": f["is_day_time"],
                    "issued_at": str(f["issued_at"]) if f["issued_at"] else None,
                    "effective_at": str(f["effective_at"]) if f["effective_at"] else None,
                    "expires_at": str(f["expires_at"]) if f["expires_at"] else None,
                    "created_at": str(f["created_at"]) if f["created_at"] else None,
                }
                for f in forecasts
            ],
        }
        
    except Exception as e:
        logger.exception(f"Failed to get forecasts for {city}, {state}")
        return {
            "status": "error",
            "message": f"Failed to retrieve forecasts: {str(e)}",
        }


@mcp.tool
def get_city_alerts(city: str, state: str = None, limit: int = 10) -> dict:
    """
    Retrieve weather alert narratives for a city from the weather_documents table.
    
    This tool looks up the location by city/state name and returns all alert
    documents (source_type='alert') with their narrative texts.
    
    Args:
        city: City name (e.g., "Boston")
        state: Optional state abbreviation or name (e.g., "MA")
        limit: Maximum number of alert documents to return (default: 10)
    
    Returns:
        A dict with status, city info, and a list of alert documents.
    """
    try:
        # Find the location by city/state
        where_conditions = ["city ILIKE %s"]
        params = [f"%{city}%"]
        
        if state:
            where_conditions.append("state ILIKE %s")
            params.append(f"%{state}%")
        
        where_clause = " AND ".join(where_conditions)
        
        locations = lakebase.run_query(
            f"SELECT loc_id, city, state, latitude, longitude FROM {LOCATION_TABLE_NAME} WHERE {where_clause}",
            tuple(params),
        )
        
        if not locations:
            return {
                "status": "not_found",
                "message": f"No location found for city '{city}', state '{state}'",
            }
        
        location = locations[0]
        loc_id = location["loc_id"]
        
        # Fetch alert documents for this location
        alerts = lakebase.run_query(
            f"""
            SELECT 
                document_id,
                source_type,
                is_day_time,
                event,
                headline,
                narrative_text,
                issued_at,
                effective_at,
                expires_at,
                created_at
            FROM {WEATHER_DOCUMENTS_TABLE_NAME}
            WHERE loc_id = %s AND source_type = 'alert'
            ORDER BY issued_at DESC
            LIMIT %s
            """,
            (loc_id, limit),
        )
        
        return {
            "status": "success",
            "location": {
                "loc_id": location["loc_id"],
                "city": location["city"],
                "state": location["state"],
                "latitude": location["latitude"],
                "longitude": location["longitude"],
            },
            "count": len(alerts),
            "alerts": [
                {
                    "document_id": a["document_id"],
                    "event": a["event"],
                    "headline": a["headline"],
                    "narrative_text": a["narrative_text"],
                    "is_day_time": a["is_day_time"],
                    "issued_at": str(a["issued_at"]) if a["issued_at"] else None,
                    "effective_at": str(a["effective_at"]) if a["effective_at"] else None,
                    "expires_at": str(a["expires_at"]) if a["expires_at"] else None,
                    "created_at": str(a["created_at"]) if a["created_at"] else None,
                }
                for a in alerts
            ],
        }
        
    except Exception as e:
        logger.exception(f"Failed to get alerts for {city}, {state}")
        return {
            "status": "error",
            "message": f"Failed to retrieve alerts: {str(e)}",
        }


@mcp.tool
def list_all_locations(author: str = None, limit: int = 100) -> dict:
    """
    List all locations in the location table.
    
    Args:
        author: Optional filter by author email (default: None, returns all)
        limit: Maximum number of locations to return (default: 100)
    
    Returns:
        A dict with status and a list of all tracked locations.
    """
    try:
        if author:
            locations = lakebase.run_query(
                f"""
                SELECT loc_id, city, state, latitude, longitude, grid_id, grid_x, grid_y, author, created_at
                FROM {LOCATION_TABLE_NAME}
                WHERE author = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (author, limit),
            )
        else:
            locations = lakebase.run_query(
                f"""
                SELECT loc_id, city, state, latitude, longitude, grid_id, grid_x, grid_y, author, created_at
                FROM {LOCATION_TABLE_NAME}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
        
        return {
            "status": "success",
            "count": len(locations),
            "locations": [
                {
                    "loc_id": loc["loc_id"],
                    "city": loc["city"],
                    "state": loc["state"],
                    "latitude": loc["latitude"],
                    "longitude": loc["longitude"],
                    "grid_id": loc["grid_id"],
                    "grid_x": loc["grid_x"],
                    "grid_y": loc["grid_y"],
                    "author": loc["author"],
                    "created_at": str(loc["created_at"]),
                }
                for loc in locations
            ],
        }
        
    except Exception as e:
        logger.exception("Failed to list locations")
        return {
            "status": "error",
            "message": f"Failed to list locations: {str(e)}",
        }


@mcp.tool
def ask_about_going_outside(city: str, state: str = None, question: str = None, limit: int = 5) -> dict:
    """
    Ask a question about going outside in a specific city using semantic search over weather data.
    
    This tool uses vector embeddings to find the most relevant weather forecasts and alerts
    to answer questions like "Should I go outside?", "Is it safe to walk?", "Do I need an umbrella?", etc.
    
    Args:
        city: City name (e.g., "Boston")
        state: Optional state abbreviation or name (e.g., "MA")
        question: Optional specific question (default: "Should I go outside? Is the weather good?")
        limit: Maximum number of relevant weather documents to return (default: 5)
    
    Returns:
        A dict with status, the question, and relevant weather information with similarity scores.
    """
    try:
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            return {
                "status": "error",
                "message": "Semantic search is not available. Install sentence-transformers: pip install sentence-transformers",
            }
        
        # Default question if none provided
        if not question:
            question = "Should I go outside? Is the weather good for outdoor activities?"
        
        # Find the location by city/state
        where_conditions = ["city ILIKE %s"]
        params = [f"%{city}%"]
        
        if state:
            where_conditions.append("state ILIKE %s")
            params.append(f"%{state}%")
        
        where_clause = " AND ".join(where_conditions)
        
        locations = lakebase.run_query(
            f"SELECT loc_id, city, state, latitude, longitude FROM {LOCATION_TABLE_NAME} WHERE {where_clause}",
            tuple(params),
        )
        
        if not locations:
            return {
                "status": "not_found",
                "message": f"No location found for city '{city}', state '{state}'. Please add this location first using add_city_location.",
            }
        
        location = locations[0]
        loc_id = location["loc_id"]
        
        # Generate embedding for the question
        model = get_embedding_model()
        question_embedding = model.encode(question).tolist()
        
        # Check if embeddings table exists and has data for this location
        check_embeddings = lakebase.run_query(
            f"""
            SELECT COUNT(*) as count
            FROM {CHUNK_EMBEDDINGS_TABLE_NAME}
            WHERE loc_id = %s
            """,
            (loc_id,),
        )
        
        if not check_embeddings or check_embeddings[0]["count"] == 0:
            # Fall back to non-semantic search - just return recent forecasts and alerts
            logger.warning(f"No embeddings found for location {loc_id}, falling back to recent documents")
            
            recent_docs = lakebase.run_query(
                f"""
                SELECT 
                    document_id,
                    source_type,
                    event,
                    headline,
                    narrative_text,
                    issued_at,
                    effective_at,
                    expires_at
                FROM {WEATHER_DOCUMENTS_TABLE_NAME}
                WHERE loc_id = %s
                ORDER BY issued_at DESC
                LIMIT %s
                """,
                (loc_id, limit),
            )
            
            return {
                "status": "success",
                "method": "fallback_recent",
                "location": {
                    "city": location["city"],
                    "state": location["state"],
                },
                "question": question,
                "message": "No embeddings available. Showing most recent weather documents.",
                "results": [
                    {
                        "document_id": doc["document_id"],
                        "source_type": doc["source_type"],
                        "event": doc["event"],
                        "headline": doc["headline"],
                        "narrative_text": doc["narrative_text"],
                        "issued_at": str(doc["issued_at"]) if doc["issued_at"] else None,
                        "effective_at": str(doc["effective_at"]) if doc["effective_at"] else None,
                        "expires_at": str(doc["expires_at"]) if doc["expires_at"] else None,
                    }
                    for doc in recent_docs
                ],
            }
        
        # Perform vector similarity search using cosine similarity
        # PostgreSQL with pgvector extension uses <=> for cosine distance (1 - cosine similarity)
        similar_chunks = lakebase.run_query(
            f"""
            SELECT 
                ce.document_id,
                ce.chunk_text,
                ce.chunk_index,
                wd.source_type,
                wd.event,
                wd.headline,
                wd.narrative_text,
                wd.issued_at,
                wd.effective_at,
                wd.expires_at,
                (1 - (ce.embedding <=> %s::vector)) as similarity_score
            FROM {CHUNK_EMBEDDINGS_TABLE_NAME} ce
            JOIN {WEATHER_DOCUMENTS_TABLE_NAME} wd ON ce.document_id = wd.document_id
            WHERE ce.loc_id = %s
            ORDER BY ce.embedding <=> %s::vector
            LIMIT %s
            """,
            (question_embedding, loc_id, question_embedding, limit),
        )
        
        # Group results by document to avoid duplicates
        seen_docs = set()
        results = []
        
        for chunk in similar_chunks:
            doc_id = chunk["document_id"]
            if doc_id not in seen_docs:
                seen_docs.add(doc_id)
                results.append({
                    "document_id": doc_id,
                    "source_type": chunk["source_type"],
                    "event": chunk["event"],
                    "headline": chunk["headline"],
                    "narrative_text": chunk["narrative_text"],
                    "relevant_chunk": chunk["chunk_text"],
                    "similarity_score": float(chunk["similarity_score"]),
                    "issued_at": str(chunk["issued_at"]) if chunk["issued_at"] else None,
                    "effective_at": str(chunk["effective_at"]) if chunk["effective_at"] else None,
                    "expires_at": str(chunk["expires_at"]) if chunk["expires_at"] else None,
                })
        
        # Generate a summary recommendation based on the results
        summary = _generate_outside_recommendation(results)
        
        return {
            "status": "success",
            "method": "semantic_search",
            "location": {
                "city": location["city"],
                "state": location["state"],
            },
            "question": question,
            "summary": summary,
            "count": len(results),
            "results": results,
        }
        
    except Exception as e:
        logger.exception(f"Failed to answer question for {city}, {state}")
        return {
            "status": "error",
            "message": f"Failed to answer question: {str(e)}",
        }


def _generate_outside_recommendation(results: list[dict]) -> str:
    """
    Generate a simple recommendation based on weather data.
    
    Args:
        results: List of weather documents with similarity scores
    
    Returns:
        A summary recommendation string
    """
    if not results:
        return "No weather data available to make a recommendation."
    
    # Check for high-priority alerts
    alerts = [r for r in results if r["source_type"] == "alert"]
    forecasts = [r for r in results if r["source_type"] == "forecast"]
    
    if alerts:
        alert_events = [a["event"] for a in alerts if a["event"]]
        if alert_events:
            return f"⚠️ Weather alerts active: {', '.join(alert_events)}. Check alert details before going outside."
    
    # Check forecast narratives for keywords
    if forecasts and forecasts[0].get("narrative_text"):
        narrative = forecasts[0]["narrative_text"].lower()
        
        # Severe weather indicators
        if any(word in narrative for word in ["severe", "warning", "dangerous", "extreme"]):
            return "⚠️ Severe weather expected. Consider staying indoors."
        
        # Rain/storm indicators
        if any(word in narrative for word in ["rain", "storm", "showers", "thunderstorm"]):
            return "🌧️ Rain or storms expected. Bring an umbrella if going outside."
        
        # Snow/ice indicators
        if any(word in narrative for word in ["snow", "ice", "sleet", "freezing"]):
            return "❄️ Winter weather expected. Dress warmly and watch for slippery conditions."
        
        # Hot weather
        if any(word in narrative for word in ["hot", "heat", "high temperature"]):
            return "☀️ Hot weather expected. Stay hydrated and seek shade."
        
        # Good weather
        if any(word in narrative for word in ["sunny", "clear", "fair", "pleasant"]):
            return "✅ Good weather conditions. Great time to go outside!"
    
    return "Weather conditions are generally typical. Check the detailed forecast for specifics."


if __name__ == "__main__":
    # Databricks Apps route external HTTP traffic to this port via app.yaml;
    # streamable-http is the transport Databricks' MCP client/gateway expects.
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", 8000)))
    mcp.run(transport="http", host="0.0.0.0", port=port)
