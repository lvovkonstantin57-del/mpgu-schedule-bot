<div align="center">

# 🤖 MPGU Schedule Bot

**Telegram-бот и сервер данных для расписания МПГУ — с админ-панелью и импортом из Excel**

![Python](https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white)
![aiogram](https://img.shields.io/badge/aiogram%203-26A5E4?style=flat-square&logo=telegram&logoColor=white)
![aiohttp](https://img.shields.io/badge/aiohttp-2C5BB4?style=flat-square&logo=aiohttp&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?style=flat-square&logo=sqlite&logoColor=white)
![Caddy](https://img.shields.io/badge/Caddy-1F88C0?style=flat-square&logo=caddy&logoColor=white)

</div>

Бот регистрирует студентов по ФИО из списка группы, а мини-приложение в Telegram показывает каждому расписание его группы. Администратор загружает списки и расписание в админ-панели.

> Это вторая версия проекта. Первая — мини-приложение [tg-webapp](https://github.com/lvovkonstantin57-del/tg-webapp), третья и актуальная — самостоятельное приложение **[mpgu-timetable](https://github.com/lvovkonstantin57-del/mpgu-timetable)**.

## ✨ Возможности

- **Регистрация по ФИО:** студент пишет боту своё имя, бот находит его в списке и сам определяет группу
- **Мини-приложение** с расписанием; каждый запрос подписан Telegram (`initData`), сервер знает, кто спрашивает
- **Админ-панель:** студенты, группы, расписание, семестр и звонки; вход по одноразовому коду `/admin`
- **Импорт и экспорт Excel** (openpyxl)
- **Прокси для Telegram** (SOCKS5/HTTP), если API заблокирован у хостинга
- **Деплой одной командой:** служба systemd + Caddy с HTTPS через sslip.io

## 📂 Файлы

```
bot.py           бот на aiogram 3
api.py           HTTP API для мини-приложения (aiohttp)
db.py            хранилище на SQLite
excel_io.py      импорт и экспорт xlsx
сайт/index.html  мини-приложение
server/          скрипт установки на сервер
deploy.sh        выкладка на сервер
ИНСТРУКЦИЯ.md    подробная инструкция по запуску и работе
```

## 🚀 Запуск

```bash
cp .env.example .env          # токен бота, адрес мини-приложения, код админа
pip install -r requirements.txt
python bot.py
```

Установка на сервер — `./deploy.sh root@<IP>`; подробности в [ИНСТРУКЦИЯ.md](ИНСТРУКЦИЯ.md).

---

<sub>Автор — [Константин Львов](https://github.com/lvovkonstantin57-del) · [Telegram](https://t.me/l1ttleStich)</sub>
