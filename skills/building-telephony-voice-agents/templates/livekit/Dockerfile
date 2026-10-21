# LiveKit Cloud agent image (also fine for self-hosted workers).
# Rules from https://docs.livekit.io/deploy/agents/builds.md: glibc base (no Alpine), explicit WORKDIR, non-root
# user, CMD runs `start` directly (no wrapper scripts), never bake LIVEKIT_* credentials or .env files in.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/appuser

RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY agent_gemini_live.py ./
# Prompt file if you use SYSTEM_PROMPT_FILE=/app/prompt.md
# COPY prompt.md ./

# Bake Silero / noise-cancellation model files into the image so the first call does not download them.
RUN python agent_gemini_live.py download-files

USER appuser

# Health server listens on 8081 in production mode; LiveKit Cloud checks it during rollouts (5 min limit).
CMD ["python", "agent_gemini_live.py", "start"]
