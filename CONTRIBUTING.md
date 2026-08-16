# Contributing

Спасибо за интерес к проекту.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Validation

Перед отправкой изменений выполни:

```bash
python -m compileall -q .
python main.py --check
python -m unittest discover -s tests -v
```

Не добавляй в коммиты `.env`, SQLite database, runtime logs, backup-файлы или реальные API tokens.
