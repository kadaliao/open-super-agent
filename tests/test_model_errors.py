"""模型服务临时出故障（比如 DeepSeek 偶尔连着报 500）：自动多试几次；实在不行，报错说人话。"""
import httpx
import openai._base_client
import pytest
from conftest import find, settle
from osa import bots, providers, server

REAL_CLIENT = providers.client   # llm 夹具会把它换成假模型的客户端，这里留着原来的


@pytest.fixture
def real_client(llm, monkeypatch):
    """客户端照常由 providers.client 建（带它的重试设置），请求送到假模型；重试前不真的等。"""
    make = providers.AsyncOpenAI
    monkeypatch.setattr(providers, "AsyncOpenAI", lambda **kw: make(**kw, http_client=httpx.AsyncClient(transport=llm.transport)))
    monkeypatch.setattr(providers, "client", REAL_CLIENT)
    monkeypatch.setattr(openai._base_client.BaseClient, "_calculate_retry_timeout", lambda self, *a, **kw: 0)
    providers._clients.clear()
    yield llm
    providers._clients.clear()


async def test_group_mention_survives_transient_500(real_client, events, make_bot):
    lead, mailer = make_bot("Boss"), make_bot("Mailer")
    group = bots.create_group([lead["id"], mailer["id"]], lead=lead["id"])
    real_client.fail(*[500] * (providers.RETRIES - 1))
    real_client.script("router:Mailer", "好，我来发")

    await server.safe_handle(group, "@Mailer 帮我发一封邮件")
    await settle()

    assert real_client.failed == providers.RETRIES - 1
    assert find(group["id"], role="assistant", bot_id=mailer["id"], text="好，我来发")
    assert not events.of("error")


async def test_group_mention_persistent_500_explained(real_client, events, make_bot):
    lead, mailer = make_bot("Boss2"), make_bot("Mailer2")
    group = bots.create_group([lead["id"], mailer["id"]], lead=lead["id"])
    real_client.fail(*[500] * (providers.RETRIES + 1))

    await server.safe_handle(group, "@Mailer2 帮我发一封邮件")
    await settle()

    assert real_client.failed == providers.RETRIES + 1   # 第一次加上重试的次数，都报错
    errs = [e["message"] for e in events.of("error") if e["chat_id"] == group["id"]]
    assert len(errs) == 1
    assert errs[0].startswith("Mailer2 出错了：") and "临时出了故障（500）" in errs[0]
    assert "request_id" not in errs[0] and "Error code" not in errs[0]
    assert not find(group["id"], role="assistant", bot_id=mailer["id"])


async def test_direct_chat_error_explained(real_client, events, make_bot):
    b = make_bot("Solo")
    real_client.fail(*[503] * (providers.RETRIES + 1))

    await server.safe_handle(bots.chat(b["chat_id"]), "你好")
    await settle()

    errs = [e["message"] for e in events.of("error") if e["chat_id"] == b["chat_id"]]
    assert errs == [errs[0]] and "临时出了故障（503）" in errs[0] and "request_id" not in errs[0]


def test_brief_keeps_other_errors():
    assert providers.brief(ValueError("没有这个模型服务：x")) == "没有这个模型服务：x"
