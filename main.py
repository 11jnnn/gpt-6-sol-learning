"""FastAPI 入口：路由 + 启动应用。

提供的接口：
  GET  /        返回聊天网页（static/index.html）
  POST /chat    接收消息与历史，返回回答 + 工具调用记录
  GET  /health  健康检查
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from agent import ConfigError, chat
import changes

# 启动时加载 .env（若存在），把 DEEPSEEK_API_KEY 等读进环境变量。
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="个人 AI 助手")


class ChatRequest(BaseModel):
    """POST /chat 的请求体：本次消息 + 之前的历史 + 任务 id。"""
    message: str
    history: list[dict] = []
    task_id: str = "default"


class ChangeAction(BaseModel):
    """对某个待确认修改执行操作。"""
    change_id: str


class CheckAuth(BaseModel):
    """pytest 检查授权（按 task_id 隔离）。"""
    task_id: str = "default"
    project: str
    check_type: str


class DenyCheck(BaseModel):
    task_id: str = "default"


@app.get("/", response_class=HTMLResponse)
def index():
    """返回聊天网页。前端用原生 HTML/CSS/JS，无框架。"""
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health():
    """健康检查：服务是否在运行。"""
    return {"status": "ok"}


@app.post("/chat")
def chat_endpoint(req: ChatRequest):
    """处理一条用户消息。

    这就是「FastAPI 如何把最终结果返回网页」：调用 agent.chat 得到回答和工具记录，
    再以 JSON 返回，前端 fetch 拿到后渲染到消息列表里。
    """
    if not req.message.strip():
        return JSONResponse(status_code=400, content={"error": "消息不能为空。"})

    try:
        return chat(req.message, req.history, req.task_id)
    except ConfigError as exc:
        # 未配置密钥等配置问题：给用户清楚的提示。
        return JSONResponse(status_code=500, content={"error": str(exc)})
    except Exception as exc:  # 模型连接失败、网络错误等
        return JSONResponse(
            status_code=502,
            content={"error": f"调用模型失败：{exc}"},
        )


@app.post("/change/apply")
def change_apply(req: ChangeAction):
    """用户点击「确认并应用」后，后端才真正写入。apply 不注册为模型工具。"""
    return changes.apply_change(req.change_id)


@app.post("/change/reject")
def change_reject(req: ChangeAction):
    return changes.reject_change(req.change_id)


@app.post("/change/restore")
def change_restore(req: ChangeAction):
    return changes.restore_change(req.change_id)


@app.post("/check/authorize")
def check_authorize(req: CheckAuth):
    """授权对某项目运行 pytest（只允许 pytest 这一种检查类型，按 task_id 隔离）。"""
    if req.check_type != "pytest":
        return JSONResponse(status_code=400, content={"error": "仅支持授权 pytest。"})
    return changes.authorize_pytest(req.task_id, req.project)


@app.post("/check/deny")
def check_deny(req: DenyCheck):
    return changes.deny_pytest(req.task_id)


@app.post("/task/reset")
def task_reset():
    """开始新任务：清空任务状态与待确认修改。"""
    return changes.reset_task()
