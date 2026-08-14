import streamlit as st
import re
from typing import Dict, Any, List, Tuple, Optional

# Import MCP server functions
from weather_mcp_server import (
    add_city_location,
    get_city_forecasts,
    get_city_alerts,
    list_all_locations,
    ask_about_going_outside
)

# Page configuration
st.set_page_config(
    page_title="Weather AI Agent",
    page_icon="🌦️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize chat history
if "messages" not in st.session_state:
    st.session_state.messages = []

# Initialize welcome message
if len(st.session_state.messages) == 0:
    st.session_state.messages.append({
        "role": "assistant",
        "content": """👋 Welcome to the Weather AI Agent!

I can help you with:
• 📍 Track new cities
• 🌤️ Get weather forecasts
• ⚠️ Check weather alerts
• 🤔 Answer questions about outdoor activities
• 📋 List your tracked locations

Try asking: *"What's the weather forecast for Seattle?"*
        """
    })


def parse_user_intent(user_input: str) -> Tuple[str, Dict[str, Any]]:
    """Parse user input to determine intent and extract parameters."""
    user_input_lower = user_input.lower().strip()
    
    # Help intent
    if any(word in user_input_lower for word in ['help', 'what can you do', 'how do i']):
        return 'help', {}
    
    # List locations intent
    if any(phrase in user_input_lower for phrase in [
        'list', 'show all', 'my cities', 'my locations', 'tracked cities', 'tracked locations'
    ]):
        return 'list_locations', {}
    
    # Add location intent
    add_patterns = [
        r'add\s+(?:city\s+)?(.+)',
        r'track\s+(?:city\s+)?(.+)',
        r'start tracking\s+(.+)',
        r'monitor\s+(.+)'
    ]
    for pattern in add_patterns:
        match = re.search(pattern, user_input_lower)
        if match:
            location_str = match.group(1).strip()
            # Try to parse city, state
            parts = [p.strip() for p in location_str.split(',')]
            if len(parts) == 2:
                return 'add_location', {'city': parts[0], 'state': parts[1]}
            else:
                return 'add_location', {'city': location_str, 'state': None}
    
    # Alerts intent
    alert_patterns = [
        r'alerts?\s+(?:for\s+)?(.+)',
        r'warnings?\s+(?:for\s+)?(.+)',
        r'any alerts\s+(?:in\s+)?(.+)'
    ]
    for pattern in alert_patterns:
        match = re.search(pattern, user_input_lower)
        if match:
            city = match.group(1).strip().replace('in ', '').replace('for ', '')
            return 'get_alerts', {'city': city}
    
    # Forecast intent
    forecast_patterns = [
        r'forecast\s+(?:for\s+)?(.+)',
        r'weather\s+(?:in\s+|for\s+)?(.+)',
        r"what'?s?\s+the\s+weather\s+(?:in\s+|for\s+)?(.+)"
    ]
    for pattern in forecast_patterns:
        match = re.search(pattern, user_input_lower)
        if match:
            city = match.group(1).strip().replace('in ', '').replace('for ', '')
            return 'get_forecast', {'city': city}
    
    # Semantic outdoor question (fallback for natural language questions)
    question_words = ['should', 'can', 'is it', 'will it', 'good for', 'safe to', 'okay to']
    if any(word in user_input_lower for word in question_words):
        return 'semantic_search', {'question': user_input}
    
    # Default to semantic search for anything else
    return 'semantic_search', {'question': user_input}


def execute_intent(intent: str, params: Dict[str, Any]) -> str:
    """Execute the parsed intent and return formatted response."""
    
    if intent == 'help':
        return """🌦️ **Weather AI Agent Help**

**Available Commands:**

📍 **Track a City:**
- "Add Seattle, WA"
- "Track Denver, CO"

🌤️ **Get Forecast:**
- "Weather forecast for Seattle"
- "What's the weather in Portland?"

⚠️ **Check Alerts:**
- "Alerts for Miami"
- "Any warnings in Chicago?"

🤔 **Ask Questions:**
- "Should I go hiking today in Seattle?"
- "Is it safe to drive in Denver?"

📋 **List Tracked Cities:**
- "List my cities"
- "Show all tracked locations"
        """
    
    elif intent == 'list_locations':
        result = list_all_locations()
        if result.get('status') == 'error':
            return f"❌ {result.get('message', 'Failed to list locations')}"
        
        locations = result.get('locations', [])
        if not locations:
            return "📭 No tracked locations yet. Add one with: *'Add Seattle, WA'*"
        
        response = f"📍 **Tracked Locations** ({len(locations)} total):\n\n"
        for loc in locations:
            response += f"• **{loc['city']}, {loc['state']}** (Lat: {loc['latitude']}, Lon: {loc['longitude']})\n"
        return response
    
    elif intent == 'add_location':
        city = params.get('city')
        state = params.get('state')
        
        if not city:
            return "❌ Please provide a city name. Example: *'Add Seattle, WA'*"
        
        # First, check if the city already exists
        with st.spinner(f"Checking if {city} is already tracked..."):
            existing_locations = list_all_locations()
        
        if existing_locations.get('status') == 'success':
            locations = existing_locations.get('locations', [])
            # Check if city already exists (case-insensitive comparison)
            for loc in locations:
                loc_city = loc.get('city', '').lower()
                loc_state = loc.get('state', '').lower()
                check_city = city.lower()
                check_state = state.lower() if state else ''
                
                # Match on city name, and state if provided
                if loc_city == check_city:
                    if not state or loc_state == check_state:
                        return f"ℹ️ **{loc['city']}, {loc['state']}** is already being tracked!\nLat: {loc['latitude']}, Lon: {loc['longitude']}"
        
        # City doesn't exist, proceed to add it
        with st.spinner(f"Adding {city}..."):
            result = add_city_location(city=city, state=state)
        
        if result.get('status') == 'error':
            # Check if it's a multiple results error
            if result.get('multiple_results'):
                response = f"⚠️ {result.get('message', 'Multiple locations found')}\n\n"
                response += f"**Found {result.get('count')} matching locations:**\n\n"
                
                for i, option in enumerate(result.get('options', []), 1):
                    response += f"{i}. **{option['display_name']}**\n"
                    response += f"   📍 Lat: {option['lat']}, Lon: {option['lon']}\n"
                    if option.get('type'):
                        response += f"   🏷️ Type: {option['type']}\n"
                    response += "\n"
                
                response += "\n💡 *Please specify the state or provide more details. Example: 'Add Springfield, IL'*"
                return response
            else:
                return f"❌ {result.get('message', 'Unknown error')}"
        
        # Extract location data from the nested structure
        location_data = result.get('location', {})
        return f"✅ Successfully added **{location_data.get('city')}, {location_data.get('state')}**\nLat: {location_data.get('latitude')}, Lon: {location_data.get('longitude')}"
    
    elif intent == 'get_forecast':
        city = params.get('city')
        
        if not city:
            return "❌ Please specify a city. Example: *'Forecast for Seattle'*"
        
        with st.spinner(f"Fetching forecast for {city}..."):
            result = get_city_forecasts(city=city)
        
        if result.get('status') in ['error', 'not_found']:
            return f"❌ {result.get('message', 'Failed to retrieve forecast')}"
        
        forecasts = result.get('forecasts', [])
        if not forecasts:
            return f"📭 No forecast data available for {city}"
        
        location = result.get('location', {})
        response = f"🌤️ **Weather Forecast for {location.get('city', city)}, {location.get('state', '')}**\n\n"
        for i, fc in enumerate(forecasts[:5], 1):  # Show first 5 periods
            response += f"**{fc['name']}:**\n"
            response += f"🌡️ {fc['temperature']}°{fc['temperatureUnit']} | {fc['shortForecast']}\n"
            response += f"💨 Wind: {fc.get('windSpeed', 'N/A')} {fc.get('windDirection', '')}\n"
            if fc.get('detailedForecast'):
                response += f"📝 {fc['detailedForecast']}\n"
            response += "\n"
        
        return response
    
    elif intent == 'get_alerts':
        city = params.get('city')
        
        if not city:
            return "❌ Please specify a city. Example: *'Alerts for Miami'*"
        
        with st.spinner(f"Checking alerts for {city}..."):
            result = get_city_alerts(city=city)
        
        if result.get('status') in ['error', 'not_found']:
            return f"❌ {result.get('message', 'Failed to retrieve alerts')}"
        
        alerts = result.get('alerts', [])
        location = result.get('location', {})
        if not alerts:
            return f"✅ No active weather alerts for {location.get('city', city)}, {location.get('state', '')}"
        
        response = f"⚠️ **Weather Alerts for {city}** ({len(alerts)} active)\n\n"
        for alert in alerts[:3]:  # Show top 3 alerts
            response += f"**{alert.get('event', 'Alert')}**\n"
            response += f"📅 Effective: {alert.get('effective', 'N/A')}\n"
            if alert.get('headline'):
                response += f"📰 {alert['headline']}\n"
            response += "\n"
        
        return response
    
    elif intent == 'semantic_search':
        question = params.get('question')
        
        if not question:
            return "❌ Please ask a question about weather or outdoor activities."
        
        with st.spinner("Searching weather information..."):
            result = ask_about_going_outside(question=question)
        
        if result.get('status') in ['error', 'not_found']:
            return f"❌ {result.get('message', 'Failed to search weather information')}"
        
        if 'answer' not in result:
            return "❌ Could not generate an answer. Please try rephrasing your question."
        
        response = f"🤔 **Answer:**\n\n{result['answer']}\n\n"
        
        # Show similar documents if available
        if 'similar_documents' in result and result['similar_documents']:
            response += "\n📚 **Related Weather Information:**\n\n"
            for i, doc in enumerate(result['similar_documents'][:2], 1):
                similarity = doc.get('similarity_score', 0)
                response += f"{i}. **Similarity: {similarity:.2%}**\n"
                response += f"   City: {doc.get('city', 'N/A')}\n"
                snippet = doc.get('chunk_text', '')[:200]
                response += f"   _{snippet}..._\n\n"
        
        return response
    
    else:
        return "❌ Sorry, I didn't understand that. Type 'help' for available commands."


# Main UI
st.title("🌦️ Weather AI Agent")
st.markdown("*Your intelligent weather assistant powered by National Weather Service*")

# Sidebar
with st.sidebar:
    st.header("🚀 Quick Actions")
    
    if st.button("📋 List Tracked Cities", use_container_width=True):
        st.session_state.messages.append({"role": "user", "content": "List my cities"})
        response = execute_intent('list_locations', {})
        st.session_state.messages.append({"role": "assistant", "content": response})
        st.rerun()
    
    if st.button("❓ Help", use_container_width=True):
        st.session_state.messages.append({"role": "user", "content": "Help"})
        response = execute_intent('help', {})
        st.session_state.messages.append({"role": "assistant", "content": response})
        st.rerun()
    
    st.divider()
    st.subheader("💡 Example Queries")
    st.markdown("""
    - Add Seattle, WA
    - Weather forecast for Portland
    - Alerts for Miami
    - Should I go hiking today?
    - Is it safe to drive in Denver?
    """)
    
    st.divider()
    if st.button("🗑️ Clear Chat History", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# Display chat messages
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Chat input
if prompt := st.chat_input("Ask about weather or outdoor activities..."):
    # Add user message
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    
    # Parse intent and execute
    intent, params = parse_user_intent(prompt)
    response = execute_intent(intent, params)
    
    # Add assistant response
    st.session_state.messages.append({"role": "assistant", "content": response})
    with st.chat_message("assistant"):
        st.markdown(response)