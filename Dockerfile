FROM python:3.12-slim
WORKDIR /app
COPY trading_bot /app/trading_bot
RUN useradd --uid 10001 --create-home bot && mkdir /app/data && chown bot:bot /app/data
USER bot
CMD ["python", "-m", "trading_bot"]
