print("🤖 WHALE AGENT STARTING - TEST RUN")
import os
from dotenv import load_dotenv
load_dotenv()
print("🤖 1/6 | PolyWhale Agent v1.0 LIVE")

# Imports with checks
try:
    from crewai import Agent, Task, Crew
    print("✅ 2/6 | CrewAI ready")
except Exception as e:
    print(f"❌ CrewAI error: {e}")
    exit(1)

try:
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(model="gpt-4o-mini")
    print("✅ 3/6 | OpenAI ready")
except Exception as e:
    print(f"❌ OpenAI error: {e} (check OPENAI_API_KEY)")
    exit(1)

print("✅ 4/6 | Edge prob 75% (Axios/kch123 whales)")

# Mock trade logic
markets = [{"id": "trump-2028", "price": 0.52}]
our_p = 0.75
kelly_f = 0.032
size = 320
print(f"✅ 5/6 | Market: trump@0.52 → Kelly {kelly_f:.1%} = ${size}")

print("💰 6/6 | TRADE SIGNAL: BUY YES trump-2028 ${size} @0.52")
print("✅ AGENT COMPLETE - Cron will repeat every 6h")
