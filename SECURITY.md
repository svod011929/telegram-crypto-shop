# Security Policy

## Reporting a vulnerability

Если ты обнаружил уязвимость, не публикуй рабочие токены, приватные данные пользователей или детали, которые позволяют эксплуатировать production-инстанс.

Связь с автором:

- GitHub: [@svod011929](https://github.com/svod011929)
- Email: [antihype2205@yandex.ru](mailto:antihype2205@yandex.ru)

## Secrets

Проект ожидает секреты только через `.env`/environment variables. Реальные значения `BOT_TOKEN` и `CRYPTO_PAY_TOKEN` не должны попадать в git, issue, логи или публичные backup-файлы.

Если секрет случайно опубликован, считай его скомпрометированным: отзови его у провайдера и выпусти новый.
