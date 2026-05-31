# syntax=docker/dockerfile:1.7
FROM node:20.11.1-bookworm-slim AS build

WORKDIR /app
ARG VITE_API_BASE=
ARG VITE_ENABLE_ONBOARDING=
ARG VITE_CLIENT_AGENT_SERVER_URL=
ARG NPM_REGISTRY=https://registry.npmmirror.com
ENV VITE_API_BASE=${VITE_API_BASE} \
    VITE_ENABLE_ONBOARDING=${VITE_ENABLE_ONBOARDING} \
    VITE_CLIENT_AGENT_SERVER_URL=${VITE_CLIENT_AGENT_SERVER_URL} \
    ELECTRON_SKIP_BINARY_DOWNLOAD=1

COPY package.json package-lock.json ./
RUN --mount=type=cache,id=web-terminal-acp-frontend-npm,target=/root/.npm \
    npm config set registry "${NPM_REGISTRY}" && \
    npm ci

COPY . .
RUN npm run build

FROM nginx:1.27.0-alpine
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
