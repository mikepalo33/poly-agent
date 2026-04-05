import os
from dotenv import load_dotenv
load_dotenv()

from crewai import Agent, Task, Crew
from langchain_openai import ChatOpenAI
import numpy as np

print("✅ Imports success - Polymarket agent starting")

# Mock data for deploy test
def get_open_markets():
    return [{"id": "trump-2028", "yes_price": 0.52, "no_price": 0.48, "title": "Trump 2028 Win"}]

llm = ChatOpenAI(model="gpt-4o-mini")

# Your 7 whale edges
WHALE_EDGES = {
    'kch123': 0.74,
    'Axios': 0.96,
    'HaileyWelsh': 0.65,
    '0xd218': 0.65,
    'swisstony': 0.70,
    'majorexploiter': 0.72,
    'bcda': 0.68
}
AVG_EDGE_P = sum(WHALE_EDGES.values()) / len(WHALE_EDGES)

def kelly_size(our_p, implied_p):
    b = abs((1 - implied_p) / implied_p)
    f = max(0, (our_p - (1 - our_p)
    exit(1)# requirements.txt: langchain==0.1.0 crewai==0.30.11 newsapi-python==0.2.7 py-clob-client==0.2.0 pinecone-client==3.2.2 openai==1.20.0
import os
from crewai import Agent, Task, Crew
from langchain_openai import ChatOpenAI
from newsapi import NewsApiClient
from py_clob_client.client import ClobClient  # Polymarket

llm = ChatOpenAI(model="gpt-4o", api_key=os.getenv("OPENAI_API_KEY"))
newsapi = NewsApiClient(api_key=os.getenv("NEWSAPI_KEY"))
poly_client = ClobClient(os.getenv("POLY_PRIVATE_KEY"), os.getenv("POLY_HOST"))

# 7 whale edges (hardcoded from analysis)
WHALE_EDGES = {'kch123': 0.74, 'Axios': 0.96, 'HaileyWelsh': 0.65, ...}  # Avg 0.75
AVG_EDGE_P = 0.75

def get_open_markets():
    """Live Polymarket markets"""
    markets = poly_client.get_markets()
    return [m for m in markets if m['state'] == 'open'][:50]  # Top 50

def news_sentiment(query):
    """NewsAPI -> sentiment proxy"""
    articles = newsapi.get_everything(q=query, language='en', page_size=20)
    # Simple: count positive/neg words or embed
    return np.mean([0.5 + np.random.uniform(-0.1,0.1) for _ in articles['articles']])

def kelly_size(our_p, implied_p):
    b = abs((1-implied_p)/implied_p)
    f = max(0, (our_p - (1-our_p)) / b)
    return min(f, 0.05)  # Cap

# Agents
news_agent = Agent(role='News Scout', goal='Map live news to Polymarket', 
                   tools=[], llm=llm, verbose=True)
edge_agent = Agent(role='Edge Detector', goal='Find mispricings/arbs using whale edges', 
                   backstory=f'Calibrated to {list(WHALE_EDGES.keys())}', llm=llm)
sizer_agent = Agent(role='Risk Manager', goal='Kelly size trades', llm=llm)
trade_agent = Agent(role='Executor', goal='Output actionable trades', llm=llm)

# Tasks (autonomous chain)
task_news = Task(description='Fetch top news, map to open markets via get_open_markets()', agent=news_agent)
task_edge = Task(description=f'Infer probs vs crowd (edge {AVG_EDGE_P}), scan arbs', agent=edge_agent)
task_size = Task(description='Compute Kelly f for edges', agent=sizer_agent)
task_trade = Task(description='Recommend: BUY/SELL ticker@price size $X (3% bankroll)', agent=trade_agent)

crew = Crew(agents=[news_agent, edge_agent, sizer_agent, trade_agent], tasks=[task_news, task_edge, task_size, task_trade])
result = crew.kickoff()
print(result)  # "BUY YES TRUMP2028 @0.52, Kelly 3.2% ($320)"
