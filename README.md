# Weather MCP Server - Development Log

## Overview

This document tracks the development and features of the Weather MCP Server system, which consists of:

1. **weather_mcp_server.py** - The MCP server that exposes weather data tools via the Model Context Protocol
2. **weather_agent.py** - A Streamlit-based chat interface where users can interact with the weather system in natural language

The architecture mirrors the alpaca trading system: users talk to the **weather_agent** (frontend), which internally calls the **weather_mcp_server** tools (backend).

## Project Context

### Related Projects
* **databricks-weather-ai-agent**: Main project containing the MCP server implementations
* **vector_weather_retrieval_service**: Weather data retrieval service using National Weather Service (NWS) API

### Database Schema

The MCP server interacts with two main tables in Lakebase (Databricks-managed Postgres):

#### location table
```sql
CREATE TABLE location (
    loc_id SERIAL PRIMARY KEY,
    longitude FLOAT NOT NULL,
    latitude FLOAT NOT NULL,
    grid_id VARCHAR(240) NOT NULL,
    grid_x INT NOT NULL,
    grid_y INT NOT NULL,
    city VARCHAR(240),
    state VARCHAR(240),
    payload JSONB,
    author VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(longitude, latitude)
);
```

**Note**: The `payload` column stores the complete raw JSON response from the NWS API `/points/{lat},{lon}` endpoint, preserving all metadata including forecast URLs, timezone, radar station, office details, and grid information for auditing and debugging purposes.

