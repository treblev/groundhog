import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Annotated
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage

from config.settings import OLLAMA_SQL_MODEL

MAX_TOOL_ROUNDS = 8


class AgentState(TypedDict):
    question: str
    messages: Annotated[list, add_messages]  # auto-appends, never overwrites
    plan: str
    rounds: int
    answer: str


llm = ChatOllama(model=OLLAMA_SQL_MODEL)


def build_graph(schema: str, tool_names: list[str]):
    def plan_node(state: AgentState) -> dict:
        response = llm.invoke([
            SystemMessage(content=(
                "You are a planning assistant. Write a concise numbered plan: which tools to call, "
                "in what order, and why. Do NOT call any tools — only write the plan.\n\n"
                f"Available tools: {', '.join(tool_names)}\n\n"
                f"Database schema:\n{schema}"
            )),
            HumanMessage(content=f"Plan how to answer: {state['question']}"),
        ])
        plan = response.content.strip()
        if plan:
            print(f"  [plan] {plan[:300]}")
        return {"plan": plan}

    graph = StateGraph(AgentState)
    graph.add_node("plan", plan_node)
    graph.add_edge(START, "plan")
    # llm_node, tool_node, and remaining edges added in Steps 4-6
    graph.add_edge("plan", END)

    return graph.compile()
