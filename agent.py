"""Agent 组装与对话执行：接入 DeepSeek、加载提示词、注册工具、跑通「模型↔工具」流程。

整体链路（对应 README 的完整流程）：
  用户消息 -> chat() 组装历史 -> create_agent 拿到 model + system_prompt + tools
  -> 模型决定是否调用工具 -> 需要时执行 Python 函数 -> 工具结果交回模型 -> 生成最终回答
"""
import os

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_deepseek import ChatDeepSeek
from langgraph.errors import GraphRecursionError

import changes
from tools import (
    create_project_file,
    list_local_projects,
    list_project_files,
    read_project_file,
    replace_project_text,
    run_project_check,
    search_project_code,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SYSTEM_PROMPT_PATH = os.path.join(BASE_DIR, "system_prompt.txt")

# Agent 循环上限：限制「模型调用 + 工具执行」的往返次数，避免无限循环。
MAX_RECURSION = 20


class ConfigError(Exception):
    """缺少必要配置（如 API Key）时抛出的异常，方便 main.py 给出清晰提示。"""


def load_system_prompt() -> str:
    """从 system_prompt.txt 读取系统提示词，返回纯文本。

    这就是「模型如何收到提示词」的第一步：提示词以字符串形式读入，
    稍后由 create_agent 作为 SystemMessage 放进每次发给模型的上下文里。
    """
    with open(SYSTEM_PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def build_agent():
    """创建并返回一个可用的 Agent。

    三个关键输入：
      1. model     —— DeepSeek 大模型（ChatDeepSeek）
      2. tools     —— 工具列表（这里是 list_local_projects）。create_agent 会把
                     每个工具的 name/description/参数 转成模型可读的“工具定义”，
                     模型据此决定何时调用哪个工具。
      3. system_prompt —— 系统提示词，作为 SystemMessage 注入。
    """
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ConfigError(
            "未配置 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入你的 DeepSeek API Key。"
        )

    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()

    # 使用官方适配包 langchain-deepseek 接入 DeepSeek。temperature=0 让工具调用更稳定。
    llm = ChatDeepSeek(model=model, api_key=api_key, temperature=0)

    # create_agent 是当前 LangChain 官方推荐的写法，内部替我们处理了
    # 「绑定工具 -> 调用模型 -> 执行工具 -> 回传结果」这一整套循环。
    # 这里注册全部工具：4 个只读 + 2 个提出修改 + 1 个运行检查。
    # 注意：apply/restore/authorize 不在工具列表里，只能由用户在前端触发后端执行。
    agent = create_agent(
        llm,
        tools=[
            list_local_projects,
            list_project_files,
            read_project_file,
            search_project_code,
            create_project_file,
            replace_project_text,
            run_project_check,
        ],
        system_prompt=load_system_prompt(),
    )
    return agent


def _history_to_messages(history):
    """把前端传来的历史记录（[{role, content}]）转成 LangChain 消息对象。"""
    msgs = []
    for item in history or []:
        role = item.get("role")
        content = item.get("content", "")
        if role == "user":
            msgs.append(HumanMessage(content=content))
        elif role == "assistant":
            msgs.append(AIMessage(content=content))
    return msgs


def _extract_tool_calls(messages):
    """从 Agent 产出的消息里，把「工具调用 + 工具结果」配对提取出来，用于前端展示。"""
    records = []
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                call_id = tc.get("id")
                result = ""
                # 找到与本次调用对应的 ToolMessage（工具执行结果）。
                for tm in messages:
                    if isinstance(tm, ToolMessage) and tm.tool_call_id == call_id:
                        result = tm.content
                        break
                records.append({
                    "name": tc.get("name"),
                    "args": tc.get("args", {}),
                    "result": result,
                })
    return records


def chat(message: str, history=None, task_id: str = "default"):
    """执行一轮对话：传入用户本次消息、历史与任务 id，返回回答 + 工具记录 + 待确认信息。

    返回结构：
      {"reply", "tool_calls", "pending_changes", "pending_check", "task"}
    任务按 task_id 隔离；propose/check 工具内部通过 contextvars 读到当前任务。
    """
    agent = build_agent()  # 未配置密钥时这里会抛 ConfigError

    # 组装消息：历史 + 本次用户消息。create_agent 会自己把 system_prompt 加到最前面。
    messages = _history_to_messages(history)
    messages.append(HumanMessage(content=message))

    # 把当前任务 id 写入 contextvar，让模型调用的 propose/check 工具拿到正确任务。
    changes.set_current_task(task_id)
    try:
        # 关键一步：invoke 触发完整循环——模型决定调用工具 -> 执行 -> 结果回传 -> 生成回答。
        # recursion_limit 限制往返次数，防止无限循环。
        try:
            result = agent.invoke(
                {"messages": messages},
                config={"recursion_limit": MAX_RECURSION},
            )
        except GraphRecursionError:
            return {
                "reply": (
                    "处理过程中工具调用次数达到上限，为避免无限循环已停止。"
                    "请尝试把问题拆小、缩小搜索范围，或更明确地指定要读取的文件。"
                ),
                "tool_calls": [],
                "pending_changes": changes.list_pending_summaries(task_id),
                "pending_check": changes.drain_pending_check(task_id),
                "task": changes.get_task_summary(task_id),
            }
    finally:
        changes.set_current_task(None)

    all_messages = result["messages"]

    # 最终回答是消息列表里最后一条有文本内容的 AIMessage。
    reply = ""
    for msg in reversed(all_messages):
        if isinstance(msg, AIMessage) and msg.content:
            reply = msg.content
            break

    return {
        "reply": reply,
        "tool_calls": _extract_tool_calls(all_messages),
        # 待确认修改、待确认检查、任务状态：按 task_id 隔离，前端据此渲染按钮。
        "pending_changes": changes.list_pending_summaries(task_id),
        "pending_check": changes.drain_pending_check(task_id),
        "task": changes.get_task_summary(task_id),
    }