#### weather_documents table
```sql
CREATE TABLE weather_documents (
    document_id SERIAL PRIMARY KEY,
    loc_id INT NOT NULL REFERENCES location(loc_id) ON DELETE CASCADE,
    source_type VARCHAR(20) NOT NULL CHECK (source_type IN ('alert', 'forecast')),
    is_day_time BOOLEAN,
    event VARCHAR(100),
    headline TEXT,
    narrative_text TEXT,
    issued_at TIMESTAMPTZ,
    effective_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

#### weather_embeddings table
```sql
CREATE TABLE weather_embeddings (
    embedding_id SERIAL PRIMARY KEY,
    document_id INT NOT NULL REFERENCES weather_documents(document_id) ON DELETE CASCADE,
    loc_id INT NOT NULL REFERENCES location(loc_id) ON DELETE CASCADE,
    embedding_text TEXT NOT NULL,
    embedding VECTOR(384) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON weather_embeddings USING ivfflat (embedding vector_cosine_ops);
```

**Note**: This table stores full document embeddings (headline + narrative_text combined) for semantic search. The vector dimension (384) matches the output of the `sentence-transformers/all-MiniLM-L6-v2` model. The `pgvector` extension is required for vector operations.

#### weather_chunk_embeddings table
```sql
CREATE TABLE weather_chunk_embeddings (
    chunk_id SERIAL PRIMARY KEY,
    document_id INT NOT NULL REFERENCES weather_documents(document_id) ON DELETE CASCADE,
    loc_id INT NOT NULL REFERENCES location(loc_id) ON DELETE CASCADE,
    chunk_text TEXT NOT NULL,
    chunk_index INT NOT NULL,
    embedding VECTOR(384) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON weather_chunk_embeddings USING ivfflat (embedding vector_cosine_ops);
```

**Note**: This table stores chunked embeddings for long weather narratives that are split into overlapping segments. Each chunk has its own embedding vector for more granular semantic search. Used when narrative text exceeds token limits for the embedding model.

## Features Implemented

### 1. Add City Location Tool (`add_city_location`)

**Purpose**: Add a city to the location table if it doesn't already exist.

**Functionality**:
* Accepts city name and optional state parameter
* Geocodes the city using OpenStreetMap Nominatim API to get latitude/longitude
* Fetches NWS grid metadata (grid_id, grid_x, grid_y) for the coordinates
* Stores the complete raw NWS API response in the `payload` JSONB column for full metadata preservation
* Inserts the location into the database (or updates if it already exists)
* Returns the complete location data including database ID

**Database Tables Used**:
* `location`: Stores location metadata and NWS grid coordinates

**Parameters**:
* `city` (required): City name (e.g., "Boston", "New York")
* `state` (optional): State abbreviation or full name (e.g., "MA", "Massachusetts")
* `author` (optional): Email or identifier of the user (default: "mcp-server")

**Example Usage**:
```python
result = add_city_location(city="Boston", state="MA")
```

**Response**:
```json
{
    "status": "success",
    "message": "Successfully added location for Boston, MA",
    "location": {
        "loc_id": 1,
        "city": "Boston",
        "state": "MA",
        "latitude": 42.3601,
        "longitude": -71.0589,
        "grid_id": "BOX",
        "grid_x": 71,
        "grid_y": 90,
        "author": "mcp-server",
        "created_at": "2024-03-26 10:00:00",
        "display_name": "Boston, Suffolk County, Massachusetts, United States"
    }
}
```

### 2. Get City Forecasts Tool (`get_city_forecasts`)

**Purpose**: Retrieve forecast narratives for a specific city from the weather_documents table.

**Functionality**:
* Looks up the location by city and state name (case-insensitive partial match)
* Retrieves all forecast documents (where `source_type='forecast'`)
* Returns narrative texts with metadata (issued time, effective time, etc.)
* Sorts results by most recent first

**Database Tables Used**:
* `location`: Location lookup by city/state
* `weather_documents`: Source of forecast documents (does NOT use vector embeddings)

**Parameters**:
* `city` (required): City name (e.g., "Boston")
* `state` (optional): State abbreviation or name (e.g., "MA")
* `limit` (optional): Maximum number of forecasts to return (default: 10)

**Example Usage**:
```python
result = get_city_forecasts(city="Boston", state="MA", limit=5)
```

**Response**:
```json
{
    "status": "success",
    "location": {
        "loc_id": 1,
        "city": "Boston",
        "state": "MA",
        "latitude": 42.3601,
        "longitude": -71.0589
    },
    "count": 5,
    "forecasts": [
        {
            "document_id": 123,
            "event": "Monday Night",
            "headline": "Monday Night",
            "narrative_text": "Partly cloudy, with a low around 45. Southwest wind around 10 mph.",
            "is_day_time": false,
            "issued_at": "2024-03-26 10:00:00",
            "effective_at": "2024-03-26 18:00:00",
            "expires_at": "2024-03-27 06:00:00",
            "created_at": "2024-03-26 10:05:00"
        }
    ]
}
```

### 3. Get City Alerts Tool (`get_city_alerts`)

**Purpose**: Retrieve weather alert narratives for a specific city from the weather_documents table.

**Functionality**:
* Looks up the location by city and state name (case-insensitive partial match)
* Retrieves all alert documents (where `source_type='alert'`)
* Returns narrative texts with metadata (event type, headline, issued time, etc.)
* Sorts results by most recent first

**Database Tables Used**:
* `location`: Location lookup by city/state
* `weather_documents`: Source of alert documents (does NOT use vector embeddings)

**Parameters**:
* `city` (required): City name (e.g., "Boston")
* `state` (optional): State abbreviation or name (e.g., "MA")
* `limit` (optional): Maximum number of alerts to return (default: 10)

**Example Usage**:
```python
result = get_city_alerts(city="Boston", state="MA")
```

**Response**:
```json
{
    "status": "success",
    "location": {
        "loc_id": 1,
        "city": "Boston",
        "state": "MA",
        "latitude": 42.3601,
        "longitude": -71.0589
    },
    "count": 2,
    "alerts": [
        {
            "document_id": 456,
            "event": "Winter Storm Warning",
            "headline": "Winter Storm Warning in effect from Monday 6:00 PM to Tuesday 6:00 AM",
            "narrative_text": "Heavy snow expected. Total snow accumulations of 8 to 12 inches...",
            "is_day_time": null,
            "issued_at": "2024-03-26 08:00:00",
            "effective_at": "2024-03-26 18:00:00",
            "expires_at": "2024-03-27 06:00:00",
            "created_at": "2024-03-26 08:05:00"
        }
    ]
}
```

### 4. List All Locations Tool (`list_all_locations`)

**Purpose**: Retrieve all tracked locations from the database.

**Functionality**:
* Lists all locations in the location table
* Optional filter by author email
* Returns complete location metadata including grid coordinates
* Sorts by most recently created first

**Parameters**:
* `author` (optional): Filter by author email (default: None, returns all)
* `limit` (optional): Maximum number of locations to return (default: 100)

**Example Usage**:
```python
result = list_all_locations(limit=50)
```

**Response**:
```json
{
    "status": "success",
    "count": 3,
    "locations": [
        {
            "loc_id": 1,
            "city": "Boston",
            "state": "MA",
            "latitude": 42.3601,
            "longitude": -71.0589,
            "grid_id": "BOX",
            "grid_x": 71,
            "grid_y": 90,
            "author": "user@example.com",
            "created_at": "2024-03-26 10:00:00"
        }
    ]
}
```

### 5. Ask About Going Outside Tool (`ask_about_going_outside`) 🆕

**Purpose**: Use semantic search with vector embeddings to answer questions about going outside based on weather conditions.

**Functionality**:
* Takes a natural language question about outdoor activities
* Generates an embedding for the question using sentence-transformers
* Performs vector similarity search against weather document embeddings
* Returns the most relevant forecast and alert information
* Provides an AI-generated summary recommendation
* Falls back to recent documents if no embeddings are available

**Parameters**:
* `city` (required): City name (e.g., "Boston")
* `state` (optional): State abbreviation or name (e.g., "MA")
* `question` (optional): Natural language question (default: "Should I go outside? Is the weather good for outdoor activities?")
* `limit` (optional): Maximum number of relevant documents to return (default: 5)

**Example Usage**:
```python
# Default question
result = ask_about_going_outside(city="Boston", state="MA")

# Custom question
result = ask_about_going_outside(
    city="Boston",
    state="MA",
    question="Do I need an umbrella today?"
)

# Another example
result = ask_about_going_outside(
    city="New York",
    state="NY",
    question="Is it safe to go running outside?"
)
```

**Response**:
```json
{
    "status": "success",
    "method": "semantic_search",
    "location": {
        "city": "Boston",
        "state": "MA"
    },
    "question": "Do I need an umbrella today?",
    "summary": "🌧️ Rain or storms expected. Bring an umbrella if going outside.",
    "count": 3,
    "results": [
        {
            "document_id": 123,
            "source_type": "forecast",
            "event": "Tonight",
            "headline": "Tonight",
            "narrative_text": "Rain likely. Cloudy, with a low around 45. Chance of precipitation is 70%.",
            "relevant_chunk": "Rain likely. Cloudy, with a low around 45.",
            "similarity_score": 0.87,
            "issued_at": "2024-03-26 10:00:00",
            "effective_at": "2024-03-26 18:00:00",
            "expires_at": "2024-03-27 06:00:00"
        }
    ]
}
```

**AI-Generated Summary Types**:
* ⚠️ Weather alerts active
* ⚠️ Severe weather expected
* 🌧️ Rain or storms expected
* ❄️ Winter weather expected
* ☀️ Hot weather expected
* ✅ Good weather conditions
* General typical conditions

**Key Features**:
1. **Semantic Search**: Uses vector embeddings to understand the intent of your question
2. **Contextual Answers**: Returns the most relevant weather information, not just all forecasts
3. **Smart Recommendations**: Generates a quick summary based on weather conditions
4. **Flexible Questions**: Ask any weather-related question in natural language
5. **Fallback Mode**: If embeddings aren't available, returns recent weather documents

**Requirements**:
* `sentence-transformers` library must be installed
* Weather embeddings must be generated for the location (using the embedding pipeline)
* PostgreSQL with pgvector extension for vector similarity search

**Database Tables Used**:
* `weather_embeddings`: Full document embeddings (primary search target)
* `weather_chunk_embeddings`: Chunked embeddings for long narratives (fallback/supplementary)
* `weather_documents`: Source documents for forecast and alert details
* `location`: Location metadata and coordinates

## Technical Details

### External APIs Used

1. **OpenStreetMap Nominatim API**
   * Purpose: Geocoding city names to latitude/longitude coordinates
   * URL: `https://nominatim.openstreetmap.org/search`
   * Authentication: None required (free service)
   * Rate limits: Fair use policy (max 1 request per second)

2. **National Weather Service (NWS) API**
   * Purpose: Fetching grid metadata for weather locations
   * Base URL: `https://api.weather.gov`
   * Endpoint: `/points/{latitude},{longitude}`
   * Authentication: None required (public API)
   * Returns: grid_id, grid_x, grid_y for weather forecast retrieval

### Database Connection

The server uses the `lakebase.py` module to connect to Lakebase (Databricks-managed Postgres):
* Connection details stored in Databricks secrets
* Secret scope: `database` (configurable via `LAKEBASE_SECRET_SCOPE`)
* Secret key: `lakebase-url` (configurable via `LAKEBASE_SECRET_KEY`)
* Connection string format: `postgresql://role:password@host:5432/databricks_postgres?sslmode=require`

### Environment Variables

* `LOCATION_TABLE_NAME`: Name of the location table (default: "location")
* `WEATHER_DOCUMENTS_TABLE_NAME`: Name of the weather documents table (default: "weather_documents")
* `EMBEDDINGS_TABLE_NAME`: Name of the embeddings table (default: "weather_embeddings")
* `CHUNK_EMBEDDINGS_TABLE_NAME`: Name of the chunk embeddings table (default: "weather_chunk_embeddings")
* `EMBEDDING_MODEL_NAME`: Sentence-transformers model name (default: "sentence-transformers/all-MiniLM-L6-v2")
* `NWS_API_BASE_URL`: Base URL for NWS API (default: "https://api.weather.gov")
* `DATABRICKS_APP_PORT` or `PORT`: Server port (default: 8000)

## Deployment

### Running Locally

```bash
python weather_mcp_server.py
```

The server will start on port 8000 (or the port specified by `DATABRICKS_APP_PORT` or `PORT` environment variables).

### Deploying as Databricks Apps

#### Option A: MCP Server Only (Backend)

For Agent Bricks integration or when you need MCP protocol access:

1. Use the provided `app.yaml` file
2. Deploy using Databricks Apps CLI or UI
3. Register the app URL as an external MCP server in Agent Bricks

```bash
databricks apps deploy weather-mcp-server --config app.yaml
```

**app.yaml Configuration:**
```yaml
command:
  - "python"
  - "weather_mcp_server.py"

resources:
  - name: requirements
    source:
      path: ./requirements.txt

env:
  - name: LAKEBASE_SECRET_SCOPE
    value: "database"
  - name: LAKEBASE_SECRET_KEY
    value: "lakebase-url"
  - name: NWS_API_BASE_URL
    value: "https://api.weather.gov"
  - name: LOCATION_TABLE_NAME
    value: "location"
  - name: WEATHER_DOCUMENTS_TABLE_NAME
    value: "weather_documents"
  - name: CHUNK_EMBEDDINGS_TABLE_NAME
    value: "weather_chunk_embeddings"
  - name: EMBEDDING_MODEL_NAME
    value: "sentence-transformers/all-MiniLM-L6-v2"
```

#### Option B: Weather Agent (Full User Interface)

For a complete chat interface where end-users can interact:

1. Use the provided `weather_agent_app.yaml` file
2. Deploy using Databricks Apps CLI or UI
3. Share the app URL with users

```bash
databricks apps deploy weather-agent --config weather_agent_app.yaml
```

**weather_agent_app.yaml Configuration:**
```yaml
command:
  - "streamlit"
  - "run"
  - "weather_agent.py"
  - "--server.port=8501"
  - "--server.address=0.0.0.0"

resources:
  - name: requirements
    source:
      path: ./requirements.txt

env:
  - name: LAKEBASE_SECRET_SCOPE
    value: "database"
  - name: LAKEBASE_SECRET_KEY
    value: "lakebase-url"
  - name: NWS_API_BASE_URL
    value: "https://api.weather.gov"
  # ... (same env vars as above)
```

#### Which One Should I Deploy?

| Use Case | Deploy |
|----------|--------|
| Building an Agent Bricks agent | `app.yaml` (MCP Server) |
| Integrating with external MCP clients | `app.yaml` (MCP Server) |
| Providing a chat UI to end-users | `weather_agent_app.yaml` (Weather Agent) |
| Testing locally with a UI | `weather_agent_app.yaml` (Weather Agent) |
| Both! (separate apps) | Deploy both with different names |

## Change Log

### 2024-03-26 - Semantic Search Tool Added

**Added**:
* `ask_about_going_outside`: Semantic search tool using vector embeddings to answer weather-related questions

**New Features**:
* Natural language question answering about outdoor activities
* Vector similarity search using sentence-transformers and pgvector
* AI-generated weather recommendations (alerts, severe weather, rain, snow, heat, good conditions)
* Fallback to recent weather documents when embeddings are unavailable
* Support for PostgreSQL pgvector extension for cosine similarity search

**New Dependencies**:
* `sentence-transformers`: For generating question embeddings
* PostgreSQL with pgvector extension: For vector similarity operations

**New Environment Variables**:
* `EMBEDDINGS_TABLE_NAME`: Name of the embeddings table (default: "weather_embeddings")
* `CHUNK_EMBEDDINGS_TABLE_NAME`: Name of the chunk embeddings table (default: "weather_chunk_embeddings")
* `EMBEDDING_MODEL_NAME`: Sentence-transformers model name (default: "sentence-transformers/all-MiniLM-L6-v2")

### 2024-03-26 - Initial Implementation

**Created**:
* `weather_mcp_server.py`: Main MCP server implementation
* `WEATHER_MCP_README.md`: This documentation file

**Implemented Tools**:
1. `add_city_location`: Add cities to the location tracking database
2. `get_city_forecasts`: Retrieve forecast narratives for a city
3. `get_city_alerts`: Retrieve weather alert narratives for a city
4. `list_all_locations`: List all tracked weather locations

**Key Features**:
* Automatic geocoding of city names to coordinates
* Integration with National Weather Service API for grid metadata
* Database upsert logic (insert or update on conflict)
* Case-insensitive city/state lookups
* Comprehensive error handling and logging

## Future Enhancements

### Potential Features

1. **Batch Location Management**
   * Add multiple cities at once
   * Import locations from CSV/JSON

2. **Real-time Weather Sync**
   * Tool to trigger weather data sync for specific cities
   * Automatic refresh of forecasts and alerts

3. **Vector Search Integration** ✅ IMPLEMENTED
   * Semantic search across weather narratives (via `ask_about_going_outside`)
   * AI-generated recommendations based on weather conditions
   * Additional enhancements: Similar weather event discovery, advanced filtering

4. **Location Deletion**
   * Tool to remove cities from tracking
   * Cascade delete related weather documents

5. **Weather History**
   * Historical weather data retrieval
   * Time-range queries for forecasts/alerts

6. **Enhanced Geocoding**
   * Support for ZIP codes
   * Support for coordinates (lat/lon) directly
   * Multiple geocoding service fallbacks

7. **Caching Layer**
   * Cache geocoding results
   * Cache NWS grid metadata
   * Reduce API calls and improve performance

## Dependencies

### Python Packages

**Core (required for both MCP server and agent):**
* `fastmcp`: MCP server framework
* `psycopg2`: PostgreSQL database adapter
* `requests`: HTTP client for external APIs
* `databricks-sdk`: Databricks SDK for secrets management
* `sqlalchemy`: SQL toolkit (via lakebase.py)
* `sentence-transformers`: For semantic search and vector embeddings (required for `ask_about_going_outside` tool)

**Weather Agent Only:**
* `streamlit`: Web UI framework for the chat interface

### Install Requirements

**For MCP Server only:**
```bash
pip install fastmcp psycopg2-binary requests databricks-sdk sqlalchemy sentence-transformers
```

**For Weather Agent (includes all MCP server dependencies):**
```bash
pip install fastmcp psycopg2-binary requests databricks-sdk sqlalchemy sentence-transformers streamlit
```

**Or install from requirements.txt:**
```bash
pip install -r requirements.txt
```

## Testing

### Manual Testing Steps

1. **Test Add Location**:
   ```python
   add_city_location(city="Boston", state="MA")
   add_city_location(city="New York", state="NY")
   add_city_location(city="San Francisco", state="CA")
   ```

2. **Test List Locations**:
   ```python
   list_all_locations()
   ```

3. **Test Get Forecasts**:
   ```python
   get_city_forecasts(city="Boston", state="MA", limit=5)
   ```

4. **Test Get Alerts**:
   ```python
   get_city_alerts(city="Boston", state="MA")
   ```

5. **Test Ask About Going Outside** (requires embeddings):
   ```python
   # Default question
   ask_about_going_outside(city="Boston", state="MA")
   
   # Custom question
   ask_about_going_outside(
       city="Boston",
       state="MA",
       question="Do I need an umbrella today?"
   )
   
   # Another question
   ask_about_going_outside(
       city="Boston",
       state="MA",
       question="Is it safe to go running outside?"
   )
   ```

### Testing the Weather Agent

1. **Start the agent locally**:
   ```bash
   cd mcp_server
   streamlit run weather_agent.py
   ```

2. **Test natural language queries**:
   - Type: "Add Boston, MA"
   - Type: "What's the forecast for Boston?"
   - Type: "Should I go outside in Boston?"
   - Type: "Any alerts for Boston?"
   - Type: "List my cities"

3. **Test intent parsing variations**:
   - "Add Seattle, WA"
   - "Track New York"
   - "Monitor Chicago, IL"
   - "Weather for Miami"
   - "Forecast in Seattle, WA"
   - "Do I need an umbrella in New York?"
   - "Is it safe to walk in Boston?"
   - "Show warnings for Chicago"

4. **Verify response formatting**:
   - Check emojis appear correctly (📍 ☀️ 🌧️ ❄️ ⚠️ ✅)
   - Verify markdown formatting (bold, italics)
   - Confirm location info displays properly
   - Test similarity scores in semantic search results

### Integration Testing

* Test with Agent Bricks agent to verify MCP protocol compatibility
* Verify all tools are discoverable via MCP client
* Test error handling with invalid inputs (non-existent cities, etc.)

## Troubleshooting

### Common Issues

1. **Geocoding Fails**
   * Verify city/state spelling
   * Check OpenStreetMap Nominatim API status
   * Ensure proper User-Agent header is set

2. **NWS API Errors**
   * Coordinates may be outside NWS coverage (primarily US only)
   * API may be temporarily unavailable
   * Check API status: https://api.weather.gov/

3. **Database Connection Issues**
   * Verify Lakebase secret scope and key are configured
   * Check database connection string format
   * Ensure proper permissions on the database

4. **Location Not Found**
   * City may not exist in the database yet
   * Try adding the location first with `add_city_location`
   * Check spelling of city/state names

5. **Semantic Search Issues**
   * Ensure `sentence-transformers` library is installed: `pip install sentence-transformers`
   * Verify weather embeddings have been generated for the location
   * Check that PostgreSQL has pgvector extension enabled
   * If embeddings are missing, the tool falls back to recent weather documents
   * Embedding model download may take time on first use (cached for subsequent calls)

## References

* **National Weather Service API**: https://www.weather.gov/documentation/services-web-api
* **OpenStreetMap Nominatim**: https://nominatim.org/release-docs/latest/api/Search/
* **FastMCP Documentation**: https://github.com/jlowin/fastmcp
* **Databricks Apps**: https://docs.databricks.com/aws/en/apps/index.html
* **Model Context Protocol**: https://modelcontextprotocol.io/

---

**Last Updated**: 2024-03-26  
**Author**: Khubaib Sattar (khubainsattar@gmail.com)  
**Version**: 1.0.0
