# Telegram News Automation Bot

## Description
A Python-based Telegram bot that aggregates news from multiple sources (inline.ru, theins.ru, meduza.io), processes and filters content, and publishes it automatically to a Telegram channel.
## Live Demo https://t.me/News2two

## Features
- Aggregates news from multiple RSS and HTML sources
- Parses and extracts structured content (BeautifulSoup, XML)
- Removes duplicates using fuzzy matching and custom text similarity logic
- Summarizes articles automatically
- Supports multiple languages (RU/EN/DE)
- Stores history to prevent reposts
- Handles network errors and auto-restarts
- Asynchronous architecture using asyncio

## Technologies
- Python
- asyncio
- Telegram Bot API
- BeautifulSoup (HTML parsing)
- XML / RSS parsing
- Text processing and deduplication algorithms


## How to run

```bash
Create a config file: config.json Then insert your Telegram bot token and chat ID.

pip install -r requirements.txt
python src/news_bot.py
