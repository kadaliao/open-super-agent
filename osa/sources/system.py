"""osa 自己的事件：后台任务失败了。"""
from ..i18n import N_
from ..registry import INTERNAL, Source
from ._util import short


def match_task_failed(params: dict, routine: dict, task: dict, bot: dict, result: str = "", **_):
    if task.get("status") != "failed":
        return []
    if params.get("scope", "mine") == "mine" and task.get("bot_id") != routine["bot_id"]:
        return []
    goal = str(task.get("goal") or "")
    return [{"id": f"task-{task.get('id')}",
             "title": f"{bot.get('name', 'Bot')} 的任务 #{task.get('id')} 失败了：{short(goal.splitlines()[0] if goal else '', 80)}",
             "text": f"要做的事：\n{short(goal, 2000)}\n\n失败原因：\n{short(result or '', 2000)}"}]


TASK_FAILED = Source(
    id="task_failed", mode=INTERNAL, category=N_("系统"), icon="🧯",
    title=N_("后台任务失败了"),
    description=N_("这个 Bot（或者所有 Bot）的后台任务、定时任务失败时触发，让 Bot 分析原因。"),
    fields=[{"key": "scope", "label": N_("看谁的任务"), "type": "select", "default": "mine",
             "options": [{"value": "mine", "label": N_("只看这个 Bot 的")}, {"value": "all", "label": N_("所有 Bot 的")}]}],
    on="task_done", match=match_task_failed,
    summary=lambda p: "所有 Bot" if p.get("scope") == "all" else "",
    sample={"title": "Nova 的任务 #12 失败了：整理今天的邮件", "text": "要做的事：\n整理今天的邮件\n\n失败原因：\n任务失败：应用 gmail 没连接"},
    scenarios=[
        {"title": N_("任务失败时分析原因"), "description": N_("后台任务、定时任务失败了，Bot 看原因、告诉你怎么办"),
         "name": N_("任务失败"), "params": {"scope": "all"},
         "prompt": N_("有个后台任务失败了。根据失败原因判断是哪类问题（应用没连接、网站打不开、额度用完、要求不清楚……），告诉我怎么解决；偶发的建议再试一次。")},
    ])

SOURCES = [TASK_FAILED]
