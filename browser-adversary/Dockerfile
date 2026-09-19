FROM mcr.microsoft.com/playwright:v1.62.1-noble
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --omit=dev
COPY tsconfig.json ./
COPY src ./src
ENTRYPOINT ["npx", "tsx", "src/runner.ts"]
