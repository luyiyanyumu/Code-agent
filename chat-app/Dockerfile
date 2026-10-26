FROM node:22-alpine AS frontend
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.js ./
COPY src ./src
COPY scripts ./scripts
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY licenses ./licenses
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY chat_agent ./chat_agent
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 chatagent
COPY --from=frontend /app/dist /app/dist
RUN mkdir /data && chown chatagent:chatagent /data
USER chatagent
ENV CHAT_AGENT_HOME=/data
EXPOSE 8766
CMD ["chat-agent-server", "--host", "0.0.0.0", "--port", "8766", "--static-dir", "/app/dist"]
