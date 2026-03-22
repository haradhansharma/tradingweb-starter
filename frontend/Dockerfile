# tradingweb/frontend/Dockerfile

FROM node:24-alpine

WORKDIR /app

# Install dependencies first (for caching)
COPY package*.json ./
RUN npm install

COPY . .

EXPOSE 4321
ENV HOST=0.0.0.0

CMD ["npm", "run", "dev"]