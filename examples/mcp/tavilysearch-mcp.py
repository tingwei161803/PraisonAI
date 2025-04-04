from praisonaiagents import Agent, MCP
import os

# Use the API key from environment or set it directly
tavily_api_key = os.getenv("TAVILY_API_KEY")

# Use a single string command with environment variables
search_agent = Agent(
    instructions="""You are a helpful assistant that can search the web for information.
    Use the available tools when relevant to answer user questions.""",
    # llm="gpt-4o-mini",
    llm="ollama/mistral-small:24b",
    tools=MCP("npx -y tavily-mcp@0.1.4", env={"TAVILY_API_KEY": tavily_api_key})
)

search_agent.start("Search more information about AI News")