"""
LangGraph agent with a built-in "ask a human" escape hatch.

The key idea: instead of guessing missing information, the agent is given
a tool called `ask_human`. When the model decides it needs to call that
tool, LangGraph's interrupt() pauses graph execution right there — state
is checkpointed, and control returns to whatever Python code invoked the
graph. That code can then go do something slow and external (like placing
a phone call) and, whenever it has an answer, resume the exact same graph
run with Command(resume=answer).

No polling, no webhook needed for this half of the system — it's just a
function call that pauses.
"""

import os

from langchain_core.tools import tool
from langchain_groq import ChatGroq
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import interrupt

GROQ_API_KEY = os.environ["GROQ_API_KEY"]


@tool
def ask_human(question: str) -> str:
    """
    Ask a human operator a question when you are missing information
    required to complete the task (e.g. a credential, a destination,
    a value only a person would know).

    Do NOT guess, invent, or use placeholder values for anything you're
    unsure about — call this tool instead and wait for a real answer.
    """
    answer = interrupt({"question": question})
    return answer


TOOLS = [ask_human]


# openai/gpt-oss-120b: OpenAI's open-weight model, served on Groq.
# Standard tool-calling format — no thought_signature-style requirement
# to worry about.
llm = ChatGroq(model="openai/gpt-oss-120b", api_key=GROQ_API_KEY)
llm_with_tools = llm.bind_tools(TOOLS)

SYSTEM_PROMPT = (
    "You are an autonomous coding/ops agent. You will be given a task. "
    "If the task is missing information you need (credentials, phone "
    "numbers, environment variable names, destinations, etc.), you MUST "
    "call the ask_human tool with a clear, specific question rather than "
    "guessing or inventing a placeholder. Once you have everything you "
    "need, respond with a final plain-text summary of what you would do."
)


def agent_node(state: MessagesState):
    messages = [("system", SYSTEM_PROMPT)] + state["messages"]
    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}


builder = StateGraph(MessagesState)
builder.add_node("agent", agent_node)
builder.add_node("tools", ToolNode(TOOLS))

builder.add_edge(START, "agent")
# tools_condition routes to "tools" if the last message has tool_calls,
# otherwise routes to END.
builder.add_conditional_edges("agent", tools_condition)
builder.add_edge("tools", "agent")

# A checkpointer is REQUIRED for interrupt()/resume to work — without one
# there's no saved state to resume from. InMemorySaver is fine for local
# testing; swap for a persistent one (e.g. SqliteSaver) once this needs to
# survive a process restart.
checkpointer = InMemorySaver()
graph = builder.compile(checkpointer=checkpointer)