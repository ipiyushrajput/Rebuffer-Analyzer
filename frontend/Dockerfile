FROM node:22-alpine AS build

WORKDIR /app

# The path the UI is served under; the API and WebSockets sit below it. VITE_API_BASE and
# VITE_WS_BASE stay empty unless a deployment points the page somewhere else: the page then
# calls <base>api and <base>ws on whatever origin served it.
ARG VITE_BASE_PATH=/streamlens/
ARG VITE_API_BASE=
ARG VITE_WS_BASE=
ENV VITE_BASE_PATH=$VITE_BASE_PATH
ENV VITE_API_BASE=$VITE_API_BASE
ENV VITE_WS_BASE=$VITE_WS_BASE

COPY package.json package-lock.json* ./
RUN npm ci || npm install

COPY . .
RUN npm run build

FROM nginx:1.27-alpine

# The nginx image renders /etc/nginx/templates/*.template with these variables at start-up,
# into NGINX_ENVSUBST_OUTPUT_DIR; nginx.conf includes the result. STREAMLENS_BACKEND is the
# backend as host:port: the compose service by default, another machine's address when the
# backend runs elsewhere. STREAMLENS_BASE_PATH must match the VITE_BASE_PATH it was built for.
ENV STREAMLENS_BASE_PATH=/streamlens \
    STREAMLENS_BACKEND=backend:8010 \
    STREAMLENS_WEBROOT=/usr/share/nginx/html \
    NGINX_ENVSUBST_OUTPUT_DIR=/etc/nginx/streamlens

# The entrypoint writes the rendered template here, and refuses a directory that is absent.
RUN mkdir -p /etc/nginx/streamlens
COPY --from=build /app/dist /usr/share/nginx/html/streamlens
COPY nginx/streamlens-locations.conf.template /etc/nginx/templates/locations.conf.template
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD wget -q -O - http://127.0.0.1/healthz || exit 1
