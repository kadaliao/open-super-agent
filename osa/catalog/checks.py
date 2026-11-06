"""连接前的检查：添加应用前先试一下用户填的东西对不对，错了马上告诉用户，不用等应用连接失败。

清单里这样用（参数里的 {字段名} 换成用户填的内容）：

    check:
      imap:
        host: imap.qq.com
        user: "{email}"
        password: "{password}"
        hint: 注意填的是授权码，不是平时的登录密码

要加新的检查：在这里写一个 async 函数，参数就是清单里能写的键（没有默认值的必须写），
发现不对时 raise ValueError(i18n.t("给用户看的话"))，再登记到 CHECKS。
给用户看的参数（比如 hint）在清单的 en.check 里可以写英文，见 docs/apps.md。
"""
import asyncio
import imaplib
from .. import i18n


async def imap(host: str, user: str, password: str, port: str = "993", hint: str = ""):
    """用 IMAP（SSL）登录一次。hint 接在登录失败的提示后面，告诉用户该填什么。"""
    def login():
        try:
            m = imaplib.IMAP4_SSL(host, int(port), timeout=20)
        except OSError as e:
            raise ValueError(i18n.t("连不上邮件服务器 {host}：{error}", host=host, error=e))
        try:
            m.login(user, password)
        except imaplib.IMAP4.error as e:
            detail = e.args[0].decode(errors="replace") if e.args and isinstance(e.args[0], bytes) else str(e)
            msg = i18n.t("邮箱登录失败：{detail}", detail=detail[:120])
            raise ValueError(i18n.t("{msg}。{hint}", msg=msg, hint=hint) if hint else msg)
        finally:
            try:
                m.logout()
            except Exception:
                pass
    await asyncio.to_thread(login)


CHECKS = {"imap": imap}
