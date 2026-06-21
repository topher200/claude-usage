FROM python:3.12-slim

WORKDIR /app

COPY scanner.py cli.py dashboard.py ./

ENV HOST=0.0.0.0
ENV PORT=8080
ENV CLAUDE_USAGE_DB=/data/usage.db

EXPOSE 8080

CMD ["python3", "cli.py", "dashboard", "--no-browser"]
