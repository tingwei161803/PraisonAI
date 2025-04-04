from praisonaiagents import Agent, MCP

stock_agent = Agent(
    instructions="""You are a Stock Price Assistant.""",
    llm="ollama/llama3.1",
    tools=MCP("/Users/praison/miniconda3/envs/mcp/bin/python /Users/praison/stockprice/app.py")
)

# NOTE: Python Path replace with yours: /Users/praison/miniconda3/envs/mcp/bin/python
# NOTE: app.py file path, replace it with yours: /Users/praison/stockprice/app.py

# stock_agent.start("What is the Stock Price of Apple?")

stock_agent.start("What is the Stock Price of NVIDIA?")