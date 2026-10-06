"""Пересылает новые письма из ящика Яндекса в Телеграм-группу.

Забирает почту напрямую по IMAP (imap.yandex.ru), ничего в ящике не меняет:
письма открываются только на чтение и не помечаются прочитанными.

Переменные окружения:
  IMAP_USER      адрес ящика, например info@runconnect.ru
  IMAP_PASSWORD  пароль приложения Яндекса (не основной пароль!)
  BOT_TOKEN      токен бота от @BotFather
  CHAT_ID        id группы в Телеграме
  STATE_FILE     где хранить номер последнего пересланного письма (по умолчанию state.json)

Запуск:
  python mail_to_telegram.py          проверить почту и переслать новое
  python mail_to_telegram.py --test   отправить в группу тестовое сообщение
"""

import email
import html
import imaplib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from email.header import decode_header, make_header
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime
from datetime import timezone, timedelta

IMAP_HOST = "imap.yandex.ru"
FOLDER = "INBOX"
MAX_BODY = 3000  # Телеграм не принимает сообщения длиннее 4096 символов
MSK = timezone(timedelta(hours=3))

IMAP_USER = os.environ.get("IMAP_USER", "info@runconnect.ru")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")


def send(text):
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if not body.get("ok"):
        raise RuntimeError(f"Телеграм вернул ошибку: {body}")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def header(msg, name):
    value = msg.get(name)
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def html_to_text(s):
    s = re.sub(r"(?is)<(script|style|head).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    return html.unescape(s)


def body_text(msg):
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        return plain.get_content()
    rich = msg.get_body(preferencelist=("html",))
    if rich is not None:
        return html_to_text(rich.get_content())
    return ""


def attachments(msg):
    names = []
    for part in msg.iter_attachments():
        name = part.get_filename()
        if name:
            names.append(name)
    return names


def format_message(raw):
    msg = email.message_from_bytes(raw, policy=default_policy)

    text = body_text(msg).replace("\r", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_BODY:
        text = text[:MAX_BODY] + "\n…(письмо обрезано)"

    try:
        date = parsedate_to_datetime(msg["Date"]).astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    except Exception:
        date = header(msg, "Date")

    lines = [
        f"📩 <b>{html.escape(header(msg, 'Subject') or '(без темы)')}</b>",
        f"От: {html.escape(header(msg, 'From'))}",
        f"Дата: {html.escape(date)}",
    ]
    files = attachments(msg)
    if files:
        lines.append("📎 Вложения: " + html.escape(", ".join(files)))
    lines += ["", html.escape(text)]
    return "\n".join(lines)


def check_mail():
    state = load_state()
    imap = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    try:
        imap.login(IMAP_USER, IMAP_PASSWORD)
        imap.select(FOLDER, readonly=True)  # только чтение: письма остаются непрочитанными

        uidvalidity = int(re.search(rb"\d+", imap.response("UIDVALIDITY")[1][0]).group())
        status = imap.status(FOLDER, "(UIDNEXT)")[1][0]
        uidnext = int(re.search(rb"UIDNEXT (\d+)", status).group(1))

        # Первый запуск (или Яндекс перенумеровал письма): запоминаем текущее место,
        # старые письма не пересылаем.
        if state.get("uidvalidity") != uidvalidity:
            save_state({"uidvalidity": uidvalidity, "last_uid": uidnext - 1})
            print(f"Старт: пересылаю письма начиная с UID {uidnext}")
            return

        last_uid = state["last_uid"]
        if uidnext - 1 <= last_uid:
            print("Новых писем нет")
            return

        found = imap.uid("SEARCH", None, f"UID {last_uid + 1}:*")[1][0].split()
        uids = sorted(u for u in map(int, found) if u > last_uid)
        for uid in uids:
            data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")[1]
            raw = next(part[1] for part in data if isinstance(part, tuple))
            send(format_message(raw))
            state["last_uid"] = uid
            save_state(state)  # после каждого письма, чтобы при сбое не было дублей
            print(f"Переслано письмо UID {uid}")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def main():
    test = "--test" in sys.argv
    needed = ("BOT_TOKEN", "CHAT_ID") if test else ("IMAP_PASSWORD", "BOT_TOKEN", "CHAT_ID")
    missing = [n for n in needed if not os.environ.get(n)]
    if missing:
        sys.exit("Не заданы переменные: " + ", ".join(missing))
    if test:
        send(f"✅ Пересылка писем с {html.escape(IMAP_USER)} подключена. Новые письма будут приходить сюда.")
        print("Тестовое сообщение отправлено")
        return
    check_mail()


if __name__ == "__main__":
    main()
