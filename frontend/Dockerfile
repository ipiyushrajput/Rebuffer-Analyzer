FROM node:22-alpine AS build

WORKDIR /app

ARG VITE_API_BASE=http://107.109.131.68:8080/api
ARG VITE_WS_BASE=ws://107.109.131.68:8080/ws
ENV VITE_API_BASE=$VITE_API_BASE
ENV VITE_WS_BASE=$VITE_WS_BASE

COPY package.json package-lock.json* ./
RUN npm ci || npm install

COPY . .
RUN npm run build

FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD wget -q -O - http://127.0.0.1/healthz || exit 1
